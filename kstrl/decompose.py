"""LLM-driven spec decomposition into components and PRDs."""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.agent_output import (
    MAX_AGENT_OUTPUT_BYTES,
    _extract_agent_json,
    timed_out_message,
)
from kstrl.agents.base import (
    ARCHITECT_COMPONENT,
    ARCHITECT_ROLE,
    Agent,
    collect_usage,
    print_usage_rollup,
    timed_out_since,
    usage_cursor,
)
from kstrl.agents.prompt_record import AgentCall, recording_prompts
from kstrl.config import KstrlConfig, relative_to_root
from kstrl.decisions import (
    SpecDecision,
    escalations,
    open_escalation_item,
    parse_decisions,
    resolve_escalation_items,
    write_decisions,
)
from kstrl.decompose_prompt import (
    SPECKIT_ARTIFACTS,
    build_decompose_prompt,
    load_spec_input,
    spec_digest,
    spec_ref,
)
from kstrl.decompose_reply import (
    _component_branch,
    _file_stack_proposal,
    _generate_component_prd,
    _prd_schema_errors,
    _prd_summary_line,
    _retry_feedback,
    _routed_prd_issues,
    _validate_decompose_output,
)
from kstrl.event_catalog import (
    ArtifactWritten,
    ComponentCompleted,
    ComponentFailed,
    ComponentStarted,
    ComponentUsage,
    Event,
    PhaseCompleted,
    PhaseStarted,
    RunCompleted,
    RunPlan,
    RunStarted,
    SpecIssueRecorded,
)
from kstrl.events import EventBus
from kstrl.linear import (
    LinearClient,
    LinearConfig,
    linear_branch_name,
    sync_decompose,
)
from kstrl.manifest import (
    Component,
    ComponentStatus,
    Manifest,
)
from kstrl.names import validate_branch_name
from kstrl.owner_answers import OwnerAnswers, read_owner_answers
from kstrl.requirements import parse_requirements
from kstrl.runid import mint_run_id
from kstrl.spec_convergence import (
    _build_convergence,
    _excluded_history,
    _journal_snapshot,
    _surface_convergence,
)
from kstrl.spec_issues import (
    _parse_spec_issues,
    _record_spec_issues_event,
    _spec_audit_journal,
    _surface_spec_decisions,
    _surface_spec_issues,
    persist_spec_issues,
)
from kstrl.stack import (
    stack_text_digest,
)
from kstrl.statedir import plan_prd_path

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from kstrl.factory_lock import _RunLock
    from kstrl.ui.base import UI


class SpecBlockerError(Exception):
    """Raised when the architect ESCALATED at least one question (#260).

    The decompose pipeline halts until the owner answers, rather than
    letting the architect guess at a product, scope or risk judgement.
    It no longer halts on blocker-severity findings as such: five real
    runs raised 26 blockers and not one of them was a judgement only the
    owner could make, so the architect halted on questions it had
    already answered in its own suggestions.

    The escalated decisions are attached via ``escalations``.
    ``artifact_path`` points at the persisted spec-issues.json (R1.7)
    and ``decisions_path`` at decisions.json, each when its write
    succeeded, so callers can direct the user at a durable record
    instead of scrollback. Both, because after #260 they hold different
    halves of the halt: the finding is in the audit, the question the
    owner has to answer and the reason it was not answered are in the
    register.
    """

    def __init__(
        self,
        escalated: list[SpecDecision],
        artifact_path: Path | None = None,
        decisions_path: Path | None = None,
    ):
        self.escalations = escalated
        self.artifact_path = artifact_path
        self.decisions_path = decisions_path
        summary_lines = [f"- {d.question}\n  owner must decide: {d.resolution}" for d in escalated]
        super().__init__(
            "Architect escalated; the owner must answer before re-running:\n"
            + "\n".join(summary_lines),
        )

    def artifact_lines(self) -> list[str]:
        """Where the operator should go to answer, one line per record.

        R1.7 says point at a file rather than at scrollback, and every
        handler of this exception owes the same pointers, so the wording
        lives here instead of being repeated at each of them.
        """
        lines: list[str] = []
        if self.artifact_path is not None:
            lines.append(f"Spec issues written to: {self.artifact_path}")
        if self.decisions_path is not None:
            lines.append(f"Architect decisions written to: {self.decisions_path}")
        return lines


def _write_decompose_artifact(
    label: str,
    noun: str,
    writer: Callable[[], Path],
    *,
    ui: UI,
    emit: Callable[[Event], None],
    rel_display: Callable[[Path], str],
    required: bool = False,
) -> Path | None:
    """Attempt one durable decompose artifact, and say what happened.

    The write policy the R1.7 audit and the #260 decision register
    share, in one place so the two cannot drift: attempt, announce,
    emit, and on ``OSError`` be loud without masking. Returns the path,
    or ``None`` when the write failed, which is what the caller passes
    to ``SpecBlockerError`` so the halt never points at a file that is
    not there.

    ``required`` says the artifact IS part of the result rather than a
    record of it. The decision register beside a saved manifest is: a
    later ``ks factory`` reads it to bind engineers, and a missing
    register binds nothing and says nothing, so one swallowed write
    error would silently disable the register for every run against
    that manifest. The halting copy is not required, because the halt
    reaches the operator through ``SpecBlockerError`` either way.
    """
    try:
        path = writer()
    except OSError as exc:
        if required:
            raise
        ui.err(f"Failed to persist {noun} to disk: {exc}")
        return None
    ui.ok(f"{noun.capitalize()} written: {path}")
    emit(ArtifactWritten(label=label, path=rel_display(path)))
    return path


def _report_architect_usage(
    agent: Agent,
    ui: UI,
    bus: EventBus | None,
    *,
    since: int,
) -> None:
    """Publish what the architect's LLM calls cost, to the bus and the
    terminal (#257).

    ``since`` is the agent's record count before this decomposition ran.
    ``decompose_spec`` is public and takes an ``Agent``, so a caller can
    hold one across two calls - re-running after editing the spec is the
    obvious way - and would then see run 1 folded into run 2's event AND
    its table. Every in-tree caller passes a fresh agent; the offset
    removes the dependency rather than documenting an invariant a caller
    cannot see (#257 review).

    The component id is the pseudo-component ``_decompose_spec_impl``
    already announced in the plan, so the event lands on an existing row
    rather than conjuring one. The PHASE key is the bare role name
    because a meter's phase key is the ROLE - it is what the coverage
    footer prints - and the taxonomy's fifth role is the architect. It
    is deliberately not ``decompose``/``audit``, the lifecycle phases
    this component reports elsewhere, which name steps rather than roles.

    The two keys are no longer the same string (#281): the phase axis is
    kstrl's vocabulary on both sides, but the component axis is one the
    architect LLM also writes to, so ``ARCHITECT_COMPONENT`` carries the
    role namespace and ``ARCHITECT_ROLE`` stays bare.

    Called from a ``finally``, so it must not raise: ``collect_usage``
    already swallows a malformed record set, and zero calls (an agent
    predating R3.1, or one that never ran) reports nothing at all.
    """
    totals = collect_usage(agent, since=since)
    if totals.calls == 0:
        return
    if bus is not None:
        bus.emit(
            ComponentUsage(
                component=ARCHITECT_COMPONENT,
                phase=ARCHITECT_ROLE,
                **totals.to_dict(),
            )
        )
    print_usage_rollup(
        ui,
        {ARCHITECT_COMPONENT: {ARCHITECT_ROLE: totals}},
        totals,
        # NOT "Usage rollup": `ks factory` decomposes and then prints its
        # own run-level rollup, whose total excludes the architect until
        # piece B. Two tables under one heading would invite reading the
        # later one as the run's whole spend.
        title="Architect usage",
    )


def _say_owner_answers(answers: OwnerAnswers, ui: UI) -> None:
    """Name the owner answers appended to the architect's input, if any (#639)."""
    if answers.item_ids:
        ui.kv("Owner answers", ", ".join(item_id[:8] for item_id in answers.item_ids))


def _for_attempt(call: AgentCall, attempt: int) -> AgentCall:
    """The architect's record identity for one decompose attempt (#532)."""
    return dataclasses.replace(call, attempt=attempt)


def _attempt_stopped(
    agent: Agent, cursor: int, *, too_large: bool, timeout: float | None
) -> str | None:
    """Why an architect attempt's reply must not be read, or None.

    A reply over the size cap, or one from a call the adapter killed at
    ``timeout`` (#603), is one failed attempt, and its lines are not parsed.
    """
    if too_large:
        return "agent output exceeded size cap"
    if timed_out_since(agent, cursor):
        return timed_out_message(timeout)
    return None


def _decompose_spec_impl(
    spec_path: Path,
    project_name: str,
    base_branch: str,
    single_pr: bool,
    agent: Agent,
    ui: UI,
    root_dir: Path,
    max_retries: int = 3,
    *,
    bus: EventBus | None = None,
    transcript: Callable[[str], None] | None = None,
    prompt_call: AgentCall,
    force_lock: bool = False,
    run_lock: _RunLock | None = None,
    timeout: float | None,
    before_spend: Callable[[], object],
) -> Manifest:
    """Decompose a spec into components and generate PRDs.

    Args:
        spec_path: Path to the markdown spec file
        project_name: Name for the project/factory run
        base_branch: Base git branch
        single_pr: Whether to use a single branch for all components
        agent: Agent to use for decomposition
        ui: UI for output
        root_dir: Project root directory
        max_retries: Max attempts for JSON parsing
        bus: Optional run event bus (TUI surface C4): the work is
            projected onto the pseudo-component "architect" - attempts
            as decompose phases, the red-team pass as an audit phase,
            spec issues and artifacts as typed events. None = today's
            behavior exactly.
        transcript: Optional sink for the architect's streamed lines
            (the run's transcript file); terminal streaming through
            ``ui`` is unchanged either way.
        prompt_call: Who the architect's prompt is recorded for (#532),
            with each attempt's number put in. Required (#567): every
            caller is inside a run, and the run is what the record names.
        force_lock: Passed to ``_acquire_run_lock`` when this call takes
            its own lock (``run_lock`` is None); ignored otherwise, since
            a caller that already holds one decided ``force`` itself.
        run_lock: A lock the caller already holds (#597), e.g. `ks
            factory --spec`, which takes it before this call so the
            architect's spend and the manifest write share one acquire.
            None (the `ks decompose` command's own case) means this call
            takes and releases its own lock around the manifest write.
        before_spend: Called once, after this run has started and before
            the first architect call (#696 slice 7). `ks factory --spec`
            measures the base in it and exits 2 there on a red one, so
            the refusal and its record belong to this run.

    Returns:
        Manifest with generated components and PRD files
    """

    def emit(event: Event) -> None:
        if bus is not None:
            bus.emit(event)

    def rel_display(path: Path) -> str:
        try:
            return path.relative_to(root_dir).as_posix()
        except ValueError:
            return str(path)

    run_started = time.monotonic()
    emit(RunStarted(project=project_name, components=0, pid=os.getpid()))
    emit(
        RunPlan(
            components=(
                {"id": ARCHITECT_COMPONENT, "title": "Architect / PRD red-team", "deps": []},
            )
        )
    )
    emit(ComponentStarted(component=ARCHITECT_COMPONENT))

    ui.section("Spec Decomposition")
    ui.kv("Spec", str(spec_path))
    if spec_path.is_dir():
        present = [name for name, _ in SPECKIT_ARTIFACTS if (spec_path / name).is_file()]
        ui.kv("SpecKit artifacts", ", ".join(present) or "<none>")
    ui.kv("Project", project_name)

    spec_content = load_spec_input(spec_path)
    # #639: the pin is the text the architect is about to read, never a
    # second read of the file, and spec_source is the path a later run
    # re-reads it from.
    pin = spec_digest(spec_content)
    spec_source = spec_ref(spec_path, root_dir)
    # #696: the [stack] this plan is made under, pinned as text; the run
    # that builds the plan refuses under any other (plan_gate.stack_pin_errors).
    stack_pin = stack_text_digest(root_dir, warn=ui.warn)
    ui.kv("Spec digest", pin[:12])
    # #639 slice 4: the owner's inbox answers to this spec's escalations,
    # appended to the architect's input AFTER the pin, so specDigest stays
    # the digest of the spec alone and the run-time re-read reproduces it.
    # An unreadable inbox raises OwnerAnswerError here, before any spend.
    answers = read_owner_answers(root_dir, project_name, spec_source)
    _say_owner_answers(answers, ui)
    # #199: the architect runs with cwd=root_dir (see `agent.run` below),
    # so it is told to read the repository rather than handed a paste.
    # The map path is the operator's: `[paths] codebase_map` can move it,
    # and a prompt naming the default would name a file that is not
    # there. One read, measured at 0.473 ms against a 23 KB kstrl.toml,
    # before the architect call this repo measures at 119 to 210 s.
    #
    # load_or_anchored, not the strict load: this line runs BEFORE the
    # halt path below that writes scripts/kstrl/spec-issues.json, so a
    # malformed kstrl.toml raising here would cost the audit artifact
    # the halt path exists to produce. EvolutionConfig.load_or_none
    # (kstrl/evolution.py:339) is the precedent for the same file at the
    # same kind of call site. Every other command still gets the strict
    # load through config_preflight at entry; only this one path, ahead
    # of the halt artifact, is tolerant.
    config = KstrlConfig.load_or_anchored(root_dir, warn=ui.warn)
    prompt = build_decompose_prompt(
        project_name,
        spec_content + answers.text,
        codebase_map_path=relative_to_root(config.codebase_map_file, root_dir),
    )

    # #696 slice 7: the caller's last check before the first paid call
    # (`ks factory --spec` measures the base here, in this run).
    before_spend()
    data = None
    last_error: str | None = None
    attempts_used = 0

    for attempt in range(1, max_retries + 1):
        attempts_used = attempt
        ui.info(f"Decomposition attempt {attempt}/{max_retries}")
        emit(
            PhaseStarted(
                component=ARCHITECT_COMPONENT,
                phase="decompose",
                attempt=attempt,
            )
        )
        phase_start = time.monotonic()

        def attempt_failed(detail: str, *, started_at: float) -> None:
            emit(
                PhaseCompleted(
                    component=ARCHITECT_COMPONENT,
                    phase="decompose",
                    passed=False,
                    detail=detail[:200],
                    duration_seconds=round(time.monotonic() - started_at, 2),
                )
            )

        if last_error:
            retry_prompt = (
                f"{prompt}\n\n"
                f"PREVIOUS ATTEMPT FAILED with error:\n{last_error}\n\n"
                f"Please fix the error and output valid JSON."
            )
        else:
            retry_prompt = prompt

        output_lines: list[str] = []
        total_bytes = 0
        too_large = False
        cursor = usage_cursor(agent)
        try:
            with recording_prompts(_for_attempt(prompt_call, attempt)):
                for line in agent.run(retry_prompt, cwd=root_dir, timeout=timeout):
                    output_lines.append(line)
                    ui.stream_line("AI", line)
                    if transcript is not None:
                        transcript(line)
                    total_bytes += len(line) + 1
                    if total_bytes > MAX_AGENT_OUTPUT_BYTES:
                        too_large = True
                        ui.warn(
                            "Decompose agent emitted "
                            f">{MAX_AGENT_OUTPUT_BYTES // 1024 // 1024}MB; "
                            "aborting this attempt."
                        )
                        break
        except BaseException as exc:
            attempt_failed(
                f"{type(exc).__name__}: {exc}",
                started_at=phase_start,
            )
            raise

        stopped = _attempt_stopped(agent, cursor, too_large=too_large, timeout=timeout)
        if stopped is not None:
            last_error = stopped
            attempt_failed(last_error, started_at=phase_start)
            continue

        try:
            data = _extract_agent_json(agent, output_lines)
        except ValueError as exc:
            last_error = str(exc)
            ui.warn(f"JSON extraction failed: {last_error}")
            attempt_failed(last_error, started_at=phase_start)
            continue

        validation_errors = _validate_decompose_output(data, has_stack=bool(stack_pin))
        if validation_errors:
            last_error = _retry_feedback(validation_errors)
            ui.warn(f"Validation failed: {last_error}")
            attempt_failed(last_error, started_at=phase_start)
            data = None
            continue

        prd_errors = _prd_schema_errors(data, project_name, single_pr)
        if prd_errors:
            last_error = _retry_feedback(prd_errors)
            ui.warn(f"PRD validation failed: {last_error}")
            attempt_failed(last_error, started_at=phase_start)
            data = None
            continue

        last_error = None
        emit(
            PhaseCompleted(
                component=ARCHITECT_COMPONENT,
                phase="decompose",
                passed=True,
                duration_seconds=round(time.monotonic() - phase_start, 2),
            )
        )
        break

    if data is None:
        # No files were written in the retry loop, so terminal failure
        # leaves no partial state behind (R1.8).
        raise ValueError(
            f"Failed to decompose spec after {max_retries} attempts. Last error: {last_error}"
        )

    # Surface AND persist red-team findings before doing any further
    # work (R1.7): the artifact and journal event are written for halt,
    # success, and clean-audit outcomes alike. If any issue is a
    # blocker, halt before generating PRDs - the architect explicitly
    # judged the spec un-decomposable.
    emit(
        PhaseStarted(
            component=ARCHITECT_COMPONENT,
            phase="audit",
            attempt=1,
        )
    )
    audit_start = time.monotonic()
    spec_issues = _parse_spec_issues(data)
    _surface_spec_issues(spec_issues, ui)
    for issue in spec_issues:
        emit(
            SpecIssueRecorded(
                severity=issue.severity,
                kind=issue.kind,
                summary=issue.summary,
                location=issue.location,
                suggestion=issue.suggestion,
            )
        )
    # #260: the halt keys on the architect's own disposition, not on a
    # severity label. Everything it decided, assumed or spiked rides
    # along; only a question it refused to answer stops the run.
    decisions = parse_decisions(data)
    requirements = parse_requirements(data)
    escalated = escalations(decisions)
    _surface_spec_decisions(decisions, ui)

    # The R1.7 artifacts are written FIRST, before any optional journal
    # work. On the halt path they are the only durable record the
    # operator gets, so nothing that can fail is allowed upstream of
    # them: #260 briefly put a config load in front of this and a typo
    # in an unrelated journal knob destroyed the findings of a paid run.
    #
    # One helper, called twice, so the two writes cannot drift on the
    # policy they share: attempt, announce, emit, and on OSError be
    # loud without masking. They stay two independent attempts because
    # neither may take the other down, and after #260 they hold
    # different halves of a halt.
    write_artifact = functools.partial(
        _write_decompose_artifact,
        ui=ui,
        emit=emit,
        rel_display=rel_display,
    )
    artifact_path = write_artifact(
        "spec_issues",
        "spec audit",
        lambda: persist_spec_issues(
            spec_issues,
            root_dir=root_dir,
            project_name=project_name,
            spec_file=spec_path.name,
            halted=bool(escalated),
        ),
    )
    # #260 round 2: the register is written on the HALT path here, and
    # on the success path only after the manifest commits (below).
    # Round 1 wrote it here unconditionally, so a decompose that halted
    # or failed later left a fresh register beside an OLDER manifest,
    # and the factory read it as that manifest's decisions. The halt
    # copy is stamped ``halted`` and the factory refuses to bind one.
    write_register = functools.partial(
        write_artifact,
        "decisions",
        "architect decisions",
    )
    decisions_path = None
    # #260: what this audit says about the previous one, read BEFORE
    # this run is appended to the journal below - otherwise the
    # "previous run" the report compares against would be this one.
    journal = _spec_audit_journal(root_dir, ui)
    # ONE read feeds both the trend and the accounting under it, so the
    # two are computed over the same snapshot and cannot disagree
    # because the file changed between two reads.
    snapshot = _journal_snapshot(journal, project_name)
    # #280: the trend is keyed on the project name, so a rename starts a
    # fresh one and the runs before it drop out of view. Keying
    # differently would be worse (the spec is edited every round, so a
    # content hash never matches, and #260's own loop renamed the file
    # too), so what is fixed is the silence rather than the key.
    _surface_convergence(
        _build_convergence(spec_issues, spec_path.name, snapshot.window),
        _excluded_history(snapshot.audits, project_name, spec_path.name, snapshot.lookback),
        project_name,
        ui,
    )
    _record_spec_issues_event(
        spec_issues,
        journal=journal,
        project_name=project_name,
        spec_file=spec_path.name,
        halted=bool(escalated),
        ui=ui,
    )
    # #696 slice 7: on the halt path too, since a conflict with the [stack]
    # kstrl.toml holds is one the architect escalates.
    _file_stack_proposal(data, root_dir, stack_pin, spec_source, bus, ui)
    emit(
        PhaseCompleted(
            component=ARCHITECT_COMPONENT,
            phase="audit",
            passed=not escalated,
            detail=f"{len(escalated)} escalation(s)" if escalated else "",
            duration_seconds=round(time.monotonic() - audit_start, 2),
        )
    )
    if escalated:
        decisions_path = write_register(
            lambda: write_decisions(
                decisions,
                root_dir=root_dir,
                project_name=project_name,
                spec_file=spec_path.name,
                halted=True,
                spec_digest=pin,
                answered_items=answers.item_ids,
                answers_digest=answers.digest,
                requirements=requirements,
            ),
        )
        open_escalation_item(
            escalated,
            root_dir,
            project_name,
            spec_source,
            spec_digest=pin,
            register_path=rel_display(decisions_path) if decisions_path is not None else "",
            run_id=bus.run_id if bus is not None else "",
            warn=ui.warn,
            spec_path=spec_path,
        )
        # The run dir must read as FINISHED, not dead: the halt is the
        # architect's judgment, delivered before the error propagates.
        emit(
            ComponentFailed(
                component=ARCHITECT_COMPONENT,
                error=f"spec halted: {len(escalated)} escalated question(s)",
            )
        )
        emit(
            RunCompleted(
                completed=0,
                failed=1,
                duration_seconds=round(time.monotonic() - run_started, 2),
            )
        )
        raise SpecBlockerError(
            escalated,
            artifact_path=artifact_path,
            decisions_path=decisions_path,
        )

    # The forming DAG, the moment it is known (C5's board draws from
    # this - no manifest read needed). The architect row stays first.
    emit(
        RunPlan(
            components=(
                {"id": ARCHITECT_COMPONENT, "title": "Architect / PRD red-team", "deps": []},
                *(
                    {
                        "id": comp_data["id"],
                        "title": comp_data["title"],
                        "deps": comp_data.get("dependencies", []),
                    }
                    for comp_data in data["components"]
                ),
            )
        )
    )

    # R7.4: Linear hook - one project per manifest, one issue per
    # component, non-blocker spec findings into Triage. Runs BEFORE
    # branch derivation so issue identifiers can ride the branch names
    # (Linear's GitHub integration links PRs by identifier-in-branch,
    # so status transitions cost zero API calls). The hook never
    # raises; any failure warns and decompose proceeds without Linear.
    linear_config = LinearConfig.load(root_dir)
    linear_sync = None
    if linear_config.enabled:
        ui.section("Syncing to Linear")
        linear_sync = sync_decompose(
            project_name=project_name,
            components=data["components"],
            spec_issues=spec_issues,
            config=linear_config,
            client=LinearClient(linear_config, warn=ui.warn, log=ui.info),
            warn=ui.warn,
        )
        if linear_sync is not None:
            ui.ok(f"Linear project created with {len(linear_sync.issues)} component issue(s)")
        if linear_sync is not None and single_pr:
            # All components share one branch in single-PR mode, so a
            # per-component identifier cannot ride the branch name.
            ui.warn(
                "linear: single_pr mode - branch names and PR bodies "
                "are not issue-linked; issues and the progress sink "
                "still work"
            )

    # Pre-validate every branch name before any file is written.
    # Component ids were validated in the retry loop; this can only
    # fire for a project_name (user input) that is not branch-safe.
    # Reject rather than sanitize so the caller sees exactly what
    # was wrong (R0.6).
    component_branches: dict[str, str] = {}
    for comp_data in data["components"]:
        comp_id = comp_data["id"]
        branch = _component_branch(comp_id, project_name, single_pr)
        issue_ref = (
            linear_sync.issues.get(comp_id) if linear_sync is not None and not single_pr else None
        )
        if issue_ref is not None:
            linear_branch = linear_branch_name(issue_ref.identifier, comp_id)
            if validate_branch_name(linear_branch) is None:
                branch = linear_branch
            else:
                # An identifier that breaks branch validation would be
                # a Linear-side surprise; degrade to the default branch
                # (losing auto-linking) rather than failing decompose.
                ui.warn(
                    f"linear: issue identifier "
                    f"'{issue_ref.identifier}' does not form a valid "
                    f"branch; component '{comp_id}' keeps its default "
                    f"branch name"
                )
        branch_error = validate_branch_name(branch)
        if branch_error:
            raise ValueError(
                f"Cannot derive a git branch for component '{comp_id}': {branch_error}"
            )
        component_branches[comp_id] = branch

    # Generate PRDs and build manifest components. Everything below has
    # already validated, so a failure here is an I/O problem or a
    # harness bug; either way, remove the files written so far rather
    # than leaving partial decompose state for the next run to trip
    # over (R1.8). The spec-issues artifact is deliberately NOT cleaned
    # up - it is the audit record.
    ui.section("Generating PRDs")
    manifest_components: list[Component] = []
    written_prds: list[Path] = []
    created_dirs: list[Path] = []
    # #260: recomputed from the same payload the retry loop validated,
    # so the specIssues block written below is the one that passed
    # PRD.validate_schema up there.
    routed_issues = _routed_prd_issues(data)
    # #568: one id for this decompose, stamped on every component it puts
    # in the manifest and naming the directory its planned PRDs go in.
    plan_id = mint_run_id("decompose")
    try:
        for comp_data in data["components"]:
            comp_id = comp_data["id"]
            branch = component_branches[comp_id]

            # Track directories this run creates so cleanup can remove
            # them; pre-existing directories are left alone.
            probe = plan_prd_path(root_dir, comp_id, plan_id=plan_id).parent
            while not probe.exists() and probe != root_dir:
                created_dirs.append(probe)
                probe = probe.parent

            prd_path = _generate_component_prd(
                comp_data,
                root_dir,
                branch,
                routed_issues[comp_id],
                plan_id=plan_id,
            )
            written_prds.append(prd_path)
            # The path the engineer's copy has in its worktree and on
            # the component branch. Nothing writes it in the root
            # checkout (#545).
            rel_prd = f"scripts/kstrl/feature/{comp_id}/prd.json"

            issue_ref = linear_sync.issues.get(comp_id) if linear_sync is not None else None
            manifest_components.append(
                Component(
                    id=comp_id,
                    title=comp_data["title"],
                    description=comp_data["description"],
                    dependencies=comp_data.get("dependencies", []),
                    prd_path=rel_prd,
                    branch_name=branch,
                    status=ComponentStatus.PENDING.value,
                    plan_id=plan_id,
                    linear_issue_id=issue_ref.id if issue_ref else "",
                    linear_issue_identifier=(issue_ref.identifier if issue_ref else ""),
                )
            )
            ui.ok(
                _prd_summary_line(
                    comp_id,
                    len(comp_data["userStories"]),
                    routed_issues[comp_id],
                )
            )

        manifest = Manifest(
            version="1",
            spec_file=spec_path.name,
            project_name=project_name,
            base_branch=base_branch,
            single_pr=single_pr,
            components=manifest_components,
            linear_project_id=(linear_sync.project_id if linear_sync is not None else ""),
            linear_sync_key=(linear_sync.sync_key if linear_sync is not None else ""),
            spec_path=spec_source,
            spec_digest=pin,
            stack_digest=stack_pin,
        )

        # Validate DAG
        dag_errors = manifest.validate_dag()
        if dag_errors:
            ui.warn("DAG validation warnings:")
            for err in dag_errors:
                ui.warn(f"  {err}")

        # Save manifest (atomic write; covered by the cleanup scope so
        # a save failure does not strand PRDs without a manifest)
        manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
        # #597: a live factory run owns this manifest and saves its whole
        # in-memory copy, so the manifest and the register are written under
        # the run lock. `ks factory --spec` already holds it (acquired
        # before this call, ahead of the architect's spend, #597); `ks
        # decompose` has no caller's lock and takes its own here. Either
        # way a held lock raises FactoryLockHeldError inside the cleanup
        # scope, so a refused decompose leaves no PRDs behind.
        from kstrl.factory_lock import held_or_acquired_run_lock

        with held_or_acquired_run_lock(run_lock, root_dir, ui, force_lock):
            manifest.save(manifest_path)
            ui.ok(f"Manifest saved: {manifest_path}")
            # AFTER the manifest, never before: the register binds engineers
            # to this manifest, so it must not exist unless this manifest
            # does. Inside the cleanup scope for the same reason.
            write_register(
                lambda: write_decisions(
                    decisions,
                    root_dir=root_dir,
                    project_name=project_name,
                    spec_file=spec_path.name,
                    halted=False,
                    spec_digest=pin,
                    answered_items=answers.item_ids,
                    answers_digest=answers.digest,
                    requirements=requirements,
                ),
                required=True,
            )
    except BaseException:
        for prd_file in written_prds:
            try:
                prd_file.unlink()
            except OSError:
                pass
        # Deepest-first so children go before parents; rmdir refuses
        # non-empty directories, which protects anything user-owned.
        for created in sorted(set(created_dirs), key=lambda p: len(p.parts), reverse=True):
            try:
                created.rmdir()
            except OSError:
                pass
        raise
    # #449: after the manifest and the register commit, never inside the
    # cleanup scope above, so an escalation is resolved only for a
    # decompose that is on disk.
    resolve_escalation_items(
        root_dir,
        project_name,
        spec_source,
        spec_digest=pin,
        run_id=bus.run_id if bus is not None else "",
        info=ui.info,
        warn=ui.warn,
    )

    # Publish transactional artifacts only after the PRD + manifest
    # write set commits. If a later write failed, the cleanup above
    # removed the PRDs and the event stream must not claim they exist.
    for component_data, prd_file in zip(
        data["components"],
        written_prds,
        strict=True,
    ):
        emit(
            ArtifactWritten(
                component=component_data["id"],
                label="prd",
                path=rel_display(prd_file),
            )
        )
    emit(
        ArtifactWritten(
            label="manifest",
            path=rel_display(manifest_path),
        )
    )

    ui.section("Decomposition Summary")
    ui.kv("Components", str(len(manifest.components)))
    total_stories = sum(len(comp_data.get("userStories", [])) for comp_data in data["components"])
    ui.kv("Total stories", str(total_stories))

    duration = round(time.monotonic() - run_started, 2)
    emit(
        ComponentCompleted(
            component=ARCHITECT_COMPONENT,
            duration_seconds=duration,
            iterations=attempts_used,
        )
    )
    # completed counts THIS run's work item (the architect); the
    # planned components are the FACTORY run's job.
    emit(RunCompleted(completed=1, duration_seconds=duration))

    return manifest


def decompose_spec(
    spec_path: Path,
    project_name: str,
    base_branch: str,
    single_pr: bool,
    agent: Agent,
    ui: UI,
    root_dir: Path,
    max_retries: int = 3,
    *,
    bus: EventBus | None = None,
    transcript: Callable[[str], None] | None = None,
    prompt_call: AgentCall,
    force_lock: bool = False,
    run_lock: _RunLock | None = None,
    timeout: float | None,
    before_spend: Callable[[], object],
) -> Manifest:
    """Run decomposition, guaranteeing a ``RunCompleted`` and a usage
    capture on every exit.

    ``run_lock``: see :func:`_decompose_spec_impl` (#597).

    ``timeout``: the architect call's limit in seconds, None for no limit
    (#603). A call the adapter kills at it is one failed attempt.

    #257: the capture sits in a ``finally`` because the blocker halt is
    the COMMON outcome on a first spec, and it is the path where the
    operator most needs the number before editing and re-running. An
    emit-after-return would miss exactly that case.

    Consequence, stated plainly: ``RunCompleted`` is emitted by the impl
    (both the halt site and the success tail) or by the ``except`` below,
    so ``ComponentUsage`` trails it on EVERY path, not just the halt. No
    consumer treats ``RunCompleted`` as a stream terminator - the reducer
    sets a flag and keeps folding - and every reader folds the whole
    stream, so position changes no total. A consumer that ever does stop
    there has to move this emit, not just handle it.
    """
    started = time.monotonic()
    # Read BEFORE the work so the report covers this call only.
    usage_before = usage_cursor(agent)
    try:
        return _decompose_spec_impl(
            spec_path=spec_path,
            project_name=project_name,
            base_branch=base_branch,
            single_pr=single_pr,
            agent=agent,
            ui=ui,
            root_dir=root_dir,
            max_retries=max_retries,
            bus=bus,
            transcript=transcript,
            prompt_call=prompt_call,
            force_lock=force_lock,
            run_lock=run_lock,
            timeout=timeout,
            before_spend=before_spend,
        )
    except SpecBlockerError:
        # Blocker halts are deliberately finalized at the audit site so
        # the durable artifact path can be attached to the exception.
        raise
    except BaseException as exc:
        if bus is not None:
            detail = f"{type(exc).__name__}: {exc}"
            bus.emit(
                ComponentFailed(
                    component=ARCHITECT_COMPONENT,
                    error=detail,
                )
            )
            bus.emit(
                RunCompleted(
                    failed=1,
                    duration_seconds=round(time.monotonic() - started, 2),
                )
            )
        raise
    finally:
        # Isolated because a `finally` that raises REPLACES the exception
        # propagating through it, which here would destroy the very halt
        # this reporting exists to cover: the SpecBlockerError becomes a
        # BrokenPipeError traceback and the documented exit code 2 is
        # lost.
        #
        # Measured (2026-08-30), because the obvious repro is the wrong
        # one: `ks decompose | head -5` does NOT do it. Both UIs default
        # to sys.stderr (ui/plain.py, ui/rich_ui.py), so piping stdout
        # leaves the stream the rollup writes to untouched. The shape
        # that fires is `ks decompose 2>&1 | head -5`, which raises
        # BrokenPipeError out of PlainUI._print's `print(...)` once the
        # writes pass the pipe buffer.
        #
        # `collect_usage` and `EventBus.emit` are already defensive; the
        # UI writes are not, and must not be made so inside
        # `print_usage_rollup` - the factory epilogue shares it and a
        # failed write there should be loud.
        try:
            _report_architect_usage(agent, ui, bus, since=usage_before)
        except Exception as exc:  # noqa: BLE001 - accounting never gates a run
            logger.warning("Failed to report architect usage: %s", exc)
