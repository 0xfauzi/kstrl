"""Factory orchestrator - parallel component execution with 3-phase verification."""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.agents.base import UsageTotals
from kstrl.agents.proc import kill_active_process_groups
from kstrl.breaker import BreakerConfig
from kstrl.config import (
    KstrlConfig,
    relative_to_root,
)
from kstrl.config_numbers import check_number
from kstrl.contract import (
    ContractCleanupError,
    ContractMode,
    run_contract_testing,
)
from kstrl.decisions import (
    build_decisions_context,
)
from kstrl.event_catalog import (
    AdversarialAgentSelected,
    AutonomyLevelApplied,
    ComponentStarted,
    PhaseStarted,
    RunPlan,
    RunStarted,
)
from kstrl.event_catalog import ContractResult as ContractResultEvent
from kstrl.events import EventBus, EventSink, JsonlSink, RunPaths, V1CompatSink
from kstrl.fact_scope import authored_paths, paths_by_component
from kstrl.factory_acceptance import (
    _carry,
    _plan_gated,
    _plan_of_kept_checks,
    _record_contract_event,
    _record_contract_failures,
    _reset_contract_breakers,
    _resolve_round_base,
    _stamp_feature_base,
)
from kstrl.factory_agents import (
    AdversarialAgentSelection,
    _adversarial_phase_gates,
    _engineer_sandbox,
    _warn_unsandboxable_reviewers,
    resolve_adversarial_selection,
)
from kstrl.factory_autonomy import _resolve_ladder
from kstrl.factory_config import (
    FactoryConfig,
    claim_gate_unreachable_warning,
    merge_gate_unreachable_warning,
)
from kstrl.factory_lock import FactoryLockHeldError, _acquire_run_lock, _RunLock
from kstrl.factory_preflight import (
    _emit_stack_confirmation,
    _record_run_scope,
    _report_preflight,
    _run_preflights,
)
from kstrl.factory_result import (
    ComponentResult,
    FactoryResult,
    _clear_partial_usage,
    _salvage_aborted_usage,
)
from kstrl.factory_run import RunContext
from kstrl.factory_summary import _finish_run
from kstrl.factory_worker import (
    _install_worker_signal_forwarding,
    _report_operator_files,
    _run_component,
)
from kstrl.factory_worktrees import (
    _cleanup_worktree,
    _interrupted_run_branches,
    _recut_point,
    _setup_worktree,
    _start_from_dependencies,
)
from kstrl.integration_loop import IntegrationLoop
from kstrl.integration_phase import IntegrationRun, Phase3Round
from kstrl.interaction import InteractionChannel
from kstrl.knowledge import (
    current_run_id,
    measure_fact_utilization,
    retrieve_knowledge_context,
)
from kstrl.knowledge_distill import distill_facts
from kstrl.knowledge_facts import KnowledgeConfig
from kstrl.linear import LinearConfig, build_linear_sink
from kstrl.loop_budget import LoopBudget
from kstrl.manifest import (
    Component,
    ComponentStatus,
    Manifest,
)
from kstrl.observability import (
    NotifyConfig,
    NotifyHooks,
    NullProgressLog,
    ProgressLog,
)
from kstrl.pipeline import ComponentPipeline
from kstrl.pipeline_state import PipelineHooks, _iso_now
from kstrl.review import (
    run_review,
)
from kstrl.runenvelope import RunEnvelope
from kstrl.rung import release
from kstrl.runstate import RunState
from kstrl.sandbox import warn_unconfined, with_stack_writable
from kstrl.scope import RunScope
from kstrl.security import (
    run_security_review,
)
from kstrl.shutdown import StopController
from kstrl.timeout import NO_LIMIT, TimeoutConfig, describe_limit_seconds
from kstrl.ui.bridge import EventBridgeUI
from kstrl.verify import run_mechanical_verification
from kstrl.verify_diff import SCOPE_UNREADABLE_CHECK, scope_unreadable_error
from kstrl.version import kstrl_version
from kstrl.worktree_sweep import (
    sweep_worktree,
)

if TYPE_CHECKING:
    from kstrl.ui.base import UI


class _InlineExecutor:
    """Synchronous stand-in for ProcessPoolExecutor when max_parallel
    is 1 (R7.3): the worker runs in-process (no pickling, no process
    spawn - the historical sequential path), wrapped in an already-
    resolved Future so ONE scheduling loop serves both modes. A worker
    exception lands on the future and surfaces at ``future.result()``,
    exactly where a pool worker's would.
    """

    def submit(
        self,
        fn: Callable[..., ComponentResult],
        /,
        *args: Any,
    ) -> Future[ComponentResult]:
        future: Future[ComponentResult] = Future()
        try:
            result = fn(*args)
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        return future

    def shutdown(
        self,
        wait: bool = True,
        cancel_futures: bool = False,
    ) -> None:
        """Nothing to shut down: every submit already ran to completion."""


def _wait_interruptible(
    futures: set[Future[ComponentResult]],
    timeout: float | None,
    stop: StopController | None,
    slice_seconds: float = 0.5,
) -> tuple[set[Future[ComponentResult]], bool]:
    """concurrent.futures.wait in stop-checkable slices (PR B).

    Returns (done, stopped). Worst-case stop latency is one slice;
    the backstop deadline math is preserved by honoring ``timeout``.
    """
    if stop is None:
        done, _ = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
        return done, False
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        if stop.is_set():
            return set(), True
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        slice_t = slice_seconds if remaining is None else min(slice_seconds, remaining)
        done, _ = wait(futures, timeout=slice_t, return_when=FIRST_COMPLETED)
        if done:
            return done, False
        if remaining is not None and remaining <= slice_seconds:
            return set(), False


def _resolve_max_parallel(factory_config: FactoryConfig, ui: UI) -> int:
    """Effective parallelism, and a warning for every setting it discards.

    Extracted from ``_run_factory_locked`` (#292) so the decision has a
    name and can be tested. It is also the decision that hid a fake test
    for months: the spine SIGTERM test asked for ``max_parallel=2``,
    silently got 1 and therefore ``_InlineExecutor``, and nothing said so.

    Both discards are REPORTED, and only when they actually discard
    something. The worktree branch used to fire unconditionally as an
    info line that never named ``max_parallel``, so an operator who
    configured 8 was never told the 8 was thrown away.
    ``ui.kv("Max parallel", ...)`` prints the effective value, which
    cannot distinguish "I asked for 1" from "I asked for 8 and lost it".
    """
    max_parallel = factory_config.max_parallel
    if not factory_config.use_worktrees:
        if max_parallel > 1:
            ui.warn(
                f"worktrees disabled: components cannot run in parallel; "
                f"forcing max_parallel=1 (you configured {max_parallel})"
            )
        return 1
    if factory_config.single_pr and max_parallel > 1:
        # R0.5 (H-8): single_pr components all live on ONE branch, and a
        # branch can only be checked out in one worktree at a time -
        # parallel same-tier components would hard-fail on "already
        # checked out". Sequential is the only layout that works.
        ui.warn(
            f"single_pr mode: components share one branch; forcing "
            f"max_parallel=1 (you configured {max_parallel})"
        )
        return 1
    return max_parallel


def _refused_before_launch(
    pipeline: ComponentPipeline,
    comp: Component,
    run_scope: RunScope,
) -> bool:
    """Every reason to fail a READY component instead of launching it.

    Returns True when the component was transitioned without a launch,
    which the caller counts so the scheduling loop re-derives its ready
    set rather than stopping while schedulable siblings remain.

    One function because the three refusals share a shape and a
    contract - fail loudly, spend nothing, do not halt the run - and
    because the scheduling loop is already far past the repo's
    complexity gate, so a fourth inline branch is a refusal at commit
    time. None of them halts: by this point siblings may have merged,
    and discarding their work to punish one component is the worse
    trade.
    """
    # R3.1 scheduling gate: a blown ceiling (tokens or cost) fails
    # pending components loudly instead of launching an engineer loop
    # that would only add spend.
    if pipeline.budget_exceeded():
        pipeline.fail_for_budget(comp, "scheduling")
        return True
    # R8 review: a loop that emits COMPLETE returns before its own
    # budget check, so an adapter that finishes on its first silent call
    # never halts itself - the ordinary success path for a custom
    # agent_cmd. This gate is what stops the run handing out NEW work
    # under a ceiling that can no longer fire. Halts only when EVERY
    # configured ceiling is dead: a cost-reporting adapter still
    # enforces max_cost_usd even though its token cap is beyond saving.
    #
    # Identity is derived inside fail_for_budget: nothing was BREACHED
    # here, so the rule falls through to the dead ceilings and names
    # only those actually configured.
    unenforceable = pipeline.budget_unenforceable()
    if unenforceable is not None:
        pipeline.fail_for_budget(comp, "scheduling", reason=unenforceable)
        return True
    # #294: the LAST point before spend. The plan-time preflight refuses
    # an unresolved scope before the run starts, but it only inspects
    # components that are PENDING then, and the contract breaker resets
    # a COMPLETED component to PENDING inside the scheduling loop, long
    # after _run_preflights returned. Without this gate such a component
    # runs a full engineer loop - up to max_iterations LLM calls - and
    # only then does Phase 1 refuse it. Measured against the same
    # dogfooding run _preflight_component_scope cites (14.49 dollars /
    # 41 minutes for three attempts), Phase 1's FAIL alone still left
    # roughly 4.83 dollars and 14 minutes on the table per reset.
    #
    # This also closes --no-verify, which returns from _phase_verify
    # before the ungated check can run: a component with no trustworthy
    # scope now never launches, so it cannot merge with the guard inert.
    comp_scope = run_scope.for_component(comp.id)
    if not comp_scope.is_trustworthy:
        pipeline.fail(
            comp,
            scope_unreadable_error(f"Error: {comp_scope.error}"),
            phase="scope",
            check=SCOPE_UNREADABLE_CHECK,
            signatures=[f"{SCOPE_UNREADABLE_CHECK}:no-trustworthy-scope"],
        )
        return True
    return False


def _abort_inflight(
    executor: ProcessPoolExecutor | _InlineExecutor,
    running_futures: dict[Future[ComponentResult], str],
    pipeline: ComponentPipeline,
    ui: UI,
    stop: StopController,
    term_grace: float = 5.0,
) -> None:
    """Group-terminate in-flight workers and record their components as
    aborted (PR B). Pool mode SIGTERMs each worker pid - the worker's
    forwarding handler group-kills its agent subprocess and exits 130;
    stragglers get SIGKILL after the grace period. A second stop request
    skips the rest of that grace period. `_processes` is private executor
    API, so process objects are inspected defensively and only workers
    still reported alive are killed.
    """
    procs = getattr(executor, "_processes", None)
    workers: list[Any] = list(procs.values()) if procs else []
    if workers:
        alive = []
        for worker in workers:
            try:
                pid = worker.pid
                if isinstance(pid, int) and pid > 1 and pid != os.getpid() and worker.is_alive():
                    worker.terminate()
                    alive.append(worker)
            except (AssertionError, AttributeError, OSError, ValueError):
                pass
        deadline = time.monotonic() + term_grace
        while alive and not stop.force and time.monotonic() < deadline:
            still_alive = []
            for worker in alive:
                try:
                    if worker.is_alive():
                        still_alive.append(worker)
                except (AssertionError, AttributeError, OSError, ValueError):
                    pass
            alive = still_alive
            if alive and not stop.force:
                time.sleep(0.1)
        for worker in alive:
            try:
                if worker.is_alive():
                    worker.kill()
            except (AssertionError, AttributeError, OSError, ValueError):
                pass
    elif isinstance(executor, _InlineExecutor):
        # Inline executor: the agents live in THIS process, so group-kill
        # them directly.
        killed = kill_active_process_groups()
        if killed:
            ui.warn(f"  Terminated {killed} in-flight agent process group(s)")
    for future, comp_id in list(running_futures.items()):
        _salvage_aborted_usage(future, comp_id, pipeline)
        pipeline.fail_aborted(comp_id, stop.reason)
        running_futures.pop(future, None)
    ui.warn(f"  Aborted in-flight work: {stop.reason}")
    executor.shutdown(wait=False, cancel_futures=True)


def _next_backstop_wait(
    running: Mapping[Future[ComponentResult], str],
    deadlines: Mapping[Future[ComponentResult], float],
    now: float,
) -> float | None:
    """Seconds until the nearest scheduler-backstop deadline among running
    futures. None means no deadline is armed (wait indefinitely for the
    next completion, the pre-R0.1 behavior)."""
    pending = [deadlines[f] for f in running if f in deadlines]
    if not pending:
        return None
    return max(0.0, min(pending) - now)


def _expired_futures(
    running: Mapping[Future[ComponentResult], str],
    deadlines: Mapping[Future[ComponentResult], float],
    now: float,
) -> list[Future[ComponentResult]]:
    """Running futures whose scheduler-backstop deadline has passed."""
    return [f for f in running if not f.done() and f in deadlines and now >= deadlines[f]]


def run_factory(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None = None,
    *,
    interaction: InteractionChannel | None = None,
    stop: StopController | None = None,
    run_id: str | None = None,
    notify_capture_output: bool = False,
    architect_usage: UsageTotals | None = None,
    architect_run_id: str = "",
    run_lock: _RunLock | None = None,
) -> FactoryResult:
    """Run the factory orchestrator with 3-phase verification.

    Phase 1: Mechanical verification (tests, typecheck, lint, PRD, diff scope)
    Phase 2: Second-opinion review (separate agent reviews diff against spec)
    Phase 3: Contract testing (merge tier branches, run integration tests)

    ``manifest_path`` is where run state is SAVED as well as where it was
    loaded from (R0.5, H-15): ``--manifest /custom.json`` must persist to
    /custom.json and ``ks run`` to its own run-manifest.json, never to
    another invocation's resumable ``scripts/kstrl/manifest.json``. None
    keeps the historical default of ``<root>/scripts/kstrl/manifest.json``.

    Holds the run-level ``.kstrl/factory.lock`` flock for the whole run
    (R0.5, H-7); a contending invocation is refused with exit code 2
    unless it passes ``--force-lock``.

    ``architect_usage`` is what the caller already spent ON THIS RUN's
    behalf, before the run existed: `ks factory` decomposes the spec
    itself and only then calls this (#257). Passing it is what makes
    ``max_cost_usd`` bound the architect - see
    :meth:`ComponentPipeline.record_architect_usage`. Named for the role
    rather than generically, because the body records it as the
    architect unconditionally: a second pre-run role would have to grow
    the signature rather than quietly borrow this one's row. None for
    every caller that resumes from a manifest, which ran no architect.

    ``architect_run_id`` names the run that holds that architect's records
    (#567), and the run's ``factory_started`` event carries it (#587): an
    operator follows it to the prompts and transcript, and `ks serve`
    reads it to leave that run uncharged, since this run carries its spend.

    ``run_lock`` is the run lock a caller already took on this root, so it
    could change state under it before the run starts (`ks retry`, and
    `ks inbox approve` on a parked merge, #597). It is used instead of a
    second acquire, which flock refuses even inside one process, and it
    is released here at the end like one taken here.
    """
    # The ceilings are validated at every CONFIG path, but a FactoryConfig
    # can also be constructed programmatically (tests, embedders, the SDK
    # path), which bypasses those. Re-check at the boundary: a safety
    # limit that only holds when you came in through the front door is
    # not a safety limit.
    check_number(factory_config.max_cost_usd, "max_cost_usd")
    check_number(factory_config.max_total_tokens, "max_total_tokens")

    if run_lock is None:
        try:
            run_lock = _acquire_run_lock(
                root_dir,
                ui,
                force=factory_config.force_lock,
            )
        except FactoryLockHeldError as exc:
            ui.err(str(exc))
            refused = FactoryResult()
            refused.exit_code = 2
            return refused
    try:
        return _run_factory_locked(
            manifest,
            factory_config,
            base_config,
            ui,
            root_dir,
            manifest_path=manifest_path,
            lock_held=run_lock.held,
            interaction=interaction,
            stop=stop,
            run_id_override=run_id,
            notify_capture_output=notify_capture_output,
            architect_usage=architect_usage,
            architect_run_id=architect_run_id,
        )
    finally:
        release((factory_config.setup_rung, factory_config.test_rung))
        factory_config.setup_rung = factory_config.test_rung = None
        run_lock.release()


def _execution_limit_rows(
    timeout_cfg: TimeoutConfig, factory_config: FactoryConfig
) -> list[tuple[str, str]]:
    """Every time and spend limit the Execution header states, set or not.

    An unset limit is printed as "no limit" rather than left out (#467):
    a missing line reads the same as a header written before the limit
    existed.

    R8 (measured): each ceiling states what it counts, before the run
    spends anything. An operator set --max-cost-usd 25.0 on a run whose
    cross-family reviewer reports tokens and no cost, and got a ceiling
    that bounded the engineer alone; nothing had told them the ceiling is
    denominated in REPORTED dollars. Nothing here names which roles will
    be covered: no call has been made yet, so that would be a prediction.
    The measured per-role figure follows as a `budget_coverage` event at
    the first call that reports nothing.
    """
    tokens = factory_config.max_total_tokens
    cost = factory_config.max_cost_usd
    calls = factory_config.max_adversarial_calls
    return [
        ("Agent timeout", describe_limit_seconds(timeout_cfg.agent_iteration)),
        ("Component timeout", describe_limit_seconds(timeout_cfg.component_total)),
        (
            "Token ceiling",
            f"{tokens} total tokens (counts only calls whose agent reports a token count)"
            if tokens > 0
            else NO_LIMIT,
        ),
        (
            "Cost ceiling",
            f"${cost} (counts only calls whose agent reports a cost; roles whose "
            "agent reports none are unpriced and unbounded by it)"
            if cost > 0
            else NO_LIMIT,
        ),
        ("Adversarial calls", str(calls) if calls > 0 else NO_LIMIT),
    ]


def _run_plan_event(manifest: Manifest, factory_config: FactoryConfig) -> RunPlan:
    """The component DAG plus budget caps (chunk 4). Emitted at run start and
    again when the integration loop appends a fix component (#483), because
    the dashboard replaces its plan only on RunPlan."""
    return RunPlan(
        components=tuple(
            {"id": c.id, "title": c.title, "deps": list(c.dependencies)}
            for c in manifest.components
        ),
        max_total_tokens=factory_config.max_total_tokens,
        max_adversarial_calls=factory_config.max_adversarial_calls,
        max_cost_usd=factory_config.max_cost_usd,
    )


def _run_contract_round(
    phase3_round: Phase3Round,
    manifest: Manifest,
    manifest_path: Path,
    root_dir: Path,
    factory_config: FactoryConfig,
    ui: UI,
    run_id: str,
    pipeline: ComponentPipeline,
    run_state: RunState,
    factory_result: FactoryResult,
    bus: EventBus,
    notify: NotifyHooks,
    components_merged: bool,
) -> None:
    """Do the contract tests of one integration round (#776, B3)."""
    # PHASE 3: Contract testing
    contract_config = factory_config.contract_config
    if contract_config is None or contract_config.mode == ContractMode.SKIP.value:
        return

    # #481: one commit per round. The integrated check tests it and a
    # later check of the same round must judge the same tree.
    round_base_sha = _resolve_round_base(manifest, root_dir, ui)
    phase3_round.base_sha = round_base_sha
    try:
        contract_results = run_contract_testing(
            manifest,
            root_dir,
            replace(contract_config, rung=factory_config.test_rung),
            ui,
            components_merged=components_merged,
            base_sha=round_base_sha,
            setup=factory_config.worktree_setup(),
            plan=_plan_of_kept_checks(factory_config),
            carried=factory_config.acceptance_carried,
        )
    except ContractCleanupError as exc:
        # A contract temp worktree survived removal. The user's
        # checkout is untouched, but .kstrl/contract holds stale
        # state - fail the run loudly instead of continuing.
        ui.err(f"  Contract cleanup FAILED: {exc}")
        factory_result.contract_failures.append(f"contract cleanup failed: {exc}")
        return

    phase3_round.record(contract_results)
    for cr in contract_results:
        bus.emit(
            ContractResultEvent(
                tier=cr.tier,
                passed=cr.passed,
                breaker=cr.breaker,
                duration_seconds=round(cr.duration_seconds, 2),
            )
        )
        _record_contract_event(cr, root_dir, ui, run_id, manifest)

    failures = [cr for cr in contract_results if not cr.passed]
    if not failures:
        _carry(pipeline, contract_results)
        return

    _reset_contract_breakers(
        failures,
        phase3_round,
        manifest,
        manifest_path,
        factory_config,
        ui,
        pipeline,
        run_state,
        factory_result,
    )

    if phase3_round.breaker_reset:
        return

    _record_contract_failures(
        failures,
        manifest,
        manifest_path,
        ui,
        pipeline,
        run_state,
        factory_result,
        bus,
        notify,
    )


def _run_factory_locked(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None,
    lock_held: bool,
    interaction: InteractionChannel | None = None,
    stop: StopController | None = None,
    run_id_override: str | None = None,
    notify_capture_output: bool = False,
    architect_usage: UsageTotals | None = None,
    architect_run_id: str = "",
) -> FactoryResult:
    """run_factory body; runs with the run-level lock resolved (held, or
    explicitly degraded via --force-lock / no-fcntl platforms)."""
    factory_start = time.monotonic()
    # #154 fix round, A1b: this run's own start, wall-clock and in the
    # same format as Component.completed_at, so release_ref_from can
    # tell a merge THIS run produced from one a previous run left on
    # the manifest. Captured before any component runs, so a merge
    # this run confirms always has completed_at >= run_started_at.
    run_started_at = _iso_now()
    factory_result = FactoryResult()
    # Stable run id shared by evolution journal and knowledge layer.
    # current_run_id() carries microseconds plus a random nonce, so two
    # factory invocations launched within the same UTC second neither
    # collide on .kstrl/knowledge/<comp>/<run_id>/ directories nor
    # order ambiguously (R1.6: same-second knowledge run dirs must sort
    # by creation time, not by nonce).
    # PR F needs the run dir known before the TUI starts: the caller
    # may mint the id (format unchanged - knowledge.current_run_id).
    run_id = run_id_override if run_id_override else current_run_id()

    # #192: the run's config envelope, resolved ONCE, HERE, and injected
    # into the pipeline, which never resolves one of its own.
    # kstrl/runenvelope.py records the divergence that costs.
    #
    # Placed above the run directory's sinks deliberately, and that is
    # the round-2 fix rather than the original placement. A section the
    # entry preflight passed can be broken by the time this line runs -
    # on `ks factory --spec` the architect spends 119 to 210 seconds
    # between the two - and resolving seven sections in front of the run
    # means seven that can reject. Rejecting HERE gives the operator the
    # sentence and exit code 2 every other pre-spend refusal gives, and
    # it happens before the run directory exists, so #257's invariant
    # (nothing between the sinks and record_architect_usage returns
    # early) is untouched.
    #
    # What it does to the money: a refusal here leaves no factory run, so
    # the architect's spend is only in the decompose run it ran as (#567).
    # `serve` charges that run to the launch whose process wrote it, the
    # same path a spec blocker takes (#587).
    resolved = RunEnvelope.resolve(
        root_dir,
        policy_override=factory_config.policy_config,
    )
    run_envelope = resolved.envelope
    if run_envelope is None:
        _report_preflight(
            ui,
            "the run configuration cannot be resolved",
            list(resolved.problems),
        )
        factory_result.exit_code = 2
        return factory_result
    # Off the envelope rather than a second AutonomyConfig.load: the two
    # reads were nine lines apart and cost two parses of the same file
    # (measured 0.988ms, and a nested toml_parse_scope does not collapse
    # them because the inner scope shadows the outer cache).
    autonomy_config = run_envelope.autonomy
    # R8.2: the level this run OPERATES at, resolved BEFORE the pipeline
    # is constructed so the pipeline's envelope is the clamped one by
    # construction rather than by a re-assignment 155 lines below it.
    # Only the RESOLUTION is here; the bus event, the operator warnings
    # and the FactoryConfig flag overrides stay at their original place
    # below, where the sinks exist and the ordering against
    # _adversarial_phase_gates is unchanged.
    run_envelope, ladder = _resolve_ladder(run_envelope, factory_config, root_dir)

    # Set up progress log. R3.2: defaults ON under .kstrl/ so a
    # walk-away run always leaves an event trail `ks status` can
    # join; [factory] progress_log_enabled = false (or env) opts out.
    # Every event carries run_id so runs sharing the default file stay
    # distinguishable.
    # Dual-write (TUI rewrite chunk 3): typed schema-v2 events go to
    # .kstrl/runs/<run_id>/events.jsonl via the EventBus; V1CompatSink
    # delegates the v1-named subset to a real ProgressLog so the
    # progress.jsonl byte format AND its attached ProgressSink
    # observers (Linear, R7.4) stay untouched. progress_log_enabled =
    # false suppresses BOTH files (symmetric opt-out).
    progress_log: ProgressLog
    # Chunk 7: when the caller's UI is the event bridge (cli commands
    # via build_console), reuse ITS bus so the run's file sinks also
    # capture every imperative Log narration - the imperative call
    # sites become replayable. Tests passing a bare PlainUI get a
    # private bus (their UI already prints directly).
    if isinstance(ui, EventBridgeUI):
        bus = ui.bus
        bus.run_id = run_id
    else:
        bus = EventBus(run_id=run_id)
    journal_path: Path | None = None
    run_paths: RunPaths | None = None
    run_file_sinks: list[EventSink] = []
    # R8 (review finding P2-d): accounting storage is allocated for
    # EVERY run, including progress_log_enabled = false. It used to be
    # `run_paths`, which is gated on progress logging, so turning the
    # observability opt-out on also deleted the only place a worker
    # could publish its spend - a killed worker then recorded nothing
    # and the run silently under-reported. Money is not observability:
    # an opt-out may drop the narration, never the meter. This is the
    # same directory as `run_paths` when progress logging is on; when it
    # is off the run writes exactly one small JSON file per component
    # attempt under .kstrl/runs/<run_id>/ and no events at all.
    usage_paths = RunPaths.for_run(root_dir, run_id)
    if not factory_config.progress_log_enabled:
        progress_log = NullProgressLog()
    else:
        log_path = factory_config.progress_log_path or root_dir / ".kstrl" / "progress.jsonl"
        progress_log = ProgressLog(log_path, run_id=run_id, warn=ui.warn)
        journal_path = log_path
        run_paths = RunPaths.for_run(root_dir, run_id)
        run_file_sinks = [
            JsonlSink(run_paths.events_file),
            V1CompatSink(progress_log),
        ]
        for _sink in run_file_sinks:
            bus.add_sink(_sink)

    # R7.4: Linear sink - mirrors failure/budget events onto the issues
    # the decompose hook mapped in the manifest. Observability only;
    # build_linear_sink returns None (with a warning) rather than ever
    # failing the run, and emit() isolates sink exceptions.
    linear_sink = build_linear_sink(
        manifest,
        factory_config.linear_config or LinearConfig.load(root_dir),
        run_id=run_id,
        warn=ui.warn,
    )
    if linear_sink is not None:
        progress_log.attach_sink(linear_sink)

    # R3.2: notification hooks - each condition fires at most once per
    # run, and hook failures only ever warn.
    notify = NotifyHooks(
        factory_config.notify_config or NotifyConfig.load(root_dir),
        run_id=run_id,
        project=manifest.project_name,
        warn=ui.warn,
        capture_output=notify_capture_output,
    )

    bus.emit(
        RunStarted(
            project=manifest.project_name,
            components=len(manifest.components),
            pid=os.getpid(),
            architect_run_id=architect_run_id,
        )
    )
    # Chunk 4: the component DAG + budget caps as one event, so a
    # dashboard can draw the board without reading the manifest.
    bus.emit(_run_plan_event(manifest, factory_config))
    _emit_stack_confirmation(bus, factory_config.project_stack)

    # R7.1: resolve which model family reviews this run's diffs ONCE so
    # the choice is stable across components and the homogeneity warning
    # prints once per run, not per component. Explicit config always
    # wins; otherwise review/security default to the opposite family
    # from the engineer when that CLI is available. The selection is an
    # audit-trail event: same-family and cross-family runs must stay
    # distinguishable in the progress log.
    gates = _adversarial_phase_gates(factory_config, autonomy_config)
    review_selection = resolve_adversarial_selection(
        "review",
        may_dispatch_adversarial=gates.may_dispatch,
        explicit_cmd=factory_config.review_agent_cmd,
        explicit_type=factory_config.review_agent_type,
        explicit_model=factory_config.review_model,
        # #498: the engineer's command, as security below and the
        # --review-agent-cmd help both say. None auto-detected a real CLI.
        fallback_cmd=base_config.agent_cmd,
        fallback_type=base_config.agent_type,
        fallback_model=None,
        fallback_reasoning=None,
        engineer_cmd=base_config.agent_cmd,
        engineer_type=base_config.agent_type,
    )
    sec_cfg = factory_config.security_config
    security_selection: AdversarialAgentSelection | None = resolve_adversarial_selection(
        "security",
        may_dispatch_adversarial=gates.may_dispatch,
        explicit_cmd=sec_cfg.agent_cmd,
        explicit_type=sec_cfg.agent_type,
        explicit_model=sec_cfg.model,
        fallback_cmd=base_config.agent_cmd,
        fallback_type=base_config.agent_type,
        fallback_model=base_config.model,
        fallback_reasoning=base_config.model_reasoning_effort,
        engineer_cmd=base_config.agent_cmd,
        engineer_type=base_config.agent_type,
    )
    for _sel, _enabled in (
        (review_selection, gates.review),
        (security_selection, gates.security),
    ):
        if _sel is None or not _enabled:
            continue
        if _sel.warning:
            ui.warn(f"  {_sel.warning}")
        bus.emit(
            AdversarialAgentSelected(
                phase=_sel.phase,
                agent_source=_sel.source,
                identity=_sel.identity,
                agent_type=_sel.agent_type,
                model=_sel.model,
                homogeneous=_sel.warning is not None,
            )
        )

    if manifest_path is None:
        manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"

    # Load knowledge config once for the entire factory run, BEFORE the
    # pipeline is constructed. Binding it at construction removes the
    # late-binding accident where the old _handle_result closure read a
    # name that was only assigned further down the function (R7.3).
    knowledge_config = KnowledgeConfig.load(root_dir)

    # #193: the mutable structures the scheduler below and the
    # pipeline SHARE. Constructed once, here, and handed to the
    # pipeline whole at the construction site below. Every field is
    # an ALIAS and nothing may copy one, so everything from here on
    # reads through `run_state.<field>`: a surviving local beside the
    # struct is how one shared object silently becomes two.
    # kstrl/runstate.py carries the measured evidence per field,
    # including the one whose copy the whole suite missed.
    #
    # `factory_result` is the one field that also keeps a local name.
    # It is built far above because the pre-flight refusal path
    # returns it before this line is reached, so it cannot be created
    # here. The local and `run_state.factory_result` are the same
    # object and the suite does see them diverge: a fresh
    # FactoryResult() here gives 11 failed / 176 passed on
    # test_factory + test_pipeline + test_timeout_enforcement against
    # a 187-passed control (#193).
    run_state = RunState(factory_result=factory_result)
    # Components abandoned by the scheduler backstop; their workers may
    # still be alive, so their worktrees are never cleaned up here.
    leaked_component_ids: set[str] = set()

    # #269: every component's write scope, resolved HERE and nowhere
    # else. Both guards read this one snapshot - the in-loop guard
    # through _submit_args -> _run_component -> run_loop, and Phase 1
    # through the pipeline below - so neither has to read a PRD to learn
    # what a component may write, and the file the agent is allowed to
    # edit stops being the file that decides what it may edit. Resolved
    # before the pipeline exists and long before the scheduler can
    # launch anything, which is what makes the value one the agent never
    # had access to, with or without worktrees.
    run_scope = RunScope.resolve(manifest, root_dir, base_config)
    _record_run_scope(run_scope, bus, ui, manifest=manifest)

    # R7.3: the per-component phase chain and every component state
    # transition live in ComponentPipeline. Hooks are resolved from this
    # module's globals HERE, at run start, so tests patching
    # kstrl.factory.run_review (and friends) keep intercepting the
    # phase functions.
    #
    # Constructed BEFORE the DAG check below, and that placement is
    # load-bearing rather than incidental: the pipeline owns the meter,
    # so the meter's lifetime has to start with the run directory's
    # rather than with the first phase's. The record just below says
    # what breaks otherwise (#257 review). Construction is pure attribute
    # assignment again after #192 round 2: the four config sections it
    # briefly resolved moved into the run envelope above, so a manifest
    # the DAG check below rejects no longer does and discards that work,
    # and there is nothing here that can raise before
    # record_architect_usage.
    pipeline = ComponentPipeline(
        manifest=manifest,
        manifest_path=manifest_path,
        # LOAD-BEARING ALIAS, do not copy or freeze this argument.
        # The autonomy clamp 100 lines below rebinds
        # `factory_config.pause_before_pr_merge` and
        # `factory_config.review_mode` on THIS object, and the
        # pipeline reads both through the alias: the merge gate at
        # pipeline_delivery.py `_phase_checkpoint` and the review gate at
        # pipeline.py `_phase_review`. Replacing this argument with
        # a copy disconnects the ladder from both gates and the
        # whole suite stays green (measured on #193: 326 passed on
        # test_autonomy_ladder + test_pipeline + test_factory +
        # test_explicit_merge_gate + test_review_gates with the
        # copy, 326 passed without it). Whoever groups config into
        # a RunContext has to move the clamp above this line first.
        factory_config=factory_config,
        base_config=base_config,
        ui=ui,
        root_dir=root_dir,
        run_id=run_id,
        bus=bus,
        journal_path=journal_path,
        run_paths=run_paths,
        interaction=interaction,
        notify=notify,
        review_selection=review_selection,
        security_selection=security_selection,
        knowledge_config=knowledge_config,
        run_scope=run_scope,
        run_envelope=run_envelope,
        hooks=PipelineHooks(
            run_mechanical_verification=run_mechanical_verification,
            run_review=run_review,
            run_security_review=run_security_review,
            distill_facts=distill_facts,
            measure_fact_utilization=measure_fact_utilization,
            cleanup_worktree=_cleanup_worktree,
        ),
        run_state=run_state,
    )

    # #257: the architect's spend, incurred by the caller before this run
    # existed, enters the meter here - the first thing done to the
    # pipeline, and deliberately AHEAD of the DAG check below.
    #
    # The money is already gone by this point, so every early exit from
    # here has to leave it recorded rather than report a run that cost
    # nothing. That is not a hypothetical: `decompose_spec` only WARNS on
    # DAG errors and returns the manifest anyway, so an LLM-produced
    # cyclic or dangling-dependency manifest reaches the return below on
    # an ordinary `--spec` run. With the meter built after it, the run
    # directory existed, the architect row did not, and `serve` charged
    # $0 for a launch that had spent real money (#257 review).
    #
    # The invariant this rests on - nothing between the run directory's
    # sinks and this line returns early - holds today and is checked by
    # hand, not by a mechanism. An early exit inserted above would break
    # it silently; the cyclic-manifest test pins only the case that was
    # actually reachable.
    #
    # The cost of the ordering is cosmetic: a coverage warning about an
    # unpriced architect prints just ahead of the "Cost ceiling" line
    # rather than just after it.
    pipeline.record_architect_usage(architect_usage)

    # Validate DAG
    ui.section("Factory: Validating DAG")
    dag_errors = manifest.validate_dag()
    if dag_errors:
        for err in dag_errors:
            ui.err(f"  {err}")
        # A refusal before any component runs: 2 under the #452 contract,
        # which is also the code `ks serve` reads as a refusal (#531).
        factory_result.exit_code = 2
        return factory_result

    topo_order = manifest.topological_order()
    ui.ok(f"DAG valid: {len(topo_order)} components in dependency order")

    # #460: read BEFORE the reset below rewrites the statuses it keys on,
    # and before _run_preflights prunes the worktree registrations it reads.
    interrupted_branches = _interrupted_run_branches(manifest, root_dir)

    # Crash recovery: reset intermediate states
    for comp in manifest.components:
        if comp.status in (
            ComponentStatus.RUNNING.value,
            ComponentStatus.VERIFYING.value,
        ):
            ui.info(f"  Resetting '{comp.id}' from {comp.status} to PENDING")
            comp.status = ComponentStatus.PENDING.value

    # #463: a run the manifest names that never reached its summary hands
    # its attempts, readings and spend to this one. After the reset above,
    # so the components this run will run again are PENDING; before the
    # run id below is saved, so a resume that dies here leaves the manifest
    # naming the run whose record it had not yet taken over.
    pipeline.carry_interrupted_run()

    # R3.3: persist which run owns this manifest state. completed_at is
    # blanked while the run is in flight and stamped in the summary
    # epilogue, so "did the last run finish?" is answerable from the
    # manifest alone (and later, from Linear).
    manifest.run_id = run_id
    manifest.kstrl_version = kstrl_version()
    manifest.completed_at = ""
    # R8.2: apply this run's permissions from the autonomy level. The
    # bundle is computed at run start and WINS over contradicting config,
    # so a hand-edited flag cannot grant autonomy the ladder never
    # awarded; contradictions are recorded rather than silently dropped.
    # Opt-in: when [autonomy] is disabled the config's own flags stand.
    #
    # RESOLVED far above, before the pipeline was constructed, because
    # the level clamps the envelope (autonomy_level) and the pipeline
    # must hold the clamped one. What is left here is the reporting and
    # the FactoryConfig overrides, which stay in this position for two
    # orderings that are load-bearing: the bus and its sinks exist by
    # now, and `_adversarial_phase_gates` above read the CONFIGURED
    # review_mode rather than the bundle's, exactly as before.
    if ladder is not None:
        factory_config.pause_before_pr_merge = ladder.bundle.pause_before_pr_merge
        factory_config.review_mode = ladder.bundle.review_mode
        bus.emit(
            AutonomyLevelApplied(
                level=int(ladder.level),
                label=ladder.level.label,
                flags=tuple(ladder.bundle.describe()),
                overrides=tuple(ladder.clamps + ladder.overrides),
            )
        )
        ui.kv("Autonomy", f"L{int(ladder.level)} - {ladder.level.label}")
        for note in ladder.clamps:
            ui.warn(f"  {note}")
        # Each note carries its own verdict since #195: one of them
        # reports a gate the run RETAINED, and a blanket "Manual override
        # ignored" prefix would have contradicted it.
        for note in ladder.overrides:
            ui.warn(f"  {note}")

    # Issue #207 (review P1): checked AFTER autonomy resolution, because
    # the L1/L2 bundle can flip pause_before_pr_merge on when no config
    # flag ever set it - any earlier check misses that path and the gate
    # would be reported ON while _phase_checkpoint stays unreachable.
    gate_warning = merge_gate_unreachable_warning(factory_config)
    if gate_warning is not None:
        ui.warn(gate_warning)
    # R10.3: same timing, same reason - the L1/L2 bundle forces
    # review_mode to hard above, so a claim gate that looked
    # unreachable before autonomy resolved may be reachable now.
    claim_warning = claim_gate_unreachable_warning(factory_config)
    if claim_warning is not None:
        ui.warn(claim_warning)

    # R8.1: the manifest is a self-contained audit record of what merge
    # guardrails were in force for this run. #192: read back off the
    # PIPELINE, not off a local, so the hash is taken from the object
    # the pipeline will enforce and an edit that forgets the assignment
    # above fails a test rather than diverging silently.
    manifest.policy_hash = pipeline.run_envelope.policy_hash()
    manifest.save(manifest_path)

    # R0.2 crash recovery: MERGE_PENDING is re-pollable, not failed.
    # Re-poll before scheduling so confirmed merges unblock dependents.
    pipeline.repoll_merge_pending()

    max_parallel = _resolve_max_parallel(factory_config, ui)

    # R0.1: TimeoutConfig is the single source for the agent-iteration and
    # component wall-clock limits. Enforcement layers: the adapters kill
    # their subprocess group, run_loop aborts on the component wall clock,
    # and the scheduler backstop below catches a worker that hangs outside
    # both (e.g. a stuck scaffold or codebase scan step).
    timeout_cfg = factory_config.timeout_config or TimeoutConfig.load(root_dir)
    backstop_seconds = (
        timeout_cfg.component_total + timeout_cfg.scheduler_backstop_margin
        if timeout_cfg.component_total > 0
        else 0.0
    )

    # R7.5: no-progress circuit breaker limits, forwarded into every
    # engineer loop. It reads the diff only (#696 decision 10).
    breaker_cfg = BreakerConfig.load(root_dir)

    # R7.5: OS-level sandbox intent for the agent subprocesses, off the
    # envelope (#192). An engineer with no sandbox (a custom agent command,
    # or [sandbox] enabled = false) is a warning the run records in its
    # events.jsonl, never a refusal (#700, owner decision 2026-10-09).
    warn_unconfined(ui.warn, run_envelope.sandbox, {"engineer": base_config.agent_cmd})
    _warn_unsandboxable_reviewers(ui, review_selection, security_selection)
    factory_config.engineer_sandbox = _engineer_sandbox(run_envelope.sandbox, base_config)

    run_register = _plan_gated(
        _run_preflights(
            manifest,
            run_scope,
            root_dir,
            factory_config,
            run_id,
            ui,
            lock_held=lock_held,
            manifest_path=manifest_path,
            interrupted_branches=interrupted_branches,
            timeout_cfg=timeout_cfg,
        ),
        pipeline,
        ladder,
    )
    # An int is the exit code of a refusal or of the #602 plan gate. Not
    # falsiness: a clean run with no register binds an empty one, which is
    # the normal state for every project that predates #260.
    if isinstance(run_register, int):
        factory_result.exit_code = run_register
        return factory_result
    # #481: after the refusals (a refused run stamps nothing) and the
    # worktree preflight's fetch, and before the merge decisions below,
    # which can merge a component into the base.
    _stamp_feature_base(manifest, manifest_path, root_dir, ui)
    # #465: after every pre-spend refusal (a refused run must not have
    # pushed or merged anything) and before anything is scheduled, so the
    # dependents of an approved component are cut from a base that holds it.
    pipeline.apply_merge_decisions()
    # #595: after the merge decisions and before any engineer is
    # scheduled, so an approval made mid-run changes nothing in it.
    pipeline.snapshot_waivers()

    ui.section("Factory: Execution")
    ui.kv("Max parallel", str(max_parallel))
    ui.kv("Max retries", str(factory_config.max_retries))
    ui.kv("Review mode", factory_config.review_mode)
    contract_mode = (
        factory_config.contract_config.mode if factory_config.contract_config else "skip"
    )
    ui.kv("Contract check", contract_mode)
    ui.kv("Worktree setup", factory_config.worktree_setup_summary())
    for label, value in _execution_limit_rows(timeout_cfg, factory_config):
        ui.kv(label, value)

    def _path_relative_to_root(path: Path) -> str:
        """Render `path` relative to root_dir for use inside per-component
        worktrees. Falls back to the absolute path string when relativization
        fails (e.g. a path on a different mount)."""
        return relative_to_root(path, root_dir)

    prompt_file_rel = _path_relative_to_root(base_config.prompt_file)
    # The progress path is deliberately NOT hoisted out of the per-component
    # loop: unless it was explicitly configured, each component derives its
    # own next to its PRD so the engineer writes inside its allowedPaths
    # (base_config.component_progress_file, called from _submit_args).
    codebase_map_file_rel = _path_relative_to_root(base_config.codebase_map_file)
    golden_patterns_file_rel = _path_relative_to_root(base_config.golden_patterns_file)
    memory_file_rel = _path_relative_to_root(base_config.memory_file)

    _report_operator_files(base_config, root_dir, ui)

    def _launch_component(comp: Component) -> Path | None:
        """Set up worktree for a component. Returns worktree path or None."""
        try:
            if factory_config.use_worktrees:
                fresh_from_base = comp.id in run_state.fresh_base_retry_ids
                run_state.fresh_base_retry_ids.discard(comp.id)
                wt_path = _setup_worktree(
                    comp.id,
                    comp.branch_name,
                    manifest.base_branch,
                    root_dir,
                    run_id,
                    fresh_from_base=fresh_from_base,
                    recut_at=(
                        _recut_point(manifest, comp, run_state.component_bases)
                        if fresh_from_base
                        else None
                    ),
                    ui=ui,
                )
                # #543: registered before the merge below, so a merge that
                # fails leaves a worktree the pass-end cleanup removes.
                run_state.worktree_paths[comp.id] = wt_path
                _start_from_dependencies(
                    manifest,
                    comp,
                    wt_path,
                    run_state.component_bases,
                    fresh_from_base=fresh_from_base,
                )
            else:
                wt_path = root_dir
            run_state.worktree_paths[comp.id] = wt_path
            return wt_path
        except RuntimeError as exc:
            ui.err(f"  Worktree setup failed for '{comp.id}': {exc}")
            # pipeline.fail covers the R3.2 notify + progress-log
            # calls and stamps the R3.3 failure/evidence fields.
            pipeline.fail(
                comp,
                str(exc),
                phase="provisioning",
                check="worktree_setup",
            )
            return None

    # Build codebase scan config dict for serialization to worker processes
    ff_config_dict: dict[str, Any] | None = None
    if factory_config.codebase_scan_config and factory_config.codebase_scan_config.enabled:
        fc = factory_config.codebase_scan_config
        ff_config_dict = {
            "enabled": fc.enabled,
            "module_map": fc.module_map,
            "max_context_tokens": fc.max_context_tokens,
        }

    def _submit_args(comp: Component, wt_path: Path) -> tuple[Any, ...]:
        ctx_json = run_state.component_contexts.get(comp.id)
        engineer_usage = pipeline.engineer_usage_totals()
        scope = pipeline.run_scope.for_component(comp.id)

        def _warn_knowledge_failure(exc: Exception, comp_id: str = comp.id) -> None:
            # Non-fatal, but NOT a metrics detail: the engineer runs
            # without any of its facts when this fires. That is a real
            # degradation of the run, and it used to be a bare
            # `except: pass` that said nothing (#191).
            ui.warn(f"  Knowledge retrieval failed for {comp_id}: {exc}")

        injected_knowledge = retrieve_knowledge_context(
            manifest,
            comp,
            knowledge_config,
            allowed_paths=authored_paths(scope),
            dependency_paths=paths_by_component(manifest, pipeline.run_scope),
            worktree=wt_path,
            on_failure=_warn_knowledge_failure,
        )
        pipeline.record_injected_knowledge(comp.id, injected_knowledge)
        knowledge_prefix = injected_knowledge or ""
        return (
            comp.id,
            comp.prd_path,
            str(wt_path),
            str(root_dir),
            prompt_file_rel,
            base_config.agent_cmd,
            base_config.model,
            base_config.model_reasoning_effort,
            base_config.agent_type,
            base_config.sleep_seconds,
            ctx_json,
            ff_config_dict,
            factory_config.worktree_setup_for_component(comp),
            knowledge_prefix,
            # #260: rides the same context-prefix path the distilled
            # facts already ride, so the register reaches the engineer
            # without a second delivery mechanism. Per component: its
            # own decisions in full, the rest of the run summarised.
            build_decisions_context(
                run_register.decisions, comp.id, requirements=factory_config.requirements
            ),
            # Per-component, not run-wide: KstrlConfig.component_progress_file
            # keeps the engineer's progress log inside allowedPaths.
            base_config.component_progress_file(comp.prd_path, root_dir),
            codebase_map_file_rel,
            # R10.8 and R10.9: the worker joins each of these onto
            # root_dir and nothing else. Sent as root-relative strings
            # for the same reason the other path slots are: the worker
            # is a separate process and gets paths, not a config object.
            golden_patterns_file_rel,
            memory_file_rel,
            timeout_cfg.agent_iteration,
            timeout_cfg.component_total,
            # R2.3 (CRIT-8): forward the invoking config's loop settings;
            # they were previously dropped here and _run_component ran a
            # hardcoded 30 non-interactive iterations with no path guard.
            base_config.max_iterations,
            base_config.interactive,
            # R8 review: the COMPONENT's allowedPaths, not the run-wide
            # --allowed-paths flag. That flag is empty in every ordinary
            # factory run, so `if config.allowed_paths` in the loop was
            # False and guards.enforce_allowed_paths never ran: the
            # in-loop guard existed and was inert. A scope violation was
            # therefore only caught at Phase 1, AFTER the whole engineer
            # loop had been paid for - measured at $12.93 for one such
            # component, versus ~$2.50 for the single iteration it takes
            # to catch it here.
            #
            # #269: the plan-time snapshot, resolved from root_dir
            # before the first engineer call and the SAME object Phase 1
            # is judged with. Read here rather than re-derived per
            # submission, which is what makes a retry inherit the scope
            # the run started with: this closure runs once per attempt,
            # so a per-attempt re-read would let an agent that edited
            # the PRD in attempt 1 widen its own guard for attempt 2
            # (which, under use_worktrees=False, it can do to the file
            # this used to read).
            scope,
            # R7.5: no-progress circuit breaker limits.
            breaker_cfg.no_progress_iterations,
            # R7.5: OS-level sandbox intent for the engineer's agent CLI.
            run_envelope.sandbox.enabled,
            run_envelope.sandbox.allow_network,
            # #700: the paths the confirmed [stack] grants its commands.
            with_stack_writable(
                run_envelope.sandbox, root_dir, factory_config.project_stack
            ).writable,
            # R7.6: in-loop USD budget for the claude-sdk engineer.
            base_config.agent_budget_usd,
            # Chunk 6: worker event channel (None when progress logging
            # is disabled - transcripts and events off together).
            str(run_paths.root) if run_paths is not None else None,
            # R8: accounting channel. Always set, precisely because the
            # line above may be None (review finding P2-d).
            str(usage_paths.root),
            run_id,
            # R8: run-level ceilings (tokens AND cost) + the spend
            # recorded so far, snapshotted AT LAUNCH so the engineer loop
            # can halt itself between iterations. With max_parallel > 1 a
            # worker cannot see a concurrent sibling's spend, only what
            # the parent had recorded when this worker started.
            # prior_calls plus prior_token_calls/prior_cost_calls carry
            # the ENGINEER's non-reporting call counts down so the
            # unenforceable rule does not reset on every attempt and
            # component (review finding P1-a), while staying blind to
            # other roles' timeouts - those say nothing about whether the
            # engineer's adapter reports tokens or cost. The two counters
            # are separate because the two axes have separate coverage:
            # codex reports tokens and no cost, claude can report cost
            # with no usage dict. prior_total_tokens/prior_cost_usd stay
            # run-wide: the overrun checks ask what the RUN has spent.
            LoopBudget(
                max_total_tokens=factory_config.max_total_tokens,
                prior_total_tokens=pipeline.run_usage.total_tokens,
                prior_known_calls=pipeline.run_usage.known_calls,
                prior_calls=engineer_usage.calls,
                prior_token_calls=engineer_usage.token_calls,
                max_cost_usd=factory_config.max_cost_usd,
                prior_cost_usd=pipeline.run_usage.cost_usd,
                prior_cost_calls=engineer_usage.cost_calls,
            ),
        )

    # #261: run-invariant, so it is resolved once here rather than per
    # component. None when Phase 1 is off.
    engineer_verify = factory_config.engineer_verify_config()

    def _run_scheduling_pass() -> None:
        """Run ready components until nothing is PENDING-and-ready.

        Called once per contract pass: a contract breaker reset to
        PENDING after a failed contract phase re-enters scheduling via
        the outer loop in run_factory (R0.3) - previously the reset
        happened after the only scheduling loop had exited, so the
        promised retry never ran.

        ONE loop serves sequential and parallel scheduling (R7.3): the
        launch protocol (budget gate -> begin_attempt -> provisioning ->
        submit) appears exactly once. max_parallel <= 1 swaps the
        process pool for _InlineExecutor, which runs the worker
        synchronously in-process - the historical sequential behavior -
        behind the identical submit/wait/result flow.

        Manual executor lifecycle: on a backstop breach we must NOT wait
        for the (possibly hung) worker at shutdown, which the
        `with ProcessPoolExecutor(...)` form would do.
        """
        executor: ProcessPoolExecutor | _InlineExecutor
        if max_parallel <= 1:
            executor = _InlineExecutor()
        else:
            executor = ProcessPoolExecutor(
                max_workers=max_parallel,
                initializer=_install_worker_signal_forwarding,
            )
        slots_cap = max(1, max_parallel)
        running_futures: dict[Future[ComponentResult], str] = {}
        future_deadlines: dict[Future[ComponentResult], float] = {}
        try:
            while True:
                if stop is not None and stop.is_set():
                    _abort_inflight(
                        executor,
                        running_futures,
                        pipeline,
                        ui,
                        stop,
                    )
                    return
                ready = manifest.get_ready_components()
                slots = slots_cap - len(running_futures)

                # Components transitioned WITHOUT a launch this pass
                # (budget gate, provisioning failure). When that happens
                # and nothing is running, the loop must re-derive the
                # ready set rather than stop - R3.1's scheduling gate
                # promises to fail every remaining pending component
                # loudly, and a provisioning failure must not strand
                # still-schedulable siblings.
                transitioned_without_launch = 0
                for comp in ready[:slots]:
                    # Budget ceilings and an untrustworthy plan-time
                    # scope: three conditions that fail the component
                    # loudly rather than spending an engineer loop on it.
                    if _refused_before_launch(pipeline, comp, pipeline.run_scope):
                        transitioned_without_launch += 1
                        continue
                    pipeline.begin_attempt(comp)
                    manifest.save(manifest_path)
                    bus.emit(ComponentStarted(component=comp.id))
                    ui.info(f"  Starting: {comp.id}")

                    # #646: None too when ks retry kept the approved head,
                    # which engineer_worktree has just judged with no engineer.
                    wt_path = pipeline.engineer_worktree(comp, _launch_component(comp))
                    if wt_path is None:
                        transitioned_without_launch += 1
                        continue

                    # Provisioning succeeded: the engineer phase starts
                    # immediately before submission. process_result closes
                    # normal exits; fail_scheduler_backstop closes timeouts.
                    bus.emit(
                        PhaseStarted(
                            component=comp.id,
                            phase="engineer",
                            attempt=comp.retries + 1,
                        )
                    )
                    args = _submit_args(comp, wt_path)

                    # R8 (P2-c): the usage snapshot is attempt-scoped by
                    # deletion, and the deletion happens HERE - before
                    # the worker exists - so an attempt cancelled during
                    # pool startup cannot salvage its predecessor's
                    # tokens on top of the ones already on the meter.
                    if _clear_partial_usage(usage_paths.engineer_usage(comp.id)):
                        pipeline.mark_usage_salvage_safe(comp.id)
                    else:
                        # A snapshot we could not delete may still hold
                        # the PREVIOUS attempt's totals, which
                        # process_result already counted. Refuse disk
                        # salvage for this attempt rather than risk
                        # counting them twice - losing an aborted
                        # attempt's spend understates a total; salvaging
                        # a stale one corrupts it.
                        pipeline.mark_usage_salvage_unsafe(comp.id)
                        ui.warn(
                            f"  Could not retire the previous usage "
                            f"snapshot for {comp.id}; disk salvage is "
                            f"disabled for this attempt"
                        )
                    if isinstance(executor, _InlineExecutor):
                        # In-process worker: no fd redirection (it would
                        # hijack the parent terminal), and each transcript
                        # line mirrors to the parent UI so sequential runs
                        # keep live engineer output. functools.partial
                        # binds kstrl.factory._run_component AT SUBMIT
                        # TIME, so tests patching it still intercept.
                        # mypy cannot prove the unknown-length *args
                        # tuple stops before the kwargs; _submit_args
                        # ends at token_budget by construction.
                        task = functools.partial(
                            _run_component,
                            *args,
                            # Keyword, NOT appended to args: the tuple
                            # ends at token_budget by construction (see
                            # above) and a positional extra silently
                            # lands on redirect_output.
                            base_branch=pipeline.component_base(comp.id),
                            verify_config=engineer_verify,
                            attempt=comp.retries + 1,
                            plan_id=comp.plan_id,
                            redirect_output=False,  # type: ignore[misc]
                            live_line=functools.partial(
                                ui.stream_line,
                                "AI",
                            ),
                            stop_check=(stop.is_set if stop is not None else None),
                        )
                        future = executor.submit(task)
                    else:
                        # Bound through partial rather than passed as a
                        # submit() kwarg: Executor.submit types its own
                        # **kwargs, so a keyword meant for the worker
                        # reads as a duplicate of submit's. Picklable -
                        # _run_component is module-level and the base
                        # branch is a plain str.
                        future = executor.submit(
                            functools.partial(
                                _run_component,
                                *args,
                                # Same unprovable-*args limitation the
                                # inline branch annotates above.
                                base_branch=pipeline.component_base(comp.id),  # type: ignore[misc]
                                verify_config=engineer_verify,
                                attempt=comp.retries + 1,
                                plan_id=comp.plan_id,
                            ),
                        )
                    running_futures[future] = comp.id
                    factory_result.scheduled.append(comp.id)
                    if backstop_seconds > 0:
                        future_deadlines[future] = time.monotonic() + backstop_seconds

                if not running_futures:
                    if transitioned_without_launch:
                        continue
                    break

                # Wait for the next completion, bounded by the nearest
                # backstop deadline. The worker enforces its own timeouts
                # (adapter kill + loop wall clock); this scheduler-side
                # deadline is the last line of defense when a worker hangs
                # outside those layers.
                wait_timeout = _next_backstop_wait(
                    running_futures,
                    future_deadlines,
                    time.monotonic(),
                )
                done, stopped = _wait_interruptible(
                    set(running_futures),
                    wait_timeout,
                    stop,
                )
                if stopped:
                    assert stop is not None
                    _abort_inflight(
                        executor,
                        running_futures,
                        pipeline,
                        ui,
                        stop,
                    )
                    return

                if done:
                    # Preserve pre-R0.1 semantics: process one completion
                    # per pass so freed slots are refilled promptly.
                    future = next(iter(done))
                    comp_id = running_futures.pop(future)
                    future_deadlines.pop(future, None)
                    try:
                        comp_result = future.result()
                    except Exception as exc:
                        comp_result = ComponentResult(
                            component_id=comp_id,
                            success=False,
                            error=str(exc),
                        )
                    pipeline.process_result(comp_id, comp_result)
                    continue

                # Nothing completed inside the window: fail every
                # component past its backstop deadline and keep going.
                now = time.monotonic()
                for future in _expired_futures(
                    running_futures,
                    future_deadlines,
                    now,
                ):
                    comp_id = running_futures.pop(future)
                    future_deadlines.pop(future, None)
                    leaked_component_ids.add(comp_id)
                    pipeline.fail_scheduler_backstop(comp_id, backstop_seconds)
        finally:
            if leaked_component_ids:
                ui.warn(
                    "Shutting down worker pool without waiting: "
                    f"{len(leaked_component_ids)} worker(s) may still be running"
                )
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=True)

    def _cleanup_pass_worktrees() -> None:
        """Remove component worktrees left behind by a scheduling pass.

        With ``keep_worktrees_on_failure`` (R3.3), a FAILED component's
        worktree is kept and recorded as its evidence pointer instead of
        removed, so the failure summary can point the operator at it.
        """
        if not factory_config.use_worktrees:
            return
        ui.section("Factory: Cleanup")
        for comp_id in run_state.worktree_paths:
            if comp_id in leaked_component_ids:
                # A possibly-live worker still owns this worktree; removing
                # it under the worker risks corrupting the main repo's
                # worktree metadata.
                ui.warn(f"  Keeping worktree for '{comp_id}' (leaked worker may still be running)")
                continue
            comp = manifest.get_component(comp_id)
            if (
                factory_config.keep_worktrees_on_failure
                and comp is not None
                and comp.status == ComponentStatus.FAILED.value
            ):
                comp.evidence_worktree = str(run_state.worktree_paths[comp_id])
                ui.info(
                    "  Keeping failed worktree for post-mortem: "
                    f"{run_state.worktree_paths[comp_id]}"
                )
                pipeline.record_worktree_sweep(
                    comp_id,
                    sweep_worktree(run_state.worktree_paths[comp_id]),
                    "cleanup",
                )
                continue
            pipeline.record_worktree_sweep(
                comp_id,
                _cleanup_worktree(comp_id, root_dir, run_id),
                "cleanup",
            )
        manifest.save(manifest_path)
        # Drop the run's now-empty worktree dir; leaked workers' and
        # failed components' kept worktrees leave it non-empty and it
        # stays for the next run's prune pass (which preserves recorded
        # evidence worktrees of still-FAILED components).
        try:
            os.rmdir(root_dir / ".kstrl" / "worktrees" / run_id)
        except OSError:
            pass
        ui.ok("Worktrees cleaned up")

    # Per-component PRs are squash-merged into base as each component
    # completes, so at contract time tier re-merges would be content
    # no-ops and blame attribution would be meaningless: the contract
    # phase instead tests the integrated base branch (R0.3). single_pr
    # defers its one PR until after the contract phase, so it stays in
    # deferred-merge (tier merge + bisection) mode.
    components_merged = factory_config.create_prs and not factory_config.single_pr

    # R0.3: scheduling + contract testing form one outer loop so a
    # contract breaker reset to PENDING actually re-enters scheduling.
    # #483: the rounds come from the integration loop. Every exit below is a
    # `continue`, so the round always returns to the loop, which reviews it
    # once and stops. Termination: every breaker reset consumes one of the
    # breaker's bounded retries, and no fix component is built (#696).
    integration = IntegrationLoop(
        IntegrationRun(
            manifest=manifest,
            manifest_path=manifest_path,
            root_dir=root_dir,
            run_id=run_id,
            pipeline=pipeline,
            ui=ui,
            requirements=run_register.requirements,
        ),
        stop,
    )
    for phase3_round in integration.rounds():
        _run_scheduling_pass()
        _cleanup_pass_worktrees()

        if stop is not None and stop.is_set():
            ui.warn(f"  Run stopped: {stop.reason}")
            continue

        _run_contract_round(
            phase3_round,
            manifest,
            manifest_path,
            root_dir,
            factory_config,
            ui,
            run_id,
            pipeline,
            run_state,
            factory_result,
            bus,
            notify,
            components_merged,
        )

    # #482: the integration review ran after the last round, inside the
    # loop. #483: with integration_blocking on, a stop without a clean
    # verdict is already in contract_failures.
    integration_record = integration.record

    return _finish_run(
        RunContext(
            manifest=manifest,
            manifest_path=manifest_path,
            factory_config=factory_config,
            ui=ui,
            root_dir=root_dir,
            stop=stop,
            run_id=run_id,
            run_started_at=run_started_at,
            factory_start=factory_start,
            factory_result=factory_result,
            run_state=run_state,
            run_envelope=run_envelope,
            autonomy_config=autonomy_config,
            ladder=ladder,
            bus=bus,
            run_file_sinks=run_file_sinks,
            journal_path=journal_path,
            pipeline=pipeline,
            notify=notify,
            integration_record=integration_record,
        )
    )
