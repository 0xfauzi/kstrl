"""R8.6 continuous intake: the daemon that drains the work queue.

PR 1 built a place for work to wait. This is what runs it without a
human firing each run - and therefore the module where a mistake spends
money while nobody is watching. A measured factory engineer iteration
costs ~$1.70-2.60 on a first attempt and $3.99-7.42 on a retry (retries
carry accumulated context), and a 5-story component with adversarial
review ran ~$29 and 96 minutes without finishing. Those numbers are why
almost every decision below errs toward NOT launching a run.

**The retry rule, stated precisely.** R8.6 says "only
``infrastructure_error`` failures auto-retry". That phrasing leaves the
UNKNOWN case undefined, and the unknown case is where the money goes, so
this module implements it as *positive evidence only, failing closed*: an
item is retried only when we can read affirmative evidence that the
failure was infrastructural. An unreadable manifest, a missing artifact,
a run that died before recording anything, an exit code we do not
recognise - all poison, none retry. This is deliberately the inverse of
"retry unless proven to be a spec failure", which is the shape that
produces an overnight crash loop.

The same class of mistake is already on record in this repo: R8.1 review
correction #2 found the changed-file reads were fail-OPEN, so a
``kstrl.toml`` diff evaluated as "0 files, 0 lines" and passed every
path and size rule. Defaulting to permissive on missing evidence looks
harmless until the evidence goes missing.

**Four independent backstops**, because a correct classifier is not
sufficient - a *persistent* infrastructure fault is retryable by the
rules and still burns money:

1. ``max_attempts`` per item, enforced by ``Queue.start`` itself.
2. Exponential backoff between attempts, so a fast-failing item cannot
   spin.
3. ``daily_budget_usd``, checked BEFORE admitting each item.
4. A consecutive-poison breaker that pauses the whole queue. If ``main``
   is broken then every run fails verification, each failure is
   individually legitimate, and per-item bounds never notice; only a
   cross-item signal does.

**Honesty about the budget (H4).** ``daily_budget_usd`` can only count
cost that an adapter reported. The codex adapter reports tokens and no
cost, so with a cost-blind agent the budget is not approximate - it is
*unenforceable*, the same condition PR #184 named for ``max_cost_usd``.
This module therefore records the day's spend together with its
coverage, never converts unreported calls into an estimated dollar
figure, and refuses to run unattended when a budget is configured but
cost coverage is absent (``allow_uncovered_cost`` is the explicit
override).
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.io import run_kind
from kstrl.queue import (
    Queue,
    QueueBudgetExhausted,
    QueueConfig,
    QueueError,
    QueueItem,
    queue_lock,
    queue_root,
    short_item_id,
)
from kstrl.serve_config import ServeConfig, ServeError, ServeObserver
from kstrl.serve_gates import (
    _pause_queue,
    _wait_gate_refusal,
    check_budget,
    check_cost_coverage,
    check_poison_breaker,
)
from kstrl.serve_github import (
    OpenPrCountStreak,
    _load_pr_count_streak,
    _run_intake,
    _save_pr_count_streak,
)
from kstrl.serve_merge_gate import _unreadable_section_reason, resolve_merge_gate
from kstrl.serve_outcome import (
    FactoryRunner,
    RunOutcome,
    Verdict,
    _pr_urls_from_manifest,
    _run_id_from_manifest,
    classify_run,
)
from kstrl.serve_process import (
    _report_reap_degrade,
    _unreaped_timeout_detail,
    backoff_seconds,
    reap_leases,
    subprocess_factory_runner,
)
from kstrl.serve_settle import (
    CycleResult,
    _file_inbox_item,
    _pause_on_unreadable_spend,
    _poison_for_merge_gate,
    _record_count_failure,
    _report_merge_gate_refusal,
    _report_reaped,
    _report_remote_outcome,
    _settle_unfailed,
)
from kstrl.serve_spend import (
    ARCHITECT_RUN_KIND,
    ServeStateError,
    SpendLedger,
    _iso,
    _local_today,
    _utc_now,
    owned_run_spend,
    run_dir_names,
)

if TYPE_CHECKING:
    pass

SERVE_LOCK_FILENAME = "serve.lock"


#: How many recent cycles an unbounded daemon keeps for diagnostics.
RECENT_CYCLE_WINDOW = 100


class ServeLockedError(ServeError):
    """Another ``ks serve`` holds the singleton lock on this root."""


# ---------------------------------------------------------------------------
# Persistent daemon state: spend, coverage, and the poison streak
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Classification - the money-critical decision
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Lease reaping - sleep and crash recovery
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The default runner: a subprocess, under caffeinate
# ---------------------------------------------------------------------------


@contextmanager
def serve_lock(root_dir: Path) -> Iterator[None]:
    """The daemon singleton lock.

    A THIRD lock, distinct from the queue's per-transition mutex and from
    ``.kstrl/factory.lock``. Held for the daemon's whole lifetime, so two
    ``ks serve`` processes cannot double-lease; the queue mutex stays
    short-lived so ``ks queue ls`` keeps working while the daemon runs.
    """
    lock_path = queue_root(root_dir) / SERVE_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield
        return
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ServeLockedError(f"another ks serve holds {lock_path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class _NullObserver:
    lines: list[str] = field(default_factory=list)

    def info(self, message: str) -> None:
        self.lines.append(f"info: {message}")

    def warn(self, message: str) -> None:
        self.lines.append(f"warn: {message}")

    def err(self, message: str) -> None:
        self.lines.append(f"err: {message}")


def serve_cycle(
    root_dir: Path,
    *,
    config: ServeConfig | None = None,
    queue_config: QueueConfig | None = None,
    runner: FactoryRunner | None = None,
    observer: ServeObserver | None = None,
    now: datetime | None = None,
    pr_count_streak: OpenPrCountStreak | None = None,
) -> CycleResult:
    """One poll cycle: recover, gate, maybe run exactly one item.

    One item per cycle on purpose. Concurrency here would mean two
    factory runs on one repo, and ``.kstrl/factory.lock`` exists
    precisely because that corrupts worktrees and the manifest.

    Order is deliberate. Recovery comes first so a crashed predecessor's
    work is reclaimed before anything new is admitted; every gate is
    checked before the CLAIM, not after, because a gate evaluated after
    the spend is a post-mortem.

    ``pr_count_streak`` is the loop's memory across cycles, and is None
    for a single cycle because one poll cannot have a streak.
    :func:`serve_loop` owns the instance.

    Both config reads are inside one ``except Exception`` and REFUSE
    (#364). ``serve`` has no per-cycle handler, so a ``[queue]`` section
    made malformed after the daemon started used to leave ``serve()``
    entirely; under launchd the job was relaunched on
    ``LAUNCHD_THROTTLE_SECONDS`` and died again on the same key. The
    fault types measured escaping here are enumerated once, at
    ``tests.test_serve_config_reads.CYCLE_CONFIG_DOCUMENTS``, which is
    why the caught set is the whole surface rather than an enumeration.
    Nothing is claimed and no attempt is charged: the message is the
    refusal's only home, and the poll interval is the retry.
    """
    obs: ServeObserver = observer or _NullObserver()
    result = CycleResult()
    section = "serve"
    try:
        cfg = config or ServeConfig.load(root_dir)
        section = "queue"
        qcfg = queue_config or QueueConfig.load(root_dir)
    except Exception as exc:  # noqa: BLE001 - anything but a config is the same answer
        result.skipped = _unreadable_section_reason(
            section,
            exc,
            "this poll is refused",
            "The poll retries after the next interval; fix the section.",
        )
        obs.err(result.skipped)
        return result
    queue = Queue(root_dir, qcfg)
    ledger = SpendLedger(root_dir)
    moment = now or _utc_now()

    queue.ensure_dirs()

    # The daemon's own state is consulted by three gates. If it cannot be
    # read, none of them can be evaluated, so stop before claiming
    # anything (#186 F4/F5) rather than proceeding with unknown totals.
    try:
        ledger.read_state()
    except ServeStateError as exc:
        result.skipped = str(exc)
        result.needs_human = True
        obs.err(str(exc))
        result.inbox_items += (
            _file_inbox_item(
                root_dir,
                kind_name="budget_overrun",
                title="Continuous intake halted: unreadable spend ledger",
                detail=str(exc),
                dedupe_key=f"serve-ledger:{ledger.path}",
                evidence={"path": str(ledger.path)},
            ),
        )
        return result

    # 1. Recovery, under the mutex: staging leftovers and dead leases.
    with queue_lock(root_dir, blocking=True):
        result.swept_staging = queue.sweep_staging()
        result.reaped = reap_leases(queue, now=moment)
        for _item_id in result.reaped.poisoned:
            ledger.record_terminal(poisoned=True)
    _report_reaped(root_dir, queue, result, obs)

    # 2. Pull remote work in BEFORE the gates, so newly-admitted items face
    #    the same budget, breaker and cap checks as everything else. The
    #    mutex is taken INSIDE, around the commit only (#189 N1).
    result.synced, result.sync_errors = _run_intake(root_dir, queue, obs)

    # 3. The pause marker is read AND cleared under the mutex, re-reading
    #    inside it. Review #186 F7: read and clear outside the lock let an
    #    operator's fresh emergency pause be overwritten by the daemon
    #    clearing a previously-expired budget pause, after which the queue
    #    started work the operator had just stopped.
    with queue_lock(root_dir, blocking=True):
        pause = queue.pause_state()
        if pause.paused and not pause.active(moment):
            queue.resume(actor="serve")
            pause = queue.pause_state()
            cleared = True
        else:
            cleared = False
    if cleared:
        obs.info("Pause window elapsed; resuming intake")
    if pause.active(moment):
        result.skipped = f"paused: {pause.reason}"
        return result

    # 4. Gates, all evaluated BEFORE the claim.
    try:
        gates = (
            check_poison_breaker(ledger, cfg),
            check_cost_coverage(ledger, cfg),
            check_budget(ledger, cfg),
        )
    except ServeStateError as exc:
        result.skipped = str(exc)
        result.needs_human = True
        obs.err(str(exc))
        return result

    for admission in gates:
        if admission.allowed:
            continue
        if admission.pause_reason:
            result.paused = _pause_queue(queue, admission, obs, root_dir)
            result.needs_human = True
            result.inbox_items += (
                _file_inbox_item(
                    root_dir,
                    kind_name="budget_overrun",
                    title="Continuous intake paused",
                    detail=admission.reason,
                    dedupe_key=(f"serve-pause:{_local_today()}:{admission.pause_reason[:40]}"),
                    evidence={"reason": admission.reason},
                ),
            )
        result.skipped = admission.reason
        return result

    waiting = _wait_gate_refusal(root_dir, cfg, obs, streak=pr_count_streak)
    if waiting is not None:
        _record_count_failure(root_dir, pr_count_streak, result)
        result.skipped = waiting
        return result

    # 5. Claim exactly one item.
    refused_item: QueueItem | None = None
    with queue_lock(root_dir, blocking=True):
        candidate = queue.next_ready(moment)
        if candidate is None:
            result.skipped = "nothing ready"
            return result
        gate = resolve_merge_gate(candidate, root_dir)
        if gate.refusal:
            refused_item = _poison_for_merge_gate(queue, ledger, candidate, gate)
        else:
            leased = queue.lease(candidate, actor="serve")

    if gate.refusal:
        result.skipped = gate.refusal
        _report_merge_gate_refusal(
            root_dir,
            result,
            candidate,
            gate,
            refused_item,
            obs,
        )
        return result

    for note in gate.notes:
        obs.warn(f"  {note}")

    # 6. Charge the attempt, then spend. Never the other way round.
    try:
        with queue_lock(root_dir, blocking=True):
            running = queue.start(leased, actor="serve")
    except QueueBudgetExhausted as exc:
        with queue_lock(root_dir, blocking=True):
            queue.poison(leased, reason=str(exc), actor="serve")
            ledger.record_terminal(poisoned=True)
            exhausted_item = queue.get(leased.item_id)
        obs.err(str(exc))
        result.skipped = str(exc)
        result.needs_human = True
        _report_remote_outcome(
            root_dir,
            exhausted_item,
            state="poison",
            detail=str(exc),
            observer=obs,
        )
        return result

    result.ran_item = running.item_id
    # The remote label now tracks the REAL queue state: admission leaves
    # the trigger label alone and `running` is applied here, when the item
    # actually starts (#187 F8).
    _report_remote_outcome(
        root_dir,
        running,
        state="running",
        detail=(
            f"Claimed by the queue as `{running.item_id}` "
            f"(attempt {running.attempts} of {running.max_attempts})."
        ),
        observer=obs,
    )
    obs.info(
        f"Running {running.item_id} ({running.title}) "
        f"attempt {running.attempts}/{running.max_attempts}, "
        f"merge gate {'on' if gate.pause_before_pr_merge else 'off'}"
    )

    manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
    # Snapshot BEFORE the launch so only runs this invocation created are
    # charged or trusted (#186 F2). Without it, an early failure charged a
    # previous run's spend and could be classified from a stale manifest.
    runs_before = run_dir_names(root_dir)
    # The child's pid: the lease owner, and the process a decompose run of
    # this launch names (#587). Empty when the runner spawned nothing.
    spawned: list[int] = []

    def _adopt(child_pid: int) -> None:
        spawned.append(child_pid)
        # The child, not the daemon, owns the lease for the duration of
        # the run (#186 F1).
        try:
            with queue_lock(root_dir, blocking=True):
                current = queue.get(running.item_id)
                if current is not None:
                    queue.adopt_lease(current, pid=child_pid, actor="serve")
        except (QueueError, OSError) as exc:
            obs.warn(f"  could not adopt the run\u2019s lease: {exc}")

    run_factory_fn = runner or _default_runner(cfg)
    spec_path = queue.spec_path(running)
    project_name = running.project_name or _derive_project_name(running)
    outcome = run_factory_fn(
        root_dir=root_dir,
        spec_path=spec_path,
        project_name=project_name,
        pause_before_pr_merge=gate.pause_before_pr_merge,
        design_acceptance=running.design_acceptance,
        timeout_seconds=cfg.factory_timeout_seconds,
        on_spawn=_adopt,
    )

    _report_reap_degrade(obs, outcome)

    if outcome.timed_out and not outcome.group_reaped:
        # A descendant may still be running against this repo. Do NOT
        # release the item for another attempt (#186 F1).
        detail = _unreaped_timeout_detail(outcome)
        with queue_lock(root_dir, blocking=True):
            current = queue.get(running.item_id)
            if current is not None:
                queue.poison(current, reason=detail, actor="serve")
                ledger.record_terminal(poisoned=True)
                current = queue.get(running.item_id)
        obs.err(f"  {running.item_id}: {detail}")
        _report_remote_outcome(
            root_dir,
            current,
            state="poison",
            detail=detail,
            observer=obs,
        )
        result.verdict = Verdict.UNCLASSIFIABLE
        result.reason = detail
        result.needs_human = True
        result.inbox_items += (
            _file_inbox_item(
                root_dir,
                kind_name="halted_run",
                title=f"Queue item {short_item_id(running.item_id)}: orphaned factory process",
                detail=detail,
                dedupe_key=f"queue-orphan:{running.item_id}",
                evidence={"item_id": running.item_id},
            ),
        )
        return result

    # 7. Charge the spend before deciding anything, so a classification bug
    #    cannot also lose the accounting. `owned_run_spend` says which dirs.
    owned_runs, owned = owned_run_spend(
        root_dir, runs_before, launch_pid=spawned[-1] if spawned else None
    )
    _pause_on_unreadable_spend(root_dir, queue, owned.unreadable, obs, result)
    total, covered_calls, total_calls = owned.cost_usd, owned.cost_calls, owned.usage_calls

    charged = ledger.charge(
        total,
        covered_calls=covered_calls,
        total_calls=total_calls,
        # Derived per run and unioned, not asserted: since #257 piece B
        # the architect reaches the meter on a run that executed, and
        # `RunSpend.unmetered_phases` states what zero calls does and
        # does not prove.
        unmetered_phases=owned.unmetered_phases,
        metered_run=bool(owned_runs),
    )
    result.charged_usd = total
    obs.info(
        f"  charged ${total:.2f} over {len(owned_runs)} run dir(s)"
        + (
            f"; {total_calls - covered_calls} call(s) reported no cost"
            if total_calls > covered_calls
            else ""
        )
        + f"; today ${charged.spent_usd:.2f}"
        + (" (a floor)" if charged.lower_bound else "")
    )

    # Classification may only read a manifest this invocation produced.
    manifest_run_after = _run_id_from_manifest(manifest_path)
    owns_manifest = bool(manifest_run_after) and manifest_run_after in owned_runs
    verdict = classify_run(
        root_dir,
        run=outcome,
        manifest_path=manifest_path if owns_manifest else None,
        owned_run_ids=owned_runs,
    )
    pr_urls = _pr_urls_from_manifest(manifest_path, owns_manifest, obs)
    result.verdict = verdict.verdict
    result.reason = verdict.reason
    evidence = dict(verdict.evidence)
    evidence.update(
        {
            "item_id": running.item_id,
            "owned_run_ids": owned_runs,
            "attempts": running.attempts,
            "max_attempts": running.max_attempts,
            "cost_usd": total,
            "cost_is_lower_bound": charged.lower_bound,
        }
    )

    # A run id reaches the queue item on two verdicts. AWAITING_APPROVAL
    # takes the manifest's, which classify_run reads only from a manifest
    # this invocation owns (#463). AWAITING_ANSWER takes the launch's one
    # architect run (#644): a halt writes no manifest, and the last owned
    # run can be a foreign factory run that landed in the window.
    if _settle_unfailed(
        root_dir,
        queue,
        ledger,
        running,
        verdict,
        pr_urls,
        manifest_run_after,
        _architect_run_id(owned_runs),
        obs,
        result,
    ):
        return result

    # The failure branch. Every remote writeback below happens AFTER the
    # mutex is released (#187 F10): report_outcome makes two gh calls at
    # the configured timeout, and an unavailable GitHub must not block
    # every local queue transition.
    retried = False
    poisoned_item: QueueItem | None = None
    retry_delay = 0.0
    with queue_lock(root_dir, blocking=True):
        current = queue.get(running.item_id)
        if current is None:
            obs.err(f"{running.item_id} vanished mid-run")
            return result

        queue.finish_failed(
            current,
            error=verdict.reason,
            actor="serve",
            pr_urls=pr_urls,
        )
        failed = queue.get(running.item_id)
        if failed is None:
            return result

        if verdict.verdict.may_retry and failed.attempts_remaining > 0:
            retry_delay = backoff_seconds(failed.attempts)
            queue.requeue(
                failed,
                reason=f"retry: {verdict.reason}",
                actor="serve",
                not_before=_iso(moment + timedelta(seconds=retry_delay)),
            )
            retried = True
        else:
            exhausted = (
                f"; no attempts left ({failed.attempts}/{failed.max_attempts})"
                if verdict.verdict.may_retry
                else ""
            )
            queue.poison(
                failed,
                reason=f"{verdict.reason}{exhausted}",
                actor="serve",
            )
            ledger.record_terminal(poisoned=True)
            poisoned_item = queue.get(running.item_id)

    if retried:
        obs.warn(
            f"  {running.item_id} retrying in {int(retry_delay)}s "
            f"({running.attempts}/{running.max_attempts} attempts used): "
            f"{verdict.reason}"
        )
        # A retry IS a state change the remote should see: the item is
        # back in the queue, not running (#187 F8).
        _report_remote_outcome(
            root_dir,
            queue.get(running.item_id),
            state="failed",
            detail=(
                f"{verdict.reason}\n\nRetrying in {int(retry_delay)}s "
                f"({running.attempts} of {running.max_attempts} attempts used)."
            ),
            observer=obs,
        )
        return result

    result.needs_human = True
    obs.err(f"  {running.item_id} poisoned: {verdict.reason}")
    _report_remote_outcome(
        root_dir,
        poisoned_item,
        state="poison",
        detail=verdict.reason,
        observer=obs,
    )
    result.inbox_items += (
        _file_inbox_item(
            root_dir,
            kind_name="halted_run",
            title=f"Queue item {short_item_id(running.item_id)} poisoned",
            detail=(
                f"{verdict.reason}\n\n"
                f"Verdict: {verdict.verdict}. This item will NOT be retried "
                "automatically. Inspect with `ks queue show "
                f"{running.item_id}`."
            ),
            dedupe_key=f"queue-poison:{running.item_id}",
            evidence=evidence,
            run_id=owned_runs[-1] if owned_runs else "",
        ),
    )
    return result


def _architect_run_id(owned_runs: Sequence[str]) -> str:
    """The launch's architect run among its owned runs, or "" (#644).

    ``owned_run_spend`` keeps a decompose run only when the launch's own
    process opened it, so a launch that halted on an escalation owns
    exactly one. Any other count names no run rather than picking one.
    """
    architect = [rid for rid in owned_runs if run_kind(rid) == ARCHITECT_RUN_KIND]
    return architect[0] if len(architect) == 1 else ""


def _default_runner(config: ServeConfig) -> FactoryRunner:
    """Bind the caffeinate preference into the subprocess runner."""

    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        design_acceptance: bool,
        timeout_seconds: float,
        on_spawn: Callable[[int], None] | None = None,
    ) -> RunOutcome:
        return subprocess_factory_runner(
            root_dir=root_dir,
            spec_path=spec_path,
            project_name=project_name,
            pause_before_pr_merge=pause_before_pr_merge,
            design_acceptance=design_acceptance,
            timeout_seconds=timeout_seconds,
            on_spawn=on_spawn,
            caffeinate=config.caffeinate,
        )

    return runner


def _derive_project_name(item: QueueItem) -> str:
    """A factory project name for an item that did not supply one.

    Derived from the item id rather than the title: the title is free
    text from a remote issue and would become a branch name.
    """
    return f"queue-{item.item_id.split('-')[-1]}"


def _refresh_ci(root_dir: Path, obs: ServeObserver) -> None:
    """Re-read the CI state of the merge commits that are due (#570).

    After every cycle, so it never runs while a factory run does, and
    in ``--once`` too, so an interval-mode LaunchAgent refreshes on each
    firing. Which commits are due is ``kstrl.ci_state.refresh_due``.
    Nothing gates on the record, so a refresh that could not read or
    write it is reported and the daemon carries on: the ledger then
    holds no new reading, and every surface shows the commit as it
    last read, with that read's time.
    """
    from kstrl.ci_state import refresh_ci

    try:
        readings = refresh_ci(root_dir)
    except Exception as exc:  # noqa: BLE001 - a refresh must never stop the daemon
        obs.err(f"CI state not refreshed: {type(exc).__name__}: {exc}")
        return
    for reading in readings:
        obs.info(f"  CI {reading.sha[:12]} {reading.state}: {reading.reason}")


def serve(
    root_dir: Path,
    *,
    once: bool = False,
    config: ServeConfig | None = None,
    queue_config: QueueConfig | None = None,
    runner: FactoryRunner | None = None,
    observer: ServeObserver | None = None,
    max_cycles: int = 0,
    sleeper: Any = None,
) -> list[CycleResult]:
    """Drain the queue, holding the daemon singleton lock.

    ``once`` runs exactly one cycle - the launchd/cron fallback mode, and
    the shape ``ks serve --once`` exposes. ``max_cycles`` bounds the loop
    for tests; 0 means run until interrupted.

    ``once`` returns BEFORE entering the loop rather than breaking out of
    it. That is deliberate: while mutation-testing this module, an
    injected fault in a ``break`` condition turned ``--once`` into an
    unbounded loop that slept 60s per iteration forever. Under launchd
    that is a job which never exits and blocks every later interval, so
    the single-shot path should not depend on a conditional being right.
    """
    cfg = config or ServeConfig.load(root_dir)
    obs: ServeObserver = observer or _NullObserver()
    sleep = sleeper or time.sleep
    # Read from the control directory, not built fresh, so `once` counts
    # too. `--plist-mode interval` runs one `ks serve --once` process per
    # firing, and a per-call streak made the alarm unreachable there.
    pr_count_streak = _load_pr_count_streak(root_dir, obs)

    def _cycle() -> CycleResult:
        result = serve_cycle(
            root_dir,
            config=cfg,
            queue_config=queue_config,
            runner=runner,
            observer=obs,
            pr_count_streak=pr_count_streak,
        )
        _save_pr_count_streak(pr_count_streak, root_dir, obs)
        _refresh_ci(root_dir, obs)
        return result

    with serve_lock(root_dir):
        if once:
            return [_cycle()]

        # A bounded window, not a growing list. Review #186 F11: the
        # unbounded daemon path appended one CycleResult per poll forever
        # with no consumer - the CLI catches KeyboardInterrupt outside
        # this call and discards the value - so memory grew by
        # construction. Bounded runs still return every result.
        if max_cycles:
            bounded: list[CycleResult] = []
            while True:
                bounded.append(_cycle())
                if len(bounded) >= max_cycles:
                    return bounded
                sleep(cfg.poll_interval_seconds)

        recent: deque[CycleResult] = deque(maxlen=RECENT_CYCLE_WINDOW)
        while True:
            recent.append(_cycle())
            sleep(cfg.poll_interval_seconds)
