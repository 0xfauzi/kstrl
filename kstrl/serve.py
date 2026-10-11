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

from kstrl.runid import run_kind
from kstrl.serve_config import ServeConfig, ServeError, ServeObserver
from kstrl.serve_gates import (
    _pause_queue,
    _wait_gate_refusal,
    check_budget,
    check_cost_coverage,
    check_parked_merges,
    check_poison_breaker,
)
from kstrl.serve_github import (
    OpenPrCountStreak,
    _load_pr_count_streak,
    _run_intake,
    _save_pr_count_streak,
)
from kstrl.serve_merge_gate import MergeGate, _unreadable_section_reason, resolve_merge_gate
from kstrl.serve_outcome import (
    FactoryRunner,
    Outcome,
    RunOutcome,
    Verdict,
    _pr_urls_from_manifest,
    _run_id_from_manifest,
    classify_run,
)
from kstrl.serve_process import (
    ReapResult,
    _report_reap_degrade,
    _unreaped_timeout_detail,
    backoff_seconds,
    reap_leases,
    subprocess_factory_runner,
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
from kstrl.workqueue import Queue
from kstrl.workqueue_items import (
    ItemSource,
    ItemState,
    QueueBudgetExhausted,
    QueueConfig,
    QueueError,
    QueueItem,
    short_item_id,
)
from kstrl.workqueue_store import queue_lock, queue_root

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
class CycleResult:
    """What one poll cycle did. Every field is something a test asserts."""

    ran_item: str = ""
    verdict: Verdict | None = None
    reason: str = ""
    #: An item ended in a state only a human can move. Set on EVERY
    #: poison and refusal path, including the reaper's and the merge-gate
    #: refusal, neither of which sets ``ran_item``. Review #186 F10: the
    #: CLI derived its exit status from ``Verdict.may_retry``, which
    #: stays true for an infra verdict whose last attempt was spent, so
    #: `ks serve --once` exited 0 on a poisoned item.
    needs_human: bool = False
    reaped: ReapResult = field(default_factory=ReapResult)
    swept_staging: int = 0
    paused: str = ""
    skipped: str = ""
    charged_usd: float = 0.0
    inbox_items: tuple[str, ...] = ()
    #: Remote refs admitted by the intake stage this cycle.
    synced: tuple[str, ...] = ()
    #: Intake errors. Non-empty does NOT stop the cycle: a front-end
    #: outage must never block the local queue (R8.6).
    sync_errors: tuple[str, ...] = ()


@dataclass
class _NullObserver:
    lines: list[str] = field(default_factory=list)

    def info(self, message: str) -> None:
        self.lines.append(f"info: {message}")

    def warn(self, message: str) -> None:
        self.lines.append(f"warn: {message}")

    def err(self, message: str) -> None:
        self.lines.append(f"err: {message}")


def _file_inbox_item(
    root_dir: Path,
    *,
    kind_name: str,
    title: str,
    detail: str,
    dedupe_key: str,
    evidence: dict[str, Any],
    run_id: str = "",
) -> str:
    """Record a decision for a human, never failing the caller.

    An inbox write must not be able to undo a queue transition that
    already happened, so a failure here returns an empty id and the
    caller carries on. The queue journal remains the authoritative record
    either way.

    The caught set is the callee's WHOLE SURFACE, not the types somebody
    expected: ``Inbox.add`` takes the control lock, and
    ``InboxConfig.load`` casts per key. Both guards that read
    this site clear on ``tests.helpers.astwalk.catches_everything``, the
    one rule #364 reconciled them onto.
    """
    try:
        from kstrl.inbox import Inbox, InboxConfig, ItemKind

        config = InboxConfig.load(root_dir)
        if not config.enabled:
            return ""
        box = Inbox(root_dir, config)
        item = box.add(
            ItemKind(kind_name),
            title,
            detail=detail,
            dedupe_key=dedupe_key,
            evidence=evidence,
            run_id=run_id,
        )
        return item.id
    except Exception:  # noqa: BLE001 - an inbox write must never fail its caller
        return ""


def _poison_for_merge_gate(
    queue: Queue,
    ledger: SpendLedger,
    candidate: QueueItem,
    gate: MergeGate,
) -> QueueItem | None:
    """Make a refused item terminal, unless the refusal clears itself.

    Called with the queue mutex held. Returns the poisoned item, or None
    when nothing was poisoned, which is the caller's signal that this
    refusal is a WAIT: nothing claimed, no attempt charged, re-checked on
    the next poll.

    The split is between a refusal about the repo's RESOLVED
    configuration and one about being unable to READ it. A ``create_prs
    = false`` repo does not stop conflicting with a promised human merge
    gate on its own, so that item needs a person and is terminal. A
    quoted boolean is a typo that clears the moment the file is fixed,
    and poison is documented as the state that is never retried
    automatically, so poisoning there would cost one terminal item per
    poll for as long as the typo stood and leave every one of them behind
    after the fix.
    """
    if gate.unreadable_section:
        return None
    queue.poison(
        candidate,
        reason=f"merge-gate conflict: {gate.refusal}",
        actor="serve",
    )
    ledger.record_terminal(poisoned=True)
    return queue.get(candidate.item_id)


def _report_merge_gate_refusal(
    root_dir: Path,
    result: CycleResult,
    candidate: QueueItem,
    gate: MergeGate,
    refused_item: QueueItem | None,
    observer: ServeObserver,
) -> None:
    """Tell the operator and the front-end about a poisoned item.

    Nothing to report when ``refused_item`` is None: the refusal was a
    wait, the item is untouched, and the caller has already put the
    message on ``CycleResult.skipped``, which is where the daemon prints
    a self-clearing refusal.

    Runs OUTSIDE the queue mutex on purpose: the writeback makes two
    ``gh`` calls at the configured timeout, and holding the mutex across
    them blocks every local queue transition (#187 F10).
    """
    if refused_item is None:
        return
    observer.err(f"{candidate.item_id}: {gate.refusal}")
    result.needs_human = True
    result.inbox_items += (
        _file_inbox_item(
            root_dir,
            kind_name="merge_gate",
            title=f"Queue item {short_item_id(candidate.item_id)} needs a merge decision",
            detail=gate.refusal,
            dedupe_key=f"queue-merge-gate:{candidate.item_id}",
            evidence={"item_id": candidate.item_id},
        ),
    )
    _report_remote_outcome(
        root_dir,
        refused_item,
        state="poison",
        detail=gate.refusal,
        observer=observer,
    )


def _record_count_failure(
    root_dir: Path,
    streak: OpenPrCountStreak | None,
    result: CycleResult,
) -> None:
    """File one inbox item once the open-PR count has been unusable N polls.

    A no-op for every other wait, and for the second and later polls of
    one streak. This is the only wait that files anything, because it is
    the only one that does not clear itself: `gh` off launchd's PATH and
    an expired `gh` token both hold forever, and until this the daemon
    waited on them with the queue unpaused, the inbox empty,
    ``needs_human`` False, the exit code 0 and one WARN line per poll in
    ``serve.err.log`` as the only evidence anywhere (#354 S5).

    It writes ``result`` rather than returning, so the caller adds no
    branch: ``serve_cycle`` is grandfathered over both complexity
    ratchets and any new branch in it is a regression the hook refuses.

    The reason comes off the STREAK, not off the caller's refusal
    string. Any wait gate's reason reaches this function, and only the
    open-PR bound's belongs in an item about counting pull requests.

    ``filed`` is set only when the inbox returns a real id, and only that
    id is appended to ``result.inbox_items``. ``_file_inbox_item``
    swallows every failure and returns ``""``, and returns ``""``
    without trying when ``[inbox] enabled = false``, so marking the
    streak before the write disarmed the alarm at the exact moment it
    was needed and put an id that names nothing into the result.
    ``needs_human`` is set either way: a human is needed whether or not
    the record of it reached the disk.

    An existing kind (``halted_run``) and a fixed dedupe key on purpose:
    no new outcome vocabulary (doctrine 6), and a repeat of a still-open
    item bumps its occurrence count rather than adding a row, so two
    daemons - or one restarted into the same fault - do not stack items.
    """
    if streak is None or not streak.should_file():
        return
    result.needs_human = True
    item_id = _file_inbox_item(
        root_dir,
        kind_name="halted_run",
        title="Continuous intake cannot count open pull requests",
        detail=(
            f"{streak.consecutive} polls in a row: {streak.reason}. The daemon "
            "is waiting and will admit nothing until the count works. If `gh` "
            "cannot run, check that it is on the daemon's PATH and "
            "authenticated; if the reason above is a full page, the repository "
            "has more open pull requests than the scan window. Either way, "
            "[serve] max_open_prs = 0 switches the bound off."
        ),
        dedupe_key="serve-open-pr-count-failure",
        evidence={"reason": streak.reason, "consecutive": streak.consecutive},
    )
    if not item_id:
        return
    streak.filed = True
    result.inbox_items += (item_id,)


def _report_remote_outcome(
    root_dir: Path,
    item: QueueItem | None,
    *,
    state: str,
    detail: str,
    observer: ServeObserver,
) -> None:
    """Tell the source front-end what happened, best effort.

    R8.6 requires the adapters be strictly additive, so this swallows
    every failure into a warning: the queue transition it describes has
    already committed locally, and a GitHub outage must not be able to
    roll it back or halt the daemon.
    """
    if item is None or item.source is not ItemSource.GITHUB:
        return
    try:
        from kstrl.intake_gh import GitHubIntakeConfig, report_outcome

        config = GitHubIntakeConfig.load(root_dir)
        if not config.enabled:
            return
        error = report_outcome(
            item,
            state=state,
            detail=detail,
            config=config,
            root_dir=root_dir,
        )
        if error:
            observer.warn(f"  writeback to {item.source_ref} failed: {error}")
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"  writeback to {item.source_ref} raised: {exc}")


def _report_reaped(
    root_dir: Path,
    queue: Queue,
    result: CycleResult,
    obs: ServeObserver,
) -> None:
    """Narrate what recovery did, and file the poisons a human must see.

    Its own function because both complexity ratchets in
    `.pre-commit-config.yaml` fail on any REGRESSION, and `serve_cycle`
    measured 31 cyclomatic / 50 cognitive before #364 added the config
    refusal above. Moving this block takes it to 29 / 48. Nothing here
    changed: the caller is where it was, between the recovery lock and
    the intake stage.
    """
    if result.swept_staging:
        obs.info(f"Swept {result.swept_staging} abandoned staging item(s)")
    for item_id in result.reaped.requeued + result.reaped.failed_for_retry:
        obs.warn(f"Reaped {item_id}: owner gone, requeued")
    for item_id in result.reaped.poisoned:
        obs.err(f"Reaped {item_id}: no attempts left, poisoned")
        result.needs_human = True
        _report_remote_outcome(
            root_dir,
            queue.get(item_id),
            state="poison",
            detail=("The run was interrupted and the item had no attempts left."),
            observer=obs,
        )
        result.inbox_items += (
            _file_inbox_item(
                root_dir,
                kind_name="halted_run",
                title=f"Queue item {short_item_id(item_id)} poisoned after an interrupted run",
                detail=(
                    "The run was interrupted and the item had no attempts left. "
                    "Inspect with `ks queue show` and requeue with "
                    "`ks queue retry --reset-attempts` if it should run again."
                ),
                dedupe_key=f"queue-poison:{item_id}",
                evidence={"item_id": item_id, "cause": "interrupted run"},
            ),
        )


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

    # 7. Charge the spend before deciding anything, so a classification
    #    bug cannot also lose the accounting. NEW run dirs are not enough
    #    to go on; `owned_run_spend` says why.
    owned_runs, owned = owned_run_spend(
        root_dir, runs_before, launch_pid=spawned[-1] if spawned else None
    )
    total = owned.cost_usd
    covered_calls = owned.cost_calls
    total_calls = owned.usage_calls

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


def _settle_unfailed(
    root_dir: Path,
    queue: Queue,
    ledger: SpendLedger,
    running: QueueItem,
    verdict: Outcome,
    pr_urls: tuple[str, ...],
    run_id: str,
    architect_run_id: str,
    obs: ServeObserver,
    result: CycleResult,
) -> bool:
    """Finish an item whose run did not fail: done, awaiting approval, or
    awaiting an answer.

    True when the cycle is over for this item (including the item having
    vanished from the queue mid-run); False sends the caller down the
    failure branch. #465 added the second state: a park is neither a
    finish nor a failure, so it is not poisoned, it does not touch the
    poison streak, and it is not retried. #644 added the third on the same
    terms, for an architect escalation: the child already filed the one
    inbox row that names `ks queue answer`, so nothing is filed here. The
    remote writeback runs after the mutex is released (#187 F10); a
    GitHub-sourced item awaiting an answer gets the ``awaiting_answer``
    label and a comment naming the local command, and the claim that
    re-runs it moves the label back to ``running``. #654 added a red base,
    which is not the item's failure either: see ``_requeue_on_red_base``.
    """
    if verdict.verdict is Verdict.RED_BASE:
        _requeue_on_red_base(root_dir, queue, running, verdict, obs, result)
        return True
    with queue_lock(root_dir, blocking=True):
        current = queue.get(running.item_id)
        if current is None:
            obs.err(f"{running.item_id} vanished mid-run")
            return True
        if verdict.verdict is Verdict.SUCCESS:
            queue.finish_ok(current, actor="serve", pr_urls=pr_urls)
            ledger.record_terminal(poisoned=False)
            state, said, detail = "done", "done", "The factory run completed."
        elif verdict.verdict is Verdict.AWAITING_APPROVAL:
            queue.await_approval(
                current, reason=verdict.reason, run_id=run_id, actor="serve", pr_urls=pr_urls
            )
            result.needs_human = True
            state, said, detail = "awaiting_approval", "awaiting approval", verdict.reason
        elif verdict.verdict is Verdict.AWAITING_ANSWER:
            queue.await_answer(
                current, reason=verdict.reason, run_id=architect_run_id, actor="serve"
            )
            result.needs_human = True
            state, detail = "awaiting_answer", verdict.reason
            said = (
                "awaiting an answer: see `ks inbox ls`, then "
                f"`ks queue answer {running.item_id} <answered spec file>`"
            )
        else:
            return False
        finished = queue.get(running.item_id)
    obs.info(f"  {running.item_id} {said}")
    _report_remote_outcome(root_dir, finished, state=state, detail=detail, observer=obs)
    return True


def _requeue_on_red_base(
    root_dir: Path,
    queue: Queue,
    running: QueueItem,
    verdict: Outcome,
    obs: ServeObserver,
    result: CycleResult,
) -> None:
    """Pause claims and requeue an item whose run refused on a red base (#654).

    Owner decision 6: a red base is repo-wide, so poisoning would burn one
    item per poll and trip the breaker. The item goes back to queued through
    failed, the only legal way out of running, and keeps the attempt it was
    charged, because its architect was paid. Claims pause on the queue's own
    marker with no ``resume_after``, so only `ks queue resume` lifts it and
    the journal records who; the pause is written first, so it holds even if
    the item moved. The poison streak is not touched. One inbox item per
    measured base commit is filed, and a later refusal on the same commit
    bumps it.
    """
    evidence = verdict.evidence
    sha = str(evidence.get("base_sha", ""))
    paused = (
        f"the base branch {evidence.get('base_branch', '')} at {sha[:12]} fails its own "
        "gates; nothing is claimed until `ks queue resume`"
    )
    with queue_lock(root_dir, blocking=True):
        queue.pause(reason=paused, actor="serve")
        current = queue.get(running.item_id)
        if current is not None:
            failed = queue.finish_failed(current, error=verdict.reason, actor="serve")
            queue.requeue(failed, reason=f"red base: {verdict.reason}", actor="serve")
        requeued = queue.get(running.item_id)
    result.paused = paused
    result.needs_human = True
    obs.warn(f"Queue paused: {paused}")
    obs.warn(f"  {running.item_id} requeued, not judged: {verdict.reason}")
    detail = (
        f"{verdict.reason}\n\nQueue item {running.item_id} is queued again. Make the "
        "base branch pass its gates, then run `ks queue resume`."
    )
    result.inbox_items += (
        _file_inbox_item(
            root_dir,
            kind_name="halted_run",
            title="Continuous intake paused: the base branch fails its own gates",
            detail=detail,
            dedupe_key=f"serve-red-base:{sha}",
            evidence={**evidence, "item_id": running.item_id},
            run_id=str(evidence.get("run_id", "")),
        ),
    )
    _report_remote_outcome(root_dir, requeued, state="failed", detail=detail, observer=obs)


def settle_approval_run(
    root_dir: Path,
    queue: Queue,
    *,
    parked_run_id: str,
    returncode: int,
    actor: str,
    observer: ServeObserver,
) -> QueueItem | None:
    """Move the queue item a merge-gate decision ran for out of awaiting_approval (#464).

    Called by `ks inbox approve` and `ks inbox reject` once the factory
    run they started has exited. The item is the one ``serve`` parked on
    ``parked_run_id``, joined by the run id ``Queue.await_approval``
    recorded rather than by counting awaiting items. A park no daemon
    made (a manual `ks factory`) has no item, and this returns None.

    Read from the manifest the run left, in this order:

    - a component still awaits approval: a pre-spend check refused the
      run, or it merged the approved component and parked a dependent.
      The item stays, linked to the manifest's current run id.
    - exit 0: done, with every PR URL the manifest records.
    - anything else (a rejection, a branch that moved, an unconfirmed
      merge): poison, with the classifier's reason. The run was not a
      queue attempt, so nothing retries it.

    The park is checked first because a refused run exits 2, and the
    classifier reads exit 2 before it reads the manifest.
    """
    linked = [
        item
        for item in queue.items((ItemState.AWAITING_APPROVAL,))
        if parked_run_id and item.last_run_id == parked_run_id
    ]
    if len(linked) != 1:
        return None
    manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
    run_id = _run_id_from_manifest(manifest_path)
    park = check_parked_merges(root_dir)
    verdict = classify_run(
        root_dir,
        run=RunOutcome(returncode=returncode),
        manifest_path=manifest_path,
        owned_run_ids=(run_id,) if run_id else (),
    )
    pr_urls = _pr_urls_from_manifest(manifest_path, True, observer)
    with queue_lock(root_dir, blocking=True):
        current = queue.get(linked[0].item_id)
        if current is None or current.state is not ItemState.AWAITING_APPROVAL:
            return None
        if not park.allowed:
            if run_id == current.last_run_id:
                return current
            queue.relink_run(current, run_id=run_id, reason=park.reason, actor=actor)
            state, detail = "awaiting_approval", park.reason
        elif verdict.verdict is Verdict.SUCCESS:
            queue.finish_ok(current, actor=actor, pr_urls=pr_urls)
            merged = ", ".join(pr_urls) or "no PR recorded"
            state, detail = "done", f"Approved at the merge gate and completed: {merged}."
        else:
            queue.poison(current, reason=verdict.reason, actor=actor)
            state, detail = "poison", verdict.reason
        settled = queue.get(current.item_id)
    observer.info(f"  queue item {current.item_id} {state}")
    _report_remote_outcome(root_dir, settled, state=state, detail=detail, observer=observer)
    return settled


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
