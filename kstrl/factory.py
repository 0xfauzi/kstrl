"""Factory orchestrator - parallel component execution with 3-phase verification."""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.config import (
    KstrlConfig,
    check_number,
)
from kstrl.events import (
    ComponentStarted,
    EventBridgeUI,
    EventBus,
    NotifyHooks,
    PhaseStarted,
    RunPaths,
    UsageTotals,
)
from kstrl.factory_acceptance import (
    _plan_gated,
    _run_contract_round,
)
from kstrl.factory_agents import (
    AdversarialAgentSelection,
    select_reviewers,
)
from kstrl.factory_config import (
    FactoryConfig,
)
from kstrl.factory_launch import (
    LaunchPlan,
    _refused_before_launch,
    submit_args,
)
from kstrl.factory_preflight import (
    _run_preflights,
)
from kstrl.factory_result import (
    ComponentResult,
    FactoryResult,
    clear_partial_usage,
    salvage_aborted_usage,
)
from kstrl.factory_run import (
    RunContext,
    _announce_run,
    _make_run_state,
    _open_run_sinks,
    _resolve_envelope,
    _resolve_limits,
    _stage_launch,
    _start_manifest,
)
from kstrl.factory_summary import _finish_run
from kstrl.factory_worker import (
    _install_worker_signal_forwarding,
    _run_component,
)
from kstrl.factory_worktrees import (
    _cleanup_pass_worktrees,
    _cleanup_worktree,
    _recut_point,
    _setup_worktree,
    _start_from_dependencies,
)
from kstrl.integration_loop import IntegrationLoop
from kstrl.integration_phase import IntegrationRun
from kstrl.knowledge import (
    current_run_id,
    measure_fact_utilization,
)
from kstrl.knowledge_distill import distill_facts
from kstrl.knowledge_facts import KnowledgeConfig
from kstrl.owner import InteractionChannel
from kstrl.pipeline import ComponentPipeline
from kstrl.pipeline_state import PipelineHooks, iso_now
from kstrl.plan import (
    Component,
    Manifest,
    RunScope,
)
from kstrl.proc import (
    FactoryLockHeldError,
    RunLock,
    StopController,
    acquire_run_lock,
    kill_active_process_groups,
    release,
)
from kstrl.review import (
    run_review,
)
from kstrl.runenvelope import RunEnvelope
from kstrl.runstate import RunState
from kstrl.security import (
    run_security_review,
)
from kstrl.verify import run_mechanical_verification
from kstrl.verify_model import VerifyConfig

if TYPE_CHECKING:
    from kstrl.ui import UI


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
        salvage_aborted_usage(future, comp_id, pipeline)
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
        if clear_partial_usage(plan.usage_paths.engineer_usage(comp.id)):
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
    architect_usage: UsageTotals | None = None,
    architect_run_id: str = "",
    run_lock: RunLock | None = None,
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
            run_lock = acquire_run_lock(
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
            architect_usage=architect_usage,
            architect_run_id=architect_run_id,
        )
    finally:
        release((factory_config.setup_rung, factory_config.test_rung))
        factory_config.setup_rung = factory_config.test_rung = None
        run_lock.release()


def _make_bus(ui: UI, run_id: str) -> EventBus:
    """The event bus of the run: the bus of the UI bridge, or a private bus."""
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
    return bus


def _build_pipeline(
    manifest: Manifest,
    manifest_path: Path,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    ui: UI,
    root_dir: Path,
    run_id: str,
    bus: EventBus,
    journal_path: Path | None,
    run_paths: RunPaths | None,
    interaction: InteractionChannel | None,
    notify: NotifyHooks,
    review_selection: AdversarialAgentSelection,
    security_selection: AdversarialAgentSelection | None,
    knowledge_config: KnowledgeConfig,
    run_scope: RunScope,
    run_envelope: RunEnvelope,
    run_state: RunState,
) -> ComponentPipeline:
    """The pipeline of the run, with the hooks that this module resolves."""
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
    return ComponentPipeline(
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
    run_started_at = iso_now()
    factory_result = FactoryResult()
    # Stable run id shared by evolution journal and knowledge layer.
    # current_run_id() carries microseconds plus a random nonce, so two
    # factory invocations launched within the same UTC second neither
    # collide on .kstrl/knowledge/<comp>/<run_id>/ directories nor
    # order ambiguously (R1.6: same-second knowledge run dirs must sort
    # by creation time, not by nonce).
    # PR F: the caller can know the run dir before the run starts, so it
    # may mint the id (format unchanged - knowledge.current_run_id).
    run_id = run_id_override if run_id_override else current_run_id()

    opened = _resolve_envelope(root_dir, factory_config, ui, factory_result)
    if opened is None:
        return factory_result
    run_envelope, autonomy_config, ladder = opened

    bus = _make_bus(ui, run_id)
    progress_log, journal_path, run_paths, run_file_sinks, usage_paths = _open_run_sinks(
        factory_config, root_dir, run_id, ui, bus
    )
    notify = _announce_run(
        manifest,
        factory_config,
        root_dir,
        run_id,
        ui,
        bus,
        progress_log,
        architect_run_id,
    )
    review_selection, security_selection = select_reviewers(
        factory_config, base_config, autonomy_config, ui, bus
    )

    if manifest_path is None:
        manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"

    knowledge_config, run_state, leaked_component_ids, run_scope = _make_run_state(
        manifest, root_dir, base_config, ui, bus, factory_result
    )
    pipeline = _build_pipeline(
        manifest,
        manifest_path,
        factory_config,
        base_config,
        ui,
        root_dir,
        run_id,
        bus,
        journal_path,
        run_paths,
        interaction,
        notify,
        review_selection,
        security_selection,
        knowledge_config,
        run_scope,
        run_envelope,
        run_state,
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

    interrupted_branches = _start_manifest(
        manifest,
        manifest_path,
        root_dir,
        ui,
        pipeline,
        run_id,
        factory_config,
        bus,
        ladder,
        factory_result,
    )
    if interrupted_branches is None:
        return factory_result

    max_parallel, timeout_cfg, backstop_seconds, breaker_cfg = _resolve_limits(
        factory_config,
        base_config,
        root_dir,
        ui,
        run_envelope,
        review_selection,
        security_selection,
    )

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
    launch, engineer_verify = _stage_launch(
        manifest,
        manifest_path,
        root_dir,
        run_id,
        ui,
        pipeline,
        run_state,
        factory_config,
        base_config,
        knowledge_config,
        run_register,
        max_parallel,
        timeout_cfg,
        breaker_cfg,
        run_envelope,
        run_paths,
        usage_paths,
    )

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
