"""LLM-driven spec decomposition into components and PRDs."""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    resolve_escalation_items,
    write_decisions,
)
from kstrl.decompose_audit import (
    SpecBlockerError,
    _audit_spec,
)
from kstrl.decompose_plan import (
    _component_branches,
    _publish_plan,
    _sync_linear,
    _write_plan,
)
from kstrl.decompose_prompt import (
    SPECKIT_ARTIFACTS,
    build_decompose_prompt,
    load_spec_input,
    spec_digest,
    spec_ref,
)
from kstrl.decompose_reply import (
    _prd_schema_errors,
    _retry_feedback,
    _validate_decompose_output,
)
from kstrl.event_catalog import (
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
)
from kstrl.events import EventBus
from kstrl.manifest import (
    Manifest,
)
from kstrl.owner_answers import OwnerAnswers, read_owner_answers
from kstrl.stack import (
    stack_text_digest,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from kstrl.factory_lock import _RunLock
    from kstrl.ui.base import UI


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


@dataclasses.dataclass
class _AttemptState:
    """What the architect attempts leave behind for the next one (#776).

    ``data`` is the parsed reply of the attempt that passed, None until one
    does. ``last_error`` is why the latest attempt failed, None after a pass.
    """

    data: Any = None
    last_error: str | None = None


def _stream_reply(
    *,
    agent: Agent,
    retry_prompt: str,
    prompt_call: AgentCall,
    attempt: int,
    root_dir: Path,
    timeout: float | None,
    ui: UI,
    transcript: Callable[[str], None] | None,
    attempt_failed: Callable[..., None],
    phase_start: float,
) -> tuple[list[str], bool, int]:
    """Stream one architect reply: its lines, whether it was too large, the usage cursor."""
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
    return output_lines, too_large, cursor


def _parse_reply(
    *,
    agent: Agent,
    output_lines: list[str],
    state: _AttemptState,
    stack_pin: str,
    project_name: str,
    single_pr: bool,
    ui: UI,
    emit: Callable[[Event], None],
    attempt_failed: Callable[..., None],
    phase_start: float,
) -> None:
    """Read one architect reply and record it in ``state``, or record why not."""
    try:
        state.data = _extract_agent_json(agent, output_lines)
    except ValueError as exc:
        state.last_error = str(exc)
        ui.warn(f"JSON extraction failed: {state.last_error}")
        attempt_failed(state.last_error, started_at=phase_start)
        return

    validation_errors = _validate_decompose_output(state.data, has_stack=bool(stack_pin))
    if validation_errors:
        state.last_error = _retry_feedback(validation_errors)
        ui.warn(f"Validation failed: {state.last_error}")
        attempt_failed(state.last_error, started_at=phase_start)
        state.data = None
        return

    prd_errors = _prd_schema_errors(state.data, project_name, single_pr)
    if prd_errors:
        state.last_error = _retry_feedback(prd_errors)
        ui.warn(f"PRD validation failed: {state.last_error}")
        attempt_failed(state.last_error, started_at=phase_start)
        state.data = None
        return

    state.last_error = None
    emit(
        PhaseCompleted(
            component=ARCHITECT_COMPONENT,
            phase="decompose",
            passed=True,
            duration_seconds=round(time.monotonic() - phase_start, 2),
        )
    )


def _architect_attempt(
    *,
    state: _AttemptState,
    attempt: int,
    max_retries: int,
    prompt: str,
    agent: Agent,
    ui: UI,
    root_dir: Path,
    timeout: float | None,
    transcript: Callable[[str], None] | None,
    prompt_call: AgentCall,
    stack_pin: str,
    project_name: str,
    single_pr: bool,
    emit: Callable[[Event], None],
) -> None:
    """Make one architect call and leave its outcome in ``state``."""
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

    if state.last_error:
        retry_prompt = (
            f"{prompt}\n\n"
            f"PREVIOUS ATTEMPT FAILED with error:\n{state.last_error}\n\n"
            f"Please fix the error and output valid JSON."
        )
    else:
        retry_prompt = prompt

    output_lines, too_large, cursor = _stream_reply(
        agent=agent,
        retry_prompt=retry_prompt,
        prompt_call=prompt_call,
        attempt=attempt,
        root_dir=root_dir,
        timeout=timeout,
        ui=ui,
        transcript=transcript,
        attempt_failed=attempt_failed,
        phase_start=phase_start,
    )

    stopped = _attempt_stopped(agent, cursor, too_large=too_large, timeout=timeout)
    if stopped is not None:
        state.last_error = stopped
        attempt_failed(state.last_error, started_at=phase_start)
        return

    _parse_reply(
        agent=agent,
        output_lines=output_lines,
        state=state,
        stack_pin=stack_pin,
        project_name=project_name,
        single_pr=single_pr,
        ui=ui,
        emit=emit,
        attempt_failed=attempt_failed,
        phase_start=phase_start,
    )


def _run_architect(
    *,
    prompt: str,
    max_retries: int,
    agent: Agent,
    ui: UI,
    root_dir: Path,
    timeout: float | None,
    transcript: Callable[[str], None] | None,
    prompt_call: AgentCall,
    stack_pin: str,
    project_name: str,
    single_pr: bool,
    emit: Callable[[Event], None],
) -> tuple[Any, int]:
    """Run the architect attempts: the parsed reply that passed, and the attempts used.

    Raises ValueError when no attempt within ``max_retries`` passes.
    """
    state = _AttemptState(data=None, last_error=None)
    attempts_used = 0

    for attempt in range(1, max_retries + 1):
        attempts_used = attempt
        _architect_attempt(
            state=state,
            attempt=attempt,
            max_retries=max_retries,
            prompt=prompt,
            agent=agent,
            ui=ui,
            root_dir=root_dir,
            timeout=timeout,
            transcript=transcript,
            prompt_call=prompt_call,
            stack_pin=stack_pin,
            project_name=project_name,
            single_pr=single_pr,
            emit=emit,
        )
        if state.last_error is None:
            break

    if state.data is None:
        # No files were written in the retry loop, so terminal failure
        # leaves no partial state behind (R1.8).
        raise ValueError(
            f"Failed to decompose spec after {max_retries} attempts. Last error: {state.last_error}"
        )

    return state.data, attempts_used


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
    data, attempts_used = _run_architect(
        prompt=prompt,
        max_retries=max_retries,
        agent=agent,
        ui=ui,
        root_dir=root_dir,
        timeout=timeout,
        transcript=transcript,
        prompt_call=prompt_call,
        stack_pin=stack_pin,
        project_name=project_name,
        single_pr=single_pr,
        emit=emit,
    )

    spec_issues, decisions, requirements, write_register = _audit_spec(
        data,
        ui=ui,
        emit=emit,
        rel_display=rel_display,
        root_dir=root_dir,
        project_name=project_name,
        spec_path=spec_path,
        spec_source=spec_source,
        pin=pin,
        stack_pin=stack_pin,
        answers=answers,
        bus=bus,
        run_started=run_started,
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

    linear_sync = _sync_linear(
        data,
        spec_issues,
        ui=ui,
        root_dir=root_dir,
        project_name=project_name,
        single_pr=single_pr,
    )

    component_branches = _component_branches(
        data,
        linear_sync,
        ui=ui,
        project_name=project_name,
        single_pr=single_pr,
    )

    def commit_register() -> None:
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

    manifest, manifest_path, written_prds = _write_plan(
        data,
        linear_sync,
        component_branches,
        ui=ui,
        root_dir=root_dir,
        project_name=project_name,
        base_branch=base_branch,
        single_pr=single_pr,
        spec_path=spec_path,
        spec_source=spec_source,
        pin=pin,
        stack_pin=stack_pin,
        run_lock=run_lock,
        force_lock=force_lock,
        commit_register=commit_register,
    )

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

    _publish_plan(
        data,
        manifest,
        manifest_path,
        written_prds,
        ui=ui,
        emit=emit,
        rel_display=rel_display,
    )

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
