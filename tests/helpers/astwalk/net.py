"""The shape-independent layer: a census, and the total partition.

Neither enumerates a node type. :func:`spells` does not enumerate a
FIELD name either, which is why it catches shapes nobody thought of.
:class:`Sites` has no third bucket: a candidate lands in ``seen`` or
in ``undecided``, so a walk that could not look cannot read clean."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tests.helpers.astwalk.corpus import folded_str, label, parse, parsed

# --- the net: a census that enumerates no node types -----------------------

#: A per-node predicate, applied to every node in a module and counted,
#: so one that enumerates no node types is a net with no shape list to be
#: incomplete.
Sees = Callable[[ast.AST], bool]


def spells(token: str) -> Sees:
    """Does this node write ``token`` anywhere the AST can hold a string?

    The strongest net here: it enumerates no node types AND no field
    names, asking ``ast.iter_fields`` for every string the node holds.
    That reaches ``Name.id``, ``Attribute.attr``, ``alias.name``,
    ``alias.asname``, ``arg.arg``, ``keyword.arg``, ``FunctionDef.name``,
    ``ExceptHandler.name``, ``Global.names``, a string literal, and every
    identifier slot a future CPython adds, without naming one.

    EQUALITY, not substring, so prose folding to a whole docstring is not
    a spelling, which keeps the inventory readable rather than something
    to be silenced. Use :func:`folds_containing` when the subject really
    is a substring. At most one hit per node.
    """

    def sees(node: ast.AST) -> bool:
        for _name, value in ast.iter_fields(node):
            if isinstance(value, str):
                if value == token:
                    return True
            elif isinstance(value, list) and any(
                item == token for item in value if isinstance(item, str)
            ):
                return True
        return folded_str(node) == token

    return sees


def folds_to(value: str) -> Sees:
    """Does this expression fold to exactly ``value``?

    Narrower than :func:`spells`: the VALUE an expression evaluates to,
    not every string the node holds, so a parameter called ``event_type``
    is not a spelling of the event type.
    """
    return lambda node: folded_str(node) == value


def folds_containing(part: str) -> Sees:
    """Does this expression fold to a string CONTAINING ``part``?

    For a subject that sits inside a longer string: a filename in a path,
    where ``root / ".kstrl/evolution.jsonl"`` folds to the whole relative
    path and equality would miss it. The cost is that prose folds too.
    """
    return lambda node: part in (folded_str(node) or "")


#: How an inventory row is named. The module by default, so an unrelated
#: edit does not fail a pinned dict; a guard whose message needs the
#: expression passes its own.
Keyed = Callable[[Path, ast.AST], str]


def census(sources: Iterable[Path], sees: Sees, key: Keyed | None = None) -> dict[str, int]:
    """How many nodes in each module satisfy ``sees``, keyed by label.

    Modules with no hits are left out, so the pinned dict is the size of
    the answer rather than of the package."""
    name = key or (lambda source_file, _node: label(source_file))
    built: dict[str, int] = {}
    for source_file in sources:
        for node in ast.walk(parsed(source_file)):
            if sees(node):
                row = name(source_file, node)
                built[row] = built.get(row, 0) + 1
    return built


def assert_census(
    *,
    sources: Iterable[Path],
    sees: Sees,
    expected: Mapping[str, int],
    control: str,
    message: str,
    key: Keyed | None = None,
) -> None:
    """Pin an inventory, having first proved the net still fires.

    ``control`` is source the predicate MUST hit. Required, not optional,
    because ``built == expected`` is also what a switched-off predicate
    returns, and #324's subject is guards reporting clean because they
    stopped looking. One line, and it is the difference between an
    assertion about the package and one about nothing.
    """
    proof = sum(1 for node in ast.walk(parse(control)) if sees(node))
    assert proof, (
        "the census predicate matched nothing in its own control, so the "
        "inventory below is indistinguishable from what a switched-off net "
        f"returns. Control: {control!r}"
    )
    built = census(sources, sees, key)
    assert built == expected, f"{message} Found: {built}"


# --- the partition: what a walk saw, and what it could not decide ---------


@dataclass(frozen=True)
class Sites:
    """A walk's complete answer about one corpus.

    Every candidate lands in exactly one half. There is no third "was not
    looked at" bucket, which is the whole point: an unresolvable callee
    becomes a row in ``undecided`` rather than an absence in ``seen``.
    """

    seen: tuple[str, ...] = ()
    undecided: tuple[str, ...] = ()

    def __add__(self, other: Sites) -> Sites:
        return Sites(self.seen + other.seen, self.undecided + other.undecided)


def assert_sites(
    found: Sites,
    *,
    seen: tuple[str, ...],
    undecided: tuple[str, ...],
    message: str,
) -> None:
    """Assert BOTH halves. Neither keyword has a default, deliberately.

    A guard reporting nothing undecided has to write ``undecided=()``,
    which is a claim about the walk rather than silence about it, and a
    false claim fails here instead of passing quietly.
    """
    assert found.undecided == undecided, (
        "the walk could not decide these sites, so they are neither "
        "cleared nor flagged. Resolve them, or pin them with the reason "
        f"they cannot be resolved. Undecided: {list(found.undecided)}"
    )
    assert found.seen == seen, f"{message} Sites: {list(found.seen)}"
