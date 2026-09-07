"""Every ``OperatorFileKind`` row reaches an engineer prompt, or this fails.

R10.9 review round 1, should-fix 3. ``OperatorFileKind`` and ``_rows``
each carried a sentence saying a new row reached the parent's notice,
``KstrlConfig.validate`` AND the worker's block "with no edit below this
line". Two of the three are true. The third is not: ``_run_component``
in ``kstrl/factory.py`` names each kind by hand, so a third row would be
validated, warned about on a typo, and injected into no prompt at all,
with nothing in the suite failing. ``tests/test_operator_file_kinds.py``
is parametrized over ``OPERATOR_FILES`` and reaches only the loader, and
the two spine classes name golden patterns and memory by hand.

The hand-written tuple is deliberate and stays: the ORDER is the feature
R10.9 is (memory after the retry context, #230), and a loop over a
DECLARATION order cannot express a PROMPT order. So the fix is not a
loop, it is this: count the sites and refuse a row that has not bought
one.

TWO LAYERS, and the split is the one ``tests/test_journal_one_writer.py``
records.

LAYER 1 is a census that enumerates no node types: every node in
``kstrl/`` that spells ``load_operator_file`` anywhere the AST can hold a
string. A block cannot reach a prompt without that call, so a new
injection site in ANY module has to move this dict, whatever shape it
takes and whichever kind it names. It resolves nothing, which is what
makes it closed by construction rather than closed over the shapes
somebody enumerated.

LAYER 2 resolves, and says what it could not decide rather than dropping
it. It reads each site's arguments back to a kind and asserts one site
per declared row and no other kind. A call it cannot read back is a ROW
in ``unresolved`` and fails, never an absence: a walk that stops looking
returns the same empty answer as a file with no sites in it, which is
the defect class #324 exists to end.

Both layers are proved to still fire before either is believed. Layer 1
carries an ``assert_census`` control; layer 2 has
``TestTheWalkStillFires``, which runs the extractor over source built for
it and fails if the answer is empty.
"""

from __future__ import annotations

import ast

from kstrl import operator_context
from kstrl.operator_context import OPERATOR_FILES, OperatorFileKind
from tests.helpers.astwalk import (
    KSTRL_PACKAGE,
    Bindings,
    assert_census,
    bindings,
    calls_to,
    label,
    package_sources,
    parse,
    parsed,
    resolved_calls,
    spells,
)

#: The two functions a block has to come through. Dotted from the module
#: that defines them, which is how ``astwalk`` resolves an import.
LOADER = "kstrl.operator_context.load_operator_file"
SPEC = "kstrl.operator_context.operator_file_spec"

#: The module that owns the prompt order. One file, named once.
WORKER = "factory.py"

#: Module-level constant name -> the ``[paths]`` key it declares, DERIVED
#: from ``kstrl.operator_context`` rather than listed. A row added under
#: a new constant name is covered the moment it is bound; a hand list
#: would be closed only over the names somebody remembered.
KIND_NAMES: dict[str, str] = {
    name: value.key
    for name, value in vars(operator_context).items()
    if isinstance(value, OperatorFileKind)
}

#: Every place in ``kstrl/`` that spells the loader's name: its
#: definition, ``factory.py``'s import, and one per injection site.
#: ``factory.py`` is 3 because two of its three are the calls this guard
#: is about. A fourth would be a third operator file reaching a prompt,
#: or the same file reaching one twice.
EXPECTED_LOADER_SPELLINGS: dict[str, int] = {
    "factory.py": 3,
    "operator_context.py": 1,
}

#: Every place in ``kstrl/`` that spells ``context_prefix``: the
#: parameter and its two uses in ``loop.py``, and the local plus the
#: keyword in ``factory.py``. Nothing else, and that is the whole of
#: layer 3. See :meth:`TestEveryRowReachesAPrompt.
#: test_no_other_command_hands_run_loop_a_prefix`.
EXPECTED_PREFIX_SPELLINGS: dict[str, int] = {
    "factory.py": 4,
    "loop.py": 3,
}


def kind_of(node: ast.Call, table: Bindings) -> str | None:
    """The ``[paths]`` key one ``load_operator_file`` call injects.

    None whenever the call cannot be read back, which the caller turns
    into a FAILING row rather than into silence. Every None here is the
    flag direction: an argument shape this cannot decide is reported, not
    cleared, so widening the walk costs a reader one line and narrowing
    it cannot delete the mechanism.
    """
    if not node.args or not isinstance(spec := node.args[0], ast.Call):
        return None
    if table.resolve(spec.func) != SPEC or not spec.args:
        return None
    named = spec.args[0]
    return KIND_NAMES.get(named.id) if isinstance(named, ast.Name) else None


def prompt_sites(tree: ast.Module, module: str) -> tuple[dict[str, int], tuple[str, ...]]:
    """``(sites per kind, rows the walk could not read back)`` for one module."""
    table = bindings(tree, module=module)
    counts: dict[str, int] = {}
    unresolved: list[str] = []
    for node, _origin in resolved_calls(tree, {LOADER}, module=module):
        key = kind_of(node, table)
        if key is None:
            unresolved.append(f"{node.lineno} {ast.unparse(node)}")
        else:
            counts[key] = counts.get(key, 0) + 1
    return counts, tuple(unresolved)


def worker_source() -> ast.Module:
    """``kstrl/factory.py``, parsed."""
    return parsed(KSTRL_PACKAGE / WORKER)


#: Source built to make the extractor answer. One decidable call and one
#: that is not, so both halves of layer 2 are proved rather than assumed.
DECIDABLE = (
    "from kstrl.operator_context import GOLDEN_PATTERNS, load_operator_file, operator_file_spec\n"
    "block = load_operator_file(operator_file_spec(GOLDEN_PATTERNS, root_dir, configured))\n"
)
UNDECIDABLE = (
    "from kstrl.operator_context import load_operator_file\n"
    "block = load_operator_file(spec_somebody_built_elsewhere)\n"
)


class TestTheWalkStillFires:
    """Layer 2's control, and it is a signature this file cannot skip.

    ``assert unresolved == ()`` plus ``counts == expected`` looks like two
    assertions and is one and a half: a walk that sees nothing returns
    ``({}, ())``, which passes the first and fails the second only
    because the expectation is non-empty. That is thin, and CLAUDE.md's
    rule is that a guard you did not mutate is a guard you did not test.
    So the extractor is run over source written for it, per layer.
    """

    def test_a_readable_site_is_read_back_to_its_kind(self) -> None:
        counts, unresolved = prompt_sites(parse(DECIDABLE), "control")

        assert counts == {"golden_patterns": 1}
        assert unresolved == ()

    def test_a_site_it_cannot_read_back_is_a_row_and_not_an_absence(self) -> None:
        """The flag direction. A call whose spec came from somewhere else
        is not cleared: it is reported, so the diff that adds one is where
        somebody says why the prompt block is being built another way."""
        counts, unresolved = prompt_sites(parse(UNDECIDABLE), "control")

        assert counts == {}
        assert unresolved == ("2 load_operator_file(spec_somebody_built_elsewhere)",)

    def test_the_kind_table_is_derived_and_covers_every_row(self) -> None:
        """``KIND_NAMES`` is read off the module, so this says what that
        buys: every declared row has a name the walk can match, and the
        table holds nothing that is not a row."""
        assert set(KIND_NAMES.values()) == {kind.key for kind in OPERATOR_FILES}


class TestEveryRowReachesAPrompt:
    """The guard itself."""

    def test_no_new_code_gets_hold_of_the_loader(self) -> None:
        """Layer 1, the net. A block cannot reach an engineer prompt
        without this call, so a new injection site anywhere in ``kstrl/``
        moves this dict whatever shape it takes.

        Enumerates no node types: ``spells`` asks ``ast.iter_fields`` for
        every string a node holds, so it counts the import alias, the
        callee name and the definition alike. Prose does not count,
        because the comparison is equality and a docstring mentioning the
        function folds to the whole docstring.
        """
        assert_census(
            sources=package_sources(),
            sees=spells("load_operator_file"),
            expected=EXPECTED_LOADER_SPELLINGS,
            control="block = load_operator_file(spec)\n",
            message=(
                "The set of places that get hold of the operator-file loader changed. "
                "If this is a new operator file reaching an engineer prompt, it needs "
                "an OPERATOR_FILES row and the five hand edits in factory.py that "
                "OperatorFileKind's docstring lists. If it is something else, add it "
                "to EXPECTED_LOADER_SPELLINGS with a reason."
            ),
        )

    def test_every_declared_row_has_exactly_one_site_in_the_worker(self) -> None:
        """Layer 2, the message. One ``load_operator_file`` call per
        declared kind in ``kstrl/factory.py``, and no kind that is not
        declared.

        This is what closes the gap the two corrected docstrings used to
        paper over. ``_rows`` reaches the parent's notice and
        ``KstrlConfig.validate`` by loop; the prompt block is reached by
        hand, and a row that has not bought its five edits fails here
        naming the key that is missing.
        """
        counts, unresolved = prompt_sites(worker_source(), f"kstrl.{WORKER[:-3]}")

        assert unresolved == (), (
            f"a load_operator_file call in {WORKER} whose kind this guard could not "
            "read back. It is reported rather than ignored, because a site the walk "
            f"cannot decide is a site it cannot vouch for. Rows: {list(unresolved)}"
        )
        assert counts == {kind.key: 1 for kind in OPERATOR_FILES}, (
            "every OperatorFileKind row must reach exactly one engineer prompt block "
            f"in {WORKER}, and only declared rows may. A row added to OPERATOR_FILES "
            "reaches the parent notice and KstrlConfig.validate through _rows, and "
            "reaches NO prompt until factory.py gains the import, the _run_component "
            "parameter, the load_operator_file call, the _path_relative_to_root hoist "
            f"and the _submit_args slot. Found: {counts}"
        )

    def test_no_other_command_hands_run_loop_a_prefix(self) -> None:
        """Layer 3, and it closes five documents at once.

        README.md, ``docs/env-vars.md``, ``docs/runbook.md``,
        CHANGELOG.md and ARCHITECTURE.md all say ``ks feature`` and
        ``ks understand`` read neither operator file. Round 1 (nit 10)
        found the claim true and held by nothing: the four ``run_loop``
        call sites outside ``factory.py`` pass no ``context_prefix``, and
        ``tests/test_understand_run.py``'s ``_fake_run_loop`` accepts the
        keyword without recording it, so even that seam is blind.

        A census of the NAME rather than a walk of the call sites,
        because a block cannot reach ``run_loop`` under any spelling
        without it: it is the parameter's name. ``feature_cmd.py`` and
        ``cli.py`` do not appear here, and the day either of them gains a
        ``context_prefix=`` it does, whatever it puts in it. Combined
        with layer 1 that is the closure: no operator file is loaded
        outside ``factory.py``, and no prefix is handed to ``run_loop``
        outside it either.
        """
        assert_census(
            sources=package_sources(),
            sees=spells("context_prefix"),
            expected=EXPECTED_PREFIX_SPELLINGS,
            control="run_loop(config, context_prefix=block)\n",
            message=(
                "A module other than factory.py and loop.py names context_prefix. If "
                "ks feature or ks understand is being given one, five documents say "
                "they read neither operator file and all five are now wrong; fix them "
                "in the same change."
            ),
        )

    def test_the_walk_reports_what_it_could_not_decide(self) -> None:
        """The other half of ``resolved_calls``, which answers the SEEN
        half only. ``calls_to`` partitions the same corpus, so a callee
        this could not resolve is a row here rather than an absence
        above."""
        found = calls_to(
            worker_source(),
            {LOADER},
            where=label(KSTRL_PACKAGE / WORKER),
            module=f"kstrl.{WORKER[:-3]}",
        )

        assert found.undecided == (), (
            "a call in factory.py that could be the loader and could not be "
            f"resolved. Undecided: {list(found.undecided)}"
        )
        assert len(found.seen) == len(OPERATOR_FILES)
