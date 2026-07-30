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

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from kstrl.statedir import state_dir
from kstrl.workqueue import (
    ItemState,
    MergeDisposition,
    Queue,
    QueueBudgetExhausted,
    QueueConfig,
    QueueItem,
    queue_lock,
    queue_root,
)

SERVE_LOCK_FILENAME = "serve.lock"
SPEND_FILENAME = "spend.json"

#: Retry backoff: ``base * 2 ** (attempts - 1)``, capped. Not tunable by
#: config on purpose - the cap is a safety floor on how fast an item can
#: consume its attempts, not a preference.
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_CAP_SECONDS = 1800.0


class ServeError(RuntimeError):
    """The daemon cannot run."""


class ServeLockedError(ServeError):
    """Another ``ks serve`` holds the singleton lock on this root."""


class Verdict(StrEnum):
    """What the evidence says about a finished run.

    ``UNCLASSIFIABLE`` is a first-class outcome rather than an error
    path: it is the common case for a crash, and treating it as "probably
    fine, retry" is the bug this whole module is arranged to avoid.
    """

    SUCCESS = "success"
    RETRY_INFRA = "retry_infra"
    SPEC_FAILURE = "spec_failure"
    UNCLASSIFIABLE = "unclassifiable"

    @property
    def may_retry(self) -> bool:
        """Only ONE verdict authorizes spending again."""
        return self is Verdict.RETRY_INFRA


@dataclass(frozen=True)
class Outcome:
    """A verdict plus the evidence that produced it.

    ``evidence`` is written to the inbox item, so a human deciding
    whether to requeue sees what the classifier actually read rather
    than having to trust its label.
    """

    verdict: Verdict
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunOutcome:
    """Raw result of one factory invocation, before interpretation."""

    returncode: int
    timed_out: bool = False
    #: Non-empty when the launch itself failed (binary missing, etc).
    launch_error: str = ""


class FactoryRunner(Protocol):
    """How the daemon executes one queue item.

    A Protocol so tests drive the entire loop without spawning a factory.
    That is not only a speed concern: a suite that ran the real thing
    would cost dollars per assertion.
    """

    def __call__(
        self,
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
    ) -> RunOutcome:
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _local_today() -> str:
    """Today's date in LOCAL time.

    Local rather than UTC because the budget resets "at the next day"
    from the operator's point of view; a UTC reset would land mid-evening
    for most of the world.
    """
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def next_local_midnight(now: datetime | None = None) -> str:
    """UTC ISO timestamp of the next local midnight."""
    local = (now or _utc_now()).astimezone()
    tomorrow = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return _iso(tomorrow.astimezone(UTC))


def backoff_seconds(attempts: int) -> float:
    """Delay before an item's next attempt."""
    if attempts <= 0:
        return 0.0
    grown = BACKOFF_BASE_SECONDS * float(2 ** (attempts - 1))
    return min(grown, BACKOFF_CAP_SECONDS)


@dataclass(frozen=True)
class ServeConfig:
    """``[serve]`` config: the daemon's knobs.

    ``daily_budget_usd`` lives here rather than in ``[queue]`` because
    only the daemon spends; it is nonetheless queue-LEVEL in the sense
    R8.6 means - it spans runs and it pauses the queue, as opposed to
    the per-run ``max_cost_usd`` ceiling.
    """

    poll_interval_seconds: float = 60.0
    #: 0 disables the budget entirely. Any positive value is a HARD stop.
    daily_budget_usd: float = 0.0
    #: Consecutive poisoned items that pause the whole queue. The
    #: cross-item signal per-item bounds cannot see.
    max_consecutive_poison: int = 3
    #: Hold ``caffeinate -i`` for the duration of each run so the machine
    #: does not sleep mid-factory, and sleeps freely between runs.
    caffeinate: bool = True
    #: 0 disables the per-run timeout.
    factory_timeout_seconds: float = 0.0
    #: Run unattended even when a configured budget cannot be enforced
    #: because no adapter reports cost. Explicit opt-out of the guard.
    allow_uncovered_cost: bool = False

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ServeError(
                "serve.poll_interval_seconds must be > 0, got "
                f"{self.poll_interval_seconds}"
            )
        if self.daily_budget_usd < 0:
            raise ServeError(
                f"serve.daily_budget_usd must be >= 0, got {self.daily_budget_usd}"
            )
        if self.max_consecutive_poison < 1:
            raise ServeError(
                "serve.max_consecutive_poison must be >= 1, got "
                f"{self.max_consecutive_poison}"
            )
        if self.factory_timeout_seconds < 0:
            raise ServeError(
                "serve.factory_timeout_seconds must be >= 0, got "
                f"{self.factory_timeout_seconds}"
            )

    @classmethod
    def from_env(cls) -> ServeConfig:
        defaults = cls()
        poll = os.environ.get("KSTRL_SERVE_POLL_INTERVAL")
        budget = os.environ.get("KSTRL_SERVE_DAILY_BUDGET_USD")
        poison = os.environ.get("KSTRL_SERVE_MAX_CONSECUTIVE_POISON")
        caffeinate = os.environ.get("KSTRL_SERVE_CAFFEINATE")
        timeout = os.environ.get("KSTRL_SERVE_FACTORY_TIMEOUT")
        uncovered = os.environ.get("KSTRL_SERVE_ALLOW_UNCOVERED_COST")
        return cls(
            poll_interval_seconds=(
                defaults.poll_interval_seconds if poll is None else float(poll)
            ),
            daily_budget_usd=(
                defaults.daily_budget_usd if budget is None else float(budget)
            ),
            max_consecutive_poison=(
                defaults.max_consecutive_poison if poison is None else int(poison)
            ),
            caffeinate=(
                defaults.caffeinate if caffeinate is None else caffeinate == "1"
            ),
            factory_timeout_seconds=(
                defaults.factory_timeout_seconds
                if timeout is None else float(timeout)
            ),
            allow_uncovered_cost=(
                defaults.allow_uncovered_cost
                if uncovered is None else uncovered == "1"
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> ServeConfig:
        """Precedence: env > toml > defaults; reads ``[serve]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "serve")
        defaults = cls()

        def _float(key: str, fallback: float) -> float:
            return float(section[key]) if key in section else fallback

        def _int(key: str, fallback: int) -> int:
            return int(section[key]) if key in section else fallback

        def _bool(key: str, fallback: bool) -> bool:
            return bool(section[key]) if key in section else fallback

        poll = _float("poll_interval_seconds", defaults.poll_interval_seconds)
        budget = _float("daily_budget_usd", defaults.daily_budget_usd)
        poison = _int("max_consecutive_poison", defaults.max_consecutive_poison)
        caffeinate = _bool("caffeinate", defaults.caffeinate)
        timeout = _float(
            "factory_timeout_seconds", defaults.factory_timeout_seconds,
        )
        uncovered = _bool("allow_uncovered_cost", defaults.allow_uncovered_cost)

        if "KSTRL_SERVE_POLL_INTERVAL" in os.environ:
            poll = float(os.environ["KSTRL_SERVE_POLL_INTERVAL"])
        if "KSTRL_SERVE_DAILY_BUDGET_USD" in os.environ:
            budget = float(os.environ["KSTRL_SERVE_DAILY_BUDGET_USD"])
        if "KSTRL_SERVE_MAX_CONSECUTIVE_POISON" in os.environ:
            poison = int(os.environ["KSTRL_SERVE_MAX_CONSECUTIVE_POISON"])
        if "KSTRL_SERVE_CAFFEINATE" in os.environ:
            caffeinate = os.environ["KSTRL_SERVE_CAFFEINATE"] == "1"
        if "KSTRL_SERVE_FACTORY_TIMEOUT" in os.environ:
            timeout = float(os.environ["KSTRL_SERVE_FACTORY_TIMEOUT"])
        if "KSTRL_SERVE_ALLOW_UNCOVERED_COST" in os.environ:
            uncovered = os.environ["KSTRL_SERVE_ALLOW_UNCOVERED_COST"] == "1"

        return cls(
            poll_interval_seconds=poll,
            daily_budget_usd=budget,
            max_consecutive_poison=poison,
            caffeinate=caffeinate,
            factory_timeout_seconds=timeout,
            allow_uncovered_cost=uncovered,
        )


# ---------------------------------------------------------------------------
# Daily spend ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailySpend:
    """What the queue has spent today, and how well we know it.

    ``lower_bound`` is not decoration. When it is true the real spend is
    higher than ``spent_usd`` by an amount this codebase deliberately
    does not estimate, so the budget comparison is a comparison against
    a floor. ``uncovered_calls`` says how many calls contributed nothing.
    """

    date: str = ""
    spent_usd: float = 0.0
    runs: int = 0
    lower_bound: bool = False
    uncovered_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "spent_usd": self.spent_usd,
            "runs": self.runs,
            "lower_bound": self.lower_bound,
            "uncovered_calls": self.uncovered_calls,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailySpend:
        def _num(key: str) -> float:
            value = data.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0.0
            return float(value)

        return cls(
            date=str(data.get("date") or ""),
            spent_usd=_num("spent_usd"),
            runs=int(_num("runs")),
            lower_bound=bool(data.get("lower_bound", False)),
            uncovered_calls=int(_num("uncovered_calls")),
        )


class SpendLedger:
    """Append-nothing, rewrite-atomically record of today's spend.

    Deliberately NOT a journal: the only question asked of it is "how
    much today", and a single small file keeps the pre-admission check
    cheap enough to run before every item. The per-run detail already
    lives in the run's own event stream.
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    @property
    def path(self) -> Path:
        return queue_root(self.root_dir) / SPEND_FILENAME

    def read(self, today: str | None = None) -> DailySpend:
        """Today's spend; a stale date reads as a fresh zero day.

        An unreadable or malformed ledger reads as a fresh day too, which
        is the ONE fail-open choice in this module and is called out
        here: failing closed would mean an unparseable ledger halts the
        queue permanently with no way for the daemon itself to recover.
        The exposure is bounded by ``max_attempts``, the backoff, and the
        poison breaker, and by the ledger being rewritten atomically so
        a torn write is not a normal event.
        """
        stamp = today or _local_today()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return DailySpend(date=stamp)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return DailySpend(date=stamp)
        if not isinstance(data, dict):
            return DailySpend(date=stamp)
        spend = DailySpend.from_dict(data)
        if spend.date != stamp:
            return DailySpend(date=stamp)
        return spend

    def _write(self, spend: DailySpend) -> None:
        from kstrl.workqueue import atomic_write

        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.path,
            json.dumps(spend.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )

    def charge(
        self,
        usd: float,
        *,
        lower_bound: bool = False,
        uncovered_calls: int = 0,
        today: str | None = None,
    ) -> DailySpend:
        """Add one run's reported cost to today's total.

        ``lower_bound`` is sticky for the day: once any run reported
        partial cost coverage, the day's figure is a floor and stays
        labelled as one.
        """
        stamp = today or _local_today()
        current = self.read(stamp)
        updated = DailySpend(
            date=stamp,
            spent_usd=round(current.spent_usd + max(0.0, usd), 6),
            runs=current.runs + 1,
            lower_bound=current.lower_bound or lower_bound,
            uncovered_calls=current.uncovered_calls + max(0, uncovered_calls),
        )
        self._write(updated)
        return updated


@dataclass(frozen=True)
class RunSpend:
    """One run's cost as read back from its event stream."""

    cost_usd: float = 0.0
    lower_bound: bool = False
    uncovered_calls: int = 0
    cost_calls: int = 0
    usage_calls: int = 0


def read_run_spend(root_dir: Path, run_id: str) -> RunSpend:
    """Reported cost of one run, with its coverage.

    Reads through ``kstrl.reducer`` rather than re-parsing the journals,
    so the daemon's cost figure is the same number the dashboard shows
    and the per-axis coverage flags (R8/PR #184) come along for free.
    """
    from kstrl.reducer import load_run_state

    try:
        state, _source = load_run_state(root_dir, run_id)
    except OSError:
        return RunSpend()
    return RunSpend(
        cost_usd=state.cost_usd,
        lower_bound=state.cost_is_lower_bound,
        uncovered_calls=max(0, state.usage_calls - state.cost_calls),
        cost_calls=state.cost_calls,
        usage_calls=state.usage_calls,
    )


# ---------------------------------------------------------------------------
# Classification - the money-critical decision
# ---------------------------------------------------------------------------


def _infra_casualty(component: Any) -> bool:
    """Whether a component's failure was infrastructural.

    Mirrors ``factory._infra_casualty`` exactly, and reuses the same
    ``Finding.is_infrastructure_error`` predicate rather than
    re-deriving it. Two copies of this rule that drift apart is how a
    spec failure becomes retryable, so the shared predicate is the whole
    point.
    """
    return any(f.is_infrastructure_error for f in component.findings)


def classify_run(
    root_dir: Path,
    *,
    run: RunOutcome,
    manifest_path: Path,
) -> Outcome:
    """Decide what a finished factory run means for its queue item.

    Positive evidence only. Every branch that cannot prove the failure
    was infrastructural returns a non-retrying verdict, and the reason
    string always says which branch fired so a human reading the inbox
    can audit the decision.
    """
    if run.launch_error:
        # The factory never started, so nothing was spent. Retrying is
        # free, which is what makes this safe to retry at all.
        return Outcome(
            Verdict.RETRY_INFRA,
            f"launch failed before any spend: {run.launch_error}",
            {"launch_error": run.launch_error},
        )

    if run.timed_out:
        # WE killed it. A hang is an infrastructure symptom, and
        # max_attempts plus the daily budget bound the exposure.
        return Outcome(
            Verdict.RETRY_INFRA,
            "run exceeded serve.factory_timeout_seconds and was killed",
            {"timed_out": True},
        )

    if run.returncode == 0:
        return Outcome(Verdict.SUCCESS, "factory exited 0", {"returncode": 0})

    if run.returncode < 0:
        # Killed by a signal: SIGKILL from an OOM, a suspend that took
        # the process with it, or an operator. That is affirmative
        # evidence of an EXTERNAL cause rather than a verdict on the
        # spec, so it is the one crash shape that legitimately retries.
        return Outcome(
            Verdict.RETRY_INFRA,
            f"killed by signal {-run.returncode} (external cause, not a "
            "verdict on the spec)",
            {"signal": -run.returncode},
        )

    if run.returncode == 2:
        # The factory's own "refused to proceed" code. With the run lock
        # probed before launch and --spec always supplied, the reachable
        # cause here is the architect halting on a blocker-severity spec
        # issue - a SPEC failure, and retrying it would re-run the same
        # architect against the same words.
        return Outcome(
            Verdict.SPEC_FAILURE,
            "factory exited 2: the architect halted on a blocker-severity "
            "spec issue; the spec needs a human",
            {"returncode": 2},
        )

    # Everything else needs the manifest to say something specific.
    from kstrl.manifest import Manifest

    try:
        manifest = Manifest.load(manifest_path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            f"exit {run.returncode} and the manifest could not be read "
            f"({exc}); refusing to guess whether this was infrastructural",
            {"returncode": run.returncode, "manifest": str(manifest_path)},
        )

    failed = [
        comp for comp in manifest.components
        if str(comp.status) == "failed"
    ]
    if not failed:
        # Nonzero exit with nothing blamed: unconfirmed merges, contract
        # failures, a stop mid-run. Each may well be resumable, but none
        # is an infrastructure_error, and inventing that label here is
        # exactly the fail-open shape this module refuses.
        return Outcome(
            Verdict.UNCLASSIFIABLE,
            f"exit {run.returncode} with no failed component to attribute it "
            "to (unconfirmed merge, contract failure, or an interrupted "
            "run); a human decides whether to resume",
            {
                "returncode": run.returncode,
                "statuses": sorted(
                    {str(c.status) for c in manifest.components}
                ),
            },
        )

    judged = [comp.id for comp in failed if not _infra_casualty(comp)]
    if judged:
        return Outcome(
            Verdict.SPEC_FAILURE,
            "spec-level failure: "
            + ", ".join(judged)
            + " failed on their own merits, not on infrastructure",
            {"returncode": run.returncode, "judged_failures": judged},
        )

    return Outcome(
        Verdict.RETRY_INFRA,
        "every failed component carried an infrastructure_error finding: "
        + ", ".join(comp.id for comp in failed),
        {
            "returncode": run.returncode,
            "infra_failures": [comp.id for comp in failed],
        },
    )


# ---------------------------------------------------------------------------
# Lease reaping - sleep and crash recovery
# ---------------------------------------------------------------------------


def _pid_alive(pid: int, host: str) -> bool:
    """Whether a lease holder is still running.

    A lease from ANOTHER host is treated as alive: we cannot probe a
    foreign pid, and two-machine operation is an explicit R8.6 non-goal,
    so the TTL remains the only signal there rather than us reaping work
    that may be in flight elsewhere.
    """
    if host and host != socket.gethostname():
        return True
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but is not ours. Alive as far as this check goes.
        return True
    except OSError:
        return True
    return True


@dataclass(frozen=True)
class ReapResult:
    """What one reaper pass did, for logging and tests."""

    requeued: tuple[str, ...] = ()
    poisoned: tuple[str, ...] = ()
    failed_for_retry: tuple[str, ...] = ()


def reap_leases(
    queue: Queue,
    *,
    now: datetime | None = None,
    actor: str = "reaper",
) -> ReapResult:
    """Recover items whose owner died or slept through its lease.

    The LEASED and RUNNING cases are genuinely different and that
    difference is the reason PR 1 kept two states:

    - A dead ``leased`` item spent nothing (the attempt is charged on the
      transition INTO running), so it goes back to ``queued`` untouched.
    - A dead ``running`` item spent real money. Its attempt is already
      charged, so it is treated as an infrastructure casualty - which is
      what a suspend or an OOM kill actually is - and retried only if it
      has attempts left. Otherwise it poisons.
    """
    moment = now or _utc_now()
    requeued: list[str] = []
    poisoned: list[str] = []
    failed: list[str] = []

    for item in queue.items((ItemState.LEASED,)):
        if item.lease_expired(moment) or not _pid_alive(
            item.lease_pid, item.lease_host,
        ):
            queue.requeue(
                item,
                reason="reaped: lease holder gone before any spend",
                actor=actor,
                not_before="",
            )
            requeued.append(item.item_id)

    for item in queue.items((ItemState.RUNNING,)):
        if not (
            item.lease_expired(moment)
            or not _pid_alive(item.lease_pid, item.lease_host)
        ):
            continue
        detail = (
            f"run interrupted (lease holder pid {item.lease_pid} on "
            f"{item.lease_host or 'unknown host'} is gone or its lease "
            "lapsed); classified as infrastructure"
        )
        queue.finish_failed(item, error=detail, actor=actor)
        reread = queue.get(item.item_id)
        if reread is None:
            continue
        if reread.attempts_remaining > 0:
            queue.requeue(
                reread,
                reason="reaped: retrying an interrupted run",
                actor=actor,
                not_before=_iso(
                    moment + timedelta(seconds=backoff_seconds(reread.attempts))
                ),
            )
            failed.append(item.item_id)
        else:
            queue.poison(
                reread,
                reason=(
                    f"{detail}; no attempts left "
                    f"({reread.attempts}/{reread.max_attempts})"
                ),
                actor=actor,
            )
            poisoned.append(item.item_id)

    return ReapResult(
        requeued=tuple(requeued),
        poisoned=tuple(poisoned),
        failed_for_retry=tuple(failed),
    )


# ---------------------------------------------------------------------------
# Merge disposition - the human gate must survive continuous intake
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeGate:
    """The merge-gate decision for one item.

    ``refusal`` non-empty means the item must NOT run: the autonomy
    ladder would auto-merge something that explicitly asked for a human,
    and silently proceeding is precisely the governance erosion R8.6
    lists as a failure mode.
    """

    pause_before_pr_merge: bool
    notes: tuple[str, ...] = ()
    refusal: str = ""


def resolve_merge_gate(item: QueueItem, root_dir: Path) -> MergeGate:
    """Reconcile the item's merge disposition with the autonomy ladder.

    Two directions, and they are not symmetric:

    - The item asks for AUTO_MERGE and the ladder withholds it: downgrade
      to a human gate. This is the ladder doing its job - it may always
      withhold a permission.
    - The item asks for STOP_AT_PR and the ladder's bundle forces
      ``pause_before_pr_merge=False`` (L3+): the ladder would GRANT
      auto-merge over an explicit request for a human. Refuse the item.

    The second case cannot be fixed here: ``run_factory`` assigns
    ``factory_config.pause_before_pr_merge = bundle.pause_before_pr_merge``
    unconditionally, so passing the flag would be overridden and logged
    as a "manual override ignored". Making the ladder honour a
    MORE-restrictive request is an R8.2 change, not an R8.6 one, so this
    refuses loudly instead of quietly letting a merge through.
    """
    from kstrl.autonomy import AutonomyConfig, AutonomyState, flag_bundle_for, resolve_runtime_level
    from kstrl.policy import PolicyConfig

    wants_gate = item.merge_disposition is MergeDisposition.STOP_AT_PR
    config = AutonomyConfig.load(root_dir)
    if not config.enabled:
        # No ladder: the item's own disposition is authoritative.
        return MergeGate(pause_before_pr_merge=wants_gate)

    policy = PolicyConfig.load(root_dir)
    level, clamps = resolve_runtime_level(
        AutonomyState.load(root_dir), config, policy_enabled=policy.enabled,
    )
    bundle = flag_bundle_for(level)
    notes = list(clamps)

    if not wants_gate and not bundle.auto_merge_when_green:
        notes.append(
            f"item requested auto-merge; {bundle.level.label} withholds it, "
            "so the PR waits for a human"
        )
        return MergeGate(pause_before_pr_merge=True, notes=tuple(notes))

    if wants_gate and not bundle.pause_before_pr_merge:
        return MergeGate(
            pause_before_pr_merge=True,
            notes=tuple(notes),
            refusal=(
                f"item requires a human merge gate but {bundle.level.label} "
                "auto-merges when green, and run_factory lets the ladder's "
                "bundle override the flag. Set the item to --auto-merge "
                "deliberately, or lower [autonomy] max_level, rather than "
                "having the gate removed silently."
            ),
        )

    return MergeGate(
        pause_before_pr_merge=bundle.pause_before_pr_merge, notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# The default runner: a subprocess, under caffeinate
# ---------------------------------------------------------------------------


def caffeinate_prefix(enabled: bool) -> list[str]:
    """``caffeinate -i`` when it is available and wanted.

    ``-i`` prevents idle SLEEP without keeping the display awake. Held
    only for the duration of one run (it wraps the child process, so it
    dies with it), which is what lets the laptop sleep between items
    instead of being pinned awake by the daemon itself.
    """
    if not enabled or sys.platform != "darwin":
        return []
    binary = shutil.which("caffeinate")
    return [binary, "-i"] if binary else []


def subprocess_factory_runner(
    *,
    root_dir: Path,
    spec_path: Path,
    project_name: str,
    pause_before_pr_merge: bool,
    timeout_seconds: float,
    caffeinate: bool = True,
) -> RunOutcome:
    """Run ``ks factory`` as a child process.

    A subprocess rather than an in-process ``run_factory`` call for three
    reasons: a crash or OOM in a run cannot take the daemon with it, the
    ``.kstrl/factory.lock`` flock is released by process death whatever
    happens, and a timeout is enforceable by killing a process tree.
    The cost of that choice is that classification reads artifacts from
    disk instead of holding a ``FactoryResult`` - which is also what
    makes classification work after a crash.
    """
    command = [
        *caffeinate_prefix(caffeinate),
        sys.executable, "-m", "kstrl", "factory",
        "--spec", str(spec_path),
        "--project-name", project_name,
        "--root", str(root_dir),
        "--yes",
        "--no-tui",
        "--ui", "plain",
        "--no-color",
    ]
    command.append(
        "--pause-before-pr-merge" if pause_before_pr_merge
        else "--no-pause-before-pr-merge"
    )
    env = dict(os.environ)
    env["KSTRL_NO_TUI"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=str(root_dir),
            env=env,
            timeout=timeout_seconds if timeout_seconds > 0 else None,
            check=False,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return RunOutcome(returncode=-9, timed_out=True)
    except OSError as exc:
        return RunOutcome(returncode=-1, launch_error=str(exc))
    return RunOutcome(returncode=completed.returncode)


# ---------------------------------------------------------------------------
# Admission gates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    """Whether the daemon may start another item right now."""

    allowed: bool
    reason: str = ""
    #: Set when the refusal should PAUSE the queue rather than just wait.
    pause_reason: str = ""
    resume_after: str = ""


def check_budget(
    ledger: SpendLedger, config: ServeConfig, *, today: str | None = None,
) -> Admission:
    """The daily-budget hard stop, evaluated BEFORE admitting an item.

    Checked before rather than after because after is a post-mortem: the
    point of a cap is to not spend the money.
    """
    if config.daily_budget_usd <= 0:
        return Admission(allowed=True)
    spend = ledger.read(today)
    if spend.spent_usd < config.daily_budget_usd:
        return Admission(allowed=True)
    floor = " (a FLOOR: some calls reported no cost)" if spend.lower_bound else ""
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


def check_cost_coverage(
    ledger: SpendLedger, config: ServeConfig, *, today: str | None = None,
) -> Admission:
    """Refuse to run unattended under a budget we cannot enforce.

    ``max_cost_usd`` covers only roles whose adapter reports cost, and
    the codex adapter reports tokens and no cost. The same gap makes
    ``daily_budget_usd`` UNENFORCEABLE rather than approximate. PR #184
    established that the honest response is to make the gap visible
    instead of estimating across it, so this refuses and names the
    override rather than quietly running with a cap that does nothing.

    Only fires once a run has actually reported: with no data yet there
    is no evidence of a gap, and refusing on absence of evidence would
    make the daemon unusable on a fresh repo.
    """
    if config.daily_budget_usd <= 0 or config.allow_uncovered_cost:
        return Admission(allowed=True)
    spend = ledger.read(today)
    if spend.runs == 0:
        return Admission(allowed=True)
    if spend.spent_usd > 0 and not spend.lower_bound:
        return Admission(allowed=True)
    return Admission(
        allowed=False,
        reason=(
            f"daily_budget_usd is set to ${config.daily_budget_usd:.2f} but "
            f"{spend.uncovered_calls} of this day's calls reported no cost, "
            "so the budget cannot be enforced. The unreported spend is "
            "deliberately NOT estimated. Use a cost-reporting agent, or set "
            "[serve] allow_uncovered_cost = true to run anyway."
        ),
        pause_reason="daily budget is unenforceable: no cost coverage",
    )


def consecutive_poison_count(queue: Queue) -> int:
    """How many items poisoned in a row, most recent first.

    Derived from the journal rather than a counter file: the journal is
    already the audit trail, and a separate counter is one more thing
    that can disagree with reality. Only terminal transitions count -
    a requeue or a lease in between is not a verdict.
    """
    terminal = {"done", "poison"}
    streak = 0
    for entry in reversed(queue.journal_entries()):
        to_state = str(entry.get("to") or "")
        if to_state not in terminal:
            continue
        if to_state == "poison":
            streak += 1
            continue
        break
    return streak


def check_poison_breaker(queue: Queue, config: ServeConfig) -> Admission:
    """Pause everything after a run of poisoned items.

    The failure this catches is systemic rather than per-item: if the
    base branch is broken, every run fails verification, each failure is
    a legitimate spec-level verdict, and no per-item bound ever trips.
    Only a cross-item signal notices, and by then the queue has spent
    once per item.
    """
    streak = consecutive_poison_count(queue)
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


def check_inbox_cap(root_dir: Path) -> Admission:
    """Stop admitting work when the human queue is already full.

    ``InboxConfig.open_item_cap`` was documented in R8.3 as "the backstop
    R8.6 consults before admitting more queue work"; this is that
    consultation. Producing more decisions for a human who is already
    behind is how an inbox becomes ignored.
    """
    from kstrl.inbox import Inbox, InboxConfig

    config = InboxConfig.load(root_dir)
    if not config.enabled or config.open_item_cap <= 0:
        return Admission(allowed=True)
    box = Inbox(root_dir, config)
    if not box.over_cap():
        return Admission(allowed=True)
    return Admission(
        allowed=False,
        reason=(
            f"inbox has reached its open-item cap ({config.open_item_cap}); "
            "triage before queueing more work"
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
        handle = open(lock_path, "a+")
    except OSError:
        return False
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
    handle = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ServeLockedError(
                f"another ks serve holds {lock_path}"
            ) from exc
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
    reaped: ReapResult = field(default_factory=ReapResult)
    swept_staging: int = 0
    paused: str = ""
    skipped: str = ""
    charged_usd: float = 0.0
    inbox_items: tuple[str, ...] = ()


class ServeObserver(Protocol):
    """Where the daemon narrates. Keeps the loop free of UI concerns."""

    def info(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def err(self, message: str) -> None: ...


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
    already happened, so failures here degrade to a warning. The queue
    journal remains the authoritative record either way.
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
    except (OSError, ValueError, KeyError):
        return ""


def _pause_queue(
    queue: Queue, admission: Admission, observer: ServeObserver,
) -> str:
    """Apply a pausing admission decision."""
    queue.pause(
        reason=admission.pause_reason or admission.reason,
        actor="serve",
        resume_after=admission.resume_after,
    )
    observer.warn(f"Queue paused: {admission.pause_reason or admission.reason}")
    return admission.pause_reason or admission.reason


def serve_cycle(
    root_dir: Path,
    *,
    config: ServeConfig | None = None,
    queue_config: QueueConfig | None = None,
    runner: FactoryRunner | None = None,
    observer: ServeObserver | None = None,
    now: datetime | None = None,
) -> CycleResult:
    """One poll cycle: recover, gate, maybe run exactly one item.

    One item per cycle on purpose. Concurrency here would mean two
    factory runs on one repo, and ``.kstrl/factory.lock`` exists
    precisely because that corrupts worktrees and the manifest.

    Order is deliberate. Recovery comes first so a crashed predecessor's
    work is reclaimed before anything new is admitted; every gate is
    checked before the CLAIM, not after, because a gate evaluated after
    the spend is a post-mortem.
    """
    cfg = config or ServeConfig.load(root_dir)
    qcfg = queue_config or QueueConfig.load(root_dir)
    obs: ServeObserver = observer or _NullObserver()
    queue = Queue(root_dir, qcfg)
    ledger = SpendLedger(root_dir)
    moment = now or _utc_now()
    result = CycleResult()

    queue.ensure_dirs()

    # 1. Recovery, under the mutex: staging leftovers and dead leases.
    with queue_lock(root_dir, blocking=True):
        result.swept_staging = queue.sweep_staging()
        result.reaped = reap_leases(queue, now=moment)
    if result.swept_staging:
        obs.info(f"Swept {result.swept_staging} abandoned staging item(s)")
    for item_id in result.reaped.requeued + result.reaped.failed_for_retry:
        obs.warn(f"Reaped {item_id[:12]}: owner gone, requeued")
    for item_id in result.reaped.poisoned:
        obs.err(f"Reaped {item_id[:12]}: no attempts left, poisoned")
        result.inbox_items += (_file_inbox_item(
            root_dir,
            kind_name="halted_run",
            title=f"Queue item {item_id[:12]} poisoned after an interrupted run",
            detail=(
                "The run was interrupted and the item had no attempts left. "
                "Inspect with `ks queue show` and requeue with "
                "`ks queue retry --reset-attempts` if it should run again."
            ),
            dedupe_key=f"queue-poison:{item_id}",
            evidence={"item_id": item_id, "cause": "interrupted run"},
        ),)

    # 2. An elapsed resume_after clears the pause on its own; that is what
    #    makes the daily-budget stop self-healing rather than a weekend
    #    of dead queue.
    pause = queue.pause_state()
    if pause.paused and not pause.active(moment):
        queue.resume(actor="serve")
        obs.info("Pause window elapsed; resuming intake")
    elif pause.active(moment):
        result.skipped = f"paused: {pause.reason}"
        return result

    # 3. Gates, cheapest and most consequential first.
    for admission in (
        check_poison_breaker(queue, cfg),
        check_cost_coverage(ledger, cfg),
        check_budget(ledger, cfg),
    ):
        if admission.allowed:
            continue
        if admission.pause_reason:
            result.paused = _pause_queue(queue, admission, obs)
            result.inbox_items += (_file_inbox_item(
                root_dir,
                kind_name="budget_overrun",
                title="Continuous intake paused",
                detail=admission.reason,
                dedupe_key=f"serve-pause:{_local_today()}:{admission.pause_reason[:40]}",
                evidence={"reason": admission.reason},
            ),)
        result.skipped = admission.reason
        return result

    inbox_gate = check_inbox_cap(root_dir)
    if not inbox_gate.allowed:
        obs.warn(inbox_gate.reason)
        result.skipped = inbox_gate.reason
        return result

    if factory_lock_held(root_dir):
        # Not a failure and not the item's fault: something else owns the
        # repo. Wait rather than charging an attempt.
        result.skipped = "a factory run already holds this root"
        obs.info(result.skipped)
        return result

    # 4. Claim exactly one item.
    with queue_lock(root_dir, blocking=True):
        candidate = queue.next_ready(moment)
        if candidate is None:
            result.skipped = "nothing ready"
            return result
        gate = resolve_merge_gate(candidate, root_dir)
        if gate.refusal:
            queue.poison(
                candidate,
                reason=f"merge-gate conflict: {gate.refusal}",
                actor="serve",
            )
            obs.err(f"{candidate.item_id[:12]}: {gate.refusal}")
            result.inbox_items += (_file_inbox_item(
                root_dir,
                kind_name="merge_gate",
                title=f"Queue item {candidate.item_id[:12]} needs a merge decision",
                detail=gate.refusal,
                dedupe_key=f"queue-merge-gate:{candidate.item_id}",
                evidence={"item_id": candidate.item_id},
            ),)
            result.skipped = gate.refusal
            return result
        leased = queue.lease(candidate, actor="serve")

    for note in gate.notes:
        obs.warn(f"  {note}")

    # 5. Charge the attempt, then spend. Never the other way round.
    try:
        with queue_lock(root_dir, blocking=True):
            running = queue.start(leased, actor="serve")
    except QueueBudgetExhausted as exc:
        with queue_lock(root_dir, blocking=True):
            queue.poison(leased, reason=str(exc), actor="serve")
        obs.err(str(exc))
        result.skipped = str(exc)
        return result

    result.ran_item = running.item_id
    obs.info(
        f"Running {running.item_id[:12]} ({running.title}) "
        f"attempt {running.attempts}/{running.max_attempts}, "
        f"merge gate {'on' if gate.pause_before_pr_merge else 'off'}"
    )

    run_factory_fn = runner or _default_runner(cfg)
    spec_path = queue.spec_path(running)
    project_name = running.project_name or _derive_project_name(running)
    outcome = run_factory_fn(
        root_dir=root_dir,
        spec_path=spec_path,
        project_name=project_name,
        pause_before_pr_merge=gate.pause_before_pr_merge,
        timeout_seconds=cfg.factory_timeout_seconds,
    )

    # 6. Charge the spend before deciding anything, so a classification
    #    bug cannot also lose the accounting.
    manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
    run_id = _run_id_from_manifest(manifest_path)
    spend = read_run_spend(root_dir, run_id)
    charged = ledger.charge(
        spend.cost_usd,
        lower_bound=spend.lower_bound,
        uncovered_calls=spend.uncovered_calls,
    )
    result.charged_usd = spend.cost_usd
    obs.info(
        f"  charged ${spend.cost_usd:.2f}"
        + (" (a floor: some calls reported no cost)" if spend.lower_bound else "")
        + f"; today ${charged.spent_usd:.2f}"
    )

    verdict = classify_run(root_dir, run=outcome, manifest_path=manifest_path)
    result.verdict = verdict.verdict
    result.reason = verdict.reason
    evidence = dict(verdict.evidence)
    evidence.update({
        "item_id": running.item_id,
        "run_id": run_id,
        "attempts": running.attempts,
        "max_attempts": running.max_attempts,
        "cost_usd": spend.cost_usd,
        "cost_is_lower_bound": spend.lower_bound,
    })

    with queue_lock(root_dir, blocking=True):
        current = queue.get(running.item_id)
        if current is None:
            obs.err(f"{running.item_id[:12]} vanished mid-run")
            return result
        if verdict.verdict is Verdict.SUCCESS:
            queue.finish_ok(current, actor="serve")
            obs.info(f"  {running.item_id[:12]} done")
            return result

        queue.finish_failed(current, error=verdict.reason, actor="serve")
        failed = queue.get(running.item_id)
        if failed is None:
            return result

        if verdict.verdict.may_retry and failed.attempts_remaining > 0:
            delay = backoff_seconds(failed.attempts)
            queue.requeue(
                failed,
                reason=f"retry: {verdict.reason}",
                actor="serve",
                not_before=_iso(moment + timedelta(seconds=delay)),
            )
            obs.warn(
                f"  {running.item_id[:12]} retrying in {int(delay)}s "
                f"({failed.attempts}/{failed.max_attempts} attempts used): "
                f"{verdict.reason}"
            )
            return result

        exhausted = (
            f"; no attempts left ({failed.attempts}/{failed.max_attempts})"
            if verdict.verdict.may_retry else ""
        )
        queue.poison(
            failed, reason=f"{verdict.reason}{exhausted}", actor="serve",
        )

    obs.err(f"  {running.item_id[:12]} poisoned: {verdict.reason}")
    result.inbox_items += (_file_inbox_item(
        root_dir,
        kind_name="halted_run",
        title=f"Queue item {running.item_id[:12]} poisoned",
        detail=(
            f"{verdict.reason}\n\n"
            f"Verdict: {verdict.verdict}. This item will NOT be retried "
            "automatically. Inspect with `ks queue show "
            f"{running.item_id[:12]}`."
        ),
        dedupe_key=f"queue-poison:{running.item_id}",
        evidence=evidence,
        run_id=run_id,
    ),)
    return result


def _default_runner(config: ServeConfig) -> FactoryRunner:
    """Bind the caffeinate preference into the subprocess runner."""

    def runner(
        *,
        root_dir: Path,
        spec_path: Path,
        project_name: str,
        pause_before_pr_merge: bool,
        timeout_seconds: float,
    ) -> RunOutcome:
        return subprocess_factory_runner(
            root_dir=root_dir,
            spec_path=spec_path,
            project_name=project_name,
            pause_before_pr_merge=pause_before_pr_merge,
            timeout_seconds=timeout_seconds,
            caffeinate=config.caffeinate,
        )

    return runner


def _derive_project_name(item: QueueItem) -> str:
    """A factory project name for an item that did not supply one.

    Derived from the item id rather than the title: the title is free
    text from a remote issue and would become a branch name.
    """
    return f"queue-{item.item_id.split('-')[-1]}"


def _run_id_from_manifest(manifest_path: Path) -> str:
    """The run id the factory recorded, or "" if it never got that far."""
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    run_id = data.get("run_id")
    return run_id if isinstance(run_id, str) else ""


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

    def _cycle() -> CycleResult:
        return serve_cycle(
            root_dir,
            config=cfg,
            queue_config=queue_config,
            runner=runner,
            observer=obs,
        )

    with serve_lock(root_dir):
        if once:
            return [_cycle()]

        results: list[CycleResult] = []
        while True:
            results.append(_cycle())
            if max_cycles and len(results) >= max_cycles:
                return results
            sleep(cfg.poll_interval_seconds)
