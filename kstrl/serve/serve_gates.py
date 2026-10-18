"""Admission: each check that can refuse the next claim before money is spent."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.events import read_progress_events
from kstrl.io import (
    state_dir,
)
from kstrl.plan import ComponentStatus, Manifest
from kstrl.queue import Queue, queue_lock
from kstrl.serve.serve_config import Admission, ServeConfig, ServeObserver
from kstrl.serve.serve_github import OpenPrCountStreak, check_open_pr_bound
from kstrl.serve.serve_spend import (
    DailySpend,
    SpendLedger,
    next_local_midnight,
)

if TYPE_CHECKING:
    pass


def check_budget(
    ledger: SpendLedger,
    config: ServeConfig,
    *,
    today: str | None = None,
) -> Admission:
    """The daily-budget hard stop, evaluated BEFORE admitting an item.

    Checked before rather than after because after is a post-mortem: the
    point of a cap is to not spend the money.

    The cap is a LOWER-BOUND cap whenever coverage is partial: it fires
    at or after the threshold, never before. That is a real safety
    property but not an exact one, and the reason string says so rather
    than presenting a floor as a measurement.
    """
    if config.daily_budget_usd <= 0:
        return Admission(allowed=True)
    spend = ledger.read(today)
    if spend.spent_usd < config.daily_budget_usd:
        return Admission(allowed=True)
    floor = _floor_note(spend)
    return Admission(
        allowed=False,
        reason=(
            f"daily budget reached: ${spend.spent_usd:.2f} of "
            f"${config.daily_budget_usd:.2f} over {spend.runs} run(s){floor}"
        ),
        pause_reason=(
            f"daily budget ${config.daily_budget_usd:.2f} reached "
            f"(${spend.spent_usd:.2f} spent{floor})"
        ),
        resume_after=next_local_midnight(),
    )


def _floor_note(spend: DailySpend) -> str:
    """Say out loud when a total is a floor, and why."""
    if not spend.lower_bound:
        return ""
    parts: list[str] = []
    if spend.uncovered_calls:
        parts.append(f"{spend.uncovered_calls} call(s) reported no cost")
    if spend.unmetered_phases:
        parts.append("unmetered: " + ", ".join(spend.unmetered_phases))
    return f" (a FLOOR: {'; '.join(parts)})"


def _positive(value: object) -> bool:
    """A JSON number above zero; a bool is not a number here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _recorded_cost_evidence(root_dir: Path) -> tuple[Path, int, bool]:
    """What this repo's progress log says about cost reporting (#464).

    Returns the log's path, the agent calls its ``component_usage``
    events record, and whether any of them reported a cost. An event
    reported a cost when its ``cost_calls`` is positive, or, for a
    payload written before ``cost_calls`` existed, when its ``cost_usd``
    is positive: a dollar figure above zero exists only because a call
    reported one.

    Every ``ks factory`` run writes this file unless
    ``progress_log_enabled`` is off or ``--progress-log`` moved it, so it
    holds evidence from before the daemon's first run. A missing or
    unreadable log reads as no calls, which refuses.
    """
    path = state_dir(root_dir) / "progress.jsonl"
    calls = 0
    reported = False
    for event in read_progress_events(path):
        data = event.get("data")
        if event.get("event") != "component_usage" or not isinstance(data, dict):
            continue
        raw_calls = data.get("calls")
        if isinstance(raw_calls, int) and _positive(raw_calls):
            calls += raw_calls
        reported = reported or _positive(data.get("cost_calls")) or _positive(data.get("cost_usd"))
    return path, calls, reported


def check_cost_coverage(
    ledger: SpendLedger,
    config: ServeConfig,
    *,
    today: str | None = None,
) -> Admission:
    """Refuse to run under a budget nothing shows can fire.

    Three cases, and only the third is unenforceable:

    - full coverage: the cap is exact.
    - PARTIAL coverage: the cap is a lower-bound cap. It still fires,
      just later than the threshold. Allowed, and labelled a floor
      everywhere it is reported.
    - ZERO coverage: no call has ever reported a cost figure, so the
      total stays at $0 forever and the cap can never fire. Refused.

    The distinction is drawn from CALL COUNTS, not from whether the
    dollar total is positive: a fully-metered run that legitimately cost
    $0 has perfect coverage (#186 F9).

    Evaluated before the first claim, so an unenforceable budget is
    caught without spending a run to discover it (#186 F8). Two sources
    of evidence: the ledger's ``cost_coverage_seen`` flag, which only a
    daemon run sets, and the repo's progress log, which every
    ``ks factory`` run writes (#464). Before #464 only the flag counted,
    so the first ``ks serve`` refused on a repo whose manual runs had
    reported cost. The refusal names the log it read, and a repo with no
    recorded call at all is told to record one rather than to switch
    the protection off.
    """
    if config.daily_budget_usd <= 0 or config.allow_uncovered_cost:
        return Admission(allowed=True)
    state = ledger.read_state(today)
    if state.cost_coverage_seen:
        return Admission(allowed=True)
    path, calls, reported = _recorded_cost_evidence(ledger.root_dir)
    if reported:
        return Admission(allowed=True)
    budget = f"daily_budget_usd is ${config.daily_budget_usd:.2f}"
    if calls == 0:
        return Admission(
            allowed=False,
            reason=(
                f"{budget} but no agent call is recorded on this repo yet: the "
                f"daemon has run nothing and {path} holds no component_usage "
                "event, so nothing shows the configured agent reports cost. "
                "The unreported spend is deliberately NOT estimated. Run "
                "`ks factory` on this repo once so its calls record their "
                "cost in that log, then run `ks queue resume`, because this "
                "refusal pauses the queue, and start `ks serve` again."
            ),
            pause_reason="daily budget is unproven: no agent call recorded yet",
        )
    return Admission(
        allowed=False,
        reason=(
            f"{budget} but none of the {calls} agent call(s) recorded in {path} "
            "reported a cost figure, and no daemon run has either, so the cap "
            "can never fire. The unreported spend is deliberately NOT "
            "estimated. Use a cost-reporting agent (the codex adapter reports "
            "tokens and no cost), or set [serve] allow_uncovered_cost = true "
            "to accept an unenforceable budget."
        ),
        pause_reason="daily budget is unenforceable: no cost coverage",
    )


def consecutive_poison_count(ledger: SpendLedger) -> int:
    """Poisons in a row, from the daemon's authoritative state.

    Read from :class:`ServeState`, not from the queue journal. The
    journal is best-effort by design (``Queue._journal`` swallows append
    failures; ``journal_entries`` returns ``[]`` on any read error), so
    deriving a spend backstop from it meant an unreadable journal
    reported a zero streak and re-allowed spending (#186 F5).
    """
    return ledger.read_state().consecutive_poison


def check_poison_breaker(ledger: SpendLedger, config: ServeConfig) -> Admission:
    """Pause everything after a run of poisoned items.

    The failure this catches is systemic rather than per-item: if the
    base branch is broken, every run fails verification, each failure is
    a legitimate spec-level verdict, and no per-item bound ever trips.
    Only a cross-item signal notices, and by then the queue has spent
    once per item.
    """
    streak = consecutive_poison_count(ledger)
    if streak < config.max_consecutive_poison:
        return Admission(allowed=True)
    return Admission(
        allowed=False,
        reason=f"{streak} items poisoned in a row",
        pause_reason=(
            f"{streak} consecutive items poisoned (limit "
            f"{config.max_consecutive_poison}); something systemic is "
            "failing, not one bad spec"
        ),
    )


def check_parked_merges(root_dir: Path) -> Admission:
    """Wait while the manifest holds work parked at the merge gate (#465).

    Claiming another item runs `ks factory --spec`, which writes a new
    ``scripts/kstrl/manifest.json`` over the one that records the park,
    and the approval then has nothing to merge. The wait clears itself:
    `ks inbox approve` or `ks inbox reject` re-enters the factory, which
    moves the component out of AWAITING_APPROVAL.

    A manifest that is missing or cannot be read ADMITS, and that is a
    reading of the mechanism rather than a fail-open: what this protects
    is a park `ks inbox approve` can act on, and approve loads the same
    file with the same ``Manifest.load`` and refuses when it cannot. A
    file nothing can approve holds nothing a new run could destroy, and
    serve claimed over such a file before #465.
    """
    path = root_dir / "scripts" / "kstrl" / "manifest.json"
    try:
        manifest = Manifest.load(path)
    except Exception as exc:  # noqa: BLE001 - nothing approvable is the same answer
        return Admission(allowed=True, reason=f"no approvable park in {path}: {exc}")
    if manifest.plan_awaiting_approval:
        return Admission(
            allowed=False,
            reason=(
                f"a plan waits for approval in {path} (#602): `ks inbox approve <id>` "
                "runs it, `ks inbox reject <id> --comment ...` refuses it; a new item "
                "would overwrite that manifest"
            ),
        )
    parked = [
        comp.id
        for comp in manifest.components
        if comp.status == ComponentStatus.AWAITING_APPROVAL.value
    ]
    if not parked:
        return Admission(allowed=True)
    return Admission(
        allowed=False,
        reason=(
            f"{', '.join(parked)} waits at the merge gate in {path}: `ks inbox "
            "approve <id>` merges it and continues the run, `ks inbox reject "
            "<id> --comment ...` fails it; a new item would overwrite that manifest"
        ),
    )


def check_inbox_cap(root_dir: Path) -> Admission:
    """Stop admitting work when the human queue is already full.

    ``InboxConfig.open_item_cap`` was documented in R8.3 as "the backstop
    R8.6 consults before admitting more queue work"; this is that
    consultation. Producing more decisions for a human who is already
    behind is how an inbox becomes ignored.

    Unparseable inbox lines count as OPEN here (#190). The fold skips
    them by design so one torn write cannot hide the backlog from `ks
    inbox`, but a skipped emission line undercounts open items and a cap
    sitting on that tolerant count admits work past N - a safety gate
    failing open. Only this gate pays the stricter price; the display
    path keeps the tolerant fold.

    Open and unparseable counts come from ONE ``Inbox.scan()`` snapshot
    so a concurrent torn append cannot split them into an admitting
    world. A whole-file read/decode failure is its own state: refuse
    regardless of the configured cap - collapsing it to one skipped
    line would re-admit under any cap greater than one.
    """
    from kstrl.owner import Inbox, InboxConfig

    try:
        config = InboxConfig.load(root_dir)
    except Exception as exc:  # noqa: BLE001 - anything but a config is the same answer
        # Same rule as `check_open_pr_bound`: the read is INSIDE the
        # guard, the caught set is the whole surface rather than the
        # types believed reachable, and a cap that cannot be evaluated
        # refuses. `serve` has no per-cycle handler, so letting this out
        # stops the daemon on an operator's typo (#318).
        return Admission(
            allowed=False,
            reason=f"[inbox] cannot be read, so the open-item cap cannot be evaluated: {exc}",
        )
    if not config.enabled or config.open_item_cap <= 0:
        return Admission(allowed=True)
    scan = Inbox(root_dir, config).scan()
    if scan.unreadable:
        return Admission(
            allowed=False,
            reason=(
                "inbox is unreadable (.kstrl/inbox.jsonl could not be "
                "read or decoded); refusing admission until the log is "
                "inspectable - the open-item cap fails closed, #190"
            ),
        )
    open_count = scan.open_count()
    garbled = scan.unparseable_count()
    if open_count + garbled < config.open_item_cap:
        return Admission(allowed=True)
    detail = (
        f" ({garbled} unparseable inbox line(s) counted as open - the cap "
        "fails closed, #190; inspect .kstrl/inbox.jsonl to clear them)"
        if garbled
        else ""
    )
    return Admission(
        allowed=False,
        reason=(
            f"inbox has reached its open-item cap ({config.open_item_cap}); "
            f"triage before queueing more work{detail}"
        ),
    )


def factory_lock_held(root_dir: Path) -> bool:
    """Whether a factory run already owns this root.

    Probed BEFORE launching rather than discovered from an exit code:
    ``ks factory`` exits 2 both for a held lock and for an architect
    halt on a blocker-severity spec, and those two need opposite
    treatment. Checking here keeps exit 2 unambiguous.
    """
    lock_path = state_dir(root_dir) / "factory.lock"
    if not lock_path.exists():
        return False
    try:
        import fcntl
    except ImportError:
        return False
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        # Fail CLOSED: an unopenable lock file is not evidence that no
        # run owns this root (#186 F6).
        return True
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _pause_queue(
    queue: Queue,
    admission: Admission,
    observer: ServeObserver,
    root_dir: Path,
) -> str:
    """Apply a pausing admission decision, under the queue mutex.

    Taken under the lock for the same reason the elapsed-pause clear is
    (#186 F7): a pause write that races an operator\u2019s write can lose one
    of them, and losing the operator\u2019s is the dangerous direction.
    """
    with queue_lock(root_dir, blocking=True):
        queue.pause(
            reason=admission.pause_reason or admission.reason,
            actor="serve",
            resume_after=admission.resume_after,
        )
    observer.warn(f"Queue paused: {admission.pause_reason or admission.reason}")
    return admission.pause_reason or admission.reason


def _stack_wait(root_dir: Path) -> str | None:
    """Why this cycle claims nothing until a person confirms the ``[stack]``, or None (#696).

    A wait, before the claim: no attempt is charged and nothing is
    poisoned, so the queued item runs on the first poll after
    ``ks inbox approve``. The one stack_confirmation item is filed on the
    first poll; a later poll bumps it rather than filing a second. With no
    ``[stack]`` at all every run would refuse (slice 4), so the cycle waits
    for one; nothing is filed, because there is no text to confirm.
    """
    from kstrl.project import NO_STACK, StackRefused, confirmed_stack, unconfirmed_lines

    try:
        if confirmed_stack(root_dir) is None:
            return f"waiting, nothing claimed: {NO_STACK}"
    except StackRefused as refused:
        return "waiting, nothing claimed: " + " ".join(unconfirmed_lines(root_dir, refused.stack))
    except Exception as exc:  # noqa: BLE001 - a [stack] serve cannot read is the same wait (#364)
        return f"waiting, nothing claimed: the [stack] in kstrl.toml cannot be read: {exc}"
    return None


def _wait_gate_refusal(
    root_dir: Path,
    config: ServeConfig,
    obs: ServeObserver,
    streak: OpenPrCountStreak | None = None,
) -> str | None:
    """The gates that make the cycle WAIT, in evaluation order, or None.

    These four sit outside the ``gates`` tuple and share a shape: none
    pauses the queue, none charges the item an attempt. Each is a
    condition that clears itself, so the cycle skips and re-checks on
    the next poll.

    ``None`` rather than ``""`` for "nothing refused". A gate returning
    an empty reason is a refusal, and the caller of this function
    branches on its result to decide whether to SPEND; overloading the
    empty string would turn one such refusal into an admission with
    nothing anywhere to read.

    These are ordered by cost. The inbox cap reads one local file, the
    factory lock takes one flock, the parked-merge check (#465) reads the
    manifest, and the open-PR bound reaches GitHub, so the bound is
    evaluated last and only once the other three admit. The parked-merge
    check follows the lock on purpose: while an approval's factory run
    holds the lock, "a factory run already holds this root" is the
    accurate reason.
    That is also why these are not members of the ``gates`` tuple: it is
    built eagerly, so every element is evaluated before the loop reads
    the first refusal, and a ``gh`` call per poll behind an
    already-refusing budget is a cost with no purchaser (R10.7).

    ``streak`` is recorded here and read by the caller, which owns the
    ``CycleResult`` an inbox item has to be reported in. A count that
    has been unusable ``streak.threshold`` polls running is the one
    shape a wait handles badly, because it never clears and every other
    operator surface reads healthy.
    """
    inbox_gate = check_inbox_cap(root_dir)
    if not inbox_gate.allowed:
        obs.warn(inbox_gate.reason)
        return inbox_gate.reason

    stack_wait = _stack_wait(root_dir)
    if stack_wait is not None:
        obs.warn(stack_wait)
        return stack_wait

    if factory_lock_held(root_dir):
        # Not a failure and not the item's fault: something else owns the
        # repo. Wait rather than charging an attempt. This is a courtesy
        # check only - it cannot make exit 2 unambiguous, which is why
        # classify_run reads the child's output instead (#186 F6).
        reason = "a factory run already holds this root"
        obs.info(reason)
        return reason

    parked_gate = check_parked_merges(root_dir)
    if not parked_gate.allowed:
        obs.warn(parked_gate.reason)
        return parked_gate.reason

    pr_gate = check_open_pr_bound(config, root_dir, streak=streak)
    if not pr_gate.allowed:
        obs.warn(pr_gate.reason)
        return pr_gate.reason

    return None
