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

FOUR LAYERS, and the split is the one ``tests/test_journal_one_writer.py``
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

LAYER 2b IS DELIVERY, and round 2 of the review is why it exists. Layers
1 and 2 count LOADING and the first draft called that proof the block
reaches a prompt. It is not: the reviewer removed ``memory,`` from the
literal tuple ``_run_component`` builds ``parts`` from, left the
``load_operator_file`` call in place, and kept ruff's F841 quiet with one
ordinary use of the local. Seven cases here passed, ``mypy --strict``
passed, ``ruff check`` passed, and a declared row was loaded, forwarded
to the worker and injected into no prompt. So this layer reads the
prompt-order literal itself and counts, per kind, the elements that
deliver it. It CLEARS, so it is written to flag: a tuple it cannot find,
a second binding of the name, a shape it cannot read back, all fail
rather than counting zero deliveries and agreeing with an empty answer.

LAYER 3 is the census over ``context_prefix``, which closes the five
documents that say ``ks feature`` and ``ks understand`` read neither
file.

Every layer is proved to still fire before it is believed. Layer 1 and
layer 3 carry an ``assert_census`` control; layers 2 and 2b have
``TestTheWalkStillFires``, which runs each extractor over source built
for it, including source with the defect planted, and fails if the answer
is empty or if the plant is not seen.
"""

from __future__ import annotations

import ast

from kstrl import operator_context
from kstrl.operator_context import OPERATOR_FILES, OperatorFileKind
from tests.helpers.astwalk import (
    KSTRL_PACKAGE,
    Bindings,
    all_nodes,
    assert_census,
    bindings,
    bound_names,
    calls_to,
    label,
    package_sources,
    parse,
    parsed,
    resolved_calls,
    scope_of,
    spells,
)

#: The two functions a block has to come through. Dotted from the module
#: that defines them, which is how ``astwalk`` resolves an import.
LOADER = "kstrl.operator_context.load_operator_file"
SPEC = "kstrl.operator_context.operator_file_spec"

#: The module that owns the prompt order. One file, named once.
WORKER = "factory.py"

#: The function in it that builds the prompt, and the local it builds the
#: prompt ORDER in. Both named once, so the guard's failure says which
#: name it went looking for.
WORKER_SCOPE = "_run_component"
ORDER_LOCAL = "parts"

#: The hand edits in ``kstrl/factory.py`` one new row costs, as a LIST
#: with the count DERIVED from it. Round 2 of the review found the same
#: remedy written out as five in three places, all of them missing the
#: one edit that puts the block in front of the engineer, and the count
#: had been correct when it was written. So the number is never typed:
#: ``len(WORKER_EDITS)`` is what the messages carry, and
#: ``test_the_docstrings_list_every_edit_this_guard_counts`` ties the
#: prose in ``kstrl/operator_context.py`` to this tuple.
#:
#: Each row is ``(anchor, what to do)``. The anchor is the identifier a
#: reader can grep for, which is also what the docstring test looks up.
WORKER_EDITS: tuple[tuple[str, str], ...] = (
    ("import", "the import of the kind constant"),
    (WORKER_SCOPE, f"the {WORKER_SCOPE} parameter carrying the configured path"),
    ("load_operator_file", "the load_operator_file call"),
    (ORDER_LOCAL, f"the {ORDER_LOCAL} entry that puts the block in the prompt order"),
    ("_path_relative_to_root", "the _path_relative_to_root hoist in the parent"),
    ("_submit_args", "the _submit_args positional slot"),
)

#: The count as a WORD, derived, because the two docstrings this guard
#: checks are prose. A seventh edit fails on the KeyError here rather
#: than by leaving "six" in a sentence that now lists seven things.
EDIT_COUNT_WORD: str = {4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}[len(WORKER_EDITS)]

#: What a reader is told when a row has not paid for delivery.
EDITS_SENTENCE = "; ".join(what for _anchor, what in WORKER_EDITS)

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


def _blocks_by_local(
    tree: ast.Module, table: Bindings, scope: str
) -> tuple[dict[str, str], list[str]]:
    """``local name -> kind key`` for the loader calls ``scope`` binds.

    Every row it could not read back comes out in the second half and
    FAILS, because this half of the walk is what the delivery census
    resolves names through: a loader call bound to something this cannot
    name would otherwise make the block it loads invisible to the count.
    """
    owner = scope_of(tree)
    found: dict[str, str] = {}
    unresolved: list[str] = []
    for node in all_nodes(tree):
        names, value = bound_names(node)
        if not isinstance(value, ast.Call) or table.resolve(value.func) != LOADER:
            continue
        if owner.get(id(node)) != scope:
            continue
        key = kind_of(value, table)
        if key is None or len(names) != 1:
            unresolved.append(f"{node.lineno} {ast.unparse(node)}")
        else:
            found[names[0]] = key
    return found, unresolved


def _order_elements(tree: ast.Module, scope: str) -> tuple[tuple[ast.expr, ...], list[str]]:
    """The elements of the ONE prompt-order literal ``scope`` builds.

    Flags rather than clears, at every step where it cannot prove what it
    is reading: no binding of the name in that scope, more than one, or a
    value that is not a literal sequence (nor a comprehension over one)
    all come back as a row, never as an empty element list. An empty
    element list is exactly what the defect this layer exists for
    produces, so the two must not be spelled the same way.
    """
    owner = scope_of(tree)
    values = [
        value
        for node in all_nodes(tree)
        for names, value in [bound_names(node)]
        if ORDER_LOCAL in names and value is not None and owner.get(id(node)) == scope
    ]
    if len(values) != 1:
        return (), [
            f"{len(values)} bindings of `{ORDER_LOCAL}` in {WORKER}::{scope}, expected "
            "exactly one. This guard reads the prompt ORDER off that literal, so it "
            "cannot answer while the name is bound somewhere else too."
        ]
    value = values[0]
    if isinstance(value, ast.ListComp | ast.SetComp | ast.GeneratorExp) and value.generators:
        value = value.generators[0].iter
    if isinstance(value, ast.Tuple | ast.List):
        return tuple(value.elts), []
    return (), [
        f"`{ORDER_LOCAL}` is built from {ast.unparse(value)}, which is not a literal "
        "sequence this guard can read element by element. If the prompt order is now "
        "built another way, that is where the order stopped being a value a test can "
        "pin, and this guard has to learn the new shape rather than pass."
    ]


def delivered_kinds(
    tree: ast.Module, module: str, scope: str = WORKER_SCOPE
) -> tuple[dict[str, int], tuple[str, ...]]:
    """``(prompt-order elements per kind, rows the walk could not decide)``.

    Layer 2b. An element counts for a kind when it is the local a
    ``load_operator_file(operator_file_spec(<KIND>, ...))`` call in the
    same scope bound, or when it is that call itself written inline.
    Elements naming anything else are not operator files and are not
    counted: the knowledge prefix, the decisions prefix, the feedforward
    prefix and the retry block are the four that are there today.
    """
    table = bindings(tree, module=module)
    by_local, unresolved = _blocks_by_local(tree, table, scope)
    elements, order_rows = _order_elements(tree, scope)
    unresolved += order_rows
    counts: dict[str, int] = {}
    for element in elements:
        key: str | None = None
        if isinstance(element, ast.Name):
            key = by_local.get(element.id)
        elif isinstance(element, ast.Call) and table.resolve(element.func) == LOADER:
            key = kind_of(element, table)
            if key is None:
                unresolved.append(f"{element.lineno} {ast.unparse(element)}")
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return counts, tuple(unresolved)


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

#: The worker's shape, small enough to read: two rows loaded, both put in
#: the prompt-order literal, one block in it that is not an operator file
#: at all.
_LOADS = (
    "from kstrl.operator_context import GOLDEN_PATTERNS, MEMORY\n"
    "from kstrl.operator_context import load_operator_file, operator_file_spec\n"
    f"def {WORKER_SCOPE}(root_dir, configured):\n"
    "    golden = load_operator_file(operator_file_spec(GOLDEN_PATTERNS, root_dir, configured))\n"
    "    memory = load_operator_file(operator_file_spec(MEMORY, root_dir, configured))\n"
)
DELIVERED = _LOADS + (
    f"    {ORDER_LOCAL} = [b for b in (knowledge, golden, _retry_block(ctx), memory) if b]\n"
    f"    return {ORDER_LOCAL}\n"
)

#: ROUND 2'S PLANT, in miniature and by hand. The loader call stays, the
#: local is read (which is what kept ruff's F841 quiet on the real
#: factory.py), and the entry in the prompt-order literal is gone. The
#: extractor has to report ``memory`` as delivered zero times.
NOT_DELIVERED = _LOADS + (
    f"    {ORDER_LOCAL} = [b for b in (knowledge, golden, _retry_block(ctx)) if b]\n"
    "    measured = len(memory)\n"
    f"    return {ORDER_LOCAL}, measured\n"
)

#: The order built somewhere this cannot read. It must be a ROW, because
#: an unreadable literal and a literal missing a row produce the same
#: empty count.
UNREADABLE_ORDER = _LOADS + f"    {ORDER_LOCAL} = _build_order(golden, memory)\n"


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

    def test_a_delivered_row_is_read_off_the_prompt_order(self) -> None:
        """Layer 2b's positive control."""
        counts, unresolved = delivered_kinds(parse(DELIVERED), "control")

        assert counts == {"golden_patterns": 1, "memory": 1}
        assert unresolved == ()

    def test_a_row_that_is_loaded_and_never_delivered_is_seen_as_missing(self) -> None:
        """Layer 2b's negative control, which is round 2's blocker plant.

        Loading is not delivery. Every earlier layer passes this source:
        the call is there, its kind reads back, and the local is used.
        Only the prompt-order literal says the block reaches nobody.
        """
        counts, unresolved = delivered_kinds(parse(NOT_DELIVERED), "control")

        assert counts == {"golden_patterns": 1}
        assert unresolved == ()

    def test_an_order_it_cannot_read_is_a_row_and_not_an_absence(self) -> None:
        """The flag direction for the layer that CLEARS. A prompt order
        built by a call this cannot read gives the same empty count as a
        row that was dropped, so it has to be reported instead."""
        counts, unresolved = delivered_kinds(parse(UNREADABLE_ORDER), "control")

        assert counts == {}
        assert len(unresolved) == 1
        assert ORDER_LOCAL in unresolved[0]

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
                f"an OPERATOR_FILES row and the {len(WORKER_EDITS)} hand edits in "
                f"{WORKER} that OperatorFileKind's docstring lists: {EDITS_SENTENCE}. "
                "If it is something else, add it to EXPECTED_LOADER_SPELLINGS with a "
                "reason."
            ),
        )

    def test_every_declared_row_has_exactly_one_site_in_the_worker(self) -> None:
        """Layer 2, and it is the LOADING half only.

        One ``load_operator_file`` call per declared kind in
        ``kstrl/factory.py``, and no kind that is not declared. Kept
        beside layer 2b rather than replaced by it, because a row can
        skip either edit: this one fails when the call is missing, and
        layer 2b fails when the call is there and the block reaches no
        prompt.

        ``_rows`` reaches the parent's notice and ``KstrlConfig.validate``
        by loop; the prompt block is reached by hand, and a row that has
        not bought its edits fails here or next door, naming the key that
        is missing.
        """
        counts, unresolved = prompt_sites(worker_source(), f"kstrl.{WORKER[:-3]}")

        assert unresolved == (), (
            f"a load_operator_file call in {WORKER} whose kind this guard could not "
            "read back. It is reported rather than ignored, because a site the walk "
            f"cannot decide is a site it cannot vouch for. Rows: {list(unresolved)}"
        )
        assert counts == {kind.key: 1 for kind in OPERATOR_FILES}, (
            "every OperatorFileKind row must be LOADED exactly once in "
            f"{WORKER}, and only declared rows may. A row added to OPERATOR_FILES "
            "reaches the parent notice and KstrlConfig.validate through _rows, and "
            f"reaches NO prompt until {WORKER} pays all {len(WORKER_EDITS)} of: "
            f"{EDITS_SENTENCE}. Found: {counts}"
        )

    def test_every_declared_row_reaches_the_prompt_order(self) -> None:
        """Layer 2b, and it is the DELIVERY half.

        Round 2's blocker: the guard above counts loader calls, and a row
        that is loaded and left out of the literal ``_run_component``
        builds ``parts`` from is loaded, forwarded, and injected into no
        prompt. Measured with every gate green, so nothing else was going
        to say it: ruff's F841 is silent as soon as the local is read
        once, and the two rows that exist today are held only by spine
        tests that name them by hand, which a third row would not have.
        """
        counts, unresolved = delivered_kinds(worker_source(), f"kstrl.{WORKER[:-3]}")

        assert unresolved == (), (
            f"this guard could not read the prompt order in {WORKER}::{WORKER_SCOPE} "
            "back to kinds, so it cannot vouch for any row. It reports that rather "
            f"than counting zero deliveries. Rows: {list(unresolved)}"
        )
        assert counts == {kind.key: 1 for kind in OPERATOR_FILES}, (
            "every OperatorFileKind row must appear exactly once in the literal "
            f"{WORKER}::{WORKER_SCOPE} builds `{ORDER_LOCAL}` from, which IS the "
            "prompt order. A row missing from it is loaded and reaches no engineer: "
            f"add its entry to that tuple. The {len(WORKER_EDITS)} edits a row costs "
            f"are: {EDITS_SENTENCE}. Delivered: {counts}"
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


class TestTheRemedyIsWrittenWhereAnAuthorReadsIt:
    """The prose an author of a third row follows, tied to the guard.

    Round 2's blocker had two halves and this is the second one: the
    ``parts`` entry was missing from the failure message AND from both
    docstrings that enumerate what a row costs, so all three steered an
    author into the one shape the guard could not see. A list in prose
    cannot be derived, but it can be CHECKED against the list the guard
    counts, which is what this does.
    """

    def test_the_docstrings_list_every_edit_this_guard_counts(self) -> None:
        """Each anchor in ``WORKER_EDITS`` appears in the row docstring,
        and the count is spelled as the word ``EDIT_COUNT_WORD``
        derives. A seventh edit therefore fails here until the sentence
        is rewritten, rather than leaving "six" over a list of seven."""
        doc = OperatorFileKind.__doc__ or ""

        missing = [anchor for anchor, _what in WORKER_EDITS if anchor not in doc]
        assert missing == [], (
            "OperatorFileKind's docstring is the list an author of a new row follows. "
            f"It does not name: {missing}. Every edit this guard counts has to be in "
            "it, or the remedy it prints sends somebody into a shape it refuses."
        )
        assert EDIT_COUNT_WORD in doc.lower(), (
            f"the docstring must say {EDIT_COUNT_WORD!r}, which is derived from "
            f"WORKER_EDITS ({len(WORKER_EDITS)} rows). A hand-counted numeral in this "
            "sentence is exactly what went stale in round 1."
        )

    def test_the_loop_docstring_names_the_edit_that_delivers(self) -> None:
        """``_rows`` is where somebody reads "this reaches two surfaces
        by loop". It has to name the prompt-order entry as the edit that
        is NOT free, because that is the one round 2 found missing."""
        doc = operator_context._rows.__doc__ or ""

        assert ORDER_LOCAL in doc
        assert EDIT_COUNT_WORD in doc.lower()
