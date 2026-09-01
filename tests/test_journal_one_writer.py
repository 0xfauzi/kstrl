"""``append_entries`` is the only writer of the evolution journal's lines.

Split out of ``tests/test_journal_torn_tail.py`` when the file-length
ratchet fired, and it was right: that file is about what an interrupted
write COSTS, measured against real bytes on a real file, and this one is
a static guard over the whole package with no journal in it at all.
Different subject, different failure message, different reason to fail.

#312 is why the invariant is worth a guard: there were two writers of
this file, the second one had its own copy of the defect, and the
docstring on the first one claimed to be the only one. Round 1 of review
on #327 then found the first version of this guard passing an ordinary
``open(config.journal_path, mode="a")``, which is what produced the two
layers below.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

#: The package under test, located the way every other AST-walking test
#: in this suite locates it (test_atomicio, test_prompt_versions).
KSTRL_PACKAGE = Path(__file__).resolve().parent.parent / "kstrl"


def label(source_file: Path) -> str:
    """How a file is named in a key and in a failure message.

    Not ``source_file.name``: ten basenames occur twice in ``kstrl/``
    (``config.py``, ``decompose.py``, ``inbox.py`` and seven more, once
    at the top level and once under ``tui/screens/``). Nothing collides
    in the pinned sets today, and the counts would still move if it did,
    but the message would name a file the reader cannot then find.
    Falls back to the basename outside the package, which is what the
    snippets in ``TestTheGuardDetects`` are.
    """
    try:
        return str(source_file.relative_to(KSTRL_PACKAGE))
    except ValueError:
        return source_file.name


def package_sources() -> list[Path]:
    """Every module in ``kstrl/``, in a stable order."""
    return sorted(KSTRL_PACKAGE.rglob("*.py"))


# --- the one-writer guard, in pieces small enough to read -----------------
#
# Two layers, because one of them is a net and the other is a message.
#
# LAYER 1, ``journal_path_escapes``, pins every expression in ``kstrl/``
# that reads an attribute named ``journal_path``. It is the net: code
# cannot write to a file whose path it never obtained, so a second
# writer in any shape has to appear here first, whatever it does with
# the path afterwards. It needs no alias resolution at all, which is the
# point. #324 records that this repo has about eleven AST guards each
# re-implementing that resolution and each holed independently, and this
# one deliberately does not make resolution load-bearing.
#
# LAYER 2, ``journal_writes_outside_append_entries``, does resolve
# aliases, and says what it cannot see rather than claiming to be
# exhaustive. It is not merely a nicer error message for layer 1: it
# catches one thing layer 1 provably cannot, an EXISTING attribute read
# rebound to a local that is then written through, which leaves layer
# 1's counts untouched. Layer 1 in turn catches what layer 2 cannot, a
# path handed to a helper and opened there. Both were planted and
# measured; neither is redundant.
#
# Round 1 of review on #327 found layer 2 passing an ordinary
# ``open(config.journal_path, mode="a")``, which is why layer 1 exists
# at all, and why ``TestTheGuardDetects`` below feeds this layer source
# it is supposed to flag: a guard whose only assertion is that a list is
# empty cannot notice its own detector being switched off.

#: Names that resolve to the builtin ``open`` when they own the call.
_OPEN_MODULES = frozenset({"builtins", "io", "os"})

#: Path methods that write without going through ``open``.
_PATH_WRITE_METHODS = frozenset({"write_text", "write_bytes"})


def mode_argument(node: ast.Call, index: int) -> ast.expr | None:
    """The mode/flags argument of an open-like call, positional or keyword.

    ``mode=`` was the hole round 1 found: only positional arguments were
    read, so ``open(p, mode="a")`` was classified as a read. ``flags`` is
    here for ``os.open``.
    """
    for keyword in node.keywords:
        if keyword.arg in ("mode", "flags"):
            return keyword.value
    return node.args[index] if len(node.args) > index else None


def is_write_mode(mode: ast.expr | None) -> bool:
    """Does this mode argument write? An absent mode reads.

    Every letter that can write, not just the first character: ``"r+"``
    and ``"rb+"`` write. Anything that is not a literal string counts as
    a write, including ``os.O_APPEND``, because a guard must not be
    argued out of by indirection.
    """
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(letter in mode.value for letter in "awx+")
    return True


def write_target(node: ast.Call, open_names: set[str]) -> ast.expr | None:
    """The path expression a call writes to, or None if it writes none.

    Covers ``open(p, "a")`` and any alias of ``open``; ``builtins.open``,
    ``io.open`` and ``os.open``; ``p.open("a")``; and ``p.write_text`` /
    ``p.write_bytes``.
    """
    func = node.func
    if isinstance(func, ast.Name):
        if func.id not in open_names or not node.args:
            return None
        return node.args[0] if is_write_mode(mode_argument(node, 1)) else None
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in _PATH_WRITE_METHODS:
        return func.value
    if func.attr != "open":
        return None
    if ast.unparse(func.value).split(".")[-1] in _OPEN_MODULES:
        return node.args[0] if node.args and is_write_mode(mode_argument(node, 1)) else None
    return func.value if is_write_mode(mode_argument(node, 0)) else None


def assignment_parts(node: ast.AST) -> tuple[list[str], ast.expr | None]:
    """The plain names an assignment binds, and what it binds them to.

    Handles ``AnnAssign`` as well as ``Assign``: an annotated
    ``journal_path: Path = config.journal_path`` was invisible to round
    1's walk, which looked only at ``Assign``.
    """
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id], node.value
    return [], None


def open_aliases(nodes: list[ast.AST]) -> set[str]:
    """``{"open"}`` plus any name bound to it, e.g. ``open_file = open``."""
    names = {"open"}
    for node in nodes:
        targets, value = assignment_parts(node)
        if isinstance(value, ast.Name) and value.id in names:
            names.update(targets)
    return names


def mentions_journal(rendered: str, names: set[str]) -> bool:
    """Does this expression reach the evolution journal's path?

    Substring and word-boundary rather than equality, so that a call ON
    the path counts: ``open(journal_path.resolve(), "a")`` was another
    round-1 miss.
    """
    if "config.journal_path" in rendered:
        return True
    return any(re.search(rf"\b{re.escape(name)}\b", rendered) for name in names)


def journal_aliases(nodes: list[ast.AST], exempt: set[int]) -> set[str]:
    """Local names holding the journal path, to a FIXED POINT.

    ``journal_path = config.journal_path`` is how the old
    ``commit_transition`` reached it, and ``target = journal_path`` after
    that is how round 1's single-hop walk was escaped. Iterating until
    nothing new is bound closes the chain at any length.

    Assignments inside the exempt method are skipped because aliases are
    collected per module rather than per scope, and that method binds the
    journal to ``path``: without the skip the commonest local name in
    ``evolution.py`` would mean "the journal" everywhere in the file.
    """
    names: set[str] = set()
    while True:
        found = alias_sweep(nodes, exempt, names)
        if found <= names:
            return names
        names |= found


def alias_sweep(nodes: list[ast.AST], exempt: set[int], names: set[str]) -> set[str]:
    """The names ONE pass binds to the journal, given what is known so far.

    Split from the loop above because the nesting cost 17 on the
    cognitive gate, which is a hook that fails rather than advises.
    """
    found: set[str] = set()
    for node in nodes:
        targets, value = assignment_parts(node)
        if value is None or getattr(node, "lineno", -1) in exempt:
            continue
        if mentions_journal(ast.unparse(value), names):
            found.update(targets)
    return found


def append_entries_lines(nodes: list[ast.AST], source_file: Path) -> set[int]:
    """The lines of ``EvolutionJournal.append_entries``: the one writer.

    Resolved through the CLASS, in the one module that may define it,
    rather than by function name: round 1 exempted anything anywhere
    called ``append_entries``, so an unrelated method or a nested
    function of that name was a free pass. Located by walking rather
    than by pinning a line number, so editing the file above it does not
    fail the guard.
    """
    if label(source_file) != "evolution.py":
        return set()
    for node in nodes:
        if not isinstance(node, ast.ClassDef) or node.name != "EvolutionJournal":
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "append_entries":
                return set(range(item.lineno, (item.end_lineno or item.lineno) + 1))
    return set()


def journal_writes_outside_append_entries(source_file: Path) -> list[str]:
    """Every write to the evolution journal in one file, bar the sanctioned one."""
    nodes = list(ast.walk(ast.parse(source_file.read_text(encoding="utf-8"))))
    exempt = append_entries_lines(nodes, source_file)
    names = journal_aliases(nodes, exempt)
    opens = open_aliases(nodes)
    found: list[str] = []
    for node in nodes:
        if not isinstance(node, ast.Call) or node.lineno in exempt:
            continue
        target = write_target(node, opens)
        rendered = ast.unparse(target) if target is not None else ""
        if rendered and mentions_journal(rendered, names):
            found.append(f"{label(source_file)}:{node.lineno}: writes to {rendered}")
    return found


def journal_path_escapes(source_file: Path) -> list[str]:
    """Every read or write of an attribute named ``journal_path``.

    Layer 1. Deliberately NOT filtered down to the evolution journal:
    telling ``self.config.journal_path`` from ``pipeline``'s
    progress-log ``self.journal_path`` needs the type resolution #324 is
    about, and pinning six extra sites costs one line each in the
    expected set while guessing costs a hole.
    """
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    return [
        f"{label(source_file)}: {ast.unparse(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "journal_path"
    ]


class TestTheGuardDetects:
    """Positive controls: layer 2 fed source it is SUPPOSED to flag.

    Without these the whole of layer 2 could be replaced by ``return []``
    and the three tests below would stay green, because their only
    assertion is that the offender list is empty, and an empty list is
    what a switched-off detector returns. Measured, before these existed:
    stubbing ``is_write_mode``, ``mentions_journal``, ``write_target``,
    ``journal_aliases``, ``open_aliases`` or
    ``journal_writes_outside_append_entries`` itself to a constant left
    all three passing. Only layer 1 noticed, and only because it asserts
    a NON-empty expected set.

    So each shape round 1 of review on #327 listed gets a case here, on
    a snippet rather than on the package, which is what makes the
    detector's own failure reachable.
    """

    def offenders(self, tmp_path: Path, source: str, name: str = "other.py") -> list[str]:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        return journal_writes_outside_append_entries(path)

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

    def test_a_duplicate_basename_is_named_by_its_package_path(self) -> None:
        """A key and a message that send the reader to the right file.

        Measured on this tree: ten basenames occur twice under
        ``kstrl/``, ``decompose.py`` among them. Both halves asserted,
        because a ``label`` that returned the full absolute path would
        satisfy the second line alone and break every pinned key.
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


#: Every place in ``kstrl/`` that reads or writes an attribute named
#: ``journal_path``, with how many times each expression appears in that
#: module. A second writer of the evolution journal has to obtain the
#: path, so it has to change this list, whatever shape the write takes.
#:
#: Adding a row is not forbidden, it is the point: the diff that adds
#: one is where somebody says why new code needs the journal's path and
#: why it is not going through ``append_entries``.
#:
#: The ``pipeline`` and ``workqueue`` rows are a DIFFERENT file (the
#: progress log and the queue journal). They are pinned anyway, because
#: separating them from the evolution journal by name alone is a guess
#: and #324 is the record of what guessing costs.
EXPECTED_JOURNAL_PATH_SITES: dict[str, int] = {
    "cli.py: journal.config.journal_path": 1,
    "decompose.py: journal.config.journal_path": 1,
    "evolution.py: config.journal_path": 5,
    "evolution.py: self.config.journal_path": 3,
    "pipeline.py: self.journal_path": 4,
    "workqueue.py: self.journal_path": 2,
}


class TestOneWriter:
    """``append_entries`` is the only writer of the journal's lines, and
    #312 is what the second one cost. This is the mechanism behind that
    sentence in its docstring."""

    def test_no_new_code_gets_hold_of_the_journal_path(self) -> None:
        """Layer 1, the net: pin every escape point of the path itself.

        Code cannot write to a file whose path it never obtained, so
        NEW code that gets hold of it has to change this list, whatever
        shape the write takes afterwards. That is why this layer
        resolves nothing: an exact set of expressions has no aliasing to
        be wrong about. Measured: of the eleven shapes round 1 of review
        listed, this layer catches ten.

        Two things it cannot see, both covered elsewhere rather than
        implied away:

        - an EXISTING acquisition re-spelled into a local that is then
          written through. The attribute is still read once, so these
          counts do not move. Layer 2 is what sees that, which is why
          both exist.
        - a path spelled out rather than asked for. The test below
          covers that half.
        """
        found: dict[str, int] = {}
        for source_file in package_sources():
            for site in journal_path_escapes(source_file):
                found[site] = found.get(site, 0) + 1

        assert found == EXPECTED_JOURNAL_PATH_SITES, (
            "The set of places that get hold of a journal path changed. If this is "
            "a new writer of the evolution journal, route it through "
            "EvolutionJournal.append_entries: an unguarded append concatenates onto "
            "an unterminated tail and eats the entry after it (#312). If it is a "
            "read, or another file's journal, add it to EXPECTED_JOURNAL_PATH_SITES "
            f"with a reason. Found: {found}"
        )

    def test_nobody_spells_the_journal_filename_for_themselves(self) -> None:
        """The other half of layer 1: the path obtained by construction.

        ``EXPECTED_JOURNAL_PATH_SITES`` cannot see
        ``open(root / ".kstrl" / "evolution.jsonl", "a")``, because that
        never touches the attribute. Counted per module rather than per
        line, so an edit elsewhere in a file does not fail this, and
        every module that names the file at all is accounted for: two
        defaults, one inventory, and four mentions in prose.
        """
        spellings: dict[str, int] = {}
        for source_file in package_sources():
            hits = source_file.read_text(encoding="utf-8").count("evolution.jsonl")
            if hits:
                spellings[label(source_file)] = hits

        assert spellings == {
            "atomicio.py": 1,  # prose in the module docstring
            "events.py": 1,  # prose in a docstring
            "evolution.py": 1,  # the EvolutionConfig default
            "init_cmd.py": 1,  # a commented example in the scaffolded kstrl.toml
            "knowledge.py": 1,  # prose in the module docstring
            "pipeline.py": 1,  # prose in a docstring
            "statedir.py": 1,  # the state-dir inventory, by name
        }, (
            "Somebody spelled the journal's filename instead of asking "
            f"EvolutionConfig for it. Sites: {spellings}"
        )

    def test_append_entries_is_the_only_writer_of_the_journal(self) -> None:
        """Layer 2, the message: name the offending write and the fix.

        Resolves aliases to a fixed point through ``Assign`` and
        ``AnnAssign``, reads ``mode=`` as well as positional modes,
        follows aliases of ``open`` itself, and knows ``builtins.open``,
        ``io.open``, ``os.open``, ``Path.open``, ``write_text`` and
        ``write_bytes``. The exemption is resolved through the
        ``EvolutionJournal`` class in ``evolution.py``, so an unrelated
        method or a nested function called ``append_entries`` is not
        exempt.

        What it still CANNOT see, stated rather than implied, because a
        guard that overstates its reach is worse than one that does not
        exist:

        - a path handed to a helper as a parameter and opened there.
          Planted and measured: layer 1 catches it, because the caller
          had to read the attribute to pass it on.
        - a write through a handle somebody else opened. Planted and
          measured: NEITHER layer catches it. The bound on that residual
          is that only ``append_entries`` ever holds a handle to this
          file (layer 1 is what makes that true), so the write has to be
          added inside the exempt method itself, which is the one place
          a reviewer of this invariant is already reading.
        """
        offenders = [
            offender
            for source_file in package_sources()
            for offender in journal_writes_outside_append_entries(source_file)
        ]

        assert offenders == [], (
            "A journal write outside append_entries: it will concatenate onto an "
            "unterminated tail and eat the entry after it (#312). Route it through "
            f"EvolutionJournal.append_entries. Offenders: {offenders}"
        )
