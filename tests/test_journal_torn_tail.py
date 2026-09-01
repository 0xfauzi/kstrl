"""#312: what an interrupted journal write costs the entries after it.

The read side of this was already covered, by
``tests/test_decompose.py::TestExcludedHistory::
test_a_torn_line_does_not_cost_the_note``: a torn tail must not make the
rest of the history unreadable. This file is the WRITE side, which the
read-side test cannot see. ``append_entries`` opened the file in append
mode and wrote, without ever asking whether the file ended in a newline,
so the next entry was concatenated onto the fragment and the pair became
one unparseable line. The fragment was already lost; the entry after it
was not, until the append destroyed it too.

Every assertion here runs against real bytes on a real file, torn by
truncating or by writing a partial line. A mock append cannot see the
defect, because the defect IS the bytes.

The blast radius is measured rather than asserted from the issue text,
and it is not uniform: ``test_a_tail_that_lost_only_its_newline_keeps_
its_record`` is the case where the interrupted write cost TWO records
rather than one, because a tail that lost only its terminator is a
complete record that the concatenation then destroys as well.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from kstrl.evolution import JOURNAL_REPAIR_EVENT, EvolutionConfig, EvolutionJournal
from kstrl.observability import ends_without_newline, read_progress_events
from tests.helpers.journal import DANGLING_UTF8, TORN_FRAGMENT, tear

#: The package under test, located the way every other AST-walking test
#: in this suite locates it (test_atomicio, test_prompt_versions).
KSTRL_PACKAGE = Path(__file__).resolve().parent.parent / "kstrl"


def audit(project: str) -> dict[str, Any]:
    return {
        "timestamp": "2026-08-20T00:00:00Z",
        "project": project,
        "event_type": "spec_issues",
        "spec_file": f"{project}.md",
    }


def component_result(run_id: str, component_id: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "timestamp": "2026-08-20T00:00:00Z",
        "run_id": run_id,
        "project": "p",
        "component_id": component_id,
        "event_type": "component_result",
        "failure_signatures": ["tests:assertion"],
        "findings_summary": {"by_category": {"scope_creep": 1}},
        "knowledge_utilization": {"measured": True, "injected": 3, "referenced": 2},
    }


def journal_at(tmp_path: Path) -> EvolutionJournal:
    return EvolutionJournal(EvolutionConfig.load(tmp_path))


def audits_in(path: Path) -> list[str]:
    return [
        str(entry.get("project"))
        for entry in read_progress_events(path)
        if entry.get("event_type") == "spec_issues"
    ]


def repair_rows_in(path: Path) -> list[dict[str, Any]]:
    return [e for e in read_progress_events(path) if e.get("event_type") == JOURNAL_REPAIR_EVENT]


# --- the one-writer guard, in pieces small enough to read -----------------


def is_write_mode(mode_args: list[ast.expr]) -> bool:
    """Does this open() mode argument write? An absent mode reads.

    Every mode letter that can write, not just the first character: an
    earlier draft tested ``mode[0] in "aw"`` and so read ``"r+"`` and
    ``"rb+"`` as reads, which is a hole in a guard whose docstring
    claims it cannot be argued out of by indirection. A mode that is not
    a literal string counts as a write for the same reason.
    """
    if not mode_args:
        return False
    mode = mode_args[0]
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(letter in mode.value for letter in "awx+")
    return True


def write_target(node: ast.Call) -> ast.expr | None:
    """The path expression a call writes to, or None if it writes none.

    Covers the builtin ``open(path, "a")``, the method ``path.open("a")``
    and ``path.write_text`` / ``path.write_bytes``.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in ("write_text", "write_bytes"):
            return func.value
        return func.value if func.attr == "open" and is_write_mode(node.args[:1]) else None
    if isinstance(func, ast.Name) and func.id == "open" and node.args:
        return node.args[0] if is_write_mode(node.args[1:2]) else None
    return None


def journal_aliases(nodes: list[ast.AST], permitted: set[int]) -> set[str]:
    """Local names assigned from an expression naming the journal.

    The old ``commit_transition`` reached it exactly this way:
    ``journal_path = config.journal_path`` and then a raw open of the
    local. Assignments inside ``append_entries`` are skipped because
    aliases are collected per module rather than per scope, and that
    method binds the journal to ``path``: without the skip, the
    commonest local name in ``evolution.py`` would mean "the journal"
    everywhere in the file, and the next unrelated ``open(path, "w")``
    in it would be a false offender. Measured on this tree, honestly:
    the skip changes nothing today (both ways report zero), because the
    one other write through a local ``path`` in that module moved to
    ``observability.ends_without_newline``. An earlier draft of this
    change, with the probe still in ``evolution.py``, DID report it.
    """
    names: set[str] = set()
    for node in nodes:
        if not isinstance(node, ast.Assign) or node.lineno in permitted:
            continue
        if not isinstance(node.value, ast.Attribute):
            continue
        if "config.journal_path" in ast.unparse(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def append_entries_lines(nodes: list[ast.AST]) -> set[int]:
    """Every line of ``append_entries``: the one permitted writer.

    Located by walking to the def rather than by pinning a line number,
    so editing the file above it does not fail the guard. A file with no
    such def gets an empty set, which is why this needs no exemption
    list naming ``evolution.py``.
    """
    for node in nodes:
        if isinstance(node, ast.FunctionDef) and node.name == "append_entries":
            return set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return set()


def journal_writes_outside_append_entries(source_file: Path) -> list[str]:
    """Every write to the evolution journal in one file, bar the sanctioned one.

    One ``ast.walk`` feeds all three passes. Measured over kstrl's 127
    files: walking per pass costs 232 ms, walking once costs 182 ms.
    """
    nodes = list(ast.walk(ast.parse(source_file.read_text(encoding="utf-8"))))
    permitted = append_entries_lines(nodes)
    aliases = journal_aliases(nodes, permitted)
    found: list[str] = []
    for node in nodes:
        if not isinstance(node, ast.Call) or node.lineno in permitted:
            continue
        target = write_target(node)
        rendered = ast.unparse(target) if target is not None else ""
        if "config.journal_path" in rendered or (rendered and rendered in aliases):
            found.append(f"{source_file.name}:{node.lineno}: writes to {rendered}")
    return found


class TestTheEntryAfterATear:
    def test_the_entry_after_a_torn_line_survives(self, tmp_path: Path) -> None:
        """The issue, reproduced: two audits written, one readable.

        Measured on cbdff7c before the fix: ``['alpha']``. The torn
        fragment ate 'beta', which was written after the crash and had
        nothing to do with it.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        journal.append_entries([audit("beta")])

        assert audits_in(journal.config.journal_path) == ["alpha", "beta"]

    def test_the_torn_fragment_is_not_resurrected(self, tmp_path: Path) -> None:
        """The repair isolates the fragment, it does not repair it.

        A partial JSON object was never a record and cannot become one.
        What the fix owes is that it stops costing its successor, and
        claiming more than that in a docstring would be the defect this
        suite exists to catch.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        journal.append_entries([audit("beta")])

        path = journal.config.journal_path
        assert TORN_FRAGMENT in path.read_text(encoding="utf-8")
        parsed_types = [e.get("event_type") for e in read_progress_events(path)]
        assert parsed_types == ["spec_issues", JOURNAL_REPAIR_EVENT, "spec_issues"]

    def test_a_tail_that_lost_only_its_newline_keeps_its_record(self, tmp_path: Path) -> None:
        """The tear that costs TWO records, not one.

        When the interruption lands between the last byte of a record
        and its newline, the record on disk is complete and readable.
        Appending onto it concatenates two whole objects into
        ``{...}{...}``, which ``json.loads`` rejects as "Extra data", so
        the reader loses the old record AND the new one. Measured
        before the fix: ``['a1', 'a2']`` from a file that held three
        audits and had just been handed a fourth.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("a1"), audit("a2"), audit("a3")])
        path = journal.config.journal_path
        path.write_bytes(path.read_bytes()[:-1])
        assert audits_in(path) == ["a1", "a2", "a3"]

        journal.append_entries([audit("a4")])

        assert audits_in(path) == ["a1", "a2", "a3", "a4"]

    def test_an_autonomy_transition_after_a_tear_survives(self, tmp_path: Path) -> None:
        """The second writer, which the fix to the first would not reach.

        ``commit_transition`` had its own raw ``open(journal_path, "a")``
        and so had its own copy of #312. It now goes through
        ``append_entries``, which is what makes "the one writer of the
        journal's line format" true rather than asserted.
        """
        from kstrl.autonomy import AutonomyState, Transition, commit_transition

        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        commit_transition(
            AutonomyState(level=1),
            Transition(
                at="2026-08-20T00:00:00Z",
                direction="promote",
                from_level=0,
                to_level=1,
                actor="tester",
                trigger="manual",
                reason="test",
                evidence={},
            ),
            tmp_path,
        )

        events = read_progress_events(journal.config.journal_path)
        assert [e.get("event_type") for e in events] == [
            "spec_issues",
            JOURNAL_REPAIR_EVENT,
            "autonomy_transition",
        ]


class TestWhatIsNotATear:
    def test_an_intact_journal_is_appended_to_unchanged(self, tmp_path: Path) -> None:
        """No blank line, no repair row, byte for byte what it was.

        The guard has to be silent on the overwhelmingly common path or
        it becomes noise that an operator learns to ignore.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        before = path.read_bytes()

        journal.append_entries([audit("beta")])

        after = path.read_bytes()
        assert after.startswith(before)
        assert after == before + json.dumps(audit("beta"), separators=(",", ":")).encode() + b"\n"
        assert repair_rows_in(path) == []

    def test_an_empty_journal_file_is_not_a_tear(self, tmp_path: Path) -> None:
        """Zero bytes has no unterminated line in it."""
        journal = journal_at(tmp_path)
        journal.config.journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal.config.journal_path.write_bytes(b"")

        journal.append_entries([audit("alpha")])

        assert audits_in(journal.config.journal_path) == ["alpha"]
        assert repair_rows_in(journal.config.journal_path) == []

    def test_a_missing_journal_file_is_not_a_tear(self, tmp_path: Path) -> None:
        """FileNotFoundError is an OSError, and answers "not torn"."""
        journal = journal_at(tmp_path)
        assert not journal.config.journal_path.exists()

        journal.append_entries([audit("alpha")])

        assert audits_in(journal.config.journal_path) == ["alpha"]
        assert repair_rows_in(journal.config.journal_path) == []

    def test_an_empty_append_repairs_nothing(self, tmp_path: Path) -> None:
        """Nothing to protect, so nothing is written.

        Repairing here would mutate the file on a call that was asked to
        add no records, and the next real append repairs it anyway.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)
        before = journal.config.journal_path.read_bytes()

        journal.append_entries([])

        assert journal.config.journal_path.read_bytes() == before

    def test_a_directory_where_the_journal_should_be_is_not_a_tear(self, tmp_path: Path) -> None:
        """An unreadable path is not evidence of an interrupted write.

        ``ends_without_newline`` answers False and lets the append
        raise the real ``OSError`` for the caller to surface, rather
        than inventing a repair for a file it could not read.
        """
        journal = journal_at(tmp_path)
        journal.config.journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal.config.journal_path.mkdir()

        assert ends_without_newline(journal.config.journal_path) is False
        with pytest.raises(OSError):
            journal.append_entries([audit("alpha")])


class TestTheTearIsVisible:
    def test_the_repair_is_recorded_in_the_journal(self, tmp_path: Path) -> None:
        """Healing forward is a judgement call, so it leaves a record.

        The row is the durable half: the process that tore the file is
        the process whose stderr nobody kept, and this is what an
        operator can grep months later.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        journal.append_entries([audit("beta")])

        rows = repair_rows_in(journal.config.journal_path)
        assert len(rows) == 1
        assert rows[0]["timestamp"]
        assert "not newline-terminated" in rows[0]["detail"]

    def test_the_repair_is_logged(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The live half, for whoever is watching the run it happened in."""
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        with caplog.at_level(logging.WARNING, "kstrl.evolution"):
            journal.append_entries([audit("beta")])

        assert any("did not end in a newline" in record.message for record in caplog.records)

    def test_one_tear_records_one_repair(self, tmp_path: Path) -> None:
        """The file ends in a newline once repaired, so later appends
        are ordinary appends. A repeated row would be a loop that
        reported a fresh incident on every write."""
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        tear(journal.config.journal_path)

        journal.append_entries([audit("beta")])
        journal.append_entries([audit("gamma")])

        assert len(repair_rows_in(journal.config.journal_path)) == 1

    def test_the_repair_row_counts_towards_no_aggregate(self, tmp_path: Path) -> None:
        """A repair row must not move a number anyone reads.

        Every aggregate in ``evolution`` selects on ``event_type`` or
        windows by ``run_id``, and the row has its own type and no
        ``run_id``. This compares a torn journal against a clean one
        holding the same records, and pins that the clean answers are
        non-trivial so the comparison is not two empty dicts agreeing.
        """
        clean = journal_at(tmp_path / "clean")
        clean.append_entries([component_result("r1", "c1"), component_result("r2", "c2")])

        torn = journal_at(tmp_path / "torn")
        torn.append_entries([component_result("r1", "c1")])
        tear(torn.config.journal_path)
        torn.append_entries([component_result("r2", "c2")])
        assert len(repair_rows_in(torn.config.journal_path)) == 1

        assert clean.get_concern_hit_rate()["components"] == 2
        assert torn.get_concern_hit_rate() == clean.get_concern_hit_rate()
        assert clean.get_fact_utilization()["measured"] == 2
        assert torn.get_fact_utilization() == clean.get_fact_utilization()
        assert len(clean.get_cross_run_patterns()) == 1
        assert [p.description for p in torn.get_cross_run_patterns()] == [
            p.description for p in clean.get_cross_run_patterns()
        ]
        assert clean.get_spec_issue_runs("p") == torn.get_spec_issue_runs("p")

    def test_the_repair_row_cannot_push_a_run_out_of_the_lookback_window(
        self,
        tmp_path: Path,
    ) -> None:
        """The second reason the row is invisible, and the reason it
        carries no ``run_id``.

        ``_read_journal_entries`` keeps the last N DISTINCT run_ids. A
        repair row with a run_id of its own would be one of those N, so
        a tear would silently shorten the history every aggregate reads
        by one run. Sized so that is exactly what would happen: lookback
        2, two real runs, one tear between them.
        """
        journal = journal_at(tmp_path)
        journal.config.lookback_runs = 2
        journal.append_entries([component_result("r1", "c1")])
        tear(journal.config.journal_path)
        journal.append_entries([component_result("r2", "c2")])

        assert journal.get_concern_hit_rate(lookback_runs=2)["runs"] == 2


class TestTheUndecodableTail:
    """A tear inside a multi-byte character, which is the case the write
    side survives and the read side does not."""

    def test_a_tail_torn_mid_utf8_sequence_is_still_repaired_on_disk(
        self,
        tmp_path: Path,
    ) -> None:
        """The probe reads bytes, so it works where a decode cannot."""
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        assert DANGLING_UTF8.endswith(b"\xc3")
        path.write_bytes(path.read_bytes() + DANGLING_UTF8)

        journal.append_entries([audit("beta")])

        lines = path.read_bytes().split(b"\n")
        assert DANGLING_UTF8 in lines
        assert any(b'"project":"beta"' in line for line in lines)

    def test_but_the_reader_still_loses_the_whole_file_to_it(self, tmp_path: Path) -> None:
        """Measured, and NOT fixed here: one undecodable byte anywhere
        costs every entry in the journal, not one line.

        ``read_progress_events`` names utf-8 and catches ``ValueError``,
        so it satisfies both halves of the CLAUDE.md encoding rule and is
        not one of the #320 sites. What it does with the failure is the
        gap: ``UnicodeDecodeError`` is raised by the ITERATION, outside
        the per-line ``JSONDecodeError`` handler, so the whole read
        returns []. That contradicts ``_read_all_entries``'s own stated
        policy that "one unreadable line must not cost the reader the
        rest of the history". It is a read-side change to a function
        three callers share, and
        ``test_evolve_screen_encoding.py::
        test_the_journal_reader_is_shared_with_ks_status`` pins its
        current source text, so it belongs in its own change rather than
        riding along with the write-side fix. This test exists so the
        gap is recorded rather than implied.
        """
        journal = journal_at(tmp_path)
        journal.append_entries([audit("alpha")])
        path = journal.config.journal_path
        path.write_bytes(path.read_bytes() + DANGLING_UTF8)

        journal.append_entries([audit("beta")])

        assert read_progress_events(path) == []


class TestOneWriter:
    """``append_entries`` is the only writer of the journal's lines, and
    #312 is what the second one cost. This is the mechanism behind that
    sentence in its docstring."""

    def test_append_entries_is_the_only_writer_of_the_journal(self) -> None:
        """Fails on a second raw appender anywhere in ``kstrl/``.

        What it CANNOT see, stated rather than implied: a write through
        a name that never mentions ``config.journal_path``, and a path
        handed to a helper as an argument. It sees the shape the defect
        actually had, in ``kstrl/autonomy.py``.
        """
        offenders = [
            offender
            for source_file in sorted(KSTRL_PACKAGE.rglob("*.py"))
            for offender in journal_writes_outside_append_entries(source_file)
        ]

        assert offenders == [], (
            "A journal write outside append_entries: it will concatenate onto an "
            "unterminated tail and eat the entry after it (#312). Route it through "
            f"EvolutionJournal.append_entries. Offenders: {offenders}"
        )
