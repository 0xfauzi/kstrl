"""Positive controls for the one-writer guard: source it MUST flag.

The guard lives in ``tests/test_journal_one_writer.py`` and its three
pinned inventories assert that a list is empty or that a dict is
unchanged. An empty list is also what a switched-off detector returns,
so without this file the whole of layer 2 could be replaced by
``return []`` and that file would stay green. Measured before these
existed: stubbing ``is_write_mode``, ``mentions_journal``,
``write_target``, ``journal_aliases``, ``open_aliases`` or
``journal_writes_outside_append_entries`` itself to a constant left it
all passing.

Separate from the guard because the 800-line ratchet fired, and the
seam is the right one: that file walks the package and pins what is
there, this one feeds the detector snippets and pins what it says.
Importing the walkers across test modules is the house pattern here
(``test_decompose_run``, ``test_feature_run``, ``test_prd_tamper`` and
seven others do the same).

Every shape round 1 of review on #327 listed has a case here, and so
does every shape round 2 added.
"""

from __future__ import annotations

from pathlib import Path

from tests.helpers.astwalk import KSTRL_PACKAGE, label
from tests.test_journal_one_writer import (
    JOURNAL_ATTRIBUTE,
    journal_path_escapes,
    journal_writes_outside_append_entries,
)


class TestTheGuardDetects:
    """Layer 2 fed source it is SUPPOSED to flag, snippet by snippet.

    On a snippet rather than on the package, which is what makes the
    detector's own failure reachable. Only layer 1 could ever notice a
    switched-off detector on its own, and only because it asserts a
    NON-empty expected set.
    """

    def offenders(self, tmp_path: Path, source: str, name: str = "other.py") -> list[str]:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        return journal_writes_outside_append_entries(path)

    def test_the_seed_tracks_the_attribute_name(self, tmp_path: Path) -> None:
        """Layer 2 has one seed, and it used to be a literal.

        ``mentions_journal`` is the only thing that binds the first
        alias, so if its spelling drifts from ``JOURNAL_ATTRIBUTE`` the
        alias set stays empty and layer 2 reports nothing at all, with
        every test in this class still green because their snippets
        carry the old spelling too. Built out of the constant here, so
        the two cannot drift apart in silence.
        """
        source = f'open(config.{JOURNAL_ATTRIBUTE}, "a")\n'
        assert self.offenders(tmp_path, source) == [
            f"other.py:1: writes to config.{JOURNAL_ATTRIBUTE}"
        ]

    def test_a_positional_mode_is_a_write(self, tmp_path: Path) -> None:
        found = self.offenders(tmp_path, 'open(config.journal_path, "a")\n')
        assert found == ["other.py:1: writes to config.journal_path"]

    def test_a_keyword_mode_is_a_write(self, tmp_path: Path) -> None:
        """The hole round 1 found: ``mode=`` was never read."""
        found = self.offenders(tmp_path, 'open(config.journal_path, mode="a")\n')
        assert found == ["other.py:1: writes to config.journal_path"]

    def test_r_plus_is_a_write(self, tmp_path: Path) -> None:
        """Every letter that can write, not just the first one."""
        found = self.offenders(tmp_path, 'open(config.journal_path, "r+")\n')
        assert found == ["other.py:1: writes to config.journal_path"]

    def test_a_plain_read_is_not_a_write(self, tmp_path: Path) -> None:
        """The distinction the mode test buys, pinned so it can fail.

        The journal has a legitimate reader; flagging it would make this
        guard about touching the file rather than writing it.
        """
        assert self.offenders(tmp_path, 'open(config.journal_path, "r")\n') == []
        assert self.offenders(tmp_path, "open(config.journal_path)\n") == []

    def test_an_alias_chain_of_any_length_is_followed(self, tmp_path: Path) -> None:
        """Single-hop resolution let ``target = journal_path`` through."""
        source = 'journal_path = config.journal_path\ntarget = journal_path\nopen(target, "a")\n'
        assert self.offenders(tmp_path, source) == ["other.py:3: writes to target"]

    def test_an_annotated_assignment_binds_too(self, tmp_path: Path) -> None:
        source = 'journal_path: Path = config.journal_path\nopen(journal_path, "a")\n'
        assert self.offenders(tmp_path, source) == ["other.py:2: writes to journal_path"]

    def test_an_alias_of_open_itself_is_followed(self, tmp_path: Path) -> None:
        source = 'open_file = open\nopen_file(config.journal_path, "a")\n'
        assert self.offenders(tmp_path, source) == ["other.py:2: writes to config.journal_path"]

    def test_the_dotted_opens_are_covered(self, tmp_path: Path) -> None:
        for owner in ("builtins", "io", "os"):
            found = self.offenders(tmp_path, f'{owner}.open(config.journal_path, "a")\n')
            assert found == ["other.py:1: writes to config.journal_path"], owner

    def test_a_call_on_the_path_still_counts(self, tmp_path: Path) -> None:
        source = 'journal_path = config.journal_path\nopen(journal_path.resolve(), "a")\n'
        assert self.offenders(tmp_path, source) == ["other.py:2: writes to journal_path.resolve()"]

    def test_the_path_write_methods_are_covered(self, tmp_path: Path) -> None:
        for method in ("write_text", "write_bytes"):
            found = self.offenders(tmp_path, f"config.journal_path.{method}(row)\n")
            assert found == ["other.py:1: writes to config.journal_path"], method

    def test_path_dot_open_is_covered(self, tmp_path: Path) -> None:
        found = self.offenders(tmp_path, 'config.journal_path.open("a")\n')
        assert found == ["other.py:1: writes to config.journal_path"]

    def test_a_getattr_of_the_attribute_is_a_write(self, tmp_path: Path) -> None:
        """#327 round 2, F9, shape one. Both layers were blind to it."""
        source = 'target = getattr(config, "journal_" + "path")\nopen(target, "a")\n'
        assert self.offenders(tmp_path, source) == ["other.py:2: writes to target"]

        direct = 'open(getattr(config, "journal_" + "path"), "a")\n'
        assert self.offenders(tmp_path, direct) == [
            "other.py:1: writes to getattr(config, 'journal_' + 'path')"
        ]

    def test_a_filename_built_out_of_pieces_is_a_write(self, tmp_path: Path) -> None:
        """#327 round 2, F9, shape two: no substring to search for."""
        source = 'target = root / ".kstrl" / ("evolution" + ".jsonl")\nopen(target, "a")\n'
        assert "evolution.jsonl" not in source
        assert self.offenders(tmp_path, source) == ["other.py:2: writes to target"]

    def test_the_directory_can_move_into_the_literal(self, tmp_path: Path) -> None:
        """The F9 shape with the split one literal to the left.

        Decidable, and layer 2 used to discard it: with an equality
        test the folded value was ``".kstrl/evolution.jsonl"``, which is
        not the filename, so the offender list came back empty and only
        a module COUNT moved.
        """
        source = 'open(root / (".kstrl/evolution" + ".jsonl"), "a")\n'
        assert self.offenders(tmp_path, source) == [
            "other.py:1: writes to root / ('.kstrl/evolution' + '.jsonl')"
        ]

    def test_the_namespace_spellings_of_the_attribute_are_writes(
        self,
        tmp_path: Path,
    ) -> None:
        """``__dict__`` and ``vars``: the same read, without a dot."""
        via_dict = 'target = config.__dict__["journal_path"]\nopen(target, "a")\n'
        assert self.offenders(tmp_path, via_dict) == ["other.py:2: writes to target"]

        via_vars = 'target = vars(config)["journal_path"]\nopen(target, "a")\n'
        assert self.offenders(tmp_path, via_vars) == ["other.py:2: writes to target"]

    def test_an_f_string_of_constants_folds_too(self, tmp_path: Path) -> None:
        """CPython folds ``f"a{'b'}"`` only sometimes, so this is
        checked rather than assumed."""
        source = 'open(root / f"evolution{\'.jsonl\'}", "a")\n'
        assert self.offenders(tmp_path, source) != []

    def test_a_converted_or_formatted_placeholder_does_not_fold(self, tmp_path: Path) -> None:
        """The two guards on ``folded_placeholder``, one control each.

        ``!r`` adds quotes and a format spec pads, so neither value is
        the one in the source. Both were measured removable with this
        file green before these existed.
        """
        source = 'open(root / f"evolution{\'.jsonl\'!r}", "a")\n'
        assert self.offenders(tmp_path, source) == []

        padded = 'open(root / f"evolution{\'.jsonl\':>10}", "a")\n'
        assert self.offenders(tmp_path, padded) == []

    def test_a_filename_the_interpreter_has_to_build_is_missed(self, tmp_path: Path) -> None:
        """The disclosed residual, pinned so the disclosure stays true.

        ``"".join`` needs the interpreter, so folding answers None and
        this write is NOT reported. Asserting it records the boundary of
        what the guard claims; if somebody widens the folder, this test
        is where they find out the docstring above has to change.
        """
        source = 'target = root / "".join(("evolution", ".jsonl"))\nopen(target, "a")\n'
        assert self.offenders(tmp_path, source) == []

    def test_an_attribute_name_the_interpreter_has_to_build_is_missed_too(
        self, tmp_path: Path
    ) -> None:
        """The same residual one level up, and the row #324 round 2 found
        missing.

        The disclosure above named a path or a FILENAME the interpreter
        builds. This builds the ATTRIBUTE NAME instead, so no filename
        appears anywhere and layer 1 does not fire either: ``getattr``
        folds ``"journal_" + "path"`` and does not fold ``"".join``.
        Measured on ``origin/main`` as well as here, so it is pre-existing
        and not something the migration introduced; what the migration
        owes it is a row, because a disclosure whose wording does not
        reach a shape is a clean report for that shape.
        """
        built = 'target = getattr(config, "".join(("journal_", "path")))\nopen(target, "a")\n'
        assert self.offenders(tmp_path, built) == []

        path = tmp_path / "other.py"
        path.write_text(built, encoding="utf-8")
        assert journal_path_escapes(path) == []

    def test_layer_one_sees_a_getattr_acquisition(self, tmp_path: Path) -> None:
        """The clause at the other layer, which had no control of its own.

        Every other case in this class feeds
        ``journal_writes_outside_append_entries``. Measured while this
        was missing: deleting ``or dynamic_attribute_read(node)`` from
        ``journal_path_escapes`` left the whole file green, because
        layer 1's expected dict holds no getattr row to move. Layer 1 is
        the half that catches a path handed to a helper and opened
        there, and a getattr-obtained path handed to a helper is exactly
        the residual layer 2 discloses.
        """
        path = tmp_path / "other.py"
        path.write_text('target = getattr(config, "journal_" + "path")\n', encoding="utf-8")
        assert journal_path_escapes(path) == ["other.py: getattr(config, 'journal_' + 'path')"]

    def test_a_duplicate_basename_is_named_by_its_package_path(self) -> None:
        """A key and a message that send the reader to the right file.

        ``label`` moved to ``tests/helpers/astwalk.py`` on #324 and is
        covered there too. Kept here because THIS guard's pinned keys are
        what break if it changes, and a shared helper's own test cannot
        say which caller a change costs.
        """
        assert label(KSTRL_PACKAGE / "decompose.py") == "decompose.py"
        assert label(KSTRL_PACKAGE / "tui" / "screens" / "decompose.py") == str(
            Path("tui") / "screens" / "decompose.py"
        )

    def test_an_unrelated_file_is_left_alone(self, tmp_path: Path) -> None:
        """The false-positive side. Without this, "flag everything" passes."""
        source = 'other_path = config.experiments_path\nopen(other_path, "a")\n'
        assert self.offenders(tmp_path, source) == []

    def test_the_sanctioned_writer_is_exempt(self, tmp_path: Path) -> None:
        source = (
            "class EvolutionJournal:\n"
            "    def append_entries(self, entries):\n"
            "        path = self.config.journal_path\n"
            '        with open(path, "a+b") as handle:\n'
            "            handle.write(b'{}')\n"
        )
        assert self.offenders(tmp_path, source, name="evolution.py") == []

    def test_the_exemption_is_the_class_method_and_nothing_else(
        self,
        tmp_path: Path,
    ) -> None:
        """Round 1 exempted anything anywhere named ``append_entries``."""
        nested = (
            "def record_run(self):\n"
            "    def append_entries(rows):\n"
            '        open(self.config.journal_path, "a")\n'
        )
        assert self.offenders(tmp_path, nested, name="evolution.py") == [
            "evolution.py:3: writes to self.config.journal_path"
        ]

        elsewhere = (
            "class Sneaky:\n"
            "    def append_entries(self, config):\n"
            '        open(config.journal_path, "a")\n'
        )
        assert self.offenders(tmp_path, elsewhere, name="evolution.py") == [
            "evolution.py:3: writes to config.journal_path"
        ]

    def test_the_exemption_does_not_apply_in_another_module(self, tmp_path: Path) -> None:
        source = (
            "class EvolutionJournal:\n"
            "    def append_entries(self, entries):\n"
            '        open(self.config.journal_path, "a")\n'
        )
        assert self.offenders(tmp_path, source, name="autonomy.py") == [
            "autonomy.py:3: writes to self.config.journal_path"
        ]
