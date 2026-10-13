"""The component worker: one engineer attempt, with its prompt context and process setup."""

from __future__ import annotations

import functools
import multiprocessing
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from kstrl.agents.prompt_record import AgentCall, recording_prompts
from kstrl.breaker import BreakerConfig
from kstrl.commandrun import start_heartbeat as _start_heartbeat
from kstrl.config import (
    KstrlConfig,
    component_progress_path,
)
from kstrl.context import IterationContext
from kstrl.events import EventBus, JsonlSink, RunPaths, UsageTotals, collect_usage
from kstrl.factory_result import (
    ComponentResult,
    write_partial_usage,
)
from kstrl.feedforward import CodebaseScanConfig, build_codebase_scan_context
from kstrl.git import resolve_base_ref
from kstrl.io import pre_run_prd_path
from kstrl.loop_budget import LoopBudget
from kstrl.operator_context import (
    GOLDEN_PATTERNS,
    MEMORY,
    load_operator_file,
    operator_file_notices,
    operator_file_spec,
)
from kstrl.plan import ComponentScope
from kstrl.proc import (
    TimeoutConfig,
    WorktreeSetup,
    WorktreeSweep,
    kill_active_process_groups,
    sweep_worktree,
)
from kstrl.sandbox import SandboxConfig
from kstrl.verify_model import VerifyConfig

if TYPE_CHECKING:
    from kstrl.ui import UI


#: H3 (#303): the in-loop guard-violation error _run_component builds when
#: the component's own guard catches a scope violation before Phase 1 runs.
#: Its wording differs from verify._diff_scope_details by a sentence and
#: three punctuation marks; both copies are enrolled as they stand, not
#: unified, because unifying them is a text change that owes calibration.
IN_LOOP_SCOPE_VIOLATION_PROMPT_VERSION = "1.0.0"

IN_LOOP_SCOPE_VIOLATION_PROMPT = (
    "{count} file(s) outside the "
    "component's allowed scope, caught in-loop after "
    "iteration {iterations}: {listed}. "
    "Base branch: {base_branch} "
    "(scope is judged on `git diff {base_branch}...HEAD`; "
    "do NOT `git checkout {base_branch} -- <path>`, revert "
    "only your own out-of-scope commits/edits). "
    "Allowed paths (complete list): "
    "{allowed_paths}; "
    "plus harness artifacts (kstrl's own files, already in "
    "scope, no need to widen allowedPaths): "
    "{harness_paths}. "
    "Do not widen allowedPaths."
)


def _worker_scope(scope: ComponentScope | None) -> tuple[list[str], list[str]]:
    """The snapshot's two halves as the worker's guard wants them.

    Its own function so ``_run_component`` spends no branching on
    unpacking an argument: that function is already the largest in this
    module and the complexity ratchet judges it against its own previous
    value, so a fix that reads as two harmless ternaries there is a
    refusal at commit time.

    ``None`` means the caller declared no scope, which is the documented
    no-guard behaviour: an empty authored list leaves
    ``guards.enforce_allowed_paths`` inert, and an empty carve-out
    declares nothing (reporting more, never less). Fresh lists, so the
    worker's config cannot write back into a snapshot the parent holds
    for the rest of the run.

    An UNTRUSTWORTHY snapshot gets the same answer, deliberately and by
    name rather than by ``allowed_paths or ()`` quietly reading None as
    "no constraint" (#293 review). This guard is a tripwire that fails
    open; the two layers that fail closed on that state are
    ``_preflight_component_scope``, which refuses the run before this
    function is ever reached, and Phase 1. Asking
    ``is_trustworthy`` keeps the collapse a stated decision rather than
    an accident of the expression.
    """
    if scope is None or not scope.is_trustworthy:
        return [], []
    return list(scope.allowed_paths or ()), list(scope.harness_paths)


def _retry_block(previous_context_json: str | None) -> str:
    """The previous attempt's context for the prompt, or "" for nothing.

    Hoisted out of ``_run_component`` so the prefix ORDER can be one
    literal tuple rather than a list plus a trailing ``if`` (#230). The
    behaviour is exactly what that ``if`` did, and the two "" cases are
    the same two: no context at all, and a context that formats to
    whitespace. "" is dropped by the comprehension that builds the
    order, which is why this can return one instead of signalling.
    """
    if not previous_context_json:
        return ""
    formatted = IterationContext.from_json(previous_context_json).format_for_prompt()
    return formatted if formatted.strip() else ""


def engineer_context_prefix(
    root_dir: Path,
    *,
    golden_patterns_file: Path | str,
    memory_file: Path | str,
    knowledge_prefix: str,
    decisions_prefix: str,
    codebase_scan_prefix: str,
    retry_block: str,
) -> str | None:
    """The block ``run_loop`` puts in front of every engineer prompt, or None.

    The one assembly of the engineer's context, called by
    ``_run_component`` for every factory engineer and by
    ``feature_cmd._feature_context_prefix`` for every ``ks feature`` loop
    (#599). Before #599 this body sat inside ``_run_component``, and
    ``ks feature`` called ``run_loop`` with no prefix at all, so its
    engineer read neither operator file nor any knowledge fact.

    Every argument after ``root_dir`` is keyword-only with no default, so
    each caller states in its own source which blocks it has; "" means
    the caller has none of that block. None when every block is empty,
    which is what ``run_loop`` reads as no prefix.
    """
    # R10.8 and R10.9: the operator's own files, one call each through
    # the one resolver. Resolved by `operator_file_spec` against the REPO
    # ROOT and never against a component worktree: the worktree is the
    # tree the agent has been writing to, so reading it there would let
    # one component choose what the next component is told, unfiltered
    # and under a header saying the operator wrote it (review round 1,
    # S3). The same function resolves the parent's once-per-run notice,
    # so the two cannot read different files (review round 2,
    # should-fix 2). "" when absent, empty, or an unedited `ks init`
    # scaffold.
    golden_patterns = load_operator_file(
        operator_file_spec(GOLDEN_PATTERNS, root_dir, golden_patterns_file)
    )
    memory = load_operator_file(operator_file_spec(MEMORY, root_dir, memory_file))

    # ONE literal tuple, so the ORDER is a value a reader can see and a
    # test can pin rather than a property of statement sequence. Every
    # block reaches the engineer the same way and differs only in where
    # it was built, so adding one is a row here and not a branch; the
    # retry context was an `if` appending to this list until R10.9, and a
    # second `if` would have made "memory is last" true by accident.
    #
    # Repo-standing first (knowledge, then the operator's patterns), then
    # run-level (the architect's decisions), then tree-computed
    # (codebase scan), then attempt-level (the retry context), then MEMORY.
    # Memory is last on purpose: the retry context is the controller's
    # output for this attempt, and memory is the operator's standing
    # correction to how that output should be acted on, so it is read
    # after it (#230). `run_loop` then prepends this whole prefix to
    # CLAUDE.md plus the templated prompt, so the memory block also sits
    # before `# Project Context (from CLAUDE.md)`.
    parts: list[str] = [
        block
        for block in (
            knowledge_prefix,
            golden_patterns,
            decisions_prefix,
            codebase_scan_prefix,
            retry_block,
            memory,
        )
        if block
    ]
    return "\n\n".join(parts) if parts else None


def report_operator_files(base_config: KstrlConfig, root_dir: Path, ui: UI) -> None:
    """Print each ``(subject, message)`` the operator's files produce, once.

    The subject comes off the ROW, not off this function. It used to be
    the literal ``Golden patterns:`` in front of every message, which
    would have printed R10.9's memory file under the golden-patterns
    name the day its row landed - one line below a docstring claiming
    neither caller would have to change (review round 2, should-fix 5).
    That row has now landed and this function did not change, which is
    what the claim was; ``tests/test_operator_file_warnings.py`` runs
    both files misconfigured at once and checks the two lines carry
    different subjects.
    """
    for subject, message in operator_file_notices(
        base_config, KstrlConfig.anchored(root_dir), root_dir
    ):
        ui.warn(f"  {subject}: {message}")


def _engineer_call(root_dir: Path, run_id: str, component_id: str, attempt: int) -> AgentCall:
    """Who the engineer's prompts are recorded for (#532).

    The run directory is ``RunPaths.for_run(root_dir, run_id)``, the same
    directory the factory hands the worker as its accounting directory.
    An empty ``run_id`` is a call outside any run, and it is refused
    rather than left unrecorded (#567).
    """
    if not run_id:
        raise ValueError(
            f"engineer call for {component_id!r} has no run id to record its prompt under"
        )
    return AgentCall(
        run_root=RunPaths.for_run(root_dir, run_id).root,
        run_id=run_id,
        component=component_id,
        role="engineer",
        attempt=attempt,
    )


def _run_component(
    component_id: str,
    prd_path_str: str,
    worktree_path_str: str,
    root_dir_str: str,
    prompt_file_str: str,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str | None,
    sleep_seconds: float,
    previous_context_json: str | None = None,
    codebase_scan_config_dict: dict[str, Any] | None = None,
    setup: WorktreeSetup | None = None,
    knowledge_prefix: str = "",
    decisions_prefix: str = "",
    progress_file_str: str | None = None,
    codebase_map_file_str: str = "scripts/kstrl/codebase_map.md",
    golden_patterns_file_str: str = "scripts/kstrl/golden-patterns.md",
    memory_file_str: str = "scripts/kstrl/memory.md",
    agent_iteration_timeout: float = 0.0,
    component_timeout: float = 0.0,
    max_iterations: int = 10,
    interactive: bool = False,
    scope: ComponentScope | None = None,
    breaker_iterations: int = 3,
    sandbox_enabled: bool = False,
    sandbox_allow_network: bool = False,
    sandbox_writable: tuple[str, ...] = (),
    agent_budget_usd: float | None = None,
    events_dir_str: str | None = None,
    usage_dir_str: str | None = None,
    run_id: str = "",
    token_budget: LoopBudget | None = None,
    redirect_output: bool = True,
    live_line: Callable[[str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    base_branch: str = "main",
    verify_config: VerifyConfig | None = None,
    attempt: int = 1,
    plan_id: str = "",
) -> ComponentResult:
    """Run a single component's implementation loop.

    Top-level function (picklable for ProcessPoolExecutor).
    Creates all objects internally - no shared state.

    Chunk 6 (TUI rewrite): with ``events_dir_str`` set, the worker
    writes typed events to <events_dir>/components/<id>/engineer.jsonl,
    the raw agent transcript to engineer.log, and (pool mode) dup2's
    its inherited stdout/stderr onto that same log so parallel workers
    stop interleaving raw lines on the parent terminal. ``live_line``
    (inline mode only - never pickled) mirrors each transcript line to
    the parent's UI so sequential runs keep live engineer output.
    Without ``events_dir_str`` the legacy PlainUI-on-stderr behavior is
    preserved for direct callers.

    R8: ``usage_dir_str`` is where the per-iteration usage snapshot is
    published so a killed worker's spend is recoverable. It is a
    SEPARATE argument from ``events_dir_str`` on purpose - the factory
    always sets it, including when progress logging is off, because
    accounting must not be switchable off by an observability setting
    (review finding P2-d). Direct callers that leave it None simply do
    not publish snapshots.

    R8: ``token_budget`` carries the run-level ``max_total_tokens`` plus
    the spend recorded before this worker launched, so the engineer loop
    can halt itself BETWEEN iterations instead of waiting for the
    parent's next phase boundary. None disables the in-loop check.

    #261: ``verify_config`` is forwarded to the loop so the engineer is
    told exactly what Phase 1 will run. It must be the SAME object
    ``_phase_verify`` reads, which is why both come from
    ``engineer_verify_config``: a CLI override or an uncommitted
    kstrl.toml edit lives only in the parent, and the parent's fallback
    is ``VerifyConfig()`` rather than a reload from disk. None means no
    gate runs, and the loop then states no commands at all.

    ``progress_file_str`` is None unless the operator explicitly
    configured a progress path; None means "derive it next to this
    component's PRD", which is what keeps the file inside the
    component's allowedPaths (see config.component_progress_path).

    #269: ``scope`` is the component's plan-time scope snapshot,
    resolved once by ``scope.RunScope`` before any engineer ran and
    handed down WHOLE rather than as two lists. This function derives
    nothing and reads no PRD to obtain it, which is what makes the
    in-loop guard's scope a value the agent never had access to, in
    worktree mode and under ``use_worktrees=False`` alike. One argument
    rather than two because the two halves are one decision: split into
    separate positional slots they would be kept in step by index
    alignment across two files, and the provenance recorded with them
    would stop at the pool boundary. ``None`` is the documented
    no-scope behaviour - the in-loop guard is inert without an authored
    list, and declares no carve-out.
    """
    from kstrl.agents import (
        get_agent,
    )
    from kstrl.events import EventBridgeUI, NullPrompter
    from kstrl.loop import run_loop
    from kstrl.ui import PlainUI

    start = time.monotonic()
    worktree_path = Path(worktree_path_str)
    # R0.4: every source below (the PRD seed, prompt.md, CLAUDE.md)
    # resolves against root_dir, never the worker's inherited CWD. A
    # CWD-relative lookup (e.g. --root from another directory) missed
    # them and the engineer fell back to the harness DEFAULT_PROMPT
    # (phase-f e2e validation, line 38).
    root_dir = Path(root_dir_str)

    ui: UI
    worker_bus: EventBus | None = None
    transcript_fh: TextIO | None = None
    stop_heartbeat: Callable[[], None] | None = None
    run_paths: RunPaths | None = None
    if events_dir_str is None:
        ui = PlainUI(no_color=True)
    else:
        run_paths = RunPaths(root=Path(events_dir_str))
        comp_dir = run_paths.component_dir(component_id)
        try:
            comp_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if redirect_output:
            # dup2 BEFORE any threads start (chunk 6 invariant); stray
            # library writes land in the transcript, never the terminal.
            _redirect_worker_output(run_paths.engineer_log(component_id))

    agent = get_agent(
        agent_cmd,
        model,
        reasoning,
        agent_type,
        sandbox=SandboxConfig(
            enabled=sandbox_enabled,
            allow_network=sandbox_allow_network,
            writable=sandbox_writable,
        ),
        max_budget_usd=agent_budget_usd,
        root_dir=root_dir,
    )

    # Copy PRD into worktree if needed.
    #
    # shutil.copyfile, not read_text/write_text: this is a COPY, and a
    # copy that decodes and re-encodes is only byte-exact when the
    # locale's codec round-trips. #291 made the PRD utf-8 on disk, which
    # under LC_ALL=C a bare read_text cannot decode at all. A byte copy
    # removes the encoding question rather than answering it.
    # The source is the copy the run starts from, which for a planned
    # component is under .kstrl/plan/<plan_id>/ and never at prd_path
    # (#545, #568).
    worktree_prd = worktree_path / prd_path_str
    prd_source = pre_run_prd_path(root_dir, component_id, prd_path_str, plan_id=plan_id)
    if not worktree_prd.exists() and prd_source.exists():
        worktree_prd.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prd_source, worktree_prd)

    # The prompt template's $prd_path placeholder (shipped in
    # DEFAULT_PROMPT >= 1.1.0 and the scaffolded prompt.md) is substituted
    # at runtime by loop.py with config.prd_file, so the agent reads the
    # SAME per-component PRD that check_prd_stories re-reads (R2.3, H-11)
    # without overwriting scripts/kstrl/prd.json.
    #
    # #569: prompt.md and CLAUDE.md are read from root_dir, the checkout
    # kstrl ran from, and nothing of kstrl's is copied into the worktree
    # beside the PRD. A copy there is an untracked file the engineer's
    # `git add -A` commits, and when the same file is uncommitted in the
    # root checkout, `git merge` of the component branch refuses on it and
    # Phase 1 fails the component on diff_scope. They are kstrl's context
    # for the engineer, not the component's change.
    prompt_file = root_dir / prompt_file_str
    # #585: the codebase map is read from root_dir for the same reason.
    # The engineer only reads it (DEFAULT_PROMPT >= 1.4.0), and in the
    # worktree it is missing whenever the base branch does not track it.
    codebase_map_file = root_dir / codebase_map_file_str

    # The worktree setup and the Phase 0 scan. Neither stops the engineer;
    # a failure comes back as a note that is warned once `ui` is bound
    # (#486). The setup runs again before Phase 1, where it gates (#624).
    codebase_scan_prefix, setup_notes = _prepare_component_tree(
        worktree_path,
        component_id,
        setup,
        codebase_scan_config_dict,
    )

    context_prefix = engineer_context_prefix(
        root_dir,
        golden_patterns_file=golden_patterns_file_str,
        memory_file=memory_file_str,
        knowledge_prefix=knowledge_prefix,
        decisions_prefix=decisions_prefix,
        codebase_scan_prefix=codebase_scan_prefix,
        retry_block=_retry_block(previous_context_json),
    )

    # R2.3 (CRIT-8): max_iterations, interactive, and allowed_paths come
    # from the invoking config via _submit_args. They were previously
    # hardcoded here (30 / False / unset), which made `ks run N`, -i,
    # and --allowed-paths silent no-ops under the factory pipeline.
    component_progress_rel = component_progress_path(
        prd_path_str,
        progress_file_str,
    )
    # #264/#269: the plan-time snapshot handed down by _submit_args.
    # The worker derives NOTHING from it. It used to rebuild the
    # carve-out here from its own three path strings, which agreed with
    # Phase 1 only because both derivations happened to be fed the same
    # inputs; the snapshot makes that agreement structural, and it is
    # the same value Phase 1 reads (pipeline._phase_verify ->
    # check_diff_scope). This guard fires FIRST, so a scope only one of
    # them had would still kill the component before Phase 1 was
    # reached.
    authored_paths, harness_paths = _worker_scope(scope)
    config = KstrlConfig(
        max_iterations=max_iterations,
        prompt_file=prompt_file,
        prd_file=worktree_prd,
        progress_file=worktree_path / component_progress_rel,
        codebase_map_file=codebase_map_file,
        sleep_seconds=sleep_seconds,
        interactive=interactive,
        allowed_paths=authored_paths,
        kstrl_branch="",
        kstrl_branch_explicit=True,
        agent_cmd=agent_cmd,
        model=model,
        model_reasoning_effort=reasoning,
        agent_type=agent_type,
        ui_mode="plain",
        no_color=True,
    )

    timeouts = TimeoutConfig(
        agent_iteration=agent_iteration_timeout,
        component_total=component_timeout,
    )
    breaker_config = BreakerConfig(no_progress_iterations=breaker_iterations)

    # Start event-owned resources only after setup succeeds. A get_agent,
    # file-copy, or config failure therefore cannot leak a heartbeat thread
    # or open JSONL/transcript handles in a reusable pool worker.
    if run_paths is not None:
        try:
            transcript_fh = open(
                run_paths.engineer_log(component_id),
                "a",
                buffering=1,
                encoding="utf-8",
            )
        except OSError:
            transcript_fh = None

        def _transcript(line: str) -> None:
            if transcript_fh is not None:
                transcript_fh.write(line + "\n")
            if live_line is not None:
                live_line(line)

        worker_bus = EventBus(
            JsonlSink(run_paths.engineer_events(component_id)),
            run_id=run_id,
            source="worker",
            component=component_id,
        )
        ui = EventBridgeUI(
            worker_bus,
            prompter=NullPrompter(),
            transcript=_transcript,
        )
        stop_heartbeat = _start_heartbeat(worker_bus)

    # R8: durable per-iteration usage snapshot. The parent reads it only
    # when a worker was killed before it could return a ComponentResult
    # (PR B abort path), so it can never double count with result.usage.
    # Keyed off usage_dir_str, NOT events_dir_str: the accounting file
    # is written even when progress logging is disabled (P2-d).
    on_iteration_usage: Callable[[UsageTotals], None] | None = None
    if usage_dir_str is not None:
        usage_path = RunPaths(root=Path(usage_dir_str)).engineer_usage(
            component_id,
        )
        on_iteration_usage = functools.partial(
            write_partial_usage,
            usage_path,
        )

    # #435: the guard's baseline and the message it writes name the SAME
    # ref the diff is judged on. A squash merge advances origin/<base>
    # and leaves local <base> where it was, so the bare name measures
    # against a tree the previous tier never reached.
    guard_base_ref = resolve_base_ref(base_branch, root_dir)

    try:
        for note in setup_notes:
            ui.warn(note)
        with recording_prompts(_engineer_call(root_dir, run_id, component_id, attempt)):
            result = run_loop(
                config,
                ui,
                agent,
                worktree_path,
                context_prefix=context_prefix,
                timeouts=timeouts,
                breaker_config=breaker_config,
                bus=worker_bus,
                stop_check=stop_check,
                budget=token_budget,
                on_iteration_usage=on_iteration_usage,
                guard_base_ref=guard_base_ref,
                guard_ignored_paths=harness_paths,
                # #274: the project root, NOT worktree_path. The two are the
                # same directory only under use_worktrees=False, which is
                # exactly when `.kstrl/` reaches the guard's walk; in a real
                # worktree they differ and the loop carves nothing out, so a
                # `.kstrl/` the AGENT wrote there stays a violation.
                guard_state_root=root_dir,
                context_root=root_dir,
                verify_config=verify_config,
            )
        # Report which limit fired so the retry/fail path can act on it
        # (timeout errors trigger the recreate-from-base retry hygiene).
        if result.completed:
            error = None
        elif result.budget_halt_reason:
            # First in the chain: the budget halt is the reason the loop
            # stopped short, and its message carries the numbers the
            # pipeline needs when its own totals do not yet show a
            # breach (unreported-usage case).
            error = result.budget_halt_reason
        elif result.guard_violations:
            # R8 review: name the files. The guard used to fail the
            # iteration and fall through to "Did not complete", so the
            # retry agent was told only that something went wrong - and
            # the cheapest strategy for an agent with no diagnosis is to
            # redo the same edit. Mirrors check_diff_scope's wording so
            # the two scope failures read identically wherever they are
            # caught.
            #
            # R0.4 is why the base branch and the COMPLETE allowed-paths
            # list are repeated verbatim rather than paraphrased: the
            # recorded e2e run guessed `main` as the base and reverted
            # base-branch content with `git checkout main -- ...`,
            # failing again. Those two lines used to arrive from Phase 1;
            # now that the in-loop guard fires FIRST, this message is
            # what reaches attempt 2, so it must carry them itself.
            shown = list(result.guard_violations[:15])
            more = len(result.guard_violations) - len(shown)
            listed = ", ".join(shown) + (f" ... and {more} more" if more else "")
            # #264: the two sets stay separate. The harness artifacts are
            # already in scope, so naming them here stops the retry agent
            # reading its own PRD or progress log as the thing it must
            # stop writing - which is the one edit it cannot make and
            # still pass prd_stories.
            error = IN_LOOP_SCOPE_VIOLATION_PROMPT.format(
                count=len(result.guard_violations),
                iterations=result.iterations,
                listed=listed,
                base_branch=guard_base_ref,
                allowed_paths=", ".join(authored_paths),
                harness_paths=", ".join(harness_paths),
            )
        elif result.no_progress:
            error = (
                "no-progress circuit breaker tripped: "
                f"{breaker_iterations} consecutive iteration(s) produced an "
                "unchanged diff hash and test signature"
            )
        elif result.timeout_limit == "component":
            error = (
                f"component timeout: exceeded {component_timeout}s wall clock "
                f"after {result.iterations} iteration(s)"
            )
        elif result.timed_out_iterations:
            error = (
                f"Did not complete ({result.timed_out_iterations} iteration(s) "
                f"hit the {agent_iteration_timeout}s agent iteration timeout)"
            )
        else:
            error = "Did not complete"
        return ComponentResult(
            component_id=component_id,
            success=result.completed,
            iterations=result.iterations,
            error=error,
            duration_seconds=time.monotonic() - start,
            context_json=previous_context_json,
            usage=result.usage,
            no_progress=result.no_progress,
            budget_exceeded=bool(result.budget_halt_reason),
            budget_halt_condition=result.budget_halt_condition,
            budget_halt_ceilings=result.budget_halt_ceilings,
            guard_violations=result.guard_violations,
            worktree_sweep=_attempt_sweep(worktree_path, root_dir),
        )
    except Exception as exc:
        return ComponentResult(
            component_id=component_id,
            success=False,
            iterations=0,
            error=str(exc),
            duration_seconds=time.monotonic() - start,
            context_json=previous_context_json,
            # The loop crashed, but any iterations that did run still
            # cost tokens; collect what the agent recorded (R3.1).
            usage=collect_usage(agent),
            worktree_sweep=_attempt_sweep(worktree_path, root_dir),
        )
    finally:
        if stop_heartbeat is not None:
            stop_heartbeat()
        if worker_bus is not None:
            worker_bus.close()
        if transcript_fh is not None:
            try:
                transcript_fh.close()
            except OSError:
                pass


def _prepare_component_tree(
    worktree_path: Path,
    component_id: str,
    setup: WorktreeSetup | None,
    codebase_scan_config_dict: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    """Run the worktree setup and build the Phase 0 scan context.

    Returns ``(codebase_scan_prefix, notes)``. Neither failure stops the
    engineer, and neither is passed over: each one is a note the caller
    warns on the worker's UI. The engineer runs without its Phase 0
    context when the scan note is present (#486).

    A failed setup does not stop the ENGINEER because the engineer may be
    the one who can repair it (a lockfile its previous attempt broke).
    It cannot pass a gate: the setup runs again before Phase 1, and there
    a failure is an infrastructure failure and no gate runs (#624).
    """
    notes: list[str] = []
    if setup is not None:
        setup_error = setup.prepare(worktree_path)
        if setup_error:
            notes.append(f"  Worktree setup failed for {component_id}: {setup_error}")
    codebase_scan_prefix = ""
    if codebase_scan_config_dict:
        try:
            codebase_scan_prefix = build_codebase_scan_context(
                worktree_path,
                CodebaseScanConfig(**codebase_scan_config_dict),
            )
        except Exception as exc:  # noqa: BLE001 - non-fatal, never silent
            notes.append(f"  Codebase scan failed for {component_id}: {type(exc).__name__}: {exc}")
    return codebase_scan_prefix, notes


def _attempt_sweep(worktree_path: Path, root_dir: Path) -> WorktreeSweep:
    """Sweep the attempt's worktree, or nothing when there is no worktree.

    Under ``use_worktrees=False`` the engineer ran in the project root,
    where the operator's own shells and editors also live, so nothing is
    swept there.
    """
    if worktree_path.resolve() == root_dir.resolve():
        return WorktreeSweep()
    return sweep_worktree(worktree_path)


def _install_worker_signal_forwarding() -> None:
    """A pool worker's signal handlers, installed as the pool's initializer.

    SIGTERM kills the agent's process group, then exits 130: that is how
    ``_abort_inflight`` ends a worker. SIGINT does nothing (#461). Ctrl-C
    sends SIGINT to every process in the terminal's foreground group, and
    pool workers are in it. A worker that raised KeyboardInterrupt handed
    it to the parent through ``future.result()``, which re-raised it past
    the scheduler: the run exited 1 in 0.07 s with no abort recorded, no
    worktree cleaned up and every component left RUNNING. The parent owns
    the stop.

    The worker also ends when its parent ends (#642). A worker whose
    parent alone was SIGKILLed kept running and kept starting agents. A
    daemon thread waits on the parent's sentinel and then does what
    SIGTERM does.

    An initializer rather than a call inside ``_run_component``, which
    installed it only when progress logging was on. Installed only on a
    worker's main thread."""
    if threading.current_thread() is not threading.main_thread():
        return

    def _on_term(signum: int, frame: object) -> None:
        del signum, frame
        _end_worker()

    try:
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _ignore_interrupt)
    except (ValueError, OSError):
        pass
    parent = multiprocessing.parent_process()
    if parent is not None:
        threading.Thread(
            target=_follow_parent, args=(parent,), daemon=True, name="kstrl-follow-parent"
        ).start()


def _end_worker() -> None:
    """Kill this worker's agent groups, then exit 130: SIGTERM's path."""
    try:
        kill_active_process_groups()
    finally:
        os._exit(130)


def _follow_parent(parent: multiprocessing.process.BaseProcess) -> None:
    """End the worker once its parent has gone (#642).

    ``join`` returns when the parent's sentinel closes, which the kernel
    does however the parent ended. Anything the wait raises is read as
    the parent being gone: a worker that cannot tell must not keep
    starting agents.
    """
    try:
        parent.join()
    finally:
        _end_worker()


def _ignore_interrupt(signum: int, frame: object) -> None:
    """A pool worker's SIGINT handler: do nothing (#461).

    A function and not ``signal.SIG_IGN``: an ignored disposition survives
    ``exec``, so the agent and every command its tools run would start
    with SIGINT ignored. A handler is reset to the default by ``exec``.
    """
    del signum, frame


def _redirect_worker_output(log_path: Path) -> None:
    """Point the worker's fds 1/2 (and sys.stdout/stderr) at its
    transcript so nothing reaches the parent terminal (chunk 6). Best
    effort: a worker that cannot redirect keeps inherited fds rather
    than dying."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        os.dup2(f.fileno(), 1)
        os.dup2(f.fileno(), 2)
        sys.stdout = f
        sys.stderr = f
    except OSError:
        pass
