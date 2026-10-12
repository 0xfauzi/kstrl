"""Factory orchestrator - parallel component execution with 3-phase verification."""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.agents.base import UsageTotals
from kstrl.agents.proc import kill_active_process_groups
from kstrl.breaker import BreakerConfig
from kstrl.config import (
    KstrlConfig,
)
from kstrl.config_numbers import check_number
from kstrl.contract import (
    ContractCleanupError,
    ContractMode,
    run_contract_testing,
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
from kstrl.factory_launch import (
    LaunchPlan,
    _print_execution_header,
    _refused_before_launch,
    _resolve_max_parallel,
    build_launch_plan,
    submit_args,
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
    _cleanup_pass_worktrees,
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
)
from kstrl.knowledge_distill import distill_facts
from kstrl.knowledge_facts import KnowledgeConfig
from kstrl.linear import LinearConfig, build_linear_sink
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
from kstrl.sandbox import warn_unconfined
from kstrl.scope import RunScope
from kstrl.security import (
    run_security_review,
)
from kstrl.shutdown import StopController
from kstrl.timeout import TimeoutConfig
from kstrl.ui.bridge import EventBridgeUI
from kstrl.verify import run_mechanical_verification
from kstrl.verify_model import VerifyConfig
from kstrl.version import kstrl_version

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


@dataclass(frozen=True)
class _PassContext:
    """The pass objects that the ``LaunchPlan`` does not hold, never copies of them.

    The class is frozen so that a step cannot rebind a field. A step changes
    the run through the objects that a field refers to (``factory_result``
    and ``leaked_component_ids``). A copy of one of them would leave a green
    suite and no effect (#193).
    """

    manifest_path: Path
    bus: EventBus
    stop: StopController | None
    factory_result: FactoryResult
    leaked_component_ids: set[str]
    max_parallel: int
    backstop_seconds: float
    engineer_verify: VerifyConfig | None


_FutureMap = dict[Future[ComponentResult], str]
_DeadlineMap = dict[Future[ComponentResult], float]


def _launch_component(plan: LaunchPlan, comp: Component) -> Path | None:
    """Set up worktree for a component. Returns worktree path or None."""
    try:
        if plan.factory_config.use_worktrees:
            fresh_from_base = comp.id in plan.run_state.fresh_base_retry_ids
            plan.run_state.fresh_base_retry_ids.discard(comp.id)
            wt_path = _setup_worktree(
                comp.id,
                comp.branch_name,
                plan.manifest.base_branch,
                plan.root_dir,
                plan.run_id,
                fresh_from_base=fresh_from_base,
                recut_at=(
                    _recut_point(plan.manifest, comp, plan.run_state.component_bases)
                    if fresh_from_base
                    else None
                ),
                ui=plan.ui,
            )
            # #543: registered before the merge below, so a merge that
            # fails leaves a worktree the pass-end cleanup removes.
            plan.run_state.worktree_paths[comp.id] = wt_path
            _start_from_dependencies(
                plan.manifest,
                comp,
                wt_path,
                plan.run_state.component_bases,
                fresh_from_base=fresh_from_base,
            )
        else:
            wt_path = plan.root_dir
        plan.run_state.worktree_paths[comp.id] = wt_path
        return wt_path
    except RuntimeError as exc:
        plan.ui.err(f"  Worktree setup failed for '{comp.id}': {exc}")
        # pipeline.fail covers the R3.2 notify + progress-log
        # calls and stamps the R3.3 failure/evidence fields.
        plan.pipeline.fail(
            comp,
            str(exc),
            phase="provisioning",
            check="worktree_setup",
        )
        return None


def _new_executor(max_parallel: int) -> ProcessPoolExecutor | _InlineExecutor:
    """The executor of one scheduling pass: a process pool, or the inline stand-in."""
    executor: ProcessPoolExecutor | _InlineExecutor
    if max_parallel <= 1:
        executor = _InlineExecutor()
    else:
        executor = ProcessPoolExecutor(
            max_workers=max_parallel,
            initializer=_install_worker_signal_forwarding,
        )
    return executor


def _fill_slots(
    plan: LaunchPlan,
    ctx: _PassContext,
    executor: ProcessPoolExecutor | _InlineExecutor,
    ready: list[Component],
    running_futures: _FutureMap,
    future_deadlines: _DeadlineMap,
) -> int:
    """Launch the ready components. Return how many changed state with no launch."""
    # Components transitioned WITHOUT a launch this pass
    # (budget gate, provisioning failure). When that happens
    # and nothing is running, the loop must re-derive the
    # ready set rather than stop - R3.1's scheduling gate
    # promises to fail every remaining pending component
    # loudly, and a provisioning failure must not strand
    # still-schedulable siblings.
    transitioned_without_launch = 0
    for comp in ready:
        # Budget ceilings and an untrustworthy plan-time
        # scope: three conditions that fail the component
        # loudly rather than spending an engineer loop on it.
        if _refused_before_launch(plan.pipeline, comp, plan.pipeline.run_scope):
            transitioned_without_launch += 1
            continue
        plan.pipeline.begin_attempt(comp)
        plan.manifest.save(ctx.manifest_path)
        ctx.bus.emit(ComponentStarted(component=comp.id))
        plan.ui.info(f"  Starting: {comp.id}")

        # #646: None too when ks retry kept the approved head,
        # which engineer_worktree has just judged with no engineer.
        wt_path = plan.pipeline.engineer_worktree(comp, _launch_component(plan, comp))
        if wt_path is None:
            transitioned_without_launch += 1
            continue

        # Provisioning succeeded: the engineer phase starts
        # immediately before submission. process_result closes
        # normal exits; fail_scheduler_backstop closes timeouts.
        ctx.bus.emit(
            PhaseStarted(
                component=comp.id,
                phase="engineer",
                attempt=comp.retries + 1,
            )
        )
        args = submit_args(plan, comp, wt_path)

        # R8 (P2-c): the usage snapshot is attempt-scoped by
        # deletion, and the deletion happens HERE - before
        # the worker exists - so an attempt cancelled during
        # pool startup cannot salvage its predecessor's
        # tokens on top of the ones already on the meter.
        if _clear_partial_usage(plan.usage_paths.engineer_usage(comp.id)):
            plan.pipeline.mark_usage_salvage_safe(comp.id)
        else:
            # A snapshot we could not delete may still hold
            # the PREVIOUS attempt's totals, which
            # process_result already counted. Refuse disk
            # salvage for this attempt rather than risk
            # counting them twice - losing an aborted
            # attempt's spend understates a total; salvaging
            # a stale one corrupts it.
            plan.pipeline.mark_usage_salvage_unsafe(comp.id)
            plan.ui.warn(
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
                base_branch=plan.pipeline.component_base(comp.id),
                verify_config=ctx.engineer_verify,
                attempt=comp.retries + 1,
                plan_id=comp.plan_id,
                redirect_output=False,  # type: ignore[misc]
                live_line=functools.partial(
                    plan.ui.stream_line,
                    "AI",
                ),
                stop_check=(ctx.stop.is_set if ctx.stop is not None else None),
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
                    base_branch=plan.pipeline.component_base(comp.id),  # type: ignore[misc]
                    verify_config=ctx.engineer_verify,
                    attempt=comp.retries + 1,
                    plan_id=comp.plan_id,
                ),
            )
        running_futures[future] = comp.id
        ctx.factory_result.scheduled.append(comp.id)
        if ctx.backstop_seconds > 0:
            future_deadlines[future] = time.monotonic() + ctx.backstop_seconds
    return transitioned_without_launch


def _await_next(
    plan: LaunchPlan,
    ctx: _PassContext,
    executor: ProcessPoolExecutor | _InlineExecutor,
    running_futures: _FutureMap,
    future_deadlines: _DeadlineMap,
) -> set[Future[ComponentResult]] | None:
    """Wait for a completion. Return the done futures, or None when the run stopped."""
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
        ctx.stop,
    )
    if stopped:
        assert ctx.stop is not None
        _abort_inflight(
            executor,
            running_futures,
            plan.pipeline,
            plan.ui,
            ctx.stop,
        )
        return None
    return done


def _collect_completion(
    plan: LaunchPlan,
    ctx: _PassContext,
    running_futures: _FutureMap,
    future_deadlines: _DeadlineMap,
    done: set[Future[ComponentResult]],
) -> None:
    """Process one completed future."""
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
    plan.pipeline.process_result(comp_id, comp_result)


def _fail_expired(
    plan: LaunchPlan,
    ctx: _PassContext,
    running_futures: _FutureMap,
    future_deadlines: _DeadlineMap,
) -> None:
    """Fail each running component that is past its backstop deadline."""
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
        ctx.leaked_component_ids.add(comp_id)
        plan.pipeline.fail_scheduler_backstop(comp_id, ctx.backstop_seconds)


def _run_scheduling_pass(plan: LaunchPlan, ctx: _PassContext) -> None:
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
    executor = _new_executor(ctx.max_parallel)
    slots_cap = max(1, ctx.max_parallel)
    running_futures: dict[Future[ComponentResult], str] = {}
    future_deadlines: dict[Future[ComponentResult], float] = {}
    try:
        while True:
            if ctx.stop is not None and ctx.stop.is_set():
                _abort_inflight(
                    executor,
                    running_futures,
                    plan.pipeline,
                    plan.ui,
                    ctx.stop,
                )
                return
            ready = plan.manifest.get_ready_components()
            slots = slots_cap - len(running_futures)
            transitioned_without_launch = _fill_slots(
                plan, ctx, executor, ready[:slots], running_futures, future_deadlines
            )

            if not running_futures:
                if transitioned_without_launch:
                    continue
                break

            done = _await_next(plan, ctx, executor, running_futures, future_deadlines)
            if done is None:
                return

            if done:
                _collect_completion(plan, ctx, running_futures, future_deadlines, done)
                continue

            _fail_expired(plan, ctx, running_futures, future_deadlines)
    finally:
        if ctx.leaked_component_ids:
            plan.ui.warn(
                "Shutting down worker pool without waiting: "
                f"{len(ctx.leaked_component_ids)} worker(s) may still be running"
            )
            executor.shutdown(wait=False, cancel_futures=True)
        else:
            executor.shutdown(wait=True)


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

    _print_execution_header(ui, factory_config, max_parallel, timeout_cfg)

    launch = build_launch_plan(
        manifest,
        root_dir,
        run_id,
        ui,
        pipeline,
        run_state,
        factory_config,
        base_config,
        knowledge_config,
        run_register,
        timeout_cfg,
        breaker_cfg,
        run_envelope,
        run_paths,
        usage_paths,
    )

    _report_operator_files(base_config, root_dir, ui)

    # #261: run-invariant, so it is resolved once here rather than per
    # component. None when Phase 1 is off.
    engineer_verify = factory_config.engineer_verify_config()

    pass_ctx = _PassContext(
        manifest_path=manifest_path,
        bus=bus,
        stop=stop,
        factory_result=factory_result,
        leaked_component_ids=leaked_component_ids,
        max_parallel=max_parallel,
        backstop_seconds=backstop_seconds,
        engineer_verify=engineer_verify,
    )

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
        _run_scheduling_pass(launch, pass_ctx)
        _cleanup_pass_worktrees(
            manifest,
            manifest_path,
            root_dir,
            run_id,
            factory_config,
            ui,
            run_state,
            pipeline,
            leaked_component_ids,
        )

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
