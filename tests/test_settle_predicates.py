"""Layer 3: the two holes in the settle HELPER, not in its callers.

Split out of ``tests/test_settle_discipline.py``, which this layer took
past the repo's 800-line ratchet. The split is along the real seam
rather than wherever 800 fell. Layers 1 and 2 next door are about the
TESTS: does a test wait on a condition before it reads state that async
settling decides. This layer is about ``tests/helpers/settle.py``
itself, and it exists because a five-lane review planted twelve defects
in that helper and ten of them passed the whole 330-test TUI tier with
nothing red.

3a: ``settled`` never calls its predicate inside a ``try``. For ANY
exception type, enumerating none, which is the load-bearing decision.

3b: a ``settled`` predicate must read something. ``lambda: True``
observes nothing, returns at once and blesses every read after it, and
neither other layer can see it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.test_settle_discipline import (
    SETTLE_MODULE,
    TEST_TREE,
    enclosing_function,
    label,
    parent_map,
    parsed,
    tree_sources,
)

# --- layer 3: the helper's own two holes ----------------------------------


#: Every place in the tree that hands :func:`settled` a predicate reading
#: nothing, keyed on the test that does it.
#:
#: An INVENTORY, not an exemption, and the difference is the whole point.
#: The first version of this rule exempted `tests/test_settle_helper.py`
#: wholesale, on the true observation that the file's subject IS the
#: helper, so `lambda: True` is its fixture rather than its defect. But
#: a file-wide exemption blesses every FUTURE test in that file too, in
#: silence, which is the same "exempted the file when you meant the
#: role" shape that has holed guards elsewhere in this repo.
#:
#: Keyed on the test function, so a new constant predicate - in this
#: file or any other - fails until somebody adds a row and says why.
#: That is the `EXPECTED_JOURNAL_PATH_SITES` shape: closed by
#: construction rather than closed over the shapes the walk looks at.
EXPECTED_CONSTANT_PREDICATES: dict[str, tuple[str, ...]] = {
    "tests/test_settle_helper.py": (
        # The already-true condition returns without pausing at all.
        "test_a_condition_already_true_returns_without_pausing_at_all: lambda: True",
        # A condition that never holds must raise rather than return.
        "test_a_condition_that_never_holds_raises_rather_than_returning: lambda: False",
        # The same, at an expired deadline: the check-first order.
        "test_a_condition_true_at_an_expired_deadline_still_returns: lambda: True",
        # An empty `what` is refused before the predicate matters, twice:
        # once for "" and once for whitespace.
        "test_an_empty_what_is_refused_rather_than_rendered: lambda: True",
        "test_an_empty_what_is_refused_rather_than_rendered: lambda: True",
        # The timeout message has to name the condition it waited for.
        "test_the_failure_names_the_condition_it_waited_for: lambda: False",
    ),
}

#: The helper module itself, resolved the same way its callers name it.
SETTLE_SOURCE = TEST_TREE / "helpers" / "settle.py"


def predicate_calls_under_a_handler(tree: ast.Module) -> list[str]:
    """Places inside ``settled`` where the predicate is called under a
    ``try`` that has any handler at all.

    THIS ENUMERATES NO EXCEPTION TYPE, deliberately, and that is the
    load-bearing decision rather than an implementation detail. The rule
    is the one ``tests/test_toml_readers.py`` already enforces one level
    down: an error taxonomy belongs to the thing that raises, not to the
    thing that calls it. That guard exists because a reader enumerated
    what it believed ``tomllib`` could raise and was wrong three times
    running, each escape taking most of the CLI down. A list of
    exception types here would be the same bet, and the next author
    could satisfy the list and still ship the hole. So the rule is
    structural: the predicate call is not inside a ``try``. There is no
    taxonomy to get wrong.

    A predicate that raises is telling the test author their expression
    is wrong, at the line that wrote it. Catching it here turns that
    into a five-second "never settled" pointing at the wait instead.

    Three separate plants lived in this gap: catching ``NoMatches``,
    catching ``AssertionError``, and swallowing only the first
    exception. The first is the one somebody would actually write,
    because this module's own docstring warns callers about
    ``NoMatches`` and so tells them exactly where to reach.
    """
    found: list[str] = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "settled":
            found.extend(_guarded_predicate_calls(fn))
    return found


def _nodes_under_a_handler(fn: ast.AST) -> set[int]:
    """Every node inside a ``try`` in ``fn`` that has a handler or a
    ``finally``. Identity, not equality: two ``predicate()`` calls are
    equal as AST nodes and are different sites."""
    guarded: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and (node.handlers or node.finalbody):
            guarded.update(id(child) for child in ast.walk(node))
    return guarded


def _guarded_predicate_calls(fn: ast.AST) -> list[str]:
    """The ``predicate()`` calls in ``fn`` that sit under such a try."""
    guarded = _nodes_under_a_handler(fn)
    return [
        f"line {node.lineno}: predicate() is called under a try"
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "predicate"
        and id(node) in guarded
    ]


def settled_aliases(tree: ast.Module) -> frozenset[str]:
    """Local names that mean the helper's ``settled``.

    Layer 3b WIDENS where layer 2's :func:`is_enrolled` deliberately
    stays narrow, and the two are the same principle pointed in opposite
    directions. ``is_enrolled`` decides that a wait is a real settle,
    which CLEARS every read below it, so a spelling it fails to
    recognise must fall back to "fixed" and flag. This decides that a
    predicate observes nothing, which FLAGS, so a spelling it fails to
    recognise falls silent. Over-reporting is the safe direction in both
    cases; it is just a different edit in each.

    Measured before it was widened: of five ways to reach the same
    function, the first version saw one. ``settle.settled``,
    ``s.settled``, ``settled as wait`` and a local ``w = settled`` all
    walked past it with the `lambda: True` hole intact.
    """
    return _rebindings_of(tree, _imported_as_settled(tree))


def _imported_as_settled(tree: ast.Module) -> set[str]:
    """Local names bound to the helper's ``settled`` by an import,
    under any rename."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == SETTLE_MODULE
        for alias in node.names
        if alias.name == "settled"
    }


def _rebound_once(tree: ast.Module, known: frozenset[str]) -> set[str]:
    """Names assigned directly from a name already in ``known``."""
    return {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Name)
        and node.value.id in known
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _rebindings_of(tree: ast.Module, names: set[str]) -> frozenset[str]:
    """``names`` plus every local rebinding of one, to a fixed point, so
    ``a = settled`` followed by ``b = a`` is covered too."""
    known = frozenset(names)
    while (grown := known | _rebound_once(tree, known)) != known:
        known = grown
    return known


def is_a_settled_call(node: ast.Call, names: frozenset[str]) -> bool:
    """Is this a call to the helper's ``settled``, however spelled?

    Two clauses, and the second is a deliberate blanket over-report. A
    bare name counts when it was imported from the helper, under any
    rename, or rebound from one that was. An ATTRIBUTE call counts
    whenever its leaf is ``settled``, with the receiver not resolved at
    all: ``settle.settled``, ``s.settled`` and any spelling nobody has
    thought of yet.

    A module with its own unrelated ``settled`` is therefore flagged.
    That is the intended answer, not a defect: it is resolved by a row
    in :data:`EXPECTED_CONSTANT_PREDICATES` saying so, which is a
    decision somebody makes in a diff. The alternative is resolving the
    receiver in order to CLEAR, and clearing on a resolution this walk
    cannot be sure of is the direction #324 records sixteen guards being
    holed in.
    """
    if isinstance(node.func, ast.Name):
        return node.func.id in names or node.func.id == "settled"
    return isinstance(node.func, ast.Attribute) and node.func.attr == "settled"


def constant_settle_predicates(source_file: Path) -> list[str]:
    """:func:`constant_predicates` for one file. Split from the matcher
    so the controls next door can exercise the matcher on a temporary
    path: ``label`` resolves against the repo root and raises on
    anything outside it."""
    return constant_predicates(parsed(source_file))


def constant_predicates(tree: ast.Module) -> list[str]:
    """``settled`` calls whose predicate can never observe anything.

    ``lambda: True`` type-checks, reads nothing, returns at once and
    BLESSES every read after it, so both other layers pass: the await
    count does not move, and layer 2 sees an enrolled wait. Demonstrated
    end to end by replacing both predicates in
    ``test_escape_closes_the_panel`` with ``lambda: True``, planting the
    production defect that test names, and watching the whole suite stay
    green.

    A constant is the tractable half of "a predicate that cannot cover
    the read it blesses". The general form needs to know which read the
    wait is FOR, which is the resolution #324 exists to supply; this
    catches the shape that was actually demonstrated, and the docstring
    says which half is left.
    """
    names = settled_aliases(tree)
    parents = parent_map(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not is_a_settled_call(node, names) or len(node.args) < 2:
            continue
        predicate = node.args[1]
        body = predicate.body if isinstance(predicate, ast.Lambda) else predicate
        if not any(isinstance(n, (ast.Name, ast.Attribute)) for n in ast.walk(body)):
            owner = enclosing_function(node, parents)
            name = owner.name if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) else "?"
            found.append(f"{name}: {ast.unparse(predicate)}")
    return sorted(found)


class TestTheHelpersOwnHoles:
    """One assertion each, both about the helper rather than its callers."""

    def test_the_predicate_is_never_called_under_an_exception_handler(self) -> None:
        """Layer 3a. The predicate's exceptions are the caller's.

        Not an enumeration of exception types, deliberately: the defect
        is catching ANY of them, and a list would be one more thing to
        be wrong about. This is the same rule shape as the tomllib
        readers guard, one level down.
        """
        found = predicate_calls_under_a_handler(parsed(SETTLE_SOURCE))

        assert found == [], (
            "settled() calls its predicate inside a try. A predicate that raises is "
            "reporting a mistake at the line that wrote it - a typo, a renamed "
            "attribute, a query spelled query_one - and catching it here converts "
            "that into a five-second 'never settled' pointing at the wait. The "
            f"predicate's error taxonomy belongs to the predicate. Sites: {found}"
        )

    def test_no_settle_waits_on_a_predicate_that_observes_nothing(self) -> None:
        """Layer 3b. A wait whose condition reads nothing is not a wait.

        `lambda: True` returns at once and blesses every read after it,
        and neither other layer can see it: the await count is
        unchanged, and layer 2 sees an enrolled settle.
        """
        found = {
            label(source_file): tuple(hits)
            for source_file in tree_sources()
            if (hits := constant_settle_predicates(source_file))
        }

        assert found == EXPECTED_CONSTANT_PREDICATES, (
            "A settled() predicate is a constant, so it observes nothing, returns "
            "immediately and blesses every read after it. That is a fixed wait "
            "wearing the helper's name, and neither other layer can see it: the "
            "await count does not move and layer 2 sees an enrolled settle. Wait "
            "on the state the assertion depends on. If the predicate really is "
            "the fixture - which is true only where the SUBJECT under test is the "
            "helper itself - add the row to EXPECTED_CONSTANT_PREDICATES and say "
            f"why. Found: {found}"
        )
