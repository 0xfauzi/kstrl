"""Base agent protocol, usage metering types, and the usage rollup."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from kstrl.ui.base import UI

logger = logging.getLogger(__name__)

#: Which usage figure each run-level ceiling counts. Stated once so no
#: surface re-derives "max_cost_usd means the cost axis" locally - local
#: re-derivation of a budget fact is exactly what let two sinks disagree
#: in #181.
CEILING_AXES: Final[Mapping[str, str]] = {
    "max_cost_usd": "cost",
    "max_total_tokens": "token",
}


@dataclass(frozen=True)
class UsageRecord:
    """Token/cost usage for ONE agent invocation (one ``run`` call).

    R3.1 cost meter. Every field except ``duration_seconds`` and
    ``source`` is a CLI self-report parsed from agent output; ``None``
    means "the CLI did not report it", never zero. These are hints for
    accounting - they must never gate correctness (a parse failure
    produces an all-``None`` record, not an exception).

    Measured emission formats (2026-07-18, see R3.1 PR):
    - claude CLI 2.1.214 stream-json ``result`` event: ``usage``
      (``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
      ``cache_creation_input_tokens``), ``total_cost_usd``,
      ``duration_ms``.
    - codex CLI 0.134.0 plain output: a trailing ``tokens used`` /
      ``14,511`` line pair - TOTAL tokens only, no in/out split, no cost.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    # CLI-reported total where only a total exists (codex); for claude
    # the adapter derives it as the sum of the four component fields.
    total_tokens: int | None = None
    cost_usd: float | None = None
    duration_seconds: float = 0.0
    # Provenance: "claude-stream-json", "codex-text", "unavailable",
    # "timeout", or "parse-error".
    source: str = "unavailable"


@dataclass
class UsageTotals:
    """Aggregate of UsageRecords (per phase, per component, or per run).

    ``known_calls`` counts invocations that reported at least one token
    or cost figure; ``calls - known_calls`` invocations contributed only
    wall time, so every token/cost total is a LOWER BOUND whenever
    ``unreported_calls > 0`` (H4: totals are only as honest as their
    coverage, and the rollup renders that gap explicitly).

    ``token_calls`` is the STRICTER coverage signal: invocations that
    reported an actual token figure. It is deliberately separate from
    ``known_calls`` because a record carrying only ``cost_usd`` (the
    claude adapter emits ``total_cost_usd`` even when the ``usage`` dict
    is missing or drifted) is "known" for cost purposes yet contributes
    nothing to ``total_tokens``. Anything reasoning about a TOKEN
    ceiling - see ``kstrl.loop.LoopBudget`` - must read ``token_calls``,
    not ``known_calls``; a review of R8 found the cost-only case makes a
    token cap silently unenforceable while ``known_calls`` says coverage
    is perfect.

    ``cost_calls`` is the same signal for the other axis, and the two are
    genuinely independent: the codex adapter reports a token total and no
    cost, the claude adapter can report a cost with no ``usage`` dict.
    A COST ceiling must read ``cost_calls``; a TOKEN ceiling must read
    ``token_calls``. Neither may be inferred from ``known_calls``, which
    says only that *something* was reported.
    """

    calls: int = 0
    known_calls: int = 0
    #: Invocations that reported at least one TOKEN figure. Always
    #: <= known_calls. Serialized (to_dict) so the on-disk audit trail
    #: keeps the distinction; absent in payloads written before R8.
    token_calls: int = 0
    #: Invocations that reported a COST figure. Always <= known_calls,
    #: and independent of ``token_calls`` in both directions. Serialized
    #: alongside it; absent in payloads written before the cost ceiling
    #: landed, where it decodes to 0.
    cost_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0

    @property
    def unreported_calls(self) -> int:
        """Invocations that reported no token/cost data at all."""
        return self.calls - self.known_calls

    @property
    def tokenless_calls(self) -> int:
        """Invocations that reported no TOKEN figure.

        Superset of ``unreported_calls``: a cost-only record is counted
        here but not there. This is the number a token ceiling has to
        reason about, because these calls can never move
        ``total_tokens``.
        """
        return self.calls - self.token_calls

    @property
    def costless_calls(self) -> int:
        """Invocations that reported no COST figure.

        The mirror of :attr:`tokenless_calls`, and the number a cost
        ceiling has to reason about: these calls can never move
        ``cost_usd``. A token-only adapter (codex reports a token total
        and no cost) makes this equal to ``calls`` while
        ``tokenless_calls`` is 0.
        """
        return self.calls - self.cost_calls

    def add_record(self, record: object) -> None:
        """Fold one usage record into the totals.

        Defensive by design (R3.1 requirement 4): ``record`` is read
        via ``getattr`` with per-field type checks so a malformed or
        foreign object degrades to "one call, nothing reported" instead
        of raising.
        """
        self.calls += 1
        known = False
        # Tracked separately from ``known``: a cost-only record is known
        # but tokenless, and a token cap must not mistake it for
        # coverage (R8 review, P1-b). ``cost_known`` is the same
        # distinction for the cost ceiling - a token-only record is known
        # but costless.
        token_known = False
        cost_known = False
        token_fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        )
        part_sum = 0
        parts_seen = False
        for name in token_fields:
            value = _as_int(getattr(record, name, None))
            if value is not None:
                setattr(self, name, getattr(self, name) + value)
                part_sum += value
                parts_seen = True
                known = True
                token_known = True
        total = _as_int(getattr(record, "total_tokens", None))
        if total is not None:
            self.total_tokens += total
            known = True
            token_known = True
        elif parts_seen:
            self.total_tokens += part_sum
        cost = _as_float(getattr(record, "cost_usd", None))
        if cost is not None:
            self.cost_usd += cost
            known = True
            cost_known = True
        duration = _as_float(getattr(record, "duration_seconds", None))
        if duration is not None:
            self.duration_seconds += duration
        if known:
            self.known_calls += 1
        if token_known:
            self.token_calls += 1
        if cost_known:
            self.cost_calls += 1

    def merge(self, other: UsageTotals) -> None:
        """Fold another totals object into this one."""
        self.calls += other.calls
        self.known_calls += other.known_calls
        self.token_calls += other.token_calls
        self.cost_calls += other.cost_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.total_tokens += other.total_tokens
        self.cost_usd += other.cost_usd
        self.duration_seconds += other.duration_seconds

    def to_dict(self) -> dict[str, Any]:
        """Serializable form for the progress log / journal / TSV."""
        return {
            "calls": self.calls,
            "known_calls": self.known_calls,
            "token_calls": self.token_calls,
            "cost_calls": self.cost_calls,
            "unreported_calls": self.unreported_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "duration_seconds": round(self.duration_seconds, 2),
        }


@dataclass(frozen=True)
class RoleCoverage:
    """One role's (phase's) contribution to a coverage axis.

    ``covered_calls`` counts this role's invocations that reported the
    figure the axis is denominated in - ``cost_calls`` for the cost axis,
    ``token_calls`` for the token axis. ``total_tokens`` is carried
    because it is the only MEASURED magnitude available for calls that
    reported no cost; it is never converted into one.
    """

    role: str
    calls: int
    covered_calls: int
    total_tokens: int = 0

    @property
    def uncovered_calls(self) -> int:
        return max(0, self.calls - self.covered_calls)

    @property
    def covered(self) -> bool:
        """Every one of this role's calls reported the axis figure."""
        return self.uncovered_calls == 0

    @property
    def silent(self) -> bool:
        """NOT ONE of this role's calls reported the axis figure.

        The distinction matters for attribution: a silent role's whole
        token total is provably uncounted by the ceiling, while a
        partially covered role's is not attributable at all - the
        aggregate does not say which of its calls carried the figure,
        and guessing a split would invent a number.
        """
        return self.calls > 0 and self.covered_calls == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "calls": self.calls,
            "covered_calls": self.covered_calls,
            "uncovered_calls": self.uncovered_calls,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class CeilingCoverage:
    """What fraction of a run's metered calls one axis actually counts.

    The missing middle between "the ceiling works" and "the ceiling is
    unenforceable" (``ComponentPipeline.cost_budget_unenforceable``).
    Measured on a real run: the engineer reported cost on all 8 of its
    calls while the cross-family reviewer (codex) reported tokens and no
    cost on 5, so the run's cost total equalled the engineer's exactly
    and ``max_cost_usd`` bounded one role. Neither ceiling was dead and
    nothing was breached, so every existing surface stayed silent -
    including the rollup's lower-bound footer, which fires on
    ``unreported_calls`` and saw 0 because every call reported SOMETHING.

    ``uncovered_tokens`` sums only the SILENT roles' tokens (see
    :attr:`RoleCoverage.silent`). It is a lower bound on the unpriced
    magnitude and is deliberately left in tokens: this codebase does not
    hold a price table, and inventing one to render a dollar estimate
    would put a fabricated figure in the audit trail.
    """

    axis: str
    calls: int
    covered_calls: int
    uncovered_tokens: int = 0
    roles: tuple[RoleCoverage, ...] = ()
    #: Config key of the ceiling this describes, when it describes one.
    #: Empty for the rollup's axis-only view, where no ceiling is
    #: configured but the total is still a lower bound.
    ceiling: str = ""

    @property
    def uncovered_calls(self) -> int:
        return max(0, self.calls - self.covered_calls)

    @property
    def uncovered_roles(self) -> tuple[str, ...]:
        """Roles with at least one call that reported nothing on this
        axis - the "which subset" an operator has to be told."""
        return tuple(role.role for role in self.roles if not role.covered)

    @property
    def complete(self) -> bool:
        """Every metered call reported this axis's figure."""
        return self.calls > 0 and self.uncovered_calls == 0

    @property
    def partial(self) -> bool:
        """Some calls reported it and some did not - the measured case."""
        return self.covered_calls > 0 and self.uncovered_calls > 0

    @property
    def empty(self) -> bool:
        """Calls were made and not one reported this axis's figure."""
        return self.calls > 0 and self.covered_calls == 0

    def note(self) -> str:
        """The operator sentence, or "" when there is nothing to report.

        Stops at the token count for uncovered calls and never converts
        it to dollars: the adapter reported no price, and a fabricated
        cost in an audit trail is worse than a missing one.
        """
        if self.calls == 0 or self.complete:
            return ""
        figure = "a cost" if self.axis == "cost" else "a token count"
        noun = "price" if self.axis == "cost" else "token count"
        bounded = (
            f"{self.ceiling} bounds only those"
            if self.ceiling
            else f"the {self.axis} total covers only those"
        )
        parts: list[str] = []
        for role in self.roles:
            if role.covered:
                continue
            detail = f"{role.role} ({role.uncovered_calls} of {role.calls} call(s)"
            # Only a SILENT role's tokens are attributable to uncovered
            # calls; a partially covered role's aggregate does not say
            # which call carried the figure.
            if role.silent and role.total_tokens > 0:
                detail += f", {role.total_tokens:,} token(s) unpriced"
            parts.append(detail + ")")
        state = "PARTIAL" if self.partial else "EMPTY"
        return (
            f"{self.axis} coverage is {state}: {self.covered_calls} of "
            f"{self.calls} metered call(s) reported {figure}, so {bounded}; "
            f"uncovered: {', '.join(parts)}. No {noun} is inferred for "
            f"uncovered calls, so the {self.axis} total is a lower bound"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serializable form for events, the progress log and the inbox."""
        return {
            "ceiling": self.ceiling,
            "axis": self.axis,
            "calls": self.calls,
            "covered_calls": self.covered_calls,
            "uncovered_calls": self.uncovered_calls,
            "uncovered_tokens": self.uncovered_tokens,
            "uncovered_roles": list(self.uncovered_roles),
            "roles": [role.to_dict() for role in self.roles],
        }


def usage_coverage(
    usage_meter: Mapping[str, Mapping[str, UsageTotals]],
    *,
    axis: str,
    ceiling: str = "",
) -> CeilingCoverage:
    """Fold a ``{component: {phase: UsageTotals}}`` meter by ROLE.

    Roles are the meter's phase keys (engineer / review / security /
    distill), folded across every component so the answer is run-scoped
    like the ceilings it describes. Order is alphabetical, which is
    arbitrary but fixed - the note must not reshuffle between runs.

    An unknown ``axis`` yields zero coverage rather than raising: this
    is accounting, and accounting must never gate a run.
    """
    by_role: dict[str, UsageTotals] = {}
    for phases in usage_meter.values():
        for phase, totals in phases.items():
            by_role.setdefault(phase, UsageTotals()).merge(totals)
    roles: list[RoleCoverage] = []
    calls = 0
    covered_calls = 0
    uncovered_tokens = 0
    for role_name in sorted(by_role):
        totals = by_role[role_name]
        role_covered = (
            totals.cost_calls if axis == "cost" else totals.token_calls if axis == "token" else 0
        )
        role = RoleCoverage(
            role=role_name,
            calls=totals.calls,
            covered_calls=role_covered,
            total_tokens=totals.total_tokens,
        )
        roles.append(role)
        calls += role.calls
        covered_calls += role.covered_calls
        if role.silent:
            uncovered_tokens += role.total_tokens
    return CeilingCoverage(
        axis=axis,
        calls=calls,
        covered_calls=covered_calls,
        uncovered_tokens=uncovered_tokens,
        roles=tuple(roles),
        ceiling=ceiling,
    )


#: The architect's role name, which is also its pseudo-component id
#: (``decompose.ARCHITECT_COMPONENT`` aliases this). Stated HERE because
#: the ordering tuple below has to agree with it and cannot import
#: ``decompose`` - the dependency runs the other way. The same reasoning
#: as ``CEILING_AXES`` above: a name re-derived locally is a name two
#: surfaces can disagree about, and here the disagreement would be
#: silent, dropping the architect to the tail of the rollup.
ARCHITECT_ROLE: Final = "architect"

# Rollup row order for the R3.1 usage table, in the order the roles run:
# the architect decomposes the spec before any component's engineer loop
# starts (#257). Phases outside this list (future additions) sort after,
# alphabetically.
_USAGE_PHASE_ORDER: Final[tuple[str, ...]] = (
    ARCHITECT_ROLE,
    "engineer",
    "review",
    "security",
    "distill",
)


def format_usage_rollup(
    usage_meter: Mapping[str, Mapping[str, UsageTotals]],
    run_usage: UsageTotals,
) -> list[str]:
    """Render the per-component, per-phase usage table (R3.1).

    Token and cost columns are sums of CLI self-reports: codex reports
    only a total (in/out columns stay 0), CustomAgent reports nothing.
    Whenever some calls reported no usage the footer says so explicitly -
    the totals are then lower bounds, not measurements (H4).

    R8 (measured): the ``unreported_calls`` footer alone was not enough.
    It fires only when a call reported NOTHING, so a cross-family
    reviewer that reports tokens and no cost left it at 0 while
    contributing $0 to a run whose cost total covered 8 of 13 calls -
    the footer stayed silent on exactly the run it existed for. Each
    axis now reports its own coverage, and names the roles that are
    missing from it.

    Lives here rather than in ``factory`` because ``ks decompose`` prints
    the same table for the architect alone, and a second renderer would
    be a second place for the coverage rules to drift (#257).
    """
    header = (
        f"{'component':<24} {'phase':<10} {'calls':>5} "
        f"{'tokens_in':>11} {'tokens_out':>11} {'tokens_total':>13} "
        f"{'cost_usd':>9} {'time_s':>8}"
    )
    lines = [header]

    def _phase_sort_key(phase: str) -> tuple[int, str]:
        try:
            return (_USAGE_PHASE_ORDER.index(phase), phase)
        except ValueError:
            return (len(_USAGE_PHASE_ORDER), phase)

    def _row(label: str, phase: str, totals: UsageTotals) -> str:
        # Each cell is gated by ITS OWN axis, never by known_calls (R8
        # review finding 2). known_calls means only "reported
        # something", so a cost-only invocation (known_calls=1,
        # token_calls=0) printed `0 0 0` tokens while the footer said
        # token coverage was EMPTY and the total was a lower bound - the
        # row contradicted the footer directly under it.
        #
        # "-" means "no call in this row reported this figure", never
        # "it was zero". Keyed on the call counters rather than on the
        # totals so a genuinely reported 0 tokens / $0.0000 is not
        # rendered as silence - the same distinction the ceilings make.
        if totals.token_calls > 0:
            tokens_in = f"{totals.input_tokens:,}"
            tokens_out = f"{totals.output_tokens:,}"
            tokens_total = f"{totals.total_tokens:,}"
        else:
            tokens_in = tokens_out = tokens_total = "-"
        cost = f"{totals.cost_usd:.4f}" if totals.cost_calls > 0 else "-"
        return (
            f"{label:<24} {phase:<10} {totals.calls:>5} "
            f"{tokens_in:>11} {tokens_out:>11} {tokens_total:>13} "
            f"{cost:>9} {totals.duration_seconds:>8.0f}"
        )

    for comp_id in sorted(usage_meter):
        phases = usage_meter[comp_id]
        for phase in sorted(phases, key=_phase_sort_key):
            lines.append(_row(comp_id, phase, phases[phase]))
    lines.append(_row("TOTAL", "", run_usage))
    lines.extend(_usage_rollup_notes(usage_meter, run_usage))
    return lines


def _usage_rollup_notes(
    usage_meter: Mapping[str, Mapping[str, UsageTotals]],
    run_usage: UsageTotals,
) -> list[str]:
    """The rollup's footer: what the table above does NOT account for.

    Split from the renderer because measured: inlined, ``complexipy``
    scores ``format_usage_rollup`` at 17 against the repo's gate of 15.
    """
    notes: list[str] = []
    if run_usage.unreported_calls > 0:
        notes.append(
            f"note: {run_usage.unreported_calls} of {run_usage.calls} "
            "call(s) reported no token/cost data; token and cost totals "
            "are lower bounds"
        )
    # Per-axis coverage, which the note above cannot express: a call can
    # report tokens and no cost. Suppressed when nothing at all was
    # reported, because the note above already says precisely that and
    # three lines for one fact reads as noise.
    if run_usage.known_calls > 0:
        for axis in ("token", "cost"):
            note = usage_coverage(usage_meter, axis=axis).note()
            if note:
                notes.append(f"note: {note}")
    return notes


def print_usage_rollup(
    ui: UI,
    usage_meter: Mapping[str, Mapping[str, UsageTotals]],
    run_usage: UsageTotals,
    *,
    title: str,
) -> None:
    """Put the rollup on a terminal, or print nothing at all.

    The guard, the heading and the indent live here with the renderer so
    the two commands that report spend cannot drift apart in the half the
    operator actually reads. ``title`` is a parameter rather than a
    constant because the two callers are NOT reporting the same thing:
    ``ks factory`` renders a whole run, ``ks decompose`` renders one role,
    and giving both the same heading would invite reading the run's total
    as though it included the architect (#257 - it does not, until the
    architect is metered inside the factory run).

    Zero calls prints nothing: an empty table says "this ran and cost
    nothing", which is the opposite of "this never ran".
    """
    if run_usage.calls == 0:
        return
    ui.subsection(title)
    for line in format_usage_rollup(usage_meter, run_usage):
        ui.info(f"  {line}")


def _as_int(value: object) -> int | None:
    """Non-negative int or None. bool is rejected (it is an int subclass
    and a malformed ``usage`` dict could carry flags where counts go)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _as_float(value: object) -> float | None:
    """Non-negative float or None (accepts ints, rejects bools)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def usage_cursor(agent: object) -> int:
    """How many records the agent already holds, for a later ``since=``.

    Deliberately NOT ``collect_usage(agent).calls``. Those agree only
    because ``add_record`` increments ``calls`` unconditionally, which is
    an accident of that method and not a stated contract - and they
    diverge in exactly the case the meter exists to survive.

    Measured (2026-08-30) on four records whose second raises on field
    access: ``collect_usage`` aborts the walk and reports ``calls=2``,
    while four records are present. Seeding the next call with 2 folds
    records 2 and 3 - which belong to the FIRST unit of work and went
    unreported there - into the SECOND one's total. A cursor of 4 folds
    nothing, which is correct: records the meter could not read are lost
    from the report, not silently re-attributed to whoever ran next.

    Degrades to 0 (fold everything, the pre-``since`` behavior) rather
    than raising: a cursor is accounting, and accounting never gates a
    run.
    """
    try:
        records = getattr(agent, "usage_records", None)
        return 0 if records is None else len(list(records))
    except Exception as exc:  # noqa: BLE001 - meter must never crash a run
        logger.warning("Failed to read agent usage cursor: %s", exc)
        return 0


def collect_usage(agent: object, *, since: int = 0) -> UsageTotals:
    """Aggregate an agent's accumulated ``usage_records`` defensively.

    Works on ANY object: an agent without the attribute (a third-party
    Agent implementation predating R3.1, or a test fake) yields empty
    totals rather than an error - the meter must never gate correctness.

    ``usage_records`` is CUMULATIVE for the life of the agent instance,
    so the default folds every call the instance ever made. Callers that
    hand out a fresh agent per unit of work want exactly that. A caller
    that cannot rely on freshness passes ``since`` - the record count
    taken before the work by :func:`usage_cursor` - and gets only the
    tail. Sliced rather than indexed so a list that shrank or was
    replaced underneath degrades to empty totals instead of raising.
    """
    totals = UsageTotals()
    try:
        records = getattr(agent, "usage_records", None)
        if records is None:
            return totals
        for record in list(records)[since:]:
            totals.add_record(record)
    except Exception as exc:  # noqa: BLE001 - meter must never crash a run
        logger.warning("Failed to collect agent usage records: %s", exc)
    return totals


class Agent(Protocol):
    """Protocol for kstrl agent implementations."""

    @property
    def name(self) -> str:
        """Human-readable agent name for display."""
        ...

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        """Run agent with prompt, yielding output lines.

        Args:
            prompt: The prompt text to send to the agent
            cwd: Working directory for the agent process
            timeout: Optional wall-clock timeout in seconds

        Yields:
            Output lines from the agent (without trailing newlines)
        """
        ...

    @property
    def final_message(self) -> str | None:
        """Return final message if available (for codex --output-last-message)."""
        ...

    @property
    def usage_records(self) -> list[UsageRecord]:
        """Usage records accumulated across ``run`` calls (R3.1).

        One record per ``run`` invocation, appended on every exit path
        (success, timeout, CLI-missing). Consumers must read this via
        :func:`collect_usage`, which tolerates implementations that
        predate the property - the protocol addition is backward-
        compatible at runtime for CustomAgent-style adapters and
        third-party fakes.
        """
        ...
