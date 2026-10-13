"""The meaning of the journal rows: schema version, event types, carried copies, readings."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from kstrl.plan import ComponentStatus

# R6.4: journal entries carry an explicit schema version so future
# format migrations are detectable. Version 2 = structured failure
# signatures (R6.1). Entries without the field are version 1 (the
# pre-R6 shape); wave 1 (R4.1) archived the polluted v1 journals to
# .kstrl/archive/, so fresh journals contain v2 entries only.
# Version 3 (#447): record_run writes a component_result row only for a
# component this run touched. A v1 or v2 component_result row may be a
# copy of an earlier run's result; carried_result_indices finds those.
JOURNAL_SCHEMA_VERSION = 3

# #260: the event_type of one recorded spec audit. ``decompose`` writes
# these rows and :meth:`EvolutionJournal.get_spec_audits` selects on
# them, so the name belongs on the layer that defines the journal's
# schema rather than on the writer (#314). It lived in ``decompose``
# until then, with this module holding a second copy as a literal, and
# the cost of that placement is the reason it moved: a reader added
# HERE reaches for the nearest spelling, which was the literal.
SPEC_ISSUES_EVENT = "spec_issues"

# #233: the event_type of the journal row recording an attempt that a
# retry superseded. Declared here for the reason SPEC_ISSUES_EVENT
# gives one comment up: the pipeline writes these rows and this module
# now READS them (read_attempt_iterations), and a reader added here
# reaches for the nearest spelling. There is one writer and one reader
# and they share this name.
FINDINGS_SUPERSEDED_EVENT = "findings_superseded"

# #482: the event_type of the journal row recording one integration review
# round (record only). Declared here for the reason SPEC_ISSUES_EVENT is.
INTEGRATION_RESULT_EVENT = "integration_result"


# ---------------------------------------------------------------------------
# #233: per-attempt iteration readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IterationReading:
    """One run's engineer-loop iterations summed across ALL attempts.

    ``measured`` is the point of the type. #233's entry criterion is read
    off ``avg_iterations``, which is the LAST attempt's count per
    component and therefore a lower bound; a verdict derived from a lower
    bound reads "the loop does not iterate" on a run where it did. So a
    reading this module cannot prove is REFUSED with a reason, never
    returned as a smaller number.
    """

    run_id: str
    iterations_total: int
    attempts_total: int
    components_ran: int
    avg_all_attempts: float
    measured: bool
    reason: str


def _refused(run_id: str, reason: str) -> IterationReading:
    """The one shape a refusal takes, so the seven-field constructor is
    spelled once."""
    return IterationReading(run_id, 0, 0, 0, 0.0, False, reason)


def _int_field(entry: dict[str, Any], key: str, where: str) -> tuple[int | None, str]:
    """The non-negative integer at ``key``, or ``(None, reason)``.

    ``bool`` is rejected explicitly: ``isinstance(True, int)`` is ``True`` in
    Python, so a JSON ``true`` in ``iteration_count`` would silently count as 1.
    """
    if key not in entry:
        return None, f"{where} carries no {key}"
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, f"{where}: {key} is not a non-negative integer"
    return value, ""


def _first_attempt(entry: dict[str, Any], where: str) -> tuple[int | None, str]:
    """The row's ``first_attempt``, 1 when absent (a row written before #463),
    or ``(None, reason)`` when it is present and not an integer of at least 1."""
    if "first_attempt" not in entry:
        return 1, ""
    first, reason = _int_field(entry, "first_attempt", where)
    if first == 0:
        return None, f"{where}: first_attempt is 0"
    return first, reason


def _component_attempt_readings(
    result: dict[str, Any],
    superseded: list[dict[str, Any]],
) -> tuple[dict[int, int] | None, str]:
    """One component's ``{attempt: iterations}``, or ``(None, reason)``."""
    cid = str(result.get("component_id", ""))
    retries, reason = _int_field(result, "retries", f"{cid}: component_result")
    if retries is None:
        return None, reason
    final, reason = _int_field(result, "iteration_count", f"{cid}: component_result")
    if final is None:
        return None, reason
    first, reason = _first_attempt(result, f"{cid}: component_result")
    if first is None:
        return None, reason
    # A PENDING row was retried and stopped before its next attempt began,
    # so attempt retries + 1 never ran and its iteration_count is the
    # previous attempt's (#463).
    last = retries if result.get("status") == ComponentStatus.PENDING.value else retries + 1
    seen: dict[int, int] = {} if last == retries else {last: final}
    for entry in superseded:
        attempt, reason = _int_field(entry, "attempt", f"{cid}: findings_superseded")
        if attempt is None:
            return None, reason
        count, reason = _int_field(entry, "iteration_count", f"{cid}: attempt {attempt}")
        if count is None:
            return None, reason
        if attempt in seen:
            return None, f"{cid}: attempt {attempt} recorded twice"
        seen[attempt] = count
    expected = set(range(first, last + 1))
    if set(seen) != expected:
        missing = sorted(expected - set(seen))
        if missing:
            return None, (f"{cid}: attempts {missing} have no reading (expected {first}..{last})")
        extra = sorted(set(seen) - expected)
        return None, (f"{cid}: unexpected attempts {extra} (expected {first}..{last})")
    return seen, ""


# #447: the fields a manifest carries unchanged from one run to the next
# for a component that did not run again. The journal writer before #447
# copied them onto a new run_id, so a row whose four values equal its
# component's previous row is that copy.
# duration_seconds is a wall-clock float, so a component that really ran
# again does not reproduce it.
_CARRIED_FIELDS = ("status", "retries", "iteration_count", "duration_seconds")


def _written_before_447(entry: dict[str, Any]) -> bool:
    """Whether ``entry`` came from a writer that copied carried results.

    An absent version is v1 (the comment on JOURNAL_SCHEMA_VERSION). Any
    other non-integer, a ``bool`` included, is not provably old, and a row
    that is not provably old is never dropped.
    """
    version = entry.get("schema_version", 1)
    return isinstance(version, int) and not isinstance(version, bool) and version < 3


def carried_result_indices(entries: list[dict[str, Any]]) -> set[int]:
    """Indices of ``component_result`` rows that repeat their component's
    previous row: a result from an earlier run, copied under a later id.

    Only a row written before #447 (schema_version below 3) can be a copy:
    the writer since then journals no carried component, so a v3 row is
    never dropped. A row missing any of :data:`_CARRIED_FIELDS` is never a
    copy either. Both break the chain for their component, so rows cannot
    match by a shared absence.
    """
    carried: set[int] = set()
    previous: dict[str, tuple[object, ...]] = {}
    for index, entry in enumerate(entries):
        if entry.get("event_type", "component_result") != "component_result":
            continue
        cid = str(entry.get("component_id", ""))
        if not _written_before_447(entry) or not all(key in entry for key in _CARRIED_FIELDS):
            previous.pop(cid, None)
            continue
        state = tuple(entry[key] for key in _CARRIED_FIELDS)
        if previous.get(cid) == state:
            carried.add(index)
        previous[cid] = state
    return carried


def without_carried_results(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``entries`` minus the rows :func:`carried_result_indices` names."""
    carried = carried_result_indices(entries)
    return [entry for index, entry in enumerate(entries) if index not in carried]


class LegacyJournalRowError(ValueError):
    """A journal row from before ``failure_signatures`` (R6.1)."""


def _row_signatures(entry: dict[str, Any]) -> list[str]:
    """The ``"<check>:<code>"`` signatures one journal row records.

    A v1 row has an ``error_signature`` and no ``failure_signatures``.
    It is refused, not composed into a signature (#863): the writer since
    R6.1 records both fields together, so the pair is a row that no
    current writer produced.
    """
    sigs = entry.get("failure_signatures") or []
    if not sigs and entry.get("error_signature", ""):
        raise LegacyJournalRowError(
            f"the row of component {entry.get('component_id', '?')!r} in run "
            f"{entry.get('run_id', '?')!r} has an error_signature and no "
            "failure_signatures, so it was written before R6.1 and kstrl no "
            "longer reads it. Move the file aside to start a new journal"
        )
    return [sig for sig in sigs if isinstance(sig, str) and sig]


def _signature_rows(
    entries: list[dict[str, Any]],
) -> Iterator[tuple[str, str, list[str], bool]]:
    """``(run, component, signatures, superseded)`` for every row the
    cross-run router counts: each ``component_result`` row and, since
    #496, each ``findings_superseded`` row, the attempt a retry replaced.

    ``run`` is the run the attempt ran in. A superseded row that
    :meth:`EvolutionJournal.carry_superseded` wrote again under a later
    run names its original run in ``carried_from_run``, so the copy and
    the original give the same ``(run, component, attempt)`` and count as
    one run.
    """
    for entry in entries:
        event = entry.get("event_type", "component_result")
        superseded = event == FINDINGS_SUPERSEDED_EVENT
        if not superseded and event != "component_result":
            continue
        run_id = entry.get("run_id", "")
        if superseded:
            run_id = entry.get("carried_from_run") or run_id
        yield str(run_id), str(entry.get("component_id", "")), _row_signatures(entry), superseded


def _run_results(
    entries: list[dict[str, Any]],
    run_id: str,
) -> tuple[int, list[dict[str, Any]]]:
    """How many ``component_result`` rows ``run_id`` wrote, and those of
    them that are not carried copies (#447).

    The count keeps the copies because ``components_total`` in a pre-#447
    experiments.tsv row counted them too: the torn-line join compares two
    counts of the same rows. Only the second value is summed.
    """
    carried = carried_result_indices(entries)
    indexed = [
        (index, e)
        for index, e in enumerate(entries)
        if e.get("event_type", "component_result") == "component_result"
        and e.get("run_id") == run_id
    ]
    return len(indexed), [e for index, e in indexed if index not in carried]


def read_attempt_iterations(
    entries: list[dict[str, Any]],
    run_id: str,
    components_total: int,
) -> IterationReading:
    """Sum every attempt's iterations for ``run_id``, refusing rather than
    guessing when the journal cannot support the sum.

    ``results`` selects on the module's own existing idiom for "this row
    predates the ``event_type`` column, so it is a component result"
    (``kstrl/evolution.py:1396``, ``:1418``, ``:1789``, ``:1843``), never a
    stricter one invented here (CLAUDE.md: two definitions of a valid
    record means the weaker one is the one the gate consults).

    The join against ``components_total`` is what makes the tolerant
    reader safe: ``_read_all_entries`` skips torn and blank lines
    silently, so a torn ``component_result`` line is otherwise a silently
    missing row rather than a refusal. ``components_total`` comes from
    the same list ``record_run`` writes rows from, so the two counts are
    the same quantity read twice. Since #447 that list is the components
    the run touched; before it, every manifest component, carried copies
    included, which is why the count keeps the copies and the sum drops
    them (:func:`_run_results`).
    """
    written, results = _run_results(entries, run_id)
    if written != components_total:
        return _refused(
            run_id,
            f"{written} component_result entries for {components_total} component(s) in the run",
        )
    superseded_by_component: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry.get("event_type") != FINDINGS_SUPERSEDED_EVENT:
            continue
        if entry.get("run_id") != run_id:
            continue
        cid = str(entry.get("component_id", ""))
        superseded_by_component.setdefault(cid, []).append(entry)

    iterations_total = 0
    attempts_total = 0
    components_ran = 0
    for result in results:
        cid = str(result.get("component_id", ""))
        seen, reason = _component_attempt_readings(result, superseded_by_component.get(cid, []))
        if seen is None:
            return _refused(run_id, reason)
        total = sum(seen.values())
        iterations_total += total
        attempts_total += len(seen)
        if total > 0:
            components_ran += 1

    avg = iterations_total / components_ran if components_ran else 0.0
    return IterationReading(
        run_id=run_id,
        iterations_total=iterations_total,
        attempts_total=attempts_total,
        components_ran=components_ran,
        avg_all_attempts=avg,
        measured=True,
        reason=(
            f"{components_ran} component(s) ran, {iterations_total} iteration(s) "
            f"across {attempts_total} attempt(s)"
        ),
    )


@dataclass(frozen=True)
class CriterionVerdict:
    window: int
    clause1: str  # "MET" | "NOT MET" | "REFUSED"
    clause1_detail: str
    clause2: str  # "MET" | "REFUSED"
    clause2_detail: str


def iteration_criterion_verdict(
    rows: list[dict[str, Any]],
    readings: list[IterationReading],
) -> CriterionVerdict:
    """#233's entry criterion, computed from the journal's per-attempt
    readings rather than from ``avg_iterations`` (a lower bound).

    No ``measured_runs`` and no ``runs_above_one`` field: nothing reads
    them, the detail strings already carry both numbers, and an unread
    field is weight the next reader has to check.
    """
    window = len(rows)
    if window == 0:
        return CriterionVerdict(
            window=0,
            clause1="REFUSED",
            clause1_detail="no runs in the window",
            clause2="REFUSED",
            clause2_detail="no runs in the window",
        )

    measured = [r for r in readings if r.measured]
    if len(measured) != window:
        clause1 = "REFUSED"
        clause1_detail = (
            f"{len(measured)} of {window} run(s) measured; a verdict is not "
            "derived from a lower bound"
        )
    else:
        above = sum(1 for r in measured if r.avg_all_attempts > 1.00)
        needed = window // 2 + 1
        clause1 = "MET" if above >= needed else "NOT MET"
        clause1_detail = f"{above} of {window} run(s) above 1.00; a majority needs {needed}"

    projects = sorted({str(r.get("project", "")).strip() for r in rows} - {""})
    if len(projects) >= 2:
        clause2 = "MET"
        clause2_detail = f"{len(projects)} distinct project value(s): {', '.join(projects)}"
    else:
        clause2 = "REFUSED"
        clause2_detail = (
            f"{len(projects)} distinct project value(s) in this ledger "
            f"({', '.join(projects) or 'none'}); this surface reads one project "
            "root and the criterion spans projects"
        )

    return CriterionVerdict(
        window=window,
        clause1=clause1,
        clause1_detail=clause1_detail,
        clause2=clause2,
        clause2_detail=clause2_detail,
    )


def entry_str(entry: dict[str, Any], key: str) -> str:
    """One string field of a JSON-decoded journal record, "" when absent.

    A null or non-string field is an ABSENT field, not a value to be
    stringified. ``str(None)`` renders the literal "None", which #280
    round 1 reproduced as a phantom project named 'None' in the
    convergence report and a spec file printed as ``None``. Nothing is
    assumed about a record beyond it being a JSON object, so a journal
    written by an older version, or edited by hand, still reads.

    Lives here rather than in ``decompose`` (#314) because the window
    in :meth:`EvolutionJournal.get_spec_issue_runs` matches a project
    by this rule and the report's accounting matches by the same one.
    Two copies of it would let the trend and the accounting disagree
    about which audits belong to the project being reported on.

    Applies to a record's nested objects too, which is what the stored
    issue list is: same JSON, same rule.
    """
    value = entry.get(key)
    return value if isinstance(value, str) else ""
