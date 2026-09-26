"""Continuous learning - evolution journal, experiment tracking, and pattern routing.

Records factory run outcomes and extracts recurring failure patterns across runs.
Inspired by AutoResearchClaw's evolution directory and autoresearch-agents' results.tsv.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.appendio import JOURNAL_REPAIR_EVENT, REPAIR_DETAIL, append_records
from kstrl.manifest import ADVERSARIAL_BUDGET_CHECK, ComponentStatus
from kstrl.observability import read_progress_events
from kstrl.verify import SCOPE_UNREADABLE_CHECK, SCOPE_UNREADABLE_ERROR_PREFIX
from kstrl.version import kstrl_version

if TYPE_CHECKING:
    from kstrl.factory import FactoryResult
    from kstrl.findings import Finding
    from kstrl.manifest import Component, Manifest
    from kstrl.verify import CheckResult

logger = logging.getLogger("kstrl.evolution")

# R6.4: journal entries carry an explicit schema version so future
# format migrations are detectable. Version 2 = structured failure
# signatures (R6.1). Entries without the field are version 1 (the
# pre-R6 shape); wave 1 (R4.1) archived the polluted v1 journals to
# .kstrl/archive/, so fresh journals contain v2 entries only.
# Version 3 (#447): record_run writes a component_result row only for a
# component this run touched. A v1 or v2 component_result row may be a
# copy of an earlier run's result; carried_result_indices finds those.
JOURNAL_SCHEMA_VERSION = 3

# #312 declared JOURNAL_REPAIR_EVENT here: the event_type of the row an
# append writes when it finds the file not newline-terminated. Its own
# type rather than a synthetic component_result, for the reason
# _role_usage_entries gives: every aggregate in this module selects on
# event_type, so a row of this type counts towards nothing and cannot
# invent an outcome. It exists to be grepped: it is the only durable
# trace that a crash tore the file.
#
# #331 moved the declaration to ``kstrl.appendio``, where the six
# appenders that can write it can all reach it. The import above is
# what binds the name in this module, for _repair_entry which writes
# the row and get_repair_count which counts it.

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


class ExperimentsDialect(csv.Dialect):
    """The ON-DISK shape of experiments.tsv, in one place (#352 round 2).

    The writer does not go through ``csv`` at all: ``record_run`` joins
    its fields on this dialect's delimiter, and ``EXPERIMENTS_HEADER``
    below is built the same way. So the delimiter is genuinely shared
    and the quoting rules are a description of what that writer does,
    which is nothing: it never quotes and it never escapes.

    ``quoting = QUOTE_NONE`` is therefore the reader agreeing with the
    writer rather than a new tolerance. The default dialect did not, and
    the cost was measured in review of #352: a ``"`` at the start of any
    field opens a quoted region that swallows every byte to the next
    ``"``, so a file whose first run was named ``"proj`` returned NO
    rows at 3 runs and raised ``_csv.Error: field larger than field
    limit (131072)`` at 20,001. Neither reader caught that, because
    ``_csv.Error`` is neither an ``OSError`` nor a ``ValueError``.

    ``escapechar`` is ``None`` for the same reason. A tab or a newline
    inside a field is therefore unrepresentable, which is residual 1 on
    :func:`experiment_rows` and is a property of the writer, not of this
    dialect: setting an escapechar here would only change how the reader
    misreads a row the writer already wrote wrong.

    ``lineterminator`` is what a ``csv.writer`` would emit and is unused
    on the read side, where ``csv`` recognises all three endings
    regardless. It is stated so the constant describes the whole file
    rather than half of it.
    """

    delimiter = "\t"
    quotechar = None
    doublequote = False
    escapechar = None
    lineterminator = "\n"
    quoting = csv.QUOTE_NONE
    skipinitialspace = False


# The header row record_run writes to experiments.tsv, at module scope so
# that a test can assert against the columns the writer actually emits
# rather than a shorter hand-typed row a lenient reader happens to
# tolerate. Files written before R3.1 keep their shorter header.
# Since #447 every per-component column (components_total, avg_iterations,
# avg_duration_s, retry_rate, common_failure) is computed over the
# components the run touched, the same set its journal rows describe;
# before #447 it was every manifest component, carried ones included.
EXPERIMENTS_HEADER = ExperimentsDialect.delimiter.join(
    (
        "run_id",
        "timestamp",
        "project",
        "components_total",
        "completed",
        "failed",
        "skipped",
        "avg_iterations",
        "avg_duration_s",
        "retry_rate",
        "common_failure",
        "total_tokens",
        "total_cost_usd",
        "unreported_calls",
        "kstrl_version",
    )
)


#: The width of every EXPERIMENTS_HEADER before the current one: before
#: R3.1 (11) and from R3.1 to #451 (14). A file keeps the header it was
#: started with, and each later kstrl appends its own full-width row, so
#: a row is legal at its file header's width and at every later width.
#: Adding a column appends the width it replaces here.
OLDER_EXPERIMENTS_WIDTHS: tuple[int, ...] = (11, 14)


def experiment_rows(text: str) -> list[dict[str, Any]]:
    """Parse experiments.tsv, dropping any row whose WIDTH does not fit the header.

    #331's read half. The write half pads an unterminated tail, but a
    file torn before that landed still has the concatenated row in it,
    and this reader is the one that RENDERS corruption rather than
    dropping it: measured, ``ks evolve --status`` and the TUI trends tab
    showed a run whose ``completed`` column held a timestamp, because
    ``csv.DictReader`` zipped a fragment plus a whole row against the
    header and put the overflow under the key ``None``.

    Width, not content, is the check, and it is deliberately not "the
    field count equals the header's". Files written before R3.1 have a
    SHORTER header and this writer appends the full-width row onto them,
    which the comment on :const:`EXPERIMENTS_HEADER` promises to
    tolerate; a filter that took the header's own width as the only
    legal one would answer a rendering defect by silently deleting every
    row of a legacy file, which is a worse defect than the one it fixes.
    So both widths are legal when the file's header is a PREFIX of the
    current one, and only then. A full-width row under such a header is
    read with the CURRENT header's names (#451): an experiments.tsv
    started between R3.1 and #451 has the 14-column header, and zipping
    the new row against it would drop the ``kstrl_version`` column from
    every run recorded after the upgrade. Every width in
    :data:`OLDER_EXPERIMENTS_WIDTHS` above the file header's own is legal
    too, because each kstrl in between appended its own full-width row:
    a file started before R3.1 holds 11-, 14- and 15-field rows.

    Blank lines are skipped rather than dropped as malformed, and NOT
    for the reason this said in round 1. The pad the writer leaves
    behind does not produce one: it writes ``"\n" + payload``, so the
    torn tail is terminated and the payload follows on the next line.
    Measured on both tear shapes, a whole row that lost only its
    newline and a fragment torn mid-row, and neither leaves a blank
    line. The skip is still right, for a file a person may have edited;
    the cause was invented.

    THREE RESIDUALS, measured in review of #352 rather than reasoned
    about, because "this writer never emits such a row" was the original
    claim here and it is not true.

    1. A row this writer CAN emit is dropped. ``row`` is joined on
       :class:`ExperimentsDialect`'s delimiter, not written by a
       ``csv.writer``, so a tab or a newline in ``project_name`` or
       ``common_failure`` reaches the file unescaped and nothing between
       the CLI and the write rejects one (#347's callback rejects blank
       and whitespace-only names, not embedded tabs). Measured on one
       ``record_run`` each: a project named ``pro\tject`` rendered a row
       with shifted columns before this filter and renders nothing now,
       and ``pro\nject`` produced two rows and now produces none.
       Dropping beats rendering shifted numbers to a ladder, so the
       filter is still the right call; the claim was the defect. A
       ``"`` used to belong on this list and no longer does: it damaged
       every LATER row rather than its own, and that is what reading on
       the writer's own dialect removed.
    2. A header that is NOT a prefix of the current one silently drops
       every row this writer appends. Measured with the first column
       renamed: an 11-column legacy header plus a fresh 14-field row
       returns only the legacy row. ``record_run`` never rewrites a
       disagreeing header, so in that state every new run is invisible
       to ``ks evolve --status`` and to ``ks autonomy replay`` with
       nothing logged.
    3. A fragment torn INSIDE the first field survives. A concatenation
       has ``k + 15 - 1`` fields for a fragment of ``k`` fields, which
       is 16 or more for a tear past the first tab; a tear before it
       gives ``k == 1`` and exactly 15, which is legal. Measured: both
       readers return ``run_id='run-2run-3'`` with every other column
       holding run-3's real values. ``run_id`` is column 1, so this is
       whatever share of the row's bytes a run id occupies, not a
       corner. New appends cannot create this state now that the writer
       pads.

    There is no counter for any of it, which is the fourth thing to
    know: ``[]`` from here means "no runs recorded", "the header does
    not match" and "every row was malformed" alike, and the trends tab
    renders the same empty table for all three. A parse that cannot be
    completed is deliberately NOT a fifth meaning of ``[]``: it raises.

    WHAT THE DIALECT CANNOT REMOVE, and why the parse is guarded.
    ``csv``'s field-size limit fires under ``QUOTE_NONE`` too, on any
    single field longer than 131,072 characters, and it raises
    ``_csv.Error``, which is neither an ``OSError`` nor a
    ``ValueError``: measured, both ``isinstance`` checks answer False,
    so it escaped ``get_experiment_trends`` (whose guard is around the
    read, not the parse) and ``ks autonomy replay``'s handler alike.
    CLAUDE.md's rule from #318 is that a parser's error taxonomy belongs
    to the parser, so the clause here is ``except Exception`` exactly
    rather than an enumeration of what ``csv`` is believed to raise, and
    it converts to a ``ValueError``, which is the refusal path both
    callers already take for an unreadable file. All of the I/O is
    outside the guarded block for the same reason it is in
    ``config.load_toml_document``: the caller does the reading, and the
    only thing under the ``try`` is the parse.

    That guard materialises every record before the width filter runs,
    where the previous shape streamed. Measured on a 20,000-run file
    (1.13 MB) rather than argued: 18.9 ms and a 21.1 MB peak against
    15.0 ms and 17.1 MB, so 3.9 ms and 4.0 MB at twenty thousand runs.
    Bought deliberately: streaming needs the clause twice, once on the
    header and once on the rows, and two clauses is two messages and two
    things to keep in step.

    Public because this file has TWO readers, not one:
    :meth:`EvolutionJournal.get_experiment_trends` renders it and
    ``autonomy_replay.load_runs`` feeds it to the autonomy ladder. Both
    used a bare ``csv.DictReader``, so a filter on only one of them
    would fix the screen and leave the ladder promoting on a run that
    does not exist.

    ``newline=""`` on the buffer is what ``csv`` documents as required
    for a stream handed to a reader. It is only half the contract here
    and the other half is not reachable: both callers arrive with text
    from ``Path.read_text``, which has already applied universal-newline
    translation, and ``read_text`` grew a ``newline`` parameter in 3.13
    while this project's floor is 3.11. So a lone ``\r`` inside a field
    is an ``\n`` before it gets here, which residual 1 covers.
    """
    buffer = io.StringIO(text, newline="")
    try:
        records = list(csv.reader(buffer, ExperimentsDialect))
    except Exception as exc:
        raise ValueError(f"experiments.tsv could not be parsed: {exc}") from exc
    if not records:
        return []
    header, rows = records[0], records[1:]
    columns = EXPERIMENTS_HEADER.split(ExperimentsDialect.delimiter)
    names, widths = header, {len(header)}
    if header == columns[: len(header)]:
        names = columns
        widths.update(w for w in (*OLDER_EXPERIMENTS_WIDTHS, len(columns)) if w > len(header))
    return [
        dict(zip(names[: len(fields)], fields, strict=True))
        for fields in rows
        if fields and len(fields) in widths
    ]


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


def _row_signatures(entry: dict[str, Any]) -> list[str]:
    """The ``"<check>:<code>"`` signatures one journal row records.

    A v1 row with no ``failure_signatures`` composes one from its legacy
    ``check_name`` and ``error_signature`` fields.
    """
    sigs = entry.get("failure_signatures") or []
    if not sigs:
        legacy_sig = entry.get("error_signature", "")
        if not legacy_sig:
            return []
        sigs = [f"{entry.get('check_name') or 'unknown'}:{legacy_sig}"]
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


# #191: what a component_result entry records when no fact-utilization
# measurement reached the journal - the component never got past the
# gates to the distill phase, knowledge was off, or the measurement
# itself failed. Deliberately NOT the same value as a measured zero:
# `measured=False` is "no evidence", `measured=True, referenced=0` is
# evidence that injected facts went unused. Not version-gated, because
# the key is written on every entry from here on; a pre-#191 entry has
# no key at all, which is a third, distinguishable state.
_UNMEASURED_UTILIZATION: dict[str, Any] = {
    "measured": False,
    "injected": 0,
    "referenced": 0,
    "reason": "not measured",
    # Same shape as a measured entry, so a consumer reads `measured`
    # rather than probing for key presence.
    "by_tier": {
        "core": {"injected": 0, "referenced": 0},
        "dependency": {"injected": 0, "referenced": 0},
        "sibling": {"injected": 0, "referenced": 0},
    },
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


#: [evolution] keys that did something until #217 deleted the proposal
#: generator. Existing kstrl.toml files still set them, so they are neither
#: read nor refused: EvolutionConfig.load records which are present and
#: `ks evolve` names them.
RETIRED_EVOLUTION_KEYS: tuple[str, ...] = ("auto_propose", "auto_apply_computational")


@dataclass
class EvolutionConfig:
    enabled: bool = True
    journal_path: Path = field(default_factory=lambda: Path(".kstrl/evolution.jsonl"))
    experiments_path: Path = field(default_factory=lambda: Path(".kstrl/experiments.tsv"))
    min_pattern_frequency: int = 2
    lookback_runs: int = 10
    # #217: the [evolution] keys of the deleted proposal generator that
    # load() found in kstrl.toml, so `ks evolve` can name them instead of
    # dropping them silently. Provenance, not a setting: it has no toml
    # key of its own, and metadata["provenance"] keeps the surfaces that
    # sweep dataclass fields (cli._collect_toml_notes, gen_docs) off it.
    retired_keys: tuple[str, ...] = field(default=(), metadata={"provenance": True})

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> EvolutionConfig:
        """Load evolution config from environment variables only.

        Relative journal/experiments paths resolve against ``root_dir``
        (the project root), not the process CWD, matching :meth:`load`.
        """
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        _apply_env_overrides(config, root_dir)
        _resolve_relative_paths(config, root_dir)
        return config

    @classmethod
    def load(cls, root_dir: Path | None = None) -> EvolutionConfig:
        """Load evolution config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "evolution")
        if "enabled" in section:
            config.enabled = bool(section["enabled"])
        if "journal_path" in section:
            jp = str(section["journal_path"])
            config.journal_path = Path(jp) if Path(jp).is_absolute() else root_dir / jp
        if "experiments_path" in section:
            ep = str(section["experiments_path"])
            config.experiments_path = Path(ep) if Path(ep).is_absolute() else root_dir / ep
        if "min_pattern_frequency" in section:
            config.min_pattern_frequency = int(section["min_pattern_frequency"])
        if "lookback_runs" in section:
            config.lookback_runs = int(section["lookback_runs"])
        config.retired_keys = tuple(key for key in RETIRED_EVOLUTION_KEYS if key in section)
        _apply_env_overrides(config, root_dir)
        _resolve_relative_paths(config, root_dir)
        return config

    @classmethod
    def load_or_none(
        cls,
        root_dir: Path,
        warn: Callable[[str], None],
    ) -> EvolutionConfig | None:
        """:meth:`load`, but a config that will not parse returns None.

        The journal is an optional audit trail and every caller loads it
        in the middle of work that has already been paid for, so a typo
        in one of its knobs should cost the journal, not the run.

        Which exceptions that means is stated here rather than at a call
        site, because it is a fact about :meth:`load`: ``ValueError``
        from malformed TOML or a non-integer ``lookback_runs`` (from the
        file or from ``KSTRL_EVOLUTION_LOOKBACK_RUNS``), ``TypeError``
        from a toml array where a number belongs, and ``OSError`` from
        an unreadable ``kstrl.toml``. A future coercion added to
        ``load`` is then covered here instead of silently escaping a
        guard somebody wrote around a call.

        ``TypeError`` was added when #272 gave the same section an entry
        check: ``config_preflight.REJECTIONS`` treats it as operator
        input, and two lists that disagree would degrade the same value
        at startup and then raise on it mid-run.

        It is deliberately NOT
        ``config_preflight.SURFACE_REJECTIONS``, which is this tuple
        plus ``RuntimeError``. #289 tried importing that instead, on
        the reasoning above, and
        ``test_decompose.py::test_the_artifact_is_written_before_any_journal_work``
        failed: that test raises ``RuntimeError`` from :meth:`load` on
        purpose, to assert that an error the guard does NOT catch still
        leaves the halt artifact on disk. No coercion in :meth:`load`
        produces one, so widening to it could only ever swallow a
        defect, and the entry check degrading where this raises is the
        price of keeping that defect visible mid-run.

        The cost of that widening, stated because it is real: a
        ``TypeError`` from a DEFECT inside :meth:`load` - a None where a
        path belongs, a signature that stopped matching - now reads as
        "config unreadable, skipping journal" rather than surfacing. It
        cannot be narrowed to the toml-array case without inspecting the
        message, which would be guessing. The journal going quiet is the
        signal that a defect is hiding here, so treat a "skipping
        journal" warning on a config that looks correct as a bug report
        about this method rather than about the operator's file.

        Degrades loudly: ``warn`` is called with the parse failure.
        """
        try:
            return cls.load(root_dir)
        except (ValueError, TypeError, OSError) as exc:
            warn(f"Evolution config unreadable, skipping journal: {exc}")
            return None


def _apply_env_overrides(config: EvolutionConfig, root_dir: Path) -> None:
    """Overlay env vars that are explicitly set; unset vars leave the
    existing value untouched (so toml values survive the overlay)."""
    if "KSTRL_EVOLUTION_ENABLED" in os.environ:
        config.enabled = os.environ["KSTRL_EVOLUTION_ENABLED"].lower() in {
            "1",
            "true",
            "yes",
        }
    if "KSTRL_EVOLUTION_JOURNAL_PATH" in os.environ:
        raw = os.environ["KSTRL_EVOLUTION_JOURNAL_PATH"]
        config.journal_path = Path(raw) if Path(raw).is_absolute() else root_dir / raw
    if "KSTRL_EVOLUTION_LOOKBACK_RUNS" in os.environ:
        config.lookback_runs = int(os.environ["KSTRL_EVOLUTION_LOOKBACK_RUNS"])


def _resolve_relative_paths(config: EvolutionConfig, root_dir: Path) -> None:
    """Anchor relative journal/experiments paths to the project root.

    The bare ``EvolutionConfig()`` constructor keeps its historical
    CWD-relative defaults; the load/from_env paths always hand back
    absolute paths so ``ks factory --root X`` run from elsewhere
    cannot scatter ``.kstrl/`` state into the operator's CWD.
    """
    if not config.journal_path.is_absolute():
        config.journal_path = root_dir / config.journal_path
    if not config.experiments_path.is_absolute():
        config.experiments_path = root_dir / config.experiments_path


@dataclass
class FailurePattern:
    description: str
    frequency: int
    total_components: int
    affected_components: list[str]
    check_name: str  # e.g. "test_suite", "typecheck", "linter", "review"
    # structured failure code (e.g. "S608" for ruff, "arg-type" for mypy,
    # "scope_creep" for a review concern) - the part after the colon in
    # the full "<check>:<code>" signature
    error_signature: str
    # A _CATEGORY_BY_CHECK value, or UNENROLLED_CATEGORY; see category_for_check.
    category: str
    # #496: True when every row the signature was counted from is a
    # findings_superseded row, so it was never on a component's final attempt.
    superseded_only: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Regex for linter rule codes like S608, E501, W291, etc.
_LINTER_CODE_RE = re.compile(r"\b([A-Z]\d{3,4})\b")

# Regex to strip file paths (unix and windows style)
_PATH_RE = re.compile(r"(?:/[\w./-]+|[A-Z]:\\[\w.\\-]+)")

# Regex to strip line/column numbers like ":42:" or "line 42"
_LINENO_RE = re.compile(r"(?::\d+:?\d*|line \d+|col(?:umn)? \d+)", re.IGNORECASE)

# Regex to strip quoted variable/argument names
_QUOTED_NAME_RE = re.compile(r"['\"][\w.]+['\"]")


def _normalize_error(error: str) -> str:
    """Normalize an error string into a stable signature.

    - Keeps linter rule codes as-is (e.g. S608, E501).
    - Strips file paths, line numbers, and variable names.
    - Converts the remaining message to a slug.
    """
    if not error:
        return ""

    # Check for a linter rule code first - it is the most stable identifier.
    code_match = _LINTER_CODE_RE.search(error)
    if code_match:
        return code_match.group(1)

    normalized = error
    normalized = _PATH_RE.sub("", normalized)
    normalized = _LINENO_RE.sub("", normalized)
    normalized = _QUOTED_NAME_RE.sub("", normalized)

    # Take the first meaningful line only.
    first_line = normalized.strip().split("\n")[0].strip()

    # Extract "ErrorType: message" pattern if present.
    colon_idx = first_line.find(":")
    if colon_idx > 0:
        error_type = first_line[:colon_idx].strip()
        message = first_line[colon_idx + 1 :].strip()
        # Slugify message portion.
        slug = re.sub(r"[^a-z0-9]+", "-", message.lower()).strip("-")
        if slug:
            return slug[:80]
        return re.sub(r"[^a-z0-9]+", "-", error_type.lower()).strip("-")[:80]

    slug = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
    return slug[:80] if slug else "unknown"


def _classify_check(error: str) -> str:
    """Return the check name inferred from the error text.

    The legacy fallback, used wherever a component has no in-memory
    ``failure_signatures`` entry - which is EVERY ``ks evolve`` or
    metrics read over a manifest written by an earlier process. So a
    failure whose text lands here unrecognised is filed as
    unknown/iteration no matter how carefully it was categorised at the
    time it happened.

    #294 round 2: that is what happened to the scope refusal. Its own
    ``_CATEGORY_BY_CHECK`` row was unreachable on this path, so ``ks
    evolve`` filed it under a check name that was not its own. Matched
    FIRST because the text also contains words the later rules claim.

    #315: this returned ``(check_name, category)`` until every caller
    was measured to discard the category, making it a SECOND place a
    category was decided that no consumer could reach. A dead answer
    that can silently disagree with the live one is how the live one
    later gets "fixed" in the wrong file, so the category is gone from
    here and ``category_for_check`` is the only decision.
    """
    lower = error.lower()

    if SCOPE_UNREADABLE_ERROR_PREFIX.lower() in lower:
        return SCOPE_UNREADABLE_CHECK
    if any(kw in lower for kw in ("ruff", "flake8", "pylint", "lint")):
        return "linter"
    if any(kw in lower for kw in ("mypy", "pyright", "typecheck", "type error")):
        return "typecheck"
    if any(kw in lower for kw in ("pytest", "test", "assert", "unittest")):
        return "test_suite"
    if any(kw in lower for kw in ("review", "finding", "reviewer")):
        return "review"
    if any(kw in lower for kw in ("contract", "integration")):
        return "contract"
    if any(kw in lower for kw in ("mechanical verification failed",)):
        return "verification"

    return "unknown"


# ---------------------------------------------------------------------------
# Structured failure signatures (R6.1)
#
# A failure signature is "<check_name>:<code>", e.g. "linter:E501",
# "typecheck:arg-type", "review:scope_creep", "diff_scope:files-outside-
# allowed-scope". The check prefix comes from the gate that fired; the
# code comes from the tool's parser (ruff rule, mypy error code, finding
# category) rather than from re-parsing a flattened error string, so
# cross-run grouping is on real, stable identifiers.
# ---------------------------------------------------------------------------

# Digit runs are counts/limits ("3 files outside scope", "600s wall
# clock") - stripping them keeps slugs stable across runs whose only
# difference is the number.
_DIGIT_RUN_RE = re.compile(r"\d+")

# The categories are verification, review, security, contract,
# iteration and, since #315, infrastructure. That last one is for a
# failure that is neither a gate's verdict on the change nor the
# engineer's loop: the run was shut down, hit the token ceiling, could
# not merge, could not fetch its own diff, could not provision a
# worktree. Before it existed those fell through to "iteration", so the
# journal filed them as engineer-loop problems and the evolve screen's
# category column said so: a verdict about an agent that could not have
# prevented any of them.
#
# NOT Finding.category's "infrastructure_error" (kstrl/findings.py),
# which marks one ROLE RUN that failed to execute inside an otherwise
# healthy component. Different taxonomy, different consumer,
# deliberately different spelling so a grep for either does not silently
# return the other.
#
# They also disagree, on purpose and measurably. factory's live autonomy
# accounting asks the FINDING question (`_infra_casualty`) while the
# replay asks the SIGNATURE question, and #339 review counted the
# divergence rather than leaving it at the one example this comment used
# to give. Seven signatures, in both directions:
#
#   - the whole `pr:` family, `provisioning:` and `aborted:shutdown` are
#     infrastructure to the replay and attach no finding at all, so the
#     live side calls them judgement;
#   - `review:infrastructure` and `security:infrastructure` are the
#     reverse: a finding is attached, but `review`/`security` are not
#     infrastructure prefixes, so the replay counts them as evidence;
#   - `scope_unreadable:` depends on which producer fired - the Phase 1
#     gate attaches a finding, the pre-launch refusal in factory does
#     not;
#   - `adversarial_budget:claim` is the seventh, added by #226 round
#     2 and of the first kind: the R10.3 claim gate refuses a
#     component whose reviewer never ran, the only finding on it is the
#     phase_skipped trace, so the replay calls it plumbing and the live
#     side calls it judgement. Its two siblings do NOT diverge -
#     `adversarial_budget:review` and `adversarial_budget:security` come
#     from `pipeline._budget_refusal`, which attaches the
#     infrastructure_error finding, so both consumers read the same
#     answer. Enrolling the check is what fixed those two and what
#     exposed this one: before the sweep it was spelled
#     `review:claim-budget-exhausted` and both consumers agreed by
#     both being wrong. tests/test_claim_agreement.py::
#     test_the_claim_refusal_is_a_disclosed_divergence asserts both
#     halves, so this row fails if it stops being true.
#
# Reconciling those is not this table's job (#332 holds factory.py), but
# an undercount was, because it read as a single known exception.
#
# tests/test_check_name_enrolment.py pins the whole table row by row, so
# a new row, a dropped row or a typo in a category is a red test with
# the diff as its audit trail.
_CATEGORY_BY_CHECK = {
    "linter": "verification",
    "typecheck": "verification",
    "test_suite": "verification",
    "diff_scope": "verification",
    # #294 split this out of diff_scope; diff_scope stays because
    # journal entries written before the split carry its signatures.
    SCOPE_UNREADABLE_CHECK: "verification",
    "bad_patterns": "verification",
    "self_critique": "verification",
    "dead_code": "verification",
    # #335 split the fused dead-code row in two: `dead_code` stayed on
    # the vulture-or-dead_code_command phase and this is the ruff
    # F401/F811/F841 phase beside it.
    "dead_code_ruff": "verification",
    "mutation_testing": "verification",
    # R8.5 Layer 1 (#152). Advisory and it never fails, so it files no
    # signature today; enrolled anyway because the table is the list of
    # check names, not the list of names that have failed so far.
    "patch_coverage": "verification",
    # R8.5 Layer 2 (#152). Same reasoning as patch_coverage: advisory,
    # never fails, no signature today, enrolled because the table is
    # every check name.
    "diff_mutation": "verification",
    "prd_stories": "verification",
    "verification": "verification",
    # #315: mechanical gates that the table did not carry, so every
    # approved-fixtures failure, policy-envelope breach and
    # test-adequacy block was filed under the engineer loop.
    "fixtures": "verification",
    "policy_envelope": "verification",
    "test_adequacy": "verification",
    # #315 round 2: a failure recorded with no signatures= is filed
    # under its PHASE (pipeline._record_failure_signatures), so these
    # two are check names as much as any gate is. "verify" is the phase
    # spelling of the mechanical gates above; "provisioning" is a
    # worktree that would not build, which no agent can write code
    # against.
    "verify": "verification",
    "provisioning": "infrastructure",
    # #315: recorded outside a CheckResult, by pipeline.fail(signatures=
    # ["<prefix>:<code>"]). None of these is a verdict on the change.
    "aborted": "infrastructure",
    "token_budget": "infrastructure",
    "pr": "infrastructure",
    "diff": "infrastructure",
    # R10.5 (#226): a hard-mode reviewer that never ran because
    # max_adversarial_calls was already spent. Infrastructure for the
    # same reason token_budget is: it is a ceiling the operator set,
    # not a verdict on the change, and no reviewer looked at the
    # component. Enrolling it here is what makes the replay agree with
    # factory's live accounting, which reads the infrastructure_error
    # finding the same refusal attaches.
    ADVERSARIAL_BUDGET_CHECK: "infrastructure",
    # #315: these two rows state "iteration" explicitly, and since #496
    # they are the only way a name reaches it: the fallback for a name
    # the table does not carry is UNENROLLED_CATEGORY. "unknown" is what
    # _classify_check returns when it cannot recognise a legacy error
    # string: not the engineer's fault so much as nobody's, and inventing
    # a category for "we could not tell" would be a worse answer than
    # the one this table has always given.
    "engineer": "iteration",
    "unknown": "iteration",
    "review": "review",
    "security": "security",
    "contract": "contract",
}

#: Checks whose failures are infrastructure, derived from the table so
#: there is ONE answer rather than two lists to keep in step:
#: :data:`kstrl.autonomy_replay.INFRA_FAILURE_PREFIXES` builds its
#: prefixes from this, so enrolling a check as infrastructure reaches
#: both consumers in one edit (#315).
INFRASTRUCTURE_CHECKS: frozenset[str] = frozenset(
    name for name, category in _CATEGORY_BY_CHECK.items() if category == "infrastructure"
)

#: The categories a recurring failure can teach a lesson in (#217 Slice 1).
#: ``route_patterns`` puts a pattern in ``lessons`` when its check's
#: category is one of these. "infrastructure" is not here because no agent
#: can act on it, "iteration" because it names the engineer loop rather
#: than a check, and :data:`UNENROLLED_CATEGORY` because a name the table
#: does not carry must never become a lesson.
LESSON_CATEGORIES: frozenset[str] = frozenset({"verification", "review", "security", "contract"})

# Cap on distinct per-check signatures so one catastrophic run (e.g. 40
# distinct ruff rules) cannot flood the journal entry.
_MAX_SIGNATURES_PER_CHECK = 5


def signature_slug(text: str) -> str:
    """Stable low-cardinality slug for a failure message.

    Strips file paths, line/column numbers, quoted names, and standalone
    counts, then slugifies the first line. Unlike ``_normalize_error``
    this never extracts linter codes (callers get those from the parser
    directly) and never keeps varying counts."""
    if not text:
        return ""
    normalized = _PATH_RE.sub("", text)
    normalized = _LINENO_RE.sub("", normalized)
    normalized = _QUOTED_NAME_RE.sub("", normalized)
    normalized = _DIGIT_RUN_RE.sub("", normalized)
    first_line = normalized.strip().split("\n")[0].strip()
    slug = re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
    return slug[:60]


def signature_for_error(check_name: str, error: str) -> str:
    """Fallback signature when no parser-level codes are available."""
    slug = signature_slug(error) or "failed"
    return f"{check_name or 'unknown'}:{slug}"


def split_signature(signature: str) -> tuple[str, str]:
    """Split "check:code" into (check_name, code)."""
    check, sep, code = signature.partition(":")
    if not sep:
        return "unknown", signature
    return check or "unknown", code or "failed"


#: The category of a check name ``_CATEGORY_BY_CHECK`` does not carry
#: (#496). Not a learnable category: ``route_patterns`` sends it to
#: ``unrouted``. Before #496 an unlisted name fell through to
#: ``"iteration"``, a learnable category, which
#: docs/continuous-learning-design.md section 9 requires inverted before
#: anything writes to a store shared across repositories.
UNENROLLED_CATEGORY = "unenrolled"


def category_for_check(check_name: str) -> str:
    """Map a check/gate name to a FailurePattern category.

    An unlisted name returns :data:`UNENROLLED_CATEGORY`. Until #496 it
    fell through to "iteration", which filed a gate under the engineer
    loop. Enrolling a new check in
    ``_CATEGORY_BY_CHECK`` was a convention with no mechanism, and
    measured during #294 the convention did not hold for eight of the
    nineteen names ``kstrl/`` emits. All eight are enrolled as of #315,
    four of them into the ``"infrastructure"`` category that had to be
    invented to hold them, and nothing is grandfathered any more.

    ``tests/test_check_name_enrolment.py`` is the mechanism, and #339
    review corrected what it establishes. It AST-walks ``kstrl/`` for
    the four places a component's failure signatures are written - a
    ``CheckResult`` name, a ``signatures=`` argument, the ``phase=`` of
    a failure recorded with neither, and a direct
    ``component_failure_signatures[...] = [...]`` assignment - and fails
    on a name this table does not carry. What it does NOT establish is
    that no gate can reach the journal uncategorised, which is what this
    docstring claimed while the fourth producer was invisible to it and
    eight of nine shape mutations survived.

    What it establishes now, in three files and stated as three separate
    claims because they hold three separate things:

    - a producer site the walk RECOGNISES and cannot read is enumerated
      in its ``BLIND_SITES`` ledger rather than dropped, which is what
      the old walk did;
    - ``tests/test_signature_spellings.py`` pins every signature-shaped
      string in the package whatever container it sits in, so a producer
      in a shape nobody has thought of still has to appear somewhere;
    - ``tests/test_check_name_shapes.py`` pins what the walk SAYS about
      each shape, including the shapes it says nothing about, because a
      shape the walk does not recognise leaves no trace in either of the
      two above. That is a limit, not a proof, and it is the limit
      ``pipeline._fail_pr_flow`` lived in until #339 review.

    The answer is computed here, at read time, and never stored in the
    journal - measured: a ``record_run`` entry has no ``category`` key
    and experiments.tsv records the signature only. So a correction to
    the table also corrects what is reported about runs that already
    happened. The one surface that displays it is the evolve screen's
    patterns table; the ``ks evolve`` CLI prints the check name.
    """
    return _CATEGORY_BY_CHECK.get(check_name, UNENROLLED_CATEGORY)


@dataclass(frozen=True)
class PatternRouting:
    """Where each cross-run pattern goes. Three buckets, one census.

    The three tuples partition the input: their lengths sum to the
    number of patterns routed, and ``tests/test_evolve_routing.py``
    asserts that over every name in ``_CATEGORY_BY_CHECK``. A census
    rather than a ledger of exceptions, because a ledger is closed only
    over the shapes someone already enumerated (CLAUDE.md).

    - ``mechanical``: something is broken, not a lesson. The pipeline
      already opens a deduped ``ItemKind.HALTED_RUN`` inbox item for
      this traffic, so it has a destination; what it must not have is a
      lesson telling an agent to take extra care about a failed git push.
    - ``lessons``: a check whose category is in
      :data:`LESSON_CATEGORIES`. Nothing writes them anywhere yet (#217
      Slice 1 deleted the proposal generator); ``ks evolve`` prints them
      as candidate lessons.
    - ``unrouted``: everything else: the ``iteration`` rows, and every
      check name ``_CATEGORY_BY_CHECK`` does not carry, whose category
      is :data:`UNENROLLED_CATEGORY` since #496.
    """

    mechanical: tuple[FailurePattern, ...]
    lessons: tuple[FailurePattern, ...]
    unrouted: tuple[FailurePattern, ...]


def route_patterns(patterns: list[FailurePattern]) -> PatternRouting:
    """Split patterns into mechanical, lessons and unrouted.

    Keyed on ``check_name`` through ``category_for_check``, never on the
    stamped ``FailurePattern.category``: the table is the one answer, and
    a record whose stamped field disagrees with it routes by the table.
    The stamped field stays what it has always been: what the evolve
    screen displays.
    """
    mechanical: list[FailurePattern] = []
    lessons: list[FailurePattern] = []
    unrouted: list[FailurePattern] = []
    for pattern in patterns:
        category = category_for_check(pattern.check_name)
        if category == "infrastructure":
            mechanical.append(pattern)
        elif category in LESSON_CATEGORIES:
            lessons.append(pattern)
        else:
            unrouted.append(pattern)
    return PatternRouting(
        mechanical=tuple(mechanical),
        lessons=tuple(lessons),
        unrouted=tuple(unrouted),
    )


def _check_signatures(check: CheckResult, limit: int | None) -> Counter[str]:
    """One FAILED check's signatures, counted by OCCURRENCE, in first-seen order.

    ``limit`` caps the DISTINCT codes, not the occurrences: a check that hit
    E501 twelve times and F401 once contributes both under ``limit=2``, twelve
    times and once.

    ``Counter`` over a list because a ``Counter`` built from an iterable is a
    dict subclass that keeps first-seen order, so ``list(tally)`` is the same
    sequence ``dict.fromkeys`` gave, and the count is read rather than
    accumulated one occurrence at a time.
    """
    parsed = check.parsed
    tally = Counter(f.code for f in parsed.failures if f.code) if parsed is not None else Counter()
    if not tally:
        return Counter({signature_for_error(check.name, check.message): 1})
    kept = list(tally) if limit is None else list(tally)[:limit]
    return Counter({f"{check.name}:{code}": tally[code] for code in kept})


def signature_counts_from_verification(
    checks: Iterable[CheckResult],
    *,
    limit: int | None = _MAX_SIGNATURES_PER_CHECK,
) -> dict[str, int]:
    """How many times each structured signature occurred in failed checks.

    OCCURRENCES, not presence: twelve E501 failures count twelve. That is
    what :mod:`kstrl.baseline`'s baseline needs and what
    :func:`signatures_from_verification` cannot give, because it ends in a
    dedupe - every count built from its return value would be 1 and a
    baseline's "this got worse" bucket could never fire (#227).

    ``limit`` caps the DISTINCT codes one check contributes, in first-seen
    order, before anything is counted. ``None`` means no cap. The default is
    the journal's own constant, so a defaulted call is byte-identical to what
    the journal recorded before this parameter existed.

    The ``"<check>:<code>"`` spelling lives in this ONE MODULE, in three
    functions that have to agree: this one composes it through
    :func:`_check_signatures`, :func:`signature_for_error` writes the
    ``"<check>:<slug>"`` fallback for a check with no parsed codes - which is
    most real signatures, including every one the baseline falls back to - and
    :func:`split_signature` takes it apart again, which is how
    :func:`kstrl.baseline.compare` recovers a check name before deciding
    ``fixed``. "One place" was the earlier claim and it was wrong; one module
    is what the guard against a format changed in one place only actually is.
    """
    counts: Counter[str] = Counter()
    for check in checks:
        if not check.passed:
            counts.update(_check_signatures(check, limit))
    return dict(counts)


def signatures_from_verification(
    checks: Iterable[CheckResult],
    *,
    limit: int | None = _MAX_SIGNATURES_PER_CHECK,
) -> list[str]:
    """Derive structured signatures from failed mechanical checks.

    Prefers the parser's structured codes (linter rule, checker error
    code, the exception a test died on); falls back to a slug of the
    check message when no parse is available.

    #258: this used to ask ``ParsedOutput.tool`` which of those it was,
    against the exact strings "ruff", "mypy" and "pytest". That is a name
    check standing in for a capability check, and it broke twice over as
    soon as a gate could dispatch: a newly supported tool fell silently
    through to the prose slug, and the unioned label a chained command
    produces ("pytest+vitest") matched nothing at all. The parser now
    names its own signature in ``ParsedFailure.code``, so this reads a
    capability instead of guessing from a label.

    #227: the answer is now the KEYS of
    :func:`signature_counts_from_verification`, which returns each signature
    once in the same first-seen order the old ``dict.fromkeys`` produced.
    ``limit`` is keyword-only with the journal's cap as its default: the one
    production caller passes neither, so its expression text does not move,
    and a defaulted call still records exactly what it recorded before.
    ``limit=None`` is the uncapped read the baseline asks for -
    a baseline that dropped a check's sixth signature would report it as new
    on the very next run.
    """
    return list(signature_counts_from_verification(checks, limit=limit))


def signatures_from_findings(
    phase: str,
    findings: Iterable[Finding],
    gating: frozenset[str] = frozenset({"fail", "critical", "high"}),
) -> list[str]:
    """Derive signatures from the typed findings that failed a review or
    security gate: "<phase>:<category>" for every finding whose severity
    is in ``gating`` and "<phase>:infrastructure" when the role itself
    failed to run. Phase 2.5 passes its result's own failing severities,
    because its ``fail_threshold`` decides which findings failed (#524)."""
    signatures: list[str] = []
    for finding in findings:
        if finding.is_infrastructure_error:
            signatures.append(f"{phase}:infrastructure")
        elif finding.severity in gating:
            signatures.append(f"{phase}:{finding.category}")
    return list(dict.fromkeys(signatures))[:_MAX_SIGNATURES_PER_CHECK]


def _timestamp_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _journal_line(entry: dict[str, Any]) -> str:
    """One JSONL line, terminator included. The journal's line format.

    Stamps the kstrl that wrote the row (#451), so every row carries it
    whichever of the journal's writers built the dict.
    """
    return json.dumps({**entry, "kstrl_version": kstrl_version()}, separators=(",", ":")) + "\n"


def _log_experiments_repair(repaired: bool, path: Path) -> None:
    """Say that a torn experiments.tsv was padded, since the file cannot.

    The other five appenders record a repair as a row their own reader
    returns. This one pads bare, because every marker a TSV can carry is
    a field and a row of fields is a run, so the log is the only place
    the incident can go. Without it a crash that tore this file and cost
    a run is something nothing in the product ever says: the pad leaves
    a short fragment, ``experiment_rows`` drops it on width, and there
    is no counter on that path either.

    A function rather than an ``if`` inside :meth:`record_run` because
    that method is at the cyclomatic ratchet's grandfathered value and
    one more branch would push it up. It also gives the sentence one
    home.
    """
    if repaired:
        logger.warning(
            "experiments.tsv did not end in a newline, so a crash tore it: %s. "
            "The tail was padded onto a line of its own, so the row above this "
            "run may be a fragment and the run it belonged to was lost.",
            path,
        )


def _repair_entry() -> dict[str, Any]:
    """The row :meth:`EvolutionJournal.append_entries` writes on finding
    an unterminated tail.

    Carries no ``run_id`` on purpose: ``_read_journal_entries`` keeps the
    last N distinct run_ids, so a repair row with one of its own would be
    one of the N and a single tear would shorten the history every
    aggregate reads by a whole run.
    """
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "timestamp": _timestamp_now(),
        "event_type": JOURNAL_REPAIR_EVENT,
        "detail": REPAIR_DETAIL,
    }


def _summarize_findings(findings: list[Finding]) -> dict[str, Any]:
    """Aggregate counts grouped by phase, severity, category, and OWASP
    bucket for the evolution journal. Lets dashboards query trends
    without re-walking every Finding."""
    summary: dict[str, Any] = {
        "total": len(findings),
        "by_phase": {},
        "by_severity": {},
        "by_category": {},
        "by_owasp": {},
        "infrastructure_errors": 0,
    }
    for f in findings:
        if f.is_infrastructure_error:
            summary["infrastructure_errors"] += 1
        summary["by_phase"][f.phase] = summary["by_phase"].get(f.phase, 0) + 1
        summary["by_severity"][f.severity] = summary["by_severity"].get(f.severity, 0) + 1
        summary["by_category"][f.category] = summary["by_category"].get(f.category, 0) + 1
        if f.owasp:
            summary["by_owasp"][f.owasp] = summary["by_owasp"].get(f.owasp, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


def _components_this_run(manifest: Manifest, factory_result: FactoryResult) -> list[Component]:
    """The manifest components this run touched, in manifest order (#447).

    ``scheduled`` is every component handed to the executor, which covers
    one that ended MERGE_PENDING or was stopped mid-attempt; ``failed`` and
    ``skipped`` add the failures and cascade skips made without a launch
    (a merge re-poll that finds the PR closed, a contract breaker, a
    cascade skip). ``completed`` is left out on purpose: every completion
    this run launched is already in ``scheduled``, and the only other one
    is a merge the re-poll confirms, whose iterations, duration and
    findings belong to the earlier run that did the work. A component in
    none of the three carried its state from an earlier run and gets no row.

    A component can also be in ``failed`` or ``skipped`` without ever
    being in ``scheduled``: a merge re-poll that finds the parked PR
    closed moves it straight to FAILED (kstrl/pipeline.py
    ``repoll_merge_pending``), and its cascade skips follow the same way.
    That component is admitted here (its FAILED transition is real and
    must be recorded), but it never ran in THIS run, so its row's
    iteration_count, duration_seconds and retries must not be its
    earlier run's numbers under a new run id - :func:`record_run` zeroes
    those three fields for it via :func:`_effective_result_fields`.
    """
    touched = {
        *factory_result.scheduled,
        *factory_result.failed,
        *factory_result.skipped,
    }
    return [comp for comp in manifest.components if comp.id in touched]


def _effective_result_fields(comp: Component, launched: set[str]) -> tuple[int, int, int, float]:
    """(retries, first_attempt, iteration_count, duration_seconds) for ``comp``'s row.

    A component this run moved to FAILED or skipped without launching it
    (a merge re-poll that finds the parked PR closed, and the cascade
    skips that follow) keeps whatever iteration_count, duration_seconds
    and retries an EARLIER run left on the manifest. Reporting those
    under this run's id would double-count that earlier run's work
    (#447, one path over: the same phantom-copy defect the writer fix
    above removes, reachable through ``failed``/``skipped`` instead of a
    carried manifest state). Only a component this run actually launched
    reports its own numbers; a component admitted without a launch
    reports zero for all three. ``status``, ``error`` and
    ``failure_signatures`` are untouched: the FAILED transition and its
    ``pr:closed-without-merge`` signature are real.

    The one exception (#463): a component whose earlier attempts this run
    took over from a run it resumed (``first_attempt <= retries``) keeps its
    retries and first attempt, because those attempts' readings are this
    run's rows now; only its iteration count and duration are zero.
    """
    if comp.id in launched:
        return comp.retries, comp.first_attempt, comp.iteration_count, comp.duration_seconds
    if comp.first_attempt <= comp.retries:
        return comp.retries, comp.first_attempt, 0, 0.0
    return 0, 1, 0, 0.0


def _role_usage_entries(
    usage_by_component: dict[str, dict[str, dict[str, Any]]],
    *,
    journaled: set[str],
    manifest: Manifest,
    run_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """Journal rows for spend that belongs to no manifest component.

    ``record_run`` builds its rows by walking the MANIFEST, so a usage
    key with no component was dropped on the floor while ``run_usage`` -
    which does include it - fed the TSV's ``total_cost_usd``. The
    journal's per-component rows then did not sum to its own run total
    (#257 review). The architect is what made that reachable: a one-role
    pseudo-component that spends before any component exists and never
    appears in a manifest.

    "Never" became structural in #281. This split is a set difference
    against the manifest's ids, so while role keys were bare words a
    component genuinely named `architect` swallowed the role row: the
    difference was empty, the spend was attributed to the component's
    ``usage`` field, and no ``role_usage`` row was written at all.
    ``names.role_component_key`` puts role keys where no component id can
    be spelled, so the two sets are now disjoint by construction rather
    than by what the architect happened to name things.

    Since #463 the difference is against ``journaled``, the components this
    run writes a ``component_result`` row for, not every manifest id: a run
    that resumes a killed one takes over the killed run's spend, including a
    component the killed run finished and this run does not run again, and
    that component's spend has no row of its own to sit on.

    A distinct ``event_type`` rather than a synthetic
    ``component_result``, because every field that row carries - status,
    retries, findings, failed_phase - is meaningless for something that
    is not a component, and three readers in this module aggregate over
    ``component_result`` specifically. They ignore this type, which is
    the point: the row records spend without inventing an outcome.
    """
    return [
        {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "timestamp": timestamp,
            "run_id": run_id,
            "project": manifest.project_name,
            "component_id": role,
            "event_type": "role_usage",
            "usage": usage_by_component[role],
        }
        for role in sorted(set(usage_by_component) - journaled)
    ]


class EvolutionJournal:
    def __init__(self, config: EvolutionConfig) -> None:
        self.config = config

    @classmethod
    def open(
        cls,
        root_dir: Path,
        warn: Callable[[str], None],
    ) -> EvolutionJournal | None:
        """The journal for ``root_dir``, or None when it is unusable.

        Every writer asks the same two questions in the same order -
        does the config parse, and is the journal switched on - and does
        the same thing on either No. Asking them here rather than at each
        site is what stops the pair drifting: measured, the four writers
        that existed before #257 disagreed, with two of them omitting the
        parse guard entirely and raising a config typo into work that had
        already been paid for.

        Which exceptions "does not parse" covers is ``load_or_none``'s to
        know, not a call site's. Degrades loudly through ``warn``.
        """
        config = EvolutionConfig.load_or_none(root_dir, warn=warn)
        if config is None or not config.enabled:
            return None
        return cls(config)

    # ------------------------------------------------------------------
    # record_run
    # ------------------------------------------------------------------

    def record_run(
        self,
        run_id: str,
        manifest: Manifest,
        factory_result: FactoryResult,
        usage_by_component: dict[str, dict[str, dict[str, Any]]] | None = None,
        run_usage: dict[str, Any] | None = None,
        failure_signatures: dict[str, list[str]] | None = None,
        fact_utilization: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Record a completed factory run to the journal.

        Writes individual component outcomes as JSONL entries.
        Also appends a summary line to experiments.tsv.

        #447: both describe only the components this run touched
        (:func:`_components_this_run`). A component that carried its state
        from an earlier run gets no row: the row would repeat that run's
        result under this run's id, and every reader would count it again.

        R3.1: ``usage_by_component`` maps component id -> phase ->
        UsageTotals.to_dict() and lands on each component's journal
        entry; ``run_usage`` is the run-level UsageTotals.to_dict() and
        feeds the TSV totals columns. Both optional so pre-R3.1 callers
        keep working; token/cost figures are CLI self-reports and are
        lower bounds whenever ``unreported_calls`` > 0.

        R6.1: ``failure_signatures`` maps component id -> the structured
        "<check>:<code>" signatures the factory recorded when the
        component's last attempt failed (e.g. "linter:E501",
        "review:scope_creep"). When absent for a failed component, the
        legacy flattened-string classification is the fallback so
        journal entries never lose the signature fields entirely.

        #191: ``fact_utilization`` maps component id -> ``{"measured",
        "injected", "referenced", "reason"}``. The key is written for
        EVERY component, present in the map or not. ``measured=False``
        means the run could not measure, which is not the same as a
        measured ``referenced=0``; reading a missing or false
        ``measured`` as a real zero is what made the L2+
        fact-utilization gate un-evidenceable. Only ``measured=True``
        entries are evidence.
        """
        from kstrl.manifest import ComponentStatus

        timestamp = _timestamp_now()
        usage_by_component = usage_by_component or {}
        failure_signatures = failure_signatures or {}
        fact_utilization = fact_utilization or {}

        # --- JSONL entries per component ---
        ran = _components_this_run(manifest, factory_result)
        launched = set(factory_result.scheduled)
        effective_by_comp: dict[str, tuple[int, int, float]] = {}
        entries: list[dict[str, Any]] = []
        for comp in ran:
            eff_retries, eff_first, eff_iteration_count, eff_duration = _effective_result_fields(
                comp, launched
            )
            # The TSV counts the retries this run answers for (#463).
            effective_by_comp[comp.id] = (
                eff_retries - eff_first + 1,
                eff_iteration_count,
                eff_duration,
            )
            has_error = bool(comp.error) and comp.status in (
                ComponentStatus.FAILED.value,
                ComponentStatus.PENDING.value,  # retried components reset to pending
            )
            comp_signatures: list[str] = []
            check_name = ""
            error_sig = ""
            if has_error:
                comp_signatures = list(failure_signatures.get(comp.id) or [])
                if not comp_signatures:
                    # Legacy fallback: classify the flattened string.
                    legacy_check = _classify_check(comp.error)
                    legacy_sig = _normalize_error(comp.error)
                    if legacy_sig:
                        comp_signatures = [f"{legacy_check}:{legacy_sig}"]
                if comp_signatures:
                    check_name, error_sig = split_signature(comp_signatures[0])
            # E3-consume: include typed findings in the journal so
            # downstream aggregations (concern hit-rate, OWASP-bucket
            # frequency, infrastructure_error rate) can query the
            # structured stream directly rather than re-parsing the
            # rendered string.
            findings_serialized = [f.to_dict() for f in comp.findings]
            findings_summary = _summarize_findings(comp.findings)
            entry = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "timestamp": timestamp,
                "run_id": run_id,
                "project": manifest.project_name,
                "component_id": comp.id,
                "event_type": "component_result",
                "status": comp.status,
                "retries": eff_retries,
                "first_attempt": eff_first,
                "error": comp.error,
                "check_name": check_name,
                "error_signature": error_sig,
                "failure_signatures": comp_signatures,
                "failed_phase": comp.failed_phase,
                "failed_check": comp.failed_check,
                "duration_seconds": eff_duration,
                "iteration_count": eff_iteration_count,
                "findings": findings_serialized,
                "findings_summary": findings_summary,
                "usage": usage_by_component.get(comp.id, {}),
                # #191: always present, so a pre-#191 journal (key
                # missing) is distinguishable from "measured=false, we
                # could not measure" and from "measured=true,
                # referenced=0", which is real evidence of unused facts.
                "knowledge_utilization": (
                    fact_utilization.get(comp.id) or dict(_UNMEASURED_UTILIZATION)
                ),
            }
            entries.append(entry)

        entries.extend(
            _role_usage_entries(
                usage_by_component,
                journaled={comp.id for comp in ran},
                manifest=manifest,
                run_id=run_id,
                timestamp=timestamp,
            )
        )

        try:
            self.append_entries(entries)
        except OSError as exc:
            logger.warning(
                "evolution journal write failed (non-fatal): %s: %s",
                self.config.journal_path,
                exc,
            )

        # --- Experiments TSV summary line ---
        total = len(ran)
        completed = len(factory_result.completed)
        failed = len(factory_result.failed)
        skipped = len(factory_result.skipped)

        # #447: a component admitted here without a launch (a merge
        # re-poll that finds the PR closed, and its cascade skips) has
        # its iteration_count, duration_seconds and retries zeroed in
        # ``effective_by_comp``, so its earlier run's numbers are not
        # averaged into THIS run's row under this run's id.
        iteration_counts = [
            effective_by_comp[c.id][1] for c in ran if effective_by_comp[c.id][1] > 0
        ]
        avg_iterations = sum(iteration_counts) / len(iteration_counts) if iteration_counts else 0.0

        durations = [effective_by_comp[c.id][2] for c in ran if effective_by_comp[c.id][2] > 0]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        retry_total = sum(effective_by_comp[c.id][0] for c in ran)
        retry_rate = retry_total / total if total > 0 else 0.0

        # Most common failure signature (full "<check>:<code>" form).
        failure_sigs: dict[str, int] = {}
        for comp in ran:
            if comp.status != ComponentStatus.FAILED.value or not comp.error:
                continue
            sigs = list(failure_signatures.get(comp.id) or [])
            if not sigs:
                sigs = [signature_for_error(_classify_check(comp.error), comp.error)]
            for sig in sigs:
                failure_sigs[sig] = failure_sigs.get(sig, 0) + 1
        common_failure = max(failure_sigs, key=failure_sigs.get, default="") if failure_sigs else ""  # type: ignore[arg-type]

        # R3.1 totals columns. Empty string (not 0) when no usage was
        # tracked for the run - zero would misread as "measured, free".
        # unreported_calls > 0 marks the token/cost figures as lower
        # bounds. Files written before R3.1 keep their shorter header;
        # experiment_rows keeps such a row while the file's header is a
        # PREFIX of the current one and reads it with the current header's
        # names, which is its second legal width. The reader has not been a bare
        # csv.DictReader since #331 and this comment named one until
        # #352 round 2.
        if run_usage:
            total_tokens_col = str(run_usage.get("total_tokens", ""))
            total_cost_col = str(run_usage.get("cost_usd", ""))
            unreported_col = str(run_usage.get("unreported_calls", ""))
        else:
            total_tokens_col = total_cost_col = unreported_col = ""

        # Joined on ExperimentsDialect's delimiter rather than on a
        # literal tab, so the one constant the reader parses with is the
        # one this row is built with. Identical bytes: the delimiter IS
        # "\t". Not through a csv.writer, because under QUOTE_NONE with
        # no escapechar a writer RAISES on a field holding a tab, and
        # record_run's only handler is `except OSError`; that would turn
        # residual 1 on experiment_rows from a dropped row into a lost
        # run, which is the worse of the two.
        row = ExperimentsDialect.delimiter.join(
            (
                run_id,
                timestamp,
                manifest.project_name,
                str(total),
                str(completed),
                str(failed),
                str(skipped),
                f"{avg_iterations:.2f}",
                f"{avg_duration:.1f}",
                f"{retry_rate:.2f}",
                common_failure,
                total_tokens_col,
                total_cost_col,
                unreported_col,
                kstrl_version(),
            )
        )

        try:
            self.config.experiments_path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = (
                not self.config.experiments_path.exists()
                or self.config.experiments_path.stat().st_size == 0
            )
            # #331: through appendio, which repairs an unterminated
            # tail before appending onto it. This file is the WORST of
            # the seven, because its reader RENDERS the corruption
            # instead of dropping it: measured, a torn row plus this
            # append put a run into `ks evolve --status` and the TUI
            # trends tab whose "completed" column held a timestamp,
            # while the run being recorded here vanished.
            #
            # A BARE PAD, no marker row. Every marker a TSV can carry is
            # a field, and a row of fields is a RUN to this file's
            # reader; there is no shape here that reads as "not a run"
            # the way JOURNAL_REPAIR_EVENT does in a JSONL file.
            #
            # So the incident is recorded in the log rather than in the
            # file. The pad means there is no concatenated row left for
            # get_experiment_trends to drop, only a short fragment it
            # drops silently, and a crash that tore this file and cost a
            # run would otherwise be something nothing in the product
            # ever says. That is the same argument #312 made for the
            # journal_repair row, on the one file that cannot carry one.
            #
            # needs_header is unchanged and still asked before the
            # append: a file that is missing or empty cannot have a torn
            # tail, so the two never both fire.
            #
            # The other side of the two-sided contract: utf-8 is pinned
            # in the helper, and this is the file get_experiment_trends
            # decodes as utf-8.
            payload = (EXPERIMENTS_HEADER + "\n" if needs_header else "") + row + "\n"
            _log_experiments_repair(
                append_records(self.config.experiments_path, payload, repair=""),
                self.config.experiments_path,
            )
        except OSError as exc:
            logger.warning(
                "experiments.tsv write failed (non-fatal): %s: %s",
                self.config.experiments_path,
                exc,
            )

    # ------------------------------------------------------------------
    # extract_failure_patterns (single run)
    # ------------------------------------------------------------------

    def extract_failure_patterns(
        self,
        manifest: Manifest,
        min_frequency: int = 2,
        signatures_by_component: dict[str, list[str]] | None = None,
    ) -> list[FailurePattern]:
        """Extract recurring failure patterns from a single run.

        Looks at failed/retried components to find common failure
        signatures, grouped by the full "<check>:<code>" signature.
        ``signatures_by_component`` carries the factory's structured
        signatures (R6.1); components absent from it fall back to
        classifying their flattened error string.
        """
        from kstrl.manifest import ComponentStatus

        signatures_by_component = signatures_by_component or {}

        # Collect components that failed or were retried.
        troubled: list[Component] = [
            c
            for c in manifest.components
            if c.status == ComponentStatus.FAILED.value or c.retries > 0
        ]

        if not troubled:
            return []

        # Group by full signature string.
        groups: dict[str, list[str]] = {}
        for comp in troubled:
            if not comp.error:
                continue
            sigs = list(signatures_by_component.get(comp.id) or [])
            if not sigs:
                legacy_check = _classify_check(comp.error)
                legacy_sig = _normalize_error(comp.error)
                if not legacy_sig:
                    continue
                sigs = [f"{legacy_check}:{legacy_sig}"]
            for sig in sigs:
                groups.setdefault(sig, []).append(comp.id)

        total = len(manifest.components)
        patterns: list[FailurePattern] = []
        for full_sig, comp_ids in groups.items():
            if len(comp_ids) < min_frequency:
                continue
            check_name, code = split_signature(full_sig)
            patterns.append(
                FailurePattern(
                    description=(
                        f"{check_name} failure '{code}' in {len(comp_ids)}/{total} components"
                    ),
                    frequency=len(comp_ids),
                    total_components=total,
                    affected_components=comp_ids,
                    check_name=check_name,
                    error_signature=code,
                    category=category_for_check(check_name),
                )
            )

        patterns.sort(key=lambda p: p.frequency, reverse=True)
        return patterns

    # ------------------------------------------------------------------
    # get_cross_run_patterns
    # ------------------------------------------------------------------

    def get_cross_run_patterns(
        self,
        lookback_runs: int = 10,
    ) -> list[FailurePattern]:
        """Get patterns that recur across multiple factory runs.

        Reads the journal, groups entries by their structured failure
        signatures ("<check>:<code>", R6.1), and returns patterns that
        appear in >= min_pattern_frequency distinct runs. Legacy v1
        entries without ``failure_signatures`` fall back to composing
        the signature from their check_name/error_signature fields.

        Since #496 the rows counted include every attempt a retry
        superseded (:func:`_signature_rows`). ``frequency`` is a count of
        distinct runs, so a signature on several attempts of one run
        counts once, and ``superseded_only`` marks a signature that was
        never on a component's final attempt.
        """
        entries = self._read_journal_entries(lookback_runs)
        if not entries:
            return []

        # Group by full signature across distinct run_ids.
        runs: set[str] = set()
        sig_runs: dict[str, set[str]] = {}
        sig_components: dict[str, list[str]] = {}
        on_a_final_attempt: set[str] = set()
        for run_id, comp_id, sigs, superseded in _signature_rows(entries):
            runs.add(run_id)
            for sig in sigs:
                sig_runs.setdefault(sig, set()).add(run_id)
                sig_components.setdefault(sig, []).append(comp_id)
                if not superseded:
                    on_a_final_attempt.add(sig)

        total_runs = len(runs)
        patterns: list[FailurePattern] = []

        for sig, run_ids in sig_runs.items():
            if len(run_ids) < self.config.min_pattern_frequency:
                continue
            check_name, code = split_signature(sig)
            unique_comps = list(dict.fromkeys(sig_components.get(sig, [])))
            patterns.append(
                FailurePattern(
                    description=(
                        f"'{sig}' appeared in {len(run_ids)}/{total_runs} runs "
                        f"across {len(unique_comps)} components"
                    ),
                    frequency=len(run_ids),
                    total_components=total_runs,
                    affected_components=unique_comps,
                    check_name=check_name,
                    error_signature=code,
                    category=category_for_check(check_name),
                    superseded_only=sig not in on_a_final_attempt,
                )
            )

        patterns.sort(key=lambda p: p.frequency, reverse=True)
        return patterns

    # ------------------------------------------------------------------
    # get_concern_hit_rate (D8)
    # ------------------------------------------------------------------

    def get_concern_hit_rate(self, lookback_runs: int = 10) -> dict[str, Any]:
        """Aggregate reviewer/security finding signal across recent runs.

        Returns ``{"runs": N, "components": M, "with_concern": K,
        "by_category": {...}}`` so dashboards can ask "did the
        adversarial reviewers surface anything across the last N runs?"

        R6.2: consumes the typed ``findings_summary`` that record_run
        writes on every component_result entry (E3 stream), replacing
        the old error-string scan that was structurally zero (concern
        categories never appeared in ``component.error``). A component
        counts as "with concern" when its summary has at least one
        finding in a real category - the synthetic
        ``infrastructure_error`` and ``phase_skipped`` categories mark
        non-execution, not adversarial signal, and are excluded.
        """
        entries = [
            e
            for e in self._read_journal_entries(lookback_runs)
            if e.get("event_type", "component_result") == "component_result"
        ]
        runs = len({e.get("run_id", "") for e in entries})
        components = len(entries)
        with_concern = 0
        by_category: dict[str, int] = {}
        for entry in entries:
            summary = entry.get("findings_summary") or {}
            cat_counts = summary.get("by_category") or {}
            if not isinstance(cat_counts, dict):
                continue
            hit = False
            for category, count in cat_counts.items():
                if category in ("infrastructure_error", "phase_skipped"):
                    continue
                try:
                    n = int(count)
                except (TypeError, ValueError):
                    continue
                if n <= 0:
                    continue
                hit = True
                by_category[category] = by_category.get(category, 0) + n
            if hit:
                with_concern += 1
        return {
            "runs": runs,
            "components": components,
            "with_concern": with_concern,
            "by_category": by_category,
        }

    def get_fact_utilization(self, lookback_runs: int = 10) -> dict[str, Any]:
        """Aggregate knowledge fact-utilization across recent runs (#191).

        Returns ``{"runs", "components", "measured", "unmeasured",
        "injected", "referenced", "runs_with_referenced"}``.

        Only ``measured=True`` entries contribute to ``injected`` and
        ``referenced``. An unmeasured component is counted under
        ``unmeasured`` and NEVER as a zero - that conflation is the
        defect this field exists to prevent. Entries written before
        #191 have no ``knowledge_utilization`` key and count as
        unmeasured.

        This is the query behind the L2+ cATO gate in
        ``docs/remediation-roadmap.md``: "two real factory runs with
        nonzero fact-utilization" is ``runs_with_referenced >= 2``.
        ``injected``/``referenced`` are lower bounds - see
        ``knowledge.measure_fact_utilization``.
        """
        entries = [
            e
            for e in self._read_journal_entries(lookback_runs)
            if e.get("event_type", "component_result") == "component_result"
        ]
        measured = unmeasured = 0
        injected = referenced = 0
        runs_with_referenced: set[str] = set()
        for entry in entries:
            util = entry.get("knowledge_utilization")
            if not isinstance(util, dict) or not util.get("measured"):
                unmeasured += 1
                continue
            try:
                n_injected = int(util.get("injected", 0))
                n_referenced = int(util.get("referenced", 0))
            except (TypeError, ValueError):
                # Present but unreadable is not evidence either.
                unmeasured += 1
                continue
            measured += 1
            injected += n_injected
            referenced += n_referenced
            if n_referenced > 0:
                runs_with_referenced.add(str(entry.get("run_id", "")))
        return {
            "runs": len({e.get("run_id", "") for e in entries}),
            "components": len(entries),
            "measured": measured,
            "unmeasured": unmeasured,
            "injected": injected,
            "referenced": referenced,
            "runs_with_referenced": len(runs_with_referenced),
        }

    # ------------------------------------------------------------------
    # get_experiment_trends
    # ------------------------------------------------------------------

    def get_experiment_trends(self, last_n: int = 10) -> list[dict[str, Any]]:
        """Read experiments.tsv and return the last N entries as dicts.

        Encoding is named and ``ValueError`` is caught beside
        ``OSError``, which is the house rule for a reader of any file
        kstrl writes (CLAUDE.md): ``UnicodeDecodeError`` IS a
        ``ValueError`` and escapes a fail-closed ``except OSError``.
        Measured before this line: a single non-utf-8 byte in
        experiments.tsv raised straight out of ``EvolveScreen.on_mount``
        two lines after the #289 config banner, which is that issue's
        own crash from that issue's own screen.

        The PARSE is guarded separately, and differently (#352 round 2).
        A read failure here is silent because a missing file is the
        ordinary state of a project that has recorded no runs, and
        because ``experiment_rows`` already documents ``[]`` as meaning
        three things. A parse refusal is not one of those three: it can
        only come from a file that exists and decoded, so it is an
        incident and it is logged with the path and the cause. The
        return stays ``[]`` rather than propagating, because this is
        called from ``EvolveScreen.reload`` on the Textual event loop,
        where a raise is the #289 crash again.
        """
        try:
            text = self.config.experiments_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return []

        try:
            return experiment_rows(text)[-last_n:]
        except ValueError as exc:
            logger.warning(
                "experiments.tsv could not be parsed, so no runs are shown: %s: %s",
                self.config.experiments_path,
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # #233: per-attempt iteration readings
    # ------------------------------------------------------------------

    def read_iteration_readings(self, rows: list[dict[str, Any]]) -> list[IterationReading]:
        """One reading per experiments.tsv row, in the rows' order."""
        entries = self._read_all_entries()
        readings: list[IterationReading] = []
        for row in rows:
            run_id = str(row.get("run_id", ""))
            raw = str(row.get("components_total", "")).strip()
            try:
                total = int(raw)
            except ValueError:
                readings.append(_refused(run_id, f"components_total is not an integer: {raw!r}"))
                continue
            readings.append(read_attempt_iterations(entries, run_id, total))
        return readings

    def iteration_criterion_lines(self, rows: list[dict[str, Any]]) -> list[str]:
        """The operator-facing #233 block, rendered where the path lives.

        ONE HOME, for the reason :meth:`repair_summary` gives: #352 round 2
        moved the repair sentence here so the journal path stopped escaping
        into the click module and the TUI, and ``EXPECTED_JOURNAL_PATH_SITES``
        in ``tests/test_journal_one_writer.py`` pins that. A display line in
        ``cli.py`` that reads ``config.journal_path`` would put the row back.
        """
        readings = self.read_iteration_readings(rows)
        verdict = iteration_criterion_verdict(rows, readings)
        lines = [
            f"  journal: {self.config.journal_path}",
            f"  window: last {verdict.window} run(s)",
        ]
        for reading in readings:
            if reading.measured:
                lines.append(
                    f"  {reading.run_id} | "
                    f"iterations_all_attempts={reading.avg_all_attempts:.2f} "
                    f"({reading.reason})"
                )
            else:
                lines.append(
                    f"  {reading.run_id} | iterations_all_attempts=REFUSED: {reading.reason}"
                )
        lines.append(
            "  clause 1 (journal per-attempt iterations > 1.00 in a majority "
            f"of the window): {verdict.clause1} - {verdict.clause1_detail}"
        )
        lines.append(
            "  clause 2 (at least two distinct project values): "
            f"{verdict.clause2} - {verdict.clause2_detail}"
        )
        lines.append(
            "  note: the avg_iterations column above is the LAST attempt's "
            "count per component, a lower bound, and it is the ruler #233's "
            "own text names. The clause 1 verdict here is computed from the "
            "journal's per-attempt readings instead, which is a larger number, "
            "and it never reads that column."
        )
        lines.append(
            "  note: this reads one project root, and it cannot tell a smoke "
            "run from a real project. The criterion excludes smoke runs and "
            "spans projects, so run it per root and pool the rows."
        )
        return lines

    # ------------------------------------------------------------------
    # get_repair_count
    # ------------------------------------------------------------------

    def repair_summary(self) -> str | None:
        """The operator-facing sentence about this journal's repairs, or None.

        ONE HOME for prose two surfaces render (#352 round 2, N4).
        ``cli._echo_journal_repairs`` and ``EvolveScreen._show_repairs``
        each held their own copy and each said the wording was
        deliberately the same on both, with nothing keeping it so. That
        is the same argument this PR made for hoisting
        ``REPAIR_DETAIL``, where three copies had already drifted.

        ``None`` at zero rather than a sentence saying zero, which is
        what both callers already did separately and for the same
        reason: a healthy journal that prints "0 repairs" every time
        teaches an operator to skip the line that matters. Deciding it
        here also keeps the file read ONCE. What stays per-surface is
        the prefix, two spaces for the CLI and an alert glyph for the
        TUI, and the widget or stream it goes to.
        """
        repairs = self.get_repair_count()
        if not repairs:
            return None
        return (
            f"journal: {repairs} interrupted write(s) repaired. "
            f"A crash left {self.config.journal_path} without a trailing newline. "
            "The line above each journal_repair row is what that write left behind: "
            "either a torn fragment, which is lost, or a whole record that lost only "
            "its newline, which is readable again. Read it to tell which."
        )

    def get_repair_count(self) -> int:
        """How many interrupted writes this journal has been repaired from.

        The read surface for ``JOURNAL_REPAIR_EVENT`` (#327 round 1,
        F5). Writing the row was only half of "if it's worth deciding,
        it's worth recording": a row no command reports is reachable
        only by an operator who already suspects the problem, and the
        logger warning goes to orchestrator.log under the TUI. ``ks
        evolve --status`` prints this when it is non-zero.

        Counts rows, not incidents. Residual 4 of
        :meth:`append_entries` is how a repair can happen and not be
        counted, so this is a lower bound. It is no longer an UPPER
        bound too: #330's residual 2, two processes each writing a row
        for one tear, is closed under ``fcntl`` by the lock the append
        takes, so one row is one tear on POSIX and two rows for one tear
        remain possible only on a platform with no ``fcntl``. A
        non-zero count means at least one crash left an unterminated
        tail. It does NOT mean a record was lost: the line above each
        row is either a fragment that was never readable or a whole
        record that lost only its newline and is readable again, which
        is the distinction ``docs/evolution-metrics.md`` and the status
        line both draw.
        """
        return sum(
            1 for e in self._read_all_entries() if e.get("event_type") == JOURNAL_REPAIR_EVENT
        )

    # ------------------------------------------------------------------
    # get_spec_audits / get_spec_issue_runs
    # ------------------------------------------------------------------

    def get_spec_audits(self) -> list[dict[str, Any]]:
        """Every recorded spec audit in the journal, oldest first (#314).

        The whole set, across every project, because the caller that
        accounts for the history a windowed trend leaves out needs
        exactly what the window drops: filtering by project here would
        hide the thing it is asking for.

        This is the read surface a caller uses INSTEAD of opening
        ``config.journal_path`` for itself. The difference is not
        cosmetic: if the journal ever compacts, rotates or gains a
        second segment, this method is what changes, while a caller
        holding the path would quietly return less than the journal
        holds - and silent loss of the excluded-history accounting is
        the defect #280 exists to fix.

        Deliberately NOT routed through :meth:`_read_journal_entries`:
        that reader keeps only entries whose ``run_id`` is among the
        last N distinct run ids, and a spec audit carries no ``run_id``
        at all (decompose runs before a factory run id exists), so
        every one of them is dropped there. Reading the raw entries is
        what makes the architect's own history readable.
        """
        return [e for e in self._read_all_entries() if e.get("event_type") == SPEC_ISSUES_EVENT]

    def get_spec_issue_runs(
        self,
        project: str,
        last_n: int = 10,
        audits: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """The last N recorded spec audits for ``project``, oldest first (#260).

        The one place the window rule lives (#314). ``decompose`` had a
        second copy of it, and two copies of one rule can drift: if
        they had, the convergence trend and the accounting printed
        under it would have disagreed about the same journal.

        ``last_n`` counts spec audits, not factory runs - a spec audit
        happens once per decompose, whether or not a factory run
        follows. Windowed here rather than by the caller, matching
        :meth:`get_experiment_trends`.

        ``audits`` lets a caller that has already called
        :meth:`get_spec_audits` window that snapshot instead of reading
        the file a second time, so the window and any accounting over
        the same entries cannot disagree because the file moved between
        two reads. The event-type filter is applied either way, so
        passing raw entries answers the same as passing audits.

        Nothing is assumed about an entry beyond it being a JSON
        object, so journals written by older versions read cleanly. A
        project is matched by :func:`entry_str`, so a null or
        non-string ``project`` field is an unattributed audit rather
        than a project named "None".
        """
        source = self.get_spec_audits() if audits is None else audits
        runs = [
            entry
            for entry in source
            if entry.get("event_type") == SPEC_ISSUES_EVENT
            and entry_str(entry, "project") == project
        ]
        return runs[-last_n:] if last_n > 0 else []

    # ------------------------------------------------------------------
    # append_entries
    # ------------------------------------------------------------------

    def carry_superseded(self, from_run_id: str, to_run_id: str, owed: Mapping[str, range]) -> int:
        """Write ``from_run_id``'s superseded attempts again under
        ``to_run_id`` (#463), for the attempts ``owed`` names per component;
        returns how many.

        ``carried_from_run`` names the run the attempt actually ran in, and a
        row carried twice keeps the first run's name.
        """
        rows = [
            {
                **entry,
                "run_id": to_run_id,
                "carried_from_run": entry.get("carried_from_run") or from_run_id,
            }
            for entry in self._read_all_entries()
            if entry.get("event_type") == FINDINGS_SUPERSEDED_EVENT
            and entry.get("run_id") == from_run_id
            and entry.get("attempt") in owed.get(str(entry.get("component_id", "")), range(0))
        ]
        if rows:
            self.append_entries(rows)
        return len(rows)

    def append_entries(self, entries: list[dict[str, Any]]) -> None:
        """Append entries to the journal in JSONL form.

        The one writer of the journal's line format, enforced by
        ``tests/test_journal_one_writer.py``. Raises ``OSError`` rather
        than handling it, because the three callers surface a failed
        write differently: :meth:`record_run` logs it, decompose warns
        through the run's UI, and ``autonomy.commit_transition`` warns.

        #312: a crash mid-write leaves a tail with no newline, and an
        append onto that tail concatenates the two into one unparseable
        line, so the tolerant reader drops the NEW entry as well. The
        mechanism and the repair now live in ``kstrl.appendio``, which
        #331 hoisted them into when five more appenders were measured
        with the same defect; the ``"a+b"`` open, the probe, the single
        write and the encoding are all described there.

        What stays here is the POLICY, which is what differs per file.
        This journal repairs with a ``JOURNAL_REPAIR_EVENT`` row rather
        than a bare pad, per "if it's worth deciding, it's worth
        recording": the row is what ``ks evolve --status`` counts and an
        operator greps months later, and it is durable where the logger
        warning below is not, because the process that tore the file is
        exactly the process whose stderr nobody kept.

        An empty append writes nothing, so it repairs nothing: there is
        no entry to protect and the next real append will do it.

        Both are enforced by ``tests/test_journal_write_boundary.py``,
        which counts descriptors and writes. Round 2 of review on #327
        found that neither was, and the measurement here agrees: a
        version that reopens the file for the append, and a version
        that writes the newline, the marker and the batch separately,
        each pass 284 tests and 1 xfail across
        ``test_journal_torn_tail``, ``test_journal_one_writer``,
        ``test_decompose``, ``test_autonomy_ladder`` and
        ``test_config_control_plane``. An argument in a docstring is
        not a mechanism.

        WHAT IS STILL NOT ATOMIC, precisely, because a docstring that
        implied otherwise would be worse than no docstring.

        #330 listed three residuals of the unlocked probe-and-append.
        This call now passes ``lock=True``, so ``appendio.appending``
        holds ``fcntl.LOCK_EX`` on the journal's OWN descriptor across
        the probe and the write, and all three are gone WHERE ``fcntl``
        imports, which is POSIX. On a platform without it the helper
        yields no exclusion and all three are exactly as listed. The
        same degradation ``control_lock``, ``queue_lock`` and the
        run-level factory lock already take.

        The two-process measurement that decided this is on
        :func:`appendio.appending`, where the lock lives, rather than
        copied here: two copies of one measurement drift, and this
        docstring is about which residuals close, not about the numbers
        that showed they do.

        1. A STALE PROBE. Between this process's tail read and its
           write, another process could append; if that other write
           crashed mid-line inside the window, this append landed on the
           fragment and the pair was unreadable, which is the #312
           outcome in a narrower window. The lock closes the window: no
           other locked writer is between the probe and the write.
        2. A DOUBLED REPAIR ROW. Two processes repairing one tear each
           wrote a newline and a repair row, so one incident was
           recorded twice. Both the blank line and the extra row are
           skipped by every reader and counted by none, so this cost
           :meth:`get_repair_count` accuracy and nothing else. The lock
           makes the count exact: the second writer probes AFTER the
           first has written, and finds a terminated file.
        3. INTERLEAVED LINES. O_APPEND makes each ``write`` land at the
           end, and the repair row plus the whole batch go in ONE
           ``write`` so that another appender cannot land between them.
           That was not a guarantee, but not for the reason it is
           tempting to write down. Measured on this interpreter:
           ``BufferedWriter`` hands a payload of ANY size to the raw
           layer in one ``write(2)`` (100 bytes to 5 MB, one raw call
           each), so the split is not size-driven and
           ``io.DEFAULT_BUFFER_SIZE`` is not the threshold. It loops
           only when the OS returns a SHORT write, which on a regular
           file means a signal or ENOSPC. Under the lock no other kstrl
           writer can land in that loop.

        WHAT THE LOCK DOES NOT COVER, in any case: a SHORT write inside
        the one write (residual 4 below), an appender that does not take
        the lock (a foreign process, or this one on a platform without
        ``fcntl``), and the readers, which are tolerant and unlocked and
        unchanged by this.

        Deadlock ordering: this lock is a LEAF. ``append_entries`` calls
        nothing that takes another kstrl lock. ``commit_transition``
        calls ``state.save()``, which takes and RELEASES ``control_lock``
        BEFORE the append rather than around it; ``record_run`` and
        ``_record_contract_event`` run under the run-level factory lock,
        which is a different file that nothing acquires while holding
        this one. ``flock`` is per open file description and
        :func:`appendio.appending` opens its own, so two threads in one
        process serialize against each other too.

        4. The repair is not two-phase-safe either. There is no gap
           BETWEEN two calls, because there is only one call: the
           newline that isolates the fragment, the marker and the batch
           are one ``write``. What is left is a partial write INSIDE it,
           which is residual 3's short write, landing the newline and
           not the marker. That leaves a file that is terminated,
           malformed one line up and carrying no repair row, and the
           next append reads the last BYTE, finds a newline and adds
           none. Nothing is at risk by then - the fragment is isolated,
           which was the point - so this is an audit gap, not data loss,
           and the isolated fragment line is still on disk to be read.
           Closing it means parsing the last LINE on every append, which
           would fire on any malformed tail rather than a torn one: a
           different contract, not a bug fix.
           ``test_a_terminated_but_malformed_tail_is_not_a_tear`` pins
           it, so it cannot quietly stop being true.

        Neither 1 nor 3 was made worse by the probe: at cbdff7c the
        same two writers produced the same interleaving with no probe at
        all.
        """
        path = self.config.journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        repairing = append_records(
            path,
            "".join(_journal_line(entry) for entry in entries),
            repair=_journal_line(_repair_entry()),
            lock=True,
        )
        if repairing:
            logger.warning(
                "evolution journal did not end in a newline, so a crash tore it: "
                "%s. A newline and a %s row were written before this append, so "
                "the unterminated tail cannot swallow the entries after it.",
                path,
                JOURNAL_REPAIR_EVENT,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_all_entries(self) -> list[dict[str, Any]]:
        """Every well-formed JSON object in the journal, in file order.

        Delegates to ``observability.read_progress_events``: the
        journal and the progress log are the same JSONL-of-objects
        convention, and the tolerant-read policy (missing file, blank
        line, torn line, non-object line - all skipped) should be one
        policy rather than two. One unreadable line must not cost the
        reader the rest of the history.
        """
        return read_progress_events(self.config.journal_path)

    def _read_journal_entries(self, lookback_runs: int = 10) -> list[dict[str, Any]]:
        """Read JSONL journal and return entries from the last N distinct runs.

        Entries without a ``run_id`` are dropped, because the window is
        defined in terms of runs. Spec audits are exactly that case;
        :meth:`get_spec_audits` reads those instead.
        """
        entries = without_carried_results(self._read_all_entries())
        if not entries:
            return []

        # Determine the last N distinct run_ids (preserving order of appearance).
        seen_runs: list[str] = []
        seen_set: set[str] = set()
        for entry in reversed(entries):
            rid = entry.get("run_id", "")
            if rid and rid not in seen_set:
                seen_set.add(rid)
                seen_runs.append(rid)
            if len(seen_runs) >= lookback_runs:
                break

        allowed = set(seen_runs)
        return [e for e in entries if e.get("run_id", "") in allowed]
