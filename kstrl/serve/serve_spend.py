"""What the daemon spent: the per-day ledger on disk and the spend of the run dirs a launch
owns.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from kstrl.events import ARCHITECT_COMPONENT, ARCHITECT_ROLE
from kstrl.io import (
    CONTROL_SPEND,
    control_file,
    control_lock,
    control_untrusted_reason,
    ensure_control_state,
    read_json,
    run_kind,
    state_dir,
)
from kstrl.serve.serve_config import ServeError


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
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return _iso(tomorrow.astimezone(UTC))


class ServeStateError(ServeError):
    """The daemon's own state file could not be read.

    A distinct error because every consumer must FAIL CLOSED on it.
    Review #186 F4: the ledger previously read an unparseable file as a
    fresh zero day, so charging $9, corrupting the file, and setting a
    $5 budget allowed another run - and kept allowing them. The other
    backstops do not bound that: they are per-item, and this is the only
    queue-WIDE spend limit.
    """


@dataclass(frozen=True)
class DailySpend:
    """What the queue has spent today, and how well we know it.

    Coverage is recorded as CALL COUNTS, not inferred from whether the
    dollar total is positive. Review #186 F9: a fully-metered run that
    legitimately cost $0 was reported as having no cost coverage, and a
    launch failure that metered nothing counted as a run.

    ``covered_calls``/``total_calls`` make the three cases distinct:
    full coverage (equal), partial coverage (a floor - the cap still
    fires, just late), and ZERO coverage (the cap can never fire at all).
    Only the third is unenforceable.

    ``unmetered_phases`` names phases known to spend without reporting
    anything. What goes in it is derived per launch by
    :attr:`RunSpend.unmetered_phases`, which states when and why. Naming
    a phase there is the honest alternative to estimating it (#186 F3).
    """

    date: str = ""
    spent_usd: float = 0.0
    runs: int = 0
    covered_calls: int = 0
    total_calls: int = 0
    unmetered_phases: tuple[str, ...] = ()

    @property
    def uncovered_calls(self) -> int:
        return max(0, self.total_calls - self.covered_calls)

    @property
    def lower_bound(self) -> bool:
        """Whether the real spend exceeds ``spent_usd`` by an unmeasured amount."""
        return self.uncovered_calls > 0 or bool(self.unmetered_phases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "spent_usd": self.spent_usd,
            "runs": self.runs,
            "covered_calls": self.covered_calls,
            "total_calls": self.total_calls,
            "unmetered_phases": list(self.unmetered_phases),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailySpend:
        def _num(key: str) -> float:
            value = data.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0.0
            return float(value)

        raw_phases = data.get("unmetered_phases")
        phases = (
            tuple(str(p) for p in raw_phases if isinstance(p, str))
            if isinstance(raw_phases, list)
            else ()
        )
        return cls(
            date=str(data.get("date") or ""),
            spent_usd=_num("spent_usd"),
            runs=int(_num("runs")),
            covered_calls=int(_num("covered_calls")),
            total_calls=int(_num("total_calls")),
            unmetered_phases=phases,
        )


@dataclass(frozen=True)
class ServeState:
    """Everything the daemon must remember between cycles.

    The poison streak lives HERE rather than being derived from the
    queue journal. Review #186 F5: ``Queue._journal`` deliberately
    swallows append failures and ``journal_entries`` returns ``[]`` on
    any read error, so making the journal unreadable reported a zero
    streak and re-allowed spending while three poisoned items sat on
    disk. A money backstop cannot be built on narration the queue
    explicitly permits itself to lose.
    """

    spend: DailySpend = field(default_factory=DailySpend)
    consecutive_poison: int = 0
    #: Set once a daemon run has reported a cost figure. Until then
    #: ``check_cost_coverage`` also reads the repo's progress log (#464),
    #: and refuses to claim work when neither shows a call that reported
    #: a cost (#186 F8) rather than discovering it after a run.
    cost_coverage_seen: bool = False
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "spend": self.spend.to_dict(),
            "consecutive_poison": self.consecutive_poison,
            "cost_coverage_seen": self.cost_coverage_seen,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServeState:
        raw_spend = data.get("spend")
        spend = DailySpend.from_dict(raw_spend) if isinstance(raw_spend, dict) else DailySpend()
        streak = data.get("consecutive_poison", 0)
        return cls(
            spend=spend,
            consecutive_poison=(
                streak if isinstance(streak, int) and not isinstance(streak, bool) else 0
            ),
            cost_coverage_seen=bool(data.get("cost_coverage_seen", False)),
        )


class SpendLedger:
    """Atomic store for :class:`ServeState` in the XDG control directory.

    Rewritten whole on every update rather than appended: the only
    questions asked of it are "how much today", "how many poisons in a
    row", and "has cost ever been reported", all of which must be cheap
    enough to evaluate before every item.
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    @property
    def path(self) -> Path:
        return control_file(self.root_dir, CONTROL_SPEND)

    def read_state(self, today: str | None = None) -> ServeState:
        """Load state, rolling the spend over on a new local day.

        Raises :class:`ServeStateError` for every read failure EXCEPT a
        missing file, which is the genuine first-run case. Failing closed
        here is the same correction PR #185 F7 applied to the pause
        marker: a file we cannot parse is not evidence that spending is
        safe. After a failed/partial migrate (legacy still present, or
        control dir inaccessible), missing XDG is NOT first-run - raise
        rather than zero the budget.

        "Every read failure" includes the DECODE, which until #320 it did
        not: ``UnicodeDecodeError`` is a ``ValueError``, so one non-utf-8
        byte in the ledger walked straight past the ``OSError`` clause
        below and out of a money path whose entire argument is that it
        refuses to spend against a total it could not read.
        """
        ensure_control_state(self.root_dir)
        untrusted = control_untrusted_reason(self.root_dir)
        if untrusted is not None:
            raise ServeStateError(
                f"refusing to read the daemon spend ledger: {untrusted}. "
                "Fix the control-state location or move the "
                "legacy `.kstrl/` control files before spending."
            )
        stamp = today or _local_today()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ServeState(spend=DailySpend(date=stamp))
        except OSError as exc:
            raise ServeStateError(
                f"cannot read the daemon spend ledger {self.path}: {exc}. "
                "Refusing to spend against an unknown daily total; fix the "
                "file's permissions or move it aside to start a fresh day."
            ) from exc
        except UnicodeDecodeError as exc:
            # Its own remedy, because it is its own problem: the file is
            # readable and the bytes are wrong, so chmod and chown are
            # the wrong advice. kstrl writes this ledger as ASCII JSON,
            # so a byte that will not decode means something else edited
            # it.
            raise ServeStateError(
                f"the daemon spend ledger {self.path} is not valid UTF-8 ({exc}). "
                "Refusing to spend against an unknown daily total; re-save it "
                "as UTF-8 or move it aside to start a fresh day."
            ) from exc
        try:
            data = read_json(raw)
        except json.JSONDecodeError as exc:
            raise ServeStateError(
                f"the daemon spend ledger {self.path} is malformed ({exc}). "
                "Refusing to spend against an unknown daily total; inspect "
                "it and move it aside to start a fresh day."
            ) from exc
        if not isinstance(data, dict):
            raise ServeStateError(
                f"the daemon spend ledger {self.path} is not an object. "
                "Refusing to spend against an unknown daily total."
            )
        state = ServeState.from_dict(data)
        if state.spend.date != stamp:
            # A new day resets the SPEND only. The poison streak and the
            # coverage flag are not daily facts.
            return replace(state, spend=DailySpend(date=stamp))
        return state

    def read(self, today: str | None = None) -> DailySpend:
        """Today's spend. Raises ServeStateError like ``read_state``."""
        return self.read_state(today).spend

    def _write_unlocked(self, state: ServeState) -> None:
        """Atomic rewrite; caller must already hold ``control_lock``."""
        from kstrl.queue import atomic_write

        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            path,
            json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )

    def _write(self, state: ServeState) -> None:
        ensure_control_state(self.root_dir)
        with control_lock(self.root_dir):
            self._write_unlocked(state)

    def charge(
        self,
        usd: float,
        *,
        covered_calls: int = 0,
        total_calls: int = 0,
        unmetered_phases: Sequence[str] = (),
        metered_run: bool = True,
        today: str | None = None,
    ) -> DailySpend:
        """Add one run's reported cost and coverage to today's total.

        ``metered_run=False`` records the spend without counting a run,
        for a launch that never reached an agent (#186 F9: a launch
        failure previously incremented ``runs`` and then tripped the
        coverage gate on the next cycle).

        The full read-modify-write holds ``control_lock`` so two
        origin-sharing checkouts cannot drop charges.
        """
        stamp = today or _local_today()
        ensure_control_state(self.root_dir)
        with control_lock(self.root_dir):
            state = self.read_state(stamp)
            current = state.spend
            merged_phases = tuple(
                sorted(set(current.unmetered_phases) | {p for p in unmetered_phases if p})
            )
            spend = DailySpend(
                date=stamp,
                spent_usd=round(current.spent_usd + max(0.0, usd), 6),
                runs=current.runs + (1 if metered_run else 0),
                covered_calls=current.covered_calls + max(0, covered_calls),
                total_calls=current.total_calls + max(0, total_calls),
                unmetered_phases=merged_phases,
            )
            self._write_unlocked(
                replace(
                    state,
                    spend=spend,
                    cost_coverage_seen=state.cost_coverage_seen or covered_calls > 0,
                )
            )
            return spend

    def record_terminal(self, *, poisoned: bool, today: str | None = None) -> int:
        """Update the authoritative poison streak; returns the new value."""
        ensure_control_state(self.root_dir)
        with control_lock(self.root_dir):
            state = self.read_state(today)
            streak = state.consecutive_poison + 1 if poisoned else 0
            self._write_unlocked(replace(state, consecutive_poison=streak))
            return streak

    def reset_poison_streak(self, today: str | None = None) -> int:
        """Restart the poison streak at 0; returns the streak it cleared.

        The operator's half of the breaker (#707). ``check_poison_breaker``
        reads this streak on every cycle, so lifting the pause without
        clearing it lets the next cycle pause the queue again. A streak
        already at 0 writes nothing. Raises :class:`ServeStateError` like
        ``read_state``.
        """
        ensure_control_state(self.root_dir)
        with control_lock(self.root_dir):
            state = self.read_state(today)
            if state.consecutive_poison:
                self._write_unlocked(replace(state, consecutive_poison=0))
            return state.consecutive_poison


@dataclass(frozen=True)
class RunSpend:
    """One run's cost as read back from its event stream."""

    cost_usd: float = 0.0
    cost_calls: int = 0
    usage_calls: int = 0
    #: Calls the run recorded against the architect, read for one
    #: question only: see :attr:`unmetered_phases`.
    architect_calls: int = 0
    #: Why the run's event stream could not be read, or "". The figures
    #: above are then zeros, and the daemon pauses intake on them (#863).
    unreadable: str = ""

    @property
    def uncovered_calls(self) -> int:
        return max(0, self.usage_calls - self.cost_calls)

    @property
    def lower_bound(self) -> bool:
        return self.uncovered_calls > 0

    @property
    def unmetered_phases(self) -> tuple[str, ...]:
        """Roles that spent on THIS RUN without reaching the meter.

        The one member of this class that does not survive summation,
        which is why a launch spanning several runs is a
        :class:`LaunchSpend` and not a bigger ``RunSpend``: the answer is
        all-or-nothing, so on a sum one run's architect would clear
        another's (#257 review). ``cost_usd`` and ``uncovered_calls``
        add up fine; this does not.

        It stopped being a constant in #257 piece B. `ks factory` now
        hands the architect's spend to the run it decomposed for, so a
        run that got as far as executing carries an architect row inside
        the dir the daemon just charged - already counted in
        ``cost_usd``, and naming it unmetered on top of that would call
        a measured figure a floor.

        A blocker halt exits `ks factory` before the factory run's directory
        exists, so its architect row is in the decompose run the architect
        ran as (#567), and ``owned_run_spend`` charges that run (#587): the
        halt reads as exact when the architect reported its usage.

        Zero calls does NOT mean zero spend, which is why the fallback is
        the pessimistic one. The cases that land on it deserve it: an
        adapter that reports no usage, an architect killed before it
        reported, a progress log turned off (the decompose run then has no
        event stream to read), and a resume that ran no architect at all.
        Naming the role keeps the day's total labelled a floor rather than
        estimated (#186 F3).
        """
        return () if self.architect_calls > 0 else (ARCHITECT_ROLE,)


@dataclass(frozen=True)
class LaunchSpend:
    """What ONE launch spent, across every run directory it produced.

    Separate from :class:`RunSpend` because a launch is not simply a
    bigger run. The dollar and call figures add up; ``unmetered_phases``
    does not, so summing runs into a single ``RunSpend`` and reading the
    property off the total let a run that reported an architect clear
    the claim for a sibling that had none, and the day was reported
    exact on the strength of a different run's spend (#257 review).

    Stating it as its own type is what makes that misuse unreachable
    rather than merely documented: there is no ``architect_calls`` here
    to sum, and ``unmetered_phases`` is correct by construction.
    """

    cost_usd: float = 0.0
    cost_calls: int = 0
    usage_calls: int = 0
    unmetered_phases: tuple[str, ...] = ()
    #: One sentence per run whose spend could not be read (#863).
    unreadable: tuple[str, ...] = ()

    @classmethod
    def over(cls, spends: Sequence[RunSpend]) -> LaunchSpend:
        """Fold each run's reading into the launch's."""
        # A launch that produced no directory this code charges is
        # accounted for as one empty run rather than as nothing: it spent
        # nothing this code can see, but an architect whose run recorded
        # no usage may still have spent. An empty RunSpend reports exactly
        # that, so the pessimistic answer arrives by the same path as
        # every other one.
        readings = list(spends) or [RunSpend()]
        return cls(
            cost_usd=sum(reading.cost_usd for reading in readings),
            cost_calls=sum(reading.cost_calls for reading in readings),
            usage_calls=sum(reading.usage_calls for reading in readings),
            unmetered_phases=tuple(
                sorted({phase for reading in readings for phase in reading.unmetered_phases})
            ),
            unreadable=tuple(reading.unreadable for reading in readings if reading.unreadable),
        )


def read_run_spend(root_dir: Path, run_id: str) -> RunSpend:
    """Reported cost of ONE named run, with its coverage.

    ``run_id`` must be non-empty and owned by the invocation being
    charged. Review #186 F2: an empty id made ``load_run_state`` fall
    back to the NEWEST run on disk, so a failed invocation charged a
    previous run's spend and could be classified from a stale manifest.
    An empty id now returns zeros instead of silently reading someone
    else's run.

    #281: the architect's row moved to ``ARCHITECT_COMPONENT``, so a run
    recorded before that change - whose stream says ``"architect"`` -
    reads here as having no architect row, and the day's total is
    reported as a FLOOR rather than exact. That is the pessimistic
    direction, and it is the same answer this function already gives for
    a resume and an adapter that reports nothing.

    There is deliberately no fallback to the old bare key. It would be
    unsafe rather than merely redundant: on a NEW run whose architect did
    not report and which happens to contain a component named
    `architect`, the fallback would read that component's calls as the
    architect's and clear the honesty flag - which is exactly the bug
    #281 removes. The narrower thing is not worth building either,
    because ``owned_run_spend`` only ever reads run dirs created inside
    the launch window it is charging, so the daemon never reads a
    pre-#281 run at all.
    """
    if not run_id:
        return RunSpend()
    from kstrl.events import LegacyRunStreamError, load_run_state

    kstrl_dir = state_dir(root_dir)
    run_dir = kstrl_dir / "runs" / run_id
    try:
        state, _source = load_run_state(root_dir, run_id)
    except OSError as exc:
        return RunSpend(unreadable=f"the event stream of {run_dir} cannot be read: {exc}")
    except LegacyRunStreamError:
        # #863: no readable events.jsonl, while progress.jsonl names the
        # run. The cause is not known here, so none is stated.
        return RunSpend(
            unreadable=(
                f"{run_dir} has no readable events.jsonl, and "
                f"{kstrl_dir / 'progress.jsonl'} holds events of the run"
            )
        )
    architect = state.components.get(ARCHITECT_COMPONENT)
    return RunSpend(
        cost_usd=state.cost_usd,
        cost_calls=state.cost_calls,
        usage_calls=state.usage_calls,
        architect_calls=architect.usage_calls if architect is not None else 0,
    )


#: Run-id kind prefix of the runs this daemon's own child produces;
#: ``owned_run_spend`` charges only these.
#:
#: Coupled to the spawned argv by CONVENTION, not derivation: `ks factory`
#: gets this prefix from ``mint_run_id``'s default, not from its command
#: name. Changing either without the other silently empties the charge,
#: so a test pins the two together.
SPAWNED_RUN_KIND: Final = "factory"


#: Run-id kind of the run `ks factory --spec` runs its architect as (#567).
#: `ks factory` opens it with this constant, so the two cannot drift.
#: ``owned_run_spend`` charges one only as :func:`_architect_runs` says.
ARCHITECT_RUN_KIND: Final = "decompose"


def owned_run_spend(
    root_dir: Path,
    runs_before: frozenset[str],
    *,
    launch_pid: int | None,
) -> tuple[list[str], LaunchSpend]:
    """The run dirs a launch produced, and what they add up to.

    "Produced" is narrower than "new" (#257 review). `ks decompose` takes
    no factory.lock, so an operator can start one on this repo at any
    time and its dir appears inside the window. That was harmless while
    decompose emitted no usage events; #257 gave it real spend, so
    without the kind filter the daemon would charge the queue item for
    money an operator spent by hand, and `max_daily_spend` could halt the
    queue on it.

    What the filter does and does not buy, stated honestly: it removes
    the decompose class completely, but it does NOT make the remaining
    attribution exact. ``.kstrl/factory.lock`` excludes concurrent
    factory EXECUTION, not concurrent factory DIRS - ``--force-lock``
    runs a second factory by design. A foreign factory-kind dir can still land in the window. That
    hole predates #257 and is unchanged by it; what #257 changed, and
    this restores, is that decompose dirs now carry real money.

    That the window can hold more than one dir is exactly why the return
    is a :class:`LaunchSpend`: see its docstring for what does and does
    not survive being added together.

    The one decompose run a launch does produce is its architect's
    (#567), and it is charged when no factory run carries its spend
    (#587). ``launch_pid`` is the launch's `ks factory` process: the child
    ``serve_cycle`` spawned, whose pid is also the item's lease owner, or
    this process for an approval run. None charges no decompose run.
    """
    new = run_dir_names(root_dir) - runs_before
    factory_runs = sorted(rid for rid in new if run_kind(rid) == SPAWNED_RUN_KIND)
    owned = sorted(factory_runs + _architect_runs(root_dir, new, factory_runs, launch_pid))
    return owned, LaunchSpend.over([read_run_spend(root_dir, rid) for rid in owned])


def _architect_runs(
    root_dir: Path,
    new: frozenset[str],
    factory_runs: Sequence[str],
    launch_pid: int | None,
) -> list[str]:
    """The decompose runs this launch's architect ran as that no factory run carries.

    A decompose run is the launch's when its ``factory_started`` event
    names the launch's pid; an operator's `ks decompose` names its own
    process and stays uncharged (#257 review). A factory run that names
    it as its ``architect_run_id`` was handed its spend and records it as
    its own architect row (#257), so charging both would count the
    architect twice. What is charged here is a launch that stopped
    between the architect and the factory run: a blocker halt, a failed
    decompose, a refused lock or configuration.
    """
    if launch_pid is None:
        return []
    carried = {_opened_with(root_dir, rid)[1] for rid in factory_runs}
    return [
        rid
        for rid in sorted(new)
        if run_kind(rid) == ARCHITECT_RUN_KIND
        and rid not in carried
        and _opened_with(root_dir, rid)[0] == launch_pid
    ]


def _opened_with(root_dir: Path, run_id: str) -> tuple[int, str]:
    """The pid and ``architect_run_id`` a run's ``factory_started`` recorded.

    ``(0, "")`` for a run with no readable event stream: such a decompose
    run is not charged, and such a factory run carries no architect row,
    so the architect is counted at most once either way. ``read_run_spend``
    reports such a factory run as unreadable, and `ks serve` pauses on it.
    """
    from kstrl.events import LegacyRunStreamError, load_run_state

    try:
        state, _source = load_run_state(root_dir, run_id)
    except (OSError, LegacyRunStreamError):
        return 0, ""
    return state.pid, state.architect_run_id


def run_dir_names(root_dir: Path) -> frozenset[str]:
    """Names of every run directory currently on disk.

    Every kind, deliberately: this is the raw disk fact. Snapshotted
    before and after a launch so the daemon can tell which dirs are new
    (#186 F2), but "new" alone does not mean "ours" - ``owned_run_spend``
    narrows the difference to ``SPAWNED_RUN_KIND`` before charging it,
    and states why there.
    """
    runs_root = state_dir(root_dir) / "runs"
    try:
        return frozenset(entry.name for entry in runs_root.iterdir() if entry.is_dir())
    except OSError:
        return frozenset()
