"""The feature flow: understand -> review gate -> implement -> repairs.

Mechanical extraction from cli.feature (TUI surface C2): the click
shell resolves configs/paths/validation and builds FeatureParams; this
module runs the flow and RETURNS the exit code (no sys.exit - the flow
must be hostable on a worker thread). Narration was byte-identical to
the pre-extraction command; #288 has since added calls to
``feature_verify.report_verification``, and nothing else.

The review gate goes through the interaction seam: the terminal wires
UiInteractionChannel (unchanged semantics incl. the non-TTY
"Interactive review required" refusal); the embedded TUI (C3) passes
its queue channel so the gate opens as a modal.

#288: once before the implement loop and after every engineer loop that
actually called the agent, the flow calls
``feature_verify.report_verification``, which runs the read-only
mechanical checks against the operator's checkout and REPORTS what they
found, to the terminal and to the event stream. It is a report, not a
gate: control flow, exit codes and the repair loop are untouched by the
verdict, and the only value that comes back is the failing-check set
this module carries to the next report so pre-existing breakage is named
as pre-existing.

Two consequences of naming the gate to the implement and repair loops
that are not the report itself. The engineer prompt gains the resolved
VERIFY_COMMANDS_PROMPT block, whose wording says a "gate" runs those
commands where this flow only reports them - the COMMANDS it names are
exactly the commands that run, which is the load-bearing half, but the
consequence it implies is stronger than the truth, and correcting the
wording is an H3 prompt change with a calibration cost (#288 review).
And ``loop.build_project_context`` now also runs
``scrub_project_claude_md`` here, so a project whose CLAUDE.md carries
pre-#261 ``- **Test**: ...`` bullets gets them dropped from the prompt
copy, with a ui.warn per divergence. That is #261 working as designed,
one path later than it was written for, and it never touches the file on
disk.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.agents import get_agent
from kstrl.agents.logging import LoggingAgent
from kstrl.breaker import BreakerConfig
from kstrl.events import (
    ArtifactWritten,
    CheckpointRequested,
    CheckpointResolved,
    ComponentCompleted,
    ComponentFailed,
    ComponentSkipped,
    ComponentStarted,
    Event,
    PhaseCompleted,
    PhaseStarted,
    RunCompleted,
    RunPlan,
    RunStarted,
)
from kstrl.feature_verify import report_verification, resolve_feature_verify_config
from kstrl.interaction import (
    InteractionChannel,
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from kstrl.loop import run_loop
from kstrl.timeout import TimeoutConfig

if TYPE_CHECKING:
    from kstrl.agents.base import Agent
    from kstrl.commandrun import CommandRun
    from kstrl.config import KstrlConfig
    from kstrl.prd import PRD
    from kstrl.sandbox import SandboxConfig
    from kstrl.ui.base import UI


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class FeatureParams:
    """The feature command's resolved knobs (CLI/env/toml already
    collapsed by the shell; None = "not overridden")."""

    prd_path: Path
    prd_doc: PRD
    feature_name: str
    feature_dir: Path
    feature_understand: Path
    log_dir: Path
    understand_iterations: int
    understand_prompt_file: Path | None
    #: The engineer prompt the implement and repair loops run on. Set by
    #: the caller so the CLI preflight that warns about a stale prompt
    #: (#286) and the loop that reads it are the SAME path by
    #: construction. It was a literal repeated in both modules, agreeing
    #: only by luck; nothing failed when they diverged.
    prompt_file: Path
    implementation_auto_run: bool
    repair_max_runs: int
    repair_iterations: int
    repair_agent_cmd: str | None
    branch_override: str | None
    allowed_paths_override: list[str] | None
    sandbox: SandboxConfig


def _log_path(params: FeatureParams, label: str, attempt: int | None = None) -> Path:
    stamp = _timestamp()
    if attempt is None:
        name = f"{label}_{stamp}.log"
    else:
        name = f"{label}_{attempt:02d}_{stamp}.log"
    return params.log_dir / name


def _build_repair_prd(
    params: FeatureParams,
    root_dir: Path,
    log_file: Path,
    attempt: int,
) -> Path:
    repair_dir = params.feature_dir / "repairs"
    repair_dir.mkdir(parents=True, exist_ok=True)
    repair_path = repair_dir / f"repair_{_timestamp()}.json"
    latest_path = repair_dir / "latest.json"

    verification: list[str] = []
    seen: set[str] = set()
    for story in params.prd_doc.user_stories:
        for item in story.acceptance_criteria:
            lower = item.lower()
            has_check = "typecheck" in lower or "tests" in lower or "lint" in lower
            if has_check and "pass" in lower:
                if item not in seen:
                    seen.add(item)
                    verification.append(item)

    try:
        rel_log = log_file.relative_to(root_dir)
        log_ref = rel_log.as_posix()
    except ValueError:
        log_ref = str(log_file)

    criteria = [f"Repair failures reported in {log_ref}"]
    criteria.extend(verification)

    repair_story = {
        "id": f"REPAIR-{attempt:02d}",
        "title": "Repair failures from last run",
        "acceptanceCriteria": criteria,
        "priority": 1,
        "passes": False,
        "notes": f"Original PRD: {params.prd_path}",
    }
    repair_doc = {
        "branchName": params.prd_doc.branch_name,
        "userStories": [repair_story],
    }
    # utf-8 on both, because this document is read back by ``PRD.load``,
    # which names utf-8: a writer left on the locale codec would let the
    # factory write a repair PRD it could not read on a non-utf-8
    # machine. The acceptance criteria here are copied verbatim from the
    # operator's PRD, so non-ASCII in them is ordinary.
    with open(repair_path, "w", encoding="utf-8") as handle:
        json.dump(repair_doc, handle, indent=2)
        handle.write("\n")
    with open(latest_path, "w", encoding="utf-8") as handle:
        json.dump(repair_doc, handle, indent=2)
        handle.write("\n")

    return repair_path


def run_feature(
    params: FeatureParams,
    base_config: KstrlConfig,
    agent: Agent,
    ui: UI,
    root_dir: Path,
    *,
    interaction: InteractionChannel | None = None,
    run: CommandRun | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> int:
    """Understand -> review gate -> implement -> repair loop.

    Returns the flow's exit code. ``interaction`` defaults to the
    terminal channel; ``run`` records the flow as an event-stream run
    projected onto the pseudo-component <feature_name> (phases
    understand / implement / repair-N; the gate as a checkpoint pair);
    ``stop_check`` threads into every run_loop. Narration and the
    legacy .kstrl/logs/feature_* transcripts are byte-identical with
    or without ``run``.
    """
    component = params.feature_name
    bus = run.bus if run is not None else None
    # This flow used to hand `run_loop` its own `.kstrl/logs/<feature>/`
    # entry. #274 removed it: every loop below runs with `cwd=root_dir`,
    # `guard_state_root=root_dir` carves out `.kstrl/logs/` as part of
    # the whole state directory, and `path_is_allowed` matches that as a
    # prefix. Declaring it here as well would print two entries for one
    # carve-out and read as wider than it is.

    def emit(event: Event) -> None:
        if bus is not None:
            bus.emit(event)

    def wrap(phase_agent: Agent) -> Agent:
        """Tee the phase agent onto the run transcript ON TOP of its
        legacy log (nested LoggingAgent: legacy bytes unchanged)."""
        transcript = run.transcript_path(component) if run is not None else None
        if transcript is None:
            return phase_agent
        return LoggingAgent(phase_agent, transcript)

    def skip(reason: str) -> None:
        emit(ComponentSkipped(component=component, reason=reason))
        emit(
            RunCompleted(
                skipped=1,
                duration_seconds=round(time.monotonic() - started, 2),
            )
        )

    def fail(error: str) -> None:
        emit(ComponentFailed(component=component, error=error))
        emit(
            RunCompleted(
                failed=1,
                duration_seconds=round(time.monotonic() - started, 2),
            )
        )

    def phase_detail(exit_code: int, completed: bool) -> str:
        if completed:
            return ""
        if exit_code != 0:
            return f"exit {exit_code}"
        return "ended before completion"

    started = time.monotonic()
    emit(RunStarted(project=params.feature_name, components=1))
    emit(RunPlan(components=({"id": component, "title": f"Feature: {component}", "deps": []},)))
    emit(ComponentStarted(component=component))

    # Feature understanding phase
    understand_config = copy.deepcopy(base_config)
    understand_config.max_iterations = params.understand_iterations
    if params.understand_prompt_file is not None:
        understand_config.prompt_file = params.understand_prompt_file
    understand_config.prd_file = params.prd_path
    rel_feature_understand = params.feature_understand.relative_to(root_dir).as_posix()
    understand_config.allowed_paths = [rel_feature_understand]
    if params.branch_override is not None:
        understand_config.kstrl_branch = params.branch_override
        understand_config.kstrl_branch_explicit = True

    timeouts = TimeoutConfig.load(root_dir)
    breaker_config = BreakerConfig.load(root_dir)
    # #288: the config the implement and repair loops are TOLD about and
    # the config the report below RUNS with are the same object, so the
    # engineer prompt's "these are the exact commands kstrl's mechanical
    # verification gate runs on your work" is true on this path. The
    # understand loop is deliberately left on the default None: no gate
    # runs on an understand file, so #261's rule that None states nothing
    # is still the correct and truthful value there.
    verify_config = resolve_feature_verify_config(root_dir)

    emit(PhaseStarted(component=component, phase="understand", attempt=1))
    phase_start = time.monotonic()
    understand_log = _log_path(params, "understand")
    understand_agent = wrap(LoggingAgent(agent, understand_log))
    try:
        understand_result = run_loop(
            understand_config,
            ui,
            understand_agent,
            root_dir,
            timeouts=timeouts,
            breaker_config=breaker_config,
            bus=bus,
            interaction=interaction,
            stop_check=stop_check,
            guard_state_root=root_dir,
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        emit(
            PhaseCompleted(
                component=component,
                phase="understand",
                passed=False,
                detail=detail,
                duration_seconds=round(time.monotonic() - phase_start, 2),
            )
        )
        fail(detail)
        raise
    understand_completed = understand_result.completed and understand_result.exit_code == 0
    understand_detail = phase_detail(
        understand_result.exit_code,
        understand_completed,
    )
    emit(
        PhaseCompleted(
            component=component,
            phase="understand",
            passed=understand_completed,
            detail=understand_detail,
            duration_seconds=round(time.monotonic() - phase_start, 2),
        )
    )
    if not understand_completed:
        if understand_result.exit_code == 0:
            skip("understand phase ended before completion")
        else:
            fail(f"understand phase exited {understand_result.exit_code}")
        return understand_result.exit_code
    emit(
        ArtifactWritten(
            component=component,
            label="understand_file",
            path=rel_feature_understand,
        )
    )

    # Review gate
    ui.section("Feature understand review")
    ui.kv("Understand file", str(params.feature_understand))
    if params.implementation_auto_run:
        ui.info("IMPLEMENTATION_AUTO_RUN enabled: skipping review gate")
    else:
        channel = interaction if interaction is not None else UiInteractionChannel(ui)
        gate_header = "Review the understand file and confirm implementation start:"
        emit(
            CheckpointRequested(
                component=component,
                kind="feature_gate",
                question=gate_header,
            )
        )
        if not channel.can_prompt():
            ui.err("Interactive review required. Re-run with --implementation-auto-run.")
            emit(
                CheckpointResolved(
                    component=component,
                    kind="feature_gate",
                    decision="unavailable",
                    decided_by="auto",
                )
            )
            skip("feature review gate unavailable")
            return 2

        response = channel.request(
            PromptRequest(
                kind=PromptKind.CONFIRM,
                header=gate_header,
                options=("Start implementation", "Quit to amend"),
                default=0,
            )
        )
        decided_by = "operator" if response.answered else "auto"
        if not response.answered or response.choice != 0:
            ui.info("Amend the understand file and re-run `ks feature`.")
            emit(
                CheckpointResolved(
                    component=component,
                    kind="feature_gate",
                    decision="quit_to_amend",
                    decided_by=decided_by,
                )
            )
            skip("operator quit to amend the understand file")
            return 0
        emit(
            CheckpointResolved(
                component=component,
                kind="feature_gate",
                decision="start_implementation",
                decided_by=decided_by,
            )
        )

    # Implementation phase
    run_config = copy.deepcopy(base_config)
    run_config.prd_file = params.prd_path
    run_config.max_iterations = len(params.prd_doc.user_stories)
    if run_config.max_iterations == 0:
        ui.warn("PRD has no user stories. Skipping implementation.")
        skip("PRD has no user stories")
        return 0
    run_config.prompt_file = params.prompt_file
    if params.allowed_paths_override is not None:
        run_config.allowed_paths = params.allowed_paths_override
    if params.branch_override is not None:
        run_config.kstrl_branch = params.branch_override
        run_config.kstrl_branch_explicit = True

    # The pre-implement BASELINE (#288 review). This report measures the
    # whole checkout, not the diff, so without a before-picture a
    # checkout whose lint was already red reads as the agent having
    # broken lint - and under --implementation-auto-run, where the event
    # stream is the only report, nothing distinguishes the two. That is
    # the same honesty argument that disabled the six diff-based checks,
    # so it has to apply to the three that were kept.
    #
    # ONE extra measurement per run, not per loop: every later report
    # carries this set forward, so the whole chain is attributable for
    # the price of the first. Measured on this repo: 246s, essentially
    # all test suite, against 317-348s for the engineer loop it precedes.
    # It also front-loads the answer - an operator learns their tree is
    # already broken before paying for an agent, rather than after.
    already_failing = report_verification(
        ui,
        emit,
        component,
        root_dir,
        verify_config,
        "baseline",
        stop_check=stop_check,
    )

    emit(PhaseStarted(component=component, phase="implement", attempt=1))
    phase_start = time.monotonic()
    run_log = _log_path(params, "run")
    run_agent = wrap(LoggingAgent(agent, run_log))
    try:
        result = run_loop(
            run_config,
            ui,
            run_agent,
            root_dir,
            timeouts=timeouts,
            breaker_config=breaker_config,
            bus=bus,
            interaction=interaction,
            stop_check=stop_check,
            guard_state_root=root_dir,
            verify_config=verify_config,
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        emit(
            PhaseCompleted(
                component=component,
                phase="implement",
                passed=False,
                detail=detail,
                duration_seconds=round(time.monotonic() - phase_start, 2),
            )
        )
        fail(detail)
        raise
    # Stopped BEFORE the report, so the phase's duration is still the
    # engineer loop's own and stays comparable to a pre-#288 run. The
    # report carries its own duration on its event.
    phase_duration = round(time.monotonic() - phase_start, 2)
    already_failing = report_verification(
        ui,
        emit,
        component,
        root_dir,
        verify_config,
        "implement",
        loop_result=result,
        stop_check=stop_check,
        already_failing=already_failing,
    )
    implementation_completed = result.completed and result.exit_code == 0
    implementation_detail = phase_detail(
        result.exit_code,
        implementation_completed,
    )
    emit(
        PhaseCompleted(
            component=component,
            phase="implement",
            passed=implementation_completed,
            detail=implementation_detail,
            duration_seconds=phase_duration,
        )
    )
    if implementation_completed:
        emit(
            ComponentCompleted(
                component=component,
                duration_seconds=round(time.monotonic() - started, 2),
                iterations=result.iterations,
            )
        )
        emit(
            RunCompleted(
                completed=1,
                duration_seconds=round(time.monotonic() - started, 2),
            )
        )
        return 0
    if result.exit_code == 0:
        skip("implementation ended before completion")
        return 0
    if params.repair_max_runs == 0 or result.iterations == 0:
        fail(f"implementation exited {result.exit_code}")
        return result.exit_code

    last_log = run_log
    repair_result = result
    for attempt in range(1, params.repair_max_runs + 1):
        repair_prd = _build_repair_prd(params, root_dir, last_log, attempt)
        try:
            repair_prd_display = str(repair_prd.relative_to(root_dir))
        except ValueError:
            repair_prd_display = str(repair_prd)
        emit(
            ArtifactWritten(
                component=component,
                label="repair_prd",
                path=repair_prd_display,
            )
        )
        repair_config = copy.deepcopy(base_config)
        repair_config.prd_file = repair_prd
        repair_config.prompt_file = params.prompt_file
        repair_config.max_iterations = params.repair_iterations
        if params.allowed_paths_override is not None:
            repair_config.allowed_paths = params.allowed_paths_override
        repair_config.kstrl_branch = ""
        repair_config.kstrl_branch_explicit = True

        emit(
            PhaseStarted(
                component=component,
                phase=f"repair-{attempt}",
                attempt=1,
            )
        )
        phase_start = time.monotonic()
        repair_log = _log_path(params, "repair", attempt)
        repair_agent_base = get_agent(
            params.repair_agent_cmd or base_config.agent_cmd,
            base_config.model,
            base_config.model_reasoning_effort,
            base_config.agent_type,
            sandbox=params.sandbox,
        )
        repair_agent = wrap(LoggingAgent(repair_agent_base, repair_log))
        try:
            repair_result = run_loop(
                repair_config,
                ui,
                repair_agent,
                root_dir,
                timeouts=timeouts,
                breaker_config=breaker_config,
                bus=bus,
                interaction=interaction,
                stop_check=stop_check,
                guard_state_root=root_dir,
                verify_config=verify_config,
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            emit(
                PhaseCompleted(
                    component=component,
                    phase=f"repair-{attempt}",
                    passed=False,
                    detail=detail,
                    duration_seconds=round(time.monotonic() - phase_start, 2),
                )
            )
            fail(detail)
            raise
        phase_duration = round(time.monotonic() - phase_start, 2)
        already_failing = report_verification(
            ui,
            emit,
            component,
            root_dir,
            verify_config,
            f"repair-{attempt}",
            loop_result=repair_result,
            stop_check=stop_check,
            already_failing=already_failing,
        )
        repair_completed = repair_result.completed and repair_result.exit_code == 0
        repair_detail = phase_detail(
            repair_result.exit_code,
            repair_completed,
        )
        emit(
            PhaseCompleted(
                component=component,
                phase=f"repair-{attempt}",
                passed=repair_completed,
                detail=repair_detail,
                duration_seconds=phase_duration,
            )
        )
        if repair_completed:
            emit(
                ComponentCompleted(
                    component=component,
                    duration_seconds=round(time.monotonic() - started, 2),
                    iterations=repair_result.iterations,
                )
            )
            emit(
                RunCompleted(
                    completed=1,
                    duration_seconds=round(time.monotonic() - started, 2),
                )
            )
            return 0
        if repair_result.exit_code == 0:
            skip(f"repair-{attempt} ended before completion")
            return 0
        last_log = repair_log

    fail(
        f"repairs exhausted after {params.repair_max_runs} run(s) (exit {repair_result.exit_code})"
    )
    return repair_result.exit_code
