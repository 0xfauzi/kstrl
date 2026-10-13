"""The experiments.tsv format: its dialect, its header and the tolerant reader."""

from __future__ import annotations

import csv
import io
from typing import Any


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
# #601: ``merged`` and ``clean_merged`` count only merges
# ``ComponentPipeline._record_merge`` confirmed in this run (never a bare
# completion) and only those whose PR head GitHub reported equals the
# commit the diff phase judged. A row written before this pair existed
# carries neither, and ``autonomy_replay`` treats that as "unknown", not
# zero merges predicted clean - see ``RunRecord.merged``.
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
        "merged",
        "clean_merged",
    )
)


#: The width of every EXPERIMENTS_HEADER before the current one: before
#: R3.1 (11), from R3.1 to #451 (14), and from #451 to #601 (15). A file
#: keeps the header it was started with, and each later kstrl appends its
#: own full-width row, so a row is legal at its file header's width and at
#: every later width. Adding a column appends the width it replaces here.
OLDER_EXPERIMENTS_WIDTHS: tuple[int, ...] = (11, 14, 15)


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
