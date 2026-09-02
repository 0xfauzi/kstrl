"""Positive controls for the event-name guard: source it MUST flag.

The guard lives in ``tests/test_event_names_have_one_home.py``. Its
layer-2 assertion is that a dict is empty, and an empty dict is also what
a switched-off matcher returns, so without this file every shape below
could be dropped from the matcher and that file would stay green.
Measured before this existed: each of the twenty matcher functions
stubbed to a constant in turn, and all twenty are noticed here.

Round 1 of review on #336 is what this file is made of. The matcher then
looked at ``ast.Dict`` and at ``ast.Compare`` and at nothing else, and
six ordinary shapes went past it with the whole fast tier green: a
``dict()`` call, an item assignment after the dict was built,
``setdefault``, ``update(**kwargs)``, ``match``/``case``, and a walrus.
Two of them were planted as real methods on ``EvolutionJournal`` and
produced ruff clean, mypy --strict clean, and 4776 passed. Every miss was
in the SKIP direction: a shape the matcher could not resolve, so it did
not look and reported clean. That is the defect class #324 exists to
record, and this repo has now logged eight instances of it.

Separate from the guard for the reason ``test_journal_guard_detects.py``
gives about its own sibling: that file walks the package and pins what is
there, this one feeds the matcher snippets and pins what it says.

Every shape here was MEASURED against the matcher, before and after,
under the ``__pycache__`` discipline: CPython invalidates cached bytecode
on ``(mtime_seconds, size)``, so two same-size edits inside one second
reuse stale bytecode and hand back a false pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kstrl.evolution import SPEC_ISSUES_EVENT
from tests.test_event_names_have_one_home import (
    event_name_spellings,
    literal_event_names,
)


def hits(source: str, event_name: str = SPEC_ISSUES_EVENT) -> list[str]:
    """What the matcher says about one snippet."""
    return literal_event_names(ast.parse(source), event_name)


WRITE = "line 2: writes 'spec_issues' as the event_type"
READ = "line 2: compares event_type against 'spec_issues'"


class TestEveryWritingShape:
    """A row is written. Every way of attaching the name to the column.

    ``ast.Dict`` was the only one of these the first matcher knew.
    """

    def test_a_dict_literal(self) -> None:
        assert hits('\nrow = {"event_type": "spec_issues", "project": p}\n') == [WRITE]

    def test_a_dict_call_with_keywords(self) -> None:
        """Shape B. ``C408`` would lint this, and this repo does not
        select ``C4``, so it is clean to ruff as well as to the matcher."""
        assert hits('\nrow = dict(event_type="spec_issues", project=p)\n') == [WRITE]

    def test_an_item_assignment_after_construction(self) -> None:
        """Shape C: the dict literal is innocent, the next line is not."""
        assert hits('\nrow["event_type"] = "spec_issues"\n') == [WRITE]

    def test_setdefault(self) -> None:
        """Shape D."""
        assert hits('\nrow.setdefault("event_type", "spec_issues")\n') == [WRITE]

    def test_update_with_keywords(self) -> None:
        """Shape F."""
        assert hits('\nrow.update(event_type="spec_issues")\n') == [WRITE]

    def test_an_attribute_assignment(self) -> None:
        assert hits('\nself.event_type = "spec_issues"\n') == [WRITE]

    def test_dict_unpacking_beside_the_literal_pair(self) -> None:
        """The ``**`` entry is a ``None`` key, and the pair beside it is
        still a pair. Asserted rather than assumed."""
        assert hits('\nrow = {**base, "event_type": "spec_issues"}\n') == [WRITE]

    def test_a_dict_comprehension(self) -> None:
        assert hits('\nrow = {"event_type": "spec_issues" for _k in keys}\n') == [WRITE]

    def test_a_bare_pair_a_dict_can_be_built_from(self) -> None:
        """``dict([("event_type", <name>)])`` and every other shape made
        of the same two-element pair."""
        assert hits('\nrow = dict([("event_type", "spec_issues")])\n') == [WRITE]

    def test_a_constructor_keyword(self) -> None:
        """A ``TypedDict``, a dataclass or a model: the keyword is what
        is matched, not the callable, so all of them count."""
        assert hits('\nrow = JournalRow(event_type="spec_issues")\n') == [WRITE]

    def test_a_partial_binding_the_keyword(self) -> None:
        assert hits('\nwrite = functools.partial(sink, event_type="spec_issues")\n') == [WRITE]

    def test_the_get_default_spells_the_name(self) -> None:
        """The fallback is the same bare literal deciding the same thing.

        Four rows in ``evolution.py`` classify a pre-column entry this
        way with ``component_result``, and every one of them was
        invisible to the first matcher.
        """
        assert hits('\nkind = entry.get("event_type", "spec_issues")\n') == [WRITE]

    def test_the_pop_default_spells_the_name(self) -> None:
        assert hits('\nkind = row.pop("event_type", "spec_issues")\n') == [WRITE]

    def test_a_bare_pair_in_a_list_counts_too(self) -> None:
        """``dict([["event_type", <name>]])``. One word in the matcher,
        and without this control that word can be deleted green."""
        assert hits('\nrow = dict([["event_type", "spec_issues"]])\n') == [WRITE]

    def test_a_second_constant_is_layer_ones_job_not_this_one(self) -> None:
        """The column discipline, asserted from the side that costs.

        ``_SPEC = "spec_issues"`` binds the name to a plain local, which
        never reaches the ``event_type`` column, so layer 2 says nothing.
        An earlier version of the matcher DID flag it, and that version
        also failed ``SPEC_ISSUES_KEY = "spec_issues"`` for the
        architect's JSON vocabulary and would fail ``events.py`` lines
        343 and 646 the moment the enrolment follow-up reaches
        ``contract_result``. Layer 1 counts this spelling instead, where
        a new row with a reason is the normal outcome.
        """
        assert hits('\n_SPEC = "spec_issues"\n') == []
        assert hits('\nSPEC_ISSUES_EVENT = "spec_issues"\n') == []


class TestEveryReadingShape:
    """A row is selected. Every way of comparing the column to the name.

    ``ast.Compare`` with a direct ``.get`` or subscript operand was the
    only one of these the first matcher knew.
    """

    def test_a_plain_comparison(self) -> None:
        assert hits('\nflag = entry.get("event_type") == "spec_issues"\n') == [READ]

    def test_the_reversed_and_membership_spellings(self) -> None:
        assert hits('\nflag = "spec_issues" == entry["event_type"]\n') == [READ]
        assert hits('\nflag = entry.get("event_type") in ("spec_issues", "other")\n') == [
            "line 2: compares event_type against ('spec_issues', 'other')"
        ]

    def test_an_assembled_spelling_is_not_a_way_past_it(self) -> None:
        """``folded_str`` is why: a text search and a bare
        ``ast.Constant`` check both miss these."""
        assert hits('\nflag = entry.get("event_type") == "spec" + "_issues"\n') == [
            "line 2: compares event_type against 'spec' + '_issues'"
        ]
        assert hits('\nflag = entry.get("event_type") == f"spec_issues"\n') == [
            "line 2: compares event_type against f'spec_issues'"
        ]

    def test_a_match_statement(self) -> None:
        """Shape G. ``ast.Match`` holds no comparison operator at all."""
        source = '\nmatch entry.get("event_type"):\n    case "spec_issues":\n        pass\n'
        assert hits(source) == ["line 3: compares event_type against 'spec_issues'"]

    def test_an_or_pattern_in_a_case(self) -> None:
        source = (
            '\nmatch entry.get("event_type"):\n    case "role_usage" | "spec_issues":\n'
            "        pass\n"
        )
        assert hits(source) == ["line 3: compares event_type against 'spec_issues'"]

    def test_a_mapping_pattern(self) -> None:
        """``case {"event_type": <name>}`` names the column itself, so it
        needs no subject read to be decidable."""
        source = '\nmatch entry:\n    case {"event_type": "spec_issues"}:\n        pass\n'
        assert hits(source) == ["line 3: compares event_type against 'spec_issues'"]

    def test_a_walrus_into_a_compare(self) -> None:
        """Shape J. Caught by the deep operand walk, not by the alias
        table, which is why the next test exists."""
        source = '\nif (found := entry.get("event_type")) == "spec_issues":\n    pass\n'
        assert hits(source) == [READ]

    def test_a_walrus_bound_on_an_earlier_line(self) -> None:
        """The only customer of ``assignment_parts``'s ``ast.NamedExpr``
        branch, which #324 moved into ``tests/helpers/astwalk.py``.
        Measured: delete that branch and every other test in this file
        still passes, so without this one it is invisible from here.
        ``tests/test_astwalk.py::test_a_walrus_rebind`` covers it from
        the helper's side; this one covers what losing it costs HERE."""
        source = (
            '\nif (found := entry.get("event_type")):\n    pass\nflag = found == "spec_issues"\n'
        )
        assert hits(source) == ["line 4: compares event_type against 'spec_issues'"]

    def test_an_alias_bound_on_an_earlier_line(self) -> None:
        """The plainest reader there is, and neither half of it is an
        operand that reads the column."""
        source = '\nfound = entry.get("event_type")\nflag = found == "spec_issues"\n'
        assert hits(source) == ["line 3: compares event_type against 'spec_issues'"]

    def test_an_annotated_alias(self) -> None:
        source = '\nfound: str | None = entry.get("event_type")\nflag = found == "spec_issues"\n'
        assert hits(source) == ["line 3: compares event_type against 'spec_issues'"]

    def test_an_alias_chain_of_any_length(self) -> None:
        """Iterated to a fixed point, so a second hop is not a way past."""
        source = '\nfound = entry.get("event_type")\nagain = found\nflag = again == "spec_issues"\n'
        assert hits(source) == ["line 4: compares event_type against 'spec_issues'"]

    def test_an_itemgetter_acquisition(self) -> None:
        """``operator.itemgetter("event_type")`` is a read the same way
        ``.get`` is, and the name it binds carries that."""
        source = '\nread = operator.itemgetter("event_type")\nflag = read(entry) == "spec_issues"\n'
        assert hits(source) == ["line 3: compares event_type against 'spec_issues'"]

    def test_a_method_on_the_read_with_no_comparison_at_all(self) -> None:
        source = '\nflag = str(entry.get("event_type", "")).startswith("spec_issues")\n'
        assert hits(source) == [READ]

    def test_an_if_elif_chain(self) -> None:
        source = (
            '\nif entry.get("event_type") == "role_usage":\n    pass\n'
            'elif entry.get("event_type") == "spec_issues":\n    pass\n'
        )
        assert hits(source) == ["line 4: compares event_type against 'spec_issues'"]

    def test_the_second_enrolled_name_is_detected_too(self) -> None:
        """The walk is driven by ``ENROLLED_EVENT_CONSTANTS`` rather than
        by one hardcoded name, so enrolling the next constant costs one
        line instead of a copy of this file."""
        source = '\nflag = entry.get("event_type") == "journal_repair"\n'
        assert hits(source, "journal_repair") == [
            "line 2: compares event_type against 'journal_repair'"
        ]


class TestTheOtherVocabularyIsLeftAlone:
    """The false-positive side. Without these, "flag everything" passes.

    ``spec_issues`` is the architect's own JSON key as well as a journal
    event name. Flagging that vocabulary would make this guard something
    to be silenced rather than obeyed, so the line is drawn at the
    COLUMN: a spelling that never reaches ``event_type`` is somebody
    else's word.
    """

    def test_the_architect_json_key_is_not_a_journal_row(self) -> None:
        """``spec_issues`` is also the key the architect returns its
        findings under, and ``DECOMPOSE_PROMPT`` spells it in prose."""
        assert hits('\nissues = data.get("spec_issues") or []\n') == []
        assert hits('\nPROMPT = "return a spec_issues array"\n') == []
        assert hits('\npayload = {"spec_issues": [], "components": []}\n') == []

    def test_prose_is_not_a_spelling(self) -> None:
        assert hits('\ndef f():\n    """A spec_issues row is what decompose writes."""\n') == []

    def test_another_column_compared_against_the_name(self) -> None:
        assert hits('\nflag = entry.get("kind") == "spec_issues"\n') == []

    def test_an_unrelated_row_and_an_unrelated_comparison(self) -> None:
        source = (
            '\nrow = {"project": "spec_issues_demo", "event_type": "role_usage"}\n'
            'flag = row["project"] == "other"\n'
        )
        assert hits(source) == []


class TestLayerOneCatchesWhatLayerTwoCannot:
    """The shapes the matcher does not enumerate, counted by the net.

    Without this class the two-layer claim is prose. Each case asserts
    BOTH halves: layer 2 says nothing, and layer 1 counts the spelling.
    Asserting only the second would pass on a matcher that had never been
    narrowed; asserting only the first would pass on a net that had been
    switched off.

    Every one of these was planted into ``kstrl/evolution.py`` on its own
    and measured: layer 2 green, layer 1 red.
    """

    def spellings(self, tmp_path: Path, source: str) -> int:
        path = tmp_path / "other.py"
        path.write_text(source, encoding="utf-8")
        return event_name_spellings(path)

    def check(self, tmp_path: Path, source: str, expected: int = 1) -> None:
        assert hits(source) == [], "layer 2 was expected to be silent here"
        assert self.spellings(tmp_path, source) == expected

    def test_a_dispatch_table_keyed_by_the_name(self, tmp_path: Path) -> None:
        """Decidable, and deliberately not decided by layer 2.

        ``{"spec_issues": handler}`` is indistinguishable from the
        architect's own JSON vocabulary, whose natural shape is a dict
        keyed by exactly that word, and from the TUI's artifact label.
        Layer 2 flagging it would put the matcher in the way of code that
        has nothing to do with the journal, and a guard in the way gets
        silenced. Layer 1 counts it, where a row with a reason is the
        normal outcome.
        """
        self.check(tmp_path, '\ntable = {"spec_issues": handle}\nf = table[e["event_type"]]\n')

    def test_a_read_behind_a_function_boundary(self, tmp_path: Path) -> None:
        """Aliases are resolved through bindings, not through calls.

        ``pick(entry) == "spec_issues"`` where ``pick`` reads the column
        in another function needs a call graph, which is the resolution
        #324 records eleven guards getting wrong independently. Not
        attempted in layer 2 on purpose, and it does not have to be.
        """
        source = (
            '\ndef pick(entry):\n    return entry.get("event_type")\n\n\n'
            'flag = pick(entry) == "spec_issues"\n'
        )
        self.check(tmp_path, source)

    def test_a_parameter_default_naming_the_column(self, tmp_path: Path) -> None:
        self.check(tmp_path, '\ndef emit(event_type="spec_issues"):\n    pass\n')

    def test_setattr_of_the_column(self, tmp_path: Path) -> None:
        self.check(tmp_path, '\nsetattr(row, "event_type", "spec_issues")\n')

    def test_a_tuple_unpacked_assignment(self, tmp_path: Path) -> None:
        self.check(tmp_path, '\nSPEC, OTHER = "spec_issues", "role_usage"\n')

    def test_the_name_arriving_through_a_loop_variable(self, tmp_path: Path) -> None:
        source = '\nfor name in ["spec_issues"]:\n    rows.append({"event_type": name})\n'
        self.check(tmp_path, source)

    def test_a_function_returning_the_bare_name(self, tmp_path: Path) -> None:
        self.check(tmp_path, '\ndef kind():\n    return "spec_issues"\n')

    def test_a_second_constant_and_the_compare_that_follows_it(self, tmp_path: Path) -> None:
        """Both spellings counted: the declaration and nothing else,
        because the comparison operand is a bare ``ast.Name`` that
        folding cannot decide. That is precisely why catching the
        declaration is what keeps the undecidable half small."""
        source = '\n_SPEC = "spec_issues"\nflag = e.get("event_type") == _SPEC\n'
        self.check(tmp_path, source)

    def test_the_net_is_not_simply_counting_everything(self, tmp_path: Path) -> None:
        """The false-positive side of layer 1. Without this, a net that
        returned the node count would pass every test above."""
        assert self.spellings(tmp_path, '\nrow = {"event_type": "role_usage"}\n') == 0
        assert self.spellings(tmp_path, '\nlabel = "spec_issues_demo"\n') == 0
        assert self.spellings(tmp_path, '\ndef f():\n    """A spec_issues row."""\n') == 0


class TestTheDisclosedMisses:
    """What NEITHER layer sees, asserted so it stays true.

    One thing, after #336. A disclosure nothing tests is a sentence that
    ages; this is where somebody widening ``folded_str`` finds out the
    docstrings have to change.
    """

    def test_a_name_the_interpreter_has_to_build_is_missed(self, tmp_path: Path) -> None:
        """``str.format``, ``"".join``, ``%``-formatting and a run-time
        lookup all answer ``None`` to constant folding, so neither the
        matcher nor the net can see the name.

        The bound on it: a WRITER of such a row still has to reach the
        ``event_type`` column, and a reader still has to obtain a value
        to compare, so this hides an individual site rather than the
        practice. It is the residual ``folded_str`` already discloses
        next door, reached from here.
        """
        for source in (
            '\nflag = e.get("event_type") == "{}_issues".format("spec")\n',
            '\nflag = e.get("event_type") == names["spec"]\n',
            '\nflag = e.get("event_type") == "".join(("spec", "_issues"))\n',
        ):
            assert hits(source) == [], source
            path = tmp_path / "other.py"
            path.write_text(source, encoding="utf-8")
            assert event_name_spellings(path) == 0, source
