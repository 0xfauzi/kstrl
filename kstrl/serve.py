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
import warnings
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from kstrl.atomicio import atomic_write_json
from kstrl.jsonread import read_json
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.observability import read_progress_events
from kstrl.pr import GH_TIMEOUT
from kstrl.pr_body import PR_FOOTER_MARKER
from kstrl.runid import run_kind
from kstrl.serve_config import Admission, ServeConfig, ServeError, ServeObserver
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
    DailySpend,
    ServeStateError,
    SpendLedger,
    _iso,
    _local_today,
    _utc_now,
    next_local_midnight,
    owned_run_spend,
    run_dir_names,
)
from kstrl.statedir import (
    CONTROL_PR_COUNT_STREAK,
    ControlStateError,
    control_file,
    control_lock,
    ensure_control_state,
    state_dir,
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
    from kstrl.intake_gh import GitHubIntakeConfig

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


# ---------------------------------------------------------------------------
# Admission gates
# ---------------------------------------------------------------------------


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
    from kstrl.inbox import Inbox, InboxConfig

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


@dataclass(frozen=True)
class OpenPrCount:
    """How many open kstrl PRs were seen, and whether the page was full.

    ``saturated`` travels with the count because a full page makes the
    count a LOWER BOUND rather than a count: ``gh pr list`` returns at
    most ``--limit`` rows, newest first, so an older unmerged kstrl PR
    can sit outside the window. A gate that saw only the number could
    not tell "zero open" from "zero within the newest hundred", and the
    second one is exactly the condition the bound exists for. Hence a
    pair, not an ``int``.

    ``count`` is a property over ``marked_numbers`` rather than a stored
    field, so the two cannot be constructed out of agreement.
    ``tests/test_flow_control.py`` once built a fake with a non-zero
    ``count`` and an empty ``marked_numbers``, which was uncatchable
    while both were independent fields.
    """

    saturated: bool
    #: The numbers of the PRs the marker matched, in payload order. R10.10
    #: reads comments on exactly these PRs: the list and the count come
    #: from ONE pass over ONE payload, so a second lister would be a
    #: second answer to "which PRs are kstrl's".
    marked_numbers: tuple[int, ...] = ()

    @property
    def count(self) -> int:
        return len(self.marked_numbers)


def count_open_kstrl_prs(cwd: Path, *, limit: int = 100) -> OpenPrCount:
    """Open PRs whose body ENDS with :data:`PR_FOOTER_MARKER` (R10.7).

    Runs ``gh pr list --state=open --limit <limit> --json number,body``
    under ``GH_TIMEOUT``. Every failure this function CONVERTS is raised
    as ``RuntimeError`` - gh failure, timeout, missing binary,
    unparseable output, unrecognised row - because the caller decides
    what a failed count means and an unknown number of open PRs is not
    zero. That is not a promise that nothing else escapes, and a caller
    must still catch ``Exception``: ``run_gh``'s decode raises
    ``UnicodeDecodeError`` and ``json.loads`` raises ``RecursionError``
    on deeply nested input, neither of which is converted here and
    neither of which is a ``RuntimeError`` (#318). ``check_open_pr_bound``
    catches ``Exception`` for exactly that reason.

    ``--state=open`` is deliberately one argv token. Spelled as two, the
    literal ``"open"`` lands in ``kstrl/serve.py`` and is counted by the
    encoding census in ``tests/test_encoding_readers.py``, which nets
    every expression folding to ``read_text`` or ``open``. That would let
    a later commit add a genuine read and drop this flag with the census
    count unmoved.

    Anchored at the END of the body rather than matched anywhere in it.
    Both writers in ``kstrl/pr.py`` append the marker as the final line
    (``tests/test_flow_control.py`` pins that through the real writer),
    so the anchor costs no true positive and rejects every PR that
    merely quotes the footer in prose - a body discussing the constant,
    a human pasting it. A substring match counted PR #354 of this
    repository, which no kstrl opened, as a kstrl PR.

    Two directions, both stated. FALSE NEGATIVE: a PR opened before the
    footer took its current wording is not counted (the literal was a
    different one until commit ``bd19f2b``), and neither is one whose
    body a human edited. FALSE POSITIVE: a PR whose body was written by
    hand to end with exactly this line.

    Rows are validated one by one before anything is counted. A payload
    whose rows are not PR records is a refusal with the row's index, not
    a count of zero: the ``isinstance`` filter this replaced read as
    defensive and was in fact the fail-open.

    The spawn and its four transport failures belong to
    :func:`~kstrl.intake_gh.run_gh`, which is the package's one gh
    invocation. Only the shapes of a bad PAYLOAD are decided here.

    ``number`` is now validated too, alongside ``body``, because R10.10's
    polled steering channel reads comments on exactly the PRs this
    returns as ``marked_numbers``: a row whose number is absent or the
    wrong type must refuse rather than be silently skipped.
    """
    from kstrl.intake_gh import run_gh

    args = ["pr", "list", "--state=open", "--limit", str(limit), "--json", "number,body"]
    result = run_gh(args, timeout=GH_TIMEOUT, cwd=cwd)
    if not result.ok:
        raise RuntimeError(result.error)
    try:
        rows = read_json(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"gh pr list returned unparseable JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise RuntimeError(f"gh pr list returned {type(rows).__name__}, expected a list")
    marked: list[int] = []
    for index, row in enumerate(rows):
        # The TYPE of ``body`` is checked, not just its presence. A
        # validator that admits a field the parser then coerces is the
        # fail-open one field over: ``str(row["body"] or "")`` turns
        # ``42`` into ``"42"`` and ``["x"]`` into ``"['x']"``, and both
        # would have been counted as unmarked PRs rather than refused.
        # ``gh pr list --json body`` returns a string or null today; the
        # only scenario this validation is for is the one where that
        # changes. ``bool`` is excluded from the ``number`` check because
        # ``isinstance(True, int)`` is true.
        if (
            not isinstance(row, dict)
            or "body" not in row
            or not isinstance(row["body"], str | None)
            or not isinstance(row.get("number"), int)
            or isinstance(row.get("number"), bool)
        ):
            raise RuntimeError(f"gh pr list row {index} is not a PR record: {row!r:.120}")
        if (row["body"] or "").rstrip().endswith(PR_FOOTER_MARKER):
            marked.append(row["number"])
    return OpenPrCount(
        saturated=len(rows) >= limit,
        marked_numbers=tuple(marked),
    )


#: Consecutive inconclusive counts before the daemon files an item.
STREAK_THRESHOLD: Final = 3

#: Shape of ``pr_count_streak.json``. Bumped when a field changes
#: meaning; a file carrying any other value is treated as damaged rather
#: than partially read.
STREAK_SCHEMA_VERSION: Final = 1


def _warn_streak_state(path: Path, reason: str) -> str:
    """Warn that the persisted streak was rejected; return the sentence.

    Warned here and returned, so ``serve`` can put the identical text on
    the observer an operator is actually watching. Same shape as
    ``autonomy._warn_rejected_state``.
    """
    message = (
        f"serve: rejected the open-PR count streak {path} ({reason}); "
        "treating the count as already at its alarm threshold so the next "
        "unusable count files an inbox item, and leaving the file alone"
    )
    warnings.warn(message, RuntimeWarning, stacklevel=3)
    return message


@dataclass
class OpenPrCountStreak:
    """Consecutive inconclusive open-PR counts, persisted across processes.

    It exists to file ONE inbox item once the count has been unusable
    ``threshold`` polls running. A rate limit or a brief outage clears
    itself and never reaches the threshold; an expired ``gh`` token and a
    ``gh`` missing from launchd's PATH do not clear themselves, and
    before this the daemon waited on them forever with every operator
    surface reporting healthy.

    ON DISK because ``ks serve`` ships in two deployment shapes and only
    one of them is a long-lived process. ``--plist-mode keepalive`` runs
    one process that polls; ``--plist-mode interval`` runs
    ``ks serve --once`` on a ``StartCalendarInterval``, one process per
    firing. A streak that lived in the loop's frame started at 0 on every
    interval firing, so ``consecutive`` never exceeded 1, the threshold
    was unreachable, and the alarm did not exist in the mode most exposed
    to the failure it names - a ``gh`` missing from launchd's PATH.

    ``filed`` is set by :func:`_record_count_failure` AFTER the item is
    on disk, never by the poll that merely decided to file: the inbox
    write is deliberately failure-swallowing, so marking the streak first
    would throw the alarm away at the moment it is needed.
    :meth:`record_conclusive` clears both, because the next failure after
    a recovery is new information.

    ``reason`` and ``damaged`` are transient, not persisted. ``reason``
    is what the LAST inconclusive count said, and it is what the filed
    item quotes: the caller's own refusal string may have come from a
    different wait gate entirely, and an item that names the wrong cause
    is worse than none. It is also what makes filing conditional on this
    poll having actually failed to count.
    """

    threshold: int = STREAK_THRESHOLD
    consecutive: int = 0
    filed: bool = False
    reason: str = ""
    damaged: str | None = None

    def record_inconclusive(self, reason: str) -> None:
        self.consecutive += 1
        self.reason = reason

    def record_conclusive(self) -> None:
        self.consecutive = 0
        self.filed = False
        self.reason = ""

    def should_file(self) -> bool:
        """Whether THIS poll is the one that files. Asks; marks nothing.

        A predicate, not a transition. The version that set ``filed``
        here read as one atomic step and was not: the write it authorised
        happens afterwards and can fail, and the streak had already
        disarmed itself by then.

        ``reason`` non-empty is what makes this a question about the
        COUNT. Any wait gate's refusal reaches ``_record_count_failure``,
        and a streak restored at its threshold from a damaged file would
        otherwise file an open-PR-count item on the strength of a full
        inbox.
        """
        return bool(self.reason) and not self.filed and self.consecutive >= self.threshold

    @staticmethod
    def path_for(root_dir: Path) -> Path:
        return control_file(root_dir, CONTROL_PR_COUNT_STREAK)

    @classmethod
    def load(cls, root_dir: Path) -> OpenPrCountStreak:
        """Read the streak, failing toward the ALARM rather than silence.

        A missing file is first run and is streak 0: nothing has failed
        yet, and treating that as an alarm would file an item on every
        fresh install.

        Anything else unreadable - bad permissions, bytes that are not
        utf-8, JSON that will not parse, a shape or a schema version this
        code does not recognise - comes back AT the threshold with
        ``damaged`` set. The safe direction here is the opposite of
        autonomy's: this file's whole job is to make a silent failure
        loud, so a version of it we cannot read must not quietly restart
        the count from zero and re-earn three polls of silence. The next
        unusable count files immediately.

        The file is NOT rewritten while damaged; see :meth:`save`.
        """
        ensure_control_state(root_dir)
        path = cls.path_for(root_dir)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            # ``ValueError`` beside ``OSError`` because
            # ``UnicodeDecodeError`` is one and would otherwise escape a
            # handler written to fail closed (#291).
            return cls._damaged(path, f"unreadable: {exc}")
        try:
            data = read_json(raw)
        except ValueError as exc:
            return cls._damaged(path, f"malformed JSON: {exc}")
        return cls._from_payload(path, data)

    @classmethod
    def _from_payload(cls, path: Path, data: Any) -> OpenPrCountStreak:
        """Validate every field, or return the damaged streak saying which."""
        if not isinstance(data, dict):
            return cls._damaged(path, "top-level value is not an object")
        version = data.get("schema_version")
        if version != STREAK_SCHEMA_VERSION:
            return cls._damaged(
                path,
                f"schema_version is {version!r}, expected {STREAK_SCHEMA_VERSION}",
            )
        consecutive = data.get("consecutive")
        # ``bool`` is an ``int`` subclass, so ``True`` would otherwise
        # validate as a count of 1.
        if not isinstance(consecutive, int) or isinstance(consecutive, bool) or consecutive < 0:
            return cls._damaged(path, f"consecutive is not a count: {consecutive!r:.60}")
        filed = data.get("filed")
        if not isinstance(filed, bool):
            return cls._damaged(path, f"filed is not a boolean: {filed!r:.60}")
        return cls(consecutive=consecutive, filed=filed)

    @classmethod
    def _damaged(cls, path: Path, reason: str) -> OpenPrCountStreak:
        return cls(
            consecutive=STREAK_THRESHOLD,
            filed=False,
            damaged=_warn_streak_state(path, reason),
        )

    def save(self, root_dir: Path) -> str | None:
        """Persist the streak. Returns None on success, else the reason.

        REFUSES while ``damaged``. Writing a fresh count over bytes an
        operator could have inspected destroys the only evidence of the
        damage, and the next load would then find a clean file and report
        nothing. The refusal is re-reported on every save attempt, keyed
        by the file and the cause, so it does not need a record of its
        own.

        A failed write is reported and not raised. The daemon has no
        per-cycle handler, and dying over a bookkeeping file would be a
        worse outcome than the degraded alarm it causes: without the
        write, the streak is per-process again, which is the behaviour
        this file replaced. It is not the only mechanism that would
        notice - an unwritable control directory also fails
        ``SpendLedger`` on the money path, which raises.
        """
        if self.damaged is not None:
            return self.damaged
        path = self.path_for(root_dir)
        payload = {
            "schema_version": STREAK_SCHEMA_VERSION,
            "consecutive": self.consecutive,
            "filed": self.filed,
        }
        try:
            ensure_control_state(root_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            with control_lock(root_dir):
                atomic_write_json(path, payload)
        except (OSError, ControlStateError) as exc:
            return (
                f"serve: could not persist the open-PR count streak {path}: {exc}. "
                "The count restarts at zero in the next process, so launchd "
                "`interval` mode will not reach its alarm threshold."
            )
        return None


def check_open_pr_bound(
    config: ServeConfig,
    root_dir: Path,
    *,
    counter: Callable[[Path], OpenPrCount] | None = None,
    streak: OpenPrCountStreak | None = None,
) -> Admission:
    """Flow control: refuse admission while max_open_prs kstrl PRs are open.

    Skipped (allowed) when ``max_open_prs`` is 0, or when the factory
    will not open PRs at all (``[factory] create_prs = false``), because
    then there is nothing to bound. Anything that is not a usable count
    refuses admission with the error in the reason: an unknown number of
    open PRs is not zero.

    ``except Exception`` on purpose, and the config read is INSIDE it.
    Every outcome of this block that is not a number means one thing to
    this gate, and an enumeration of the exception types believed
    reachable is the defect rather than the precaution (#318). Two were
    measured escaping the previous ``except RuntimeError``, both fatal to
    a daemon that has no per-cycle handler: a ``ValueError`` from
    ``FactoryConfig.load`` on ``[factory] max_parallel = "two"``, which
    this gate re-reads every poll while only startup validates it, and a
    ``UnicodeDecodeError`` from ``run_gh``'s locale decode.

    A saturated page refuses in both directions and says which. ``count
    >= max_open_prs`` is conclusive even on a full page, so it keeps the
    ordinary reason; ``count < max_open_prs`` on a full page is not a
    fact about the repository and must not admit.

    All refusals are WAITS, not pauses. ``pause_reason`` stays empty so
    the daemon re-checks next cycle: an open PR lifts the refusal by
    being merged or closed, which needs no operator action on the queue.
    A count that stays unusable is the case a wait alone handles badly,
    which is what ``streak`` is for; see :class:`OpenPrCountStreak`.

    ``counter`` exists so tests inject a fake without putting a ``gh``
    on PATH; it is resolved inside the body rather than as a default
    argument so patching the module-level name reaches it.

    ``streak`` defaults to a throwaway rather than to ``None``. The four
    ``if streak is not None`` guards this replaced were four branches
    whose only job was to decide whether to record, and every one of
    them was a place a later edit could forget. A caller that does not
    pass one gets a streak nobody reads, which is what ``None`` meant.

    BOTH early-allow paths record a CONCLUSIVE result. A gate that is
    switched off, or that does not apply because the factory opens no
    pull requests, is not evidence that ``gh`` is broken; leaving a
    half-built streak standing across a week of ``max_open_prs = 0``
    would file on the first failure afterwards and contradict the
    "three consecutive polls" contract in the docs.
    """
    tally = streak if streak is not None else OpenPrCountStreak()
    if config.max_open_prs == 0:
        tally.record_conclusive()
        return Admission(allowed=True, reason="open-PR bound disabled")
    count_fn = counter if counter is not None else count_open_kstrl_prs
    try:
        from kstrl.factory_config import FactoryConfig

        if not FactoryConfig.load(root_dir).create_prs:
            tally.record_conclusive()
            return Admission(
                allowed=True,
                reason="open-PR bound not applicable (create_prs = false)",
            )
        counted = count_fn(root_dir)
    except Exception as exc:  # noqa: BLE001 - anything but a count is the same answer
        reason = f"cannot count open kstrl PRs: {exc}"
        tally.record_inconclusive(reason)
        return Admission(allowed=False, reason=reason)
    if counted.count >= config.max_open_prs:
        # Conclusive even on a full page: the rows already seen carry
        # the bound, and rows outside the window can only add to them.
        tally.record_conclusive()
        return Admission(
            allowed=False,
            reason=(
                f"{counted.count} kstrl PR(s) open (bound {config.max_open_prs}); "
                "waiting for review"
            ),
        )
    if counted.saturated:
        reason = (
            f"cannot count open kstrl PRs: gh returned a full page, so "
            f"{counted.count} is a lower bound and not a count"
        )
        tally.record_inconclusive(reason)
        return Admission(allowed=False, reason=reason)
    tally.record_conclusive()
    return Admission(
        allowed=True,
        reason=f"{counted.count} of {config.max_open_prs} kstrl PRs open",
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


def _load_pr_count_streak(root_dir: Path, observer: ServeObserver) -> OpenPrCountStreak:
    """Restore the streak, reporting a damaged file where an operator looks.

    :meth:`OpenPrCountStreak.load` already emits a ``RuntimeWarning``,
    which reaches stderr and therefore ``serve.err.log``. Repeating it on
    the observer is what puts it in front of somebody running
    ``ks serve`` in a terminal, which is the surface the whole S5 finding
    was about.
    """
    streak = OpenPrCountStreak.load(root_dir)
    if streak.damaged is not None:
        observer.warn(streak.damaged)
    return streak


def _save_pr_count_streak(
    streak: OpenPrCountStreak,
    root_dir: Path,
    observer: ServeObserver,
) -> None:
    """Persist the streak after a cycle, reporting a refusal or a failure."""
    refusal = streak.save(root_dir)
    if refusal is not None:
        observer.warn(refusal)


def _run_intake(
    root_dir: Path,
    queue: Queue,
    observer: ServeObserver,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pull remote work in, then act on steering comments. Best effort.

    ONE config read, and - when either half is on - ONE resolution of
    the checkout's own repo, shared by both: `enabled` admits labelled
    issues and spends money, `steer_enabled` reads comments and writes
    the operator's checkout. Coupling them would mean an operator who
    wants only the steering channel has to switch on issue intake.

    #231's simplify pass (B2) moved the repo resolution here from `sync`
    and `poll_steering`, which each used to resolve it independently -
    a second `gh repo view` on any cycle where both are on - and moved
    the open-PR count steering needs here too, which is what deletes
    `poll_steering`'s deferred `from kstrl.serve import
    count_open_kstrl_prs`, the inward import that made this module
    import `intake_github` import this module back.

    Strictly additive, like the adapter itself: every failure is recorded
    and the cycle continues to drain whatever is already queued. A GitHub
    outage must not stop local work.
    """
    try:
        from kstrl.intake_gh import GitHubIntakeConfig

        config = GitHubIntakeConfig.load(root_dir)
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"GitHub intake raised: {exc}")
        return (), (str(exc),)
    if not config.enabled and not config.steer_enabled:
        return (), ()

    from kstrl.intake_github import checkout_repo

    repo, repo_error = checkout_repo(config, root_dir)
    if repo_error:
        observer.warn(f"GitHub intake raised: {repo_error}")
        return (), (repo_error,)

    marked_numbers: tuple[int, ...] = ()
    if config.steer_enabled:
        try:
            marked_numbers = count_open_kstrl_prs(root_dir).marked_numbers
        except Exception as exc:  # noqa: BLE001 - an unknown PR list is not an empty one
            observer.warn(f"GitHub steering raised: could not list the open kstrl PRs: {exc}")

    enqueued, errors = _sync_remote_issues(root_dir, queue, observer, config, repo)
    _run_steering(root_dir, queue, observer, config, repo, marked_numbers)
    return enqueued, errors


def _sync_remote_issues(
    root_dir: Path,
    queue: Queue,
    observer: ServeObserver,
    config: GitHubIntakeConfig,
    repo: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pull remote work into the queue, best effort.

    Review #189 F1: without this the daemon drained a queue nothing could
    fill. `ks queue sync` existed but only as a manual command, so an
    installed LaunchAgent could never admit a labelled issue - the
    adapter and the daemon were each correct and their composition did
    nothing.

    `repo` (#231 B2): the checkout's repo, already resolved once by
    `_run_intake` and passed through to `sync`'s `local_repo`, so `sync`
    does not resolve it a second time.

    Strictly additive, like the adapter itself: every failure is recorded
    and the cycle continues to drain whatever is already queued. A GitHub
    outage must not stop local work.
    """
    if not config.enabled:
        return (), ()
    try:
        from kstrl.intake_github import Decision
        from kstrl.intake_github import sync as intake_sync

        result = intake_sync(
            queue,
            config,
            root_dir,
            # Poll and authorize unlocked; take the queue mutex only for
            # the local commit, so a slow GitHub cannot block
            # `ks queue pause` or any other transition (#189 N1).
            commit_guard=lambda: queue_lock(root_dir, blocking=True),
            local_repo=repo,
        )
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"GitHub intake raised: {exc}")
        return (), (str(exc),)

    for ref in result.enqueued:
        observer.info(f"Intake queued {ref}")
    for error in result.errors:
        observer.warn(f"  intake: {error}")
    # #188 simplify pass item 2: a refusal only ``ks queue sync``/`--dry-run`
    # could show was invisible from the daemon's own narration.
    for entry in result.planned:
        if entry.decision is Decision.REFUSE_UNAUTHORIZED:
            observer.warn(f"  intake refused {entry.issue.source_ref(result.repo)}: {entry.reason}")
    return result.enqueued, result.errors


def _run_steering(
    root_dir: Path,
    queue: Queue,
    observer: ServeObserver,
    config: GitHubIntakeConfig,
    repo: str,
    marked_numbers: tuple[int, ...],
) -> None:
    """Act on `/memory` and `/iterate` comments on open kstrl PRs (#231).

    Narration only: the effects are the memory file, the queue and the
    acknowledgement comments. Every refusal is spoken, never silent,
    which is #188's rule applied to the same surface.

    `repo` and `marked_numbers` (#231 B2): resolved once by `_run_intake`
    and passed straight through, so `poll_steering` need not resolve
    either itself.

    No `steer_enabled` check here. `poll_steering` returns an empty
    result as its first statement when the switch is off, above the
    `ProcessedLedger` construction, and one gate with one home is the
    point (Decision 7).

    Returns None, so steering failures reach the observer and NOT
    `CycleResult.sync_errors`. Deliberate: `sync_errors` is the remote
    INTAKE's field, read by the CLI and the TUI as "what the issue
    adapter could not admit", and folding a comment-poll failure into it
    would make a steering outage read as an intake outage. Adding a
    `CycleResult` field instead would mean editing `serve_cycle`'s body,
    which the ratchet forbids.
    """
    try:
        from kstrl.intake_steering import poll_steering

        result = poll_steering(
            config,
            root_dir,
            queue,
            repo=repo,
            marked_numbers=marked_numbers,
            commit_guard=lambda: queue_lock(root_dir, blocking=True),
        )
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"GitHub steering raised: {exc}")
        return
    for key in result.applied:
        observer.info(f"Steering applied {key}")
    for item_id in result.enqueued:
        observer.info(f"Steering queued a re-run as {item_id}")
    for key, reason in result.skipped.items():
        observer.warn(f"  steering skipped {key}: {reason}")
    for error in result.errors:
        observer.warn(f"  steering: {error}")


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
    from kstrl.stack import NO_STACK, StackRefused, confirmed_stack, unconfirmed_lines

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
