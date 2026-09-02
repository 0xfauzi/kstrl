"""The shape-independent layer: a census, and the total partition.

Neither enumerates a node type. :func:`spells` does not enumerate a
FIELD name either, which is why it catches shapes nobody thought of.
:class:`Sites` has no third bucket: a candidate lands in ``seen`` or in
``undecided``. What that buys is stated exactly in :class:`Sites`, because
the shorter version of it was false."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tests.helpers.astwalk.corpus import all_nodes, folded_str, label, parse, parsed

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
#: expression passes its own. Not exported: one guard passes ``key=`` and
#: it passes a plain function, so the alias was a name in ``__all__`` with
#: no user.
Keyed = Callable[[Path, ast.AST], str]


def census(sources: Iterable[Path], sees: Sees, key: Keyed | None = None) -> dict[str, int]:
    """How many nodes in each module satisfy ``sees``, keyed by label.

    Modules with no hits are left out, so the pinned dict is the size of
    the answer rather than of the package."""
    name = key or (lambda source_file, _node: label(source_file))
    built: dict[str, int] = {}
    for source_file in sources:
        for node in all_nodes(parsed(source_file)):
            if sees(node):
                row = name(source_file, node)
                built[row] = built.get(row, 0) + 1
    return built


def assert_census(
    *,
    sources: Iterable[Path],
    sees: Sees,
    expected: Mapping[str, int],
    control: str | Sequence[str],
    message: str,
    key: Keyed | None = None,
) -> None:
    """Pin an inventory, having first proved the net still fires AND that
    it was pointed at something.

    ``control`` is source the predicate MUST hit. Required, not optional,
    because ``built == expected`` is also what a switched-off predicate
    returns, and #324's subject is guards reporting clean because they
    stopped looking. One line, and it is the difference between an
    assertion about the package and one about nothing.

    ONE CONTROL PER DISJUNCT. A single control is a SCALAR proof over the
    whole predicate, so ``anchor(node) or namer(node)`` stays green on it
    with either half deleted, and #324 round 2 measured exactly that in
    ``tests/test_state_dir_scope.py``: deleting a disjunct failed the
    INVENTORY and left the control passing, which makes the control read
    as a mechanism it is not. Pass a sequence and each string is proved
    separately, so a dead half fails here naming the control that went
    quiet. The helper cannot count a callable's disjuncts, so this is a
    contract the caller keeps; what it can do is make keeping it possible
    and make the failure name the half.

    THERE IS NO MECHANISM FOR IT, and the obvious one is false rather
    than merely weak, so it is not built. A static guard counting the
    ``or`` operands of each ``sees=`` predicate would report 2 for all
    four compound predicates in this suite, whose real branch counts are
    2, 4, 3 and 2. Measured, and ``_searches_the_machine``'s single
    ``or`` is ``folded_str(node) or ""``, a default rather than a
    disjunction at all, while its four-way choice is a set membership
    inside a helper. So the guard would have demanded two controls from
    the site that needs four and passed it. A check that cannot fail for
    the reason it names is the defect this branch exists to end, and
    building one here would be committing it inside the fix.
    ``tests/test_astwalk_nets.py::test_counting_or_operands_is_not_the_
    mechanism`` pins that measurement so the disclosure cannot rot.

    THE CORPUS IS THE OTHER HALF AGAIN, and no control can speak for it:
    a control parses a string, so it fires whether ``sources`` holds 128
    modules or none. This assertion covers THIS CALL only. Four guards in
    this suite walk :func:`~.corpus.package_sources` directly and never
    reach here, which is why the emptiness check also lives at that
    chokepoint; see its docstring for the measurement.

    ``sources`` is materialised once, which also stops a caller passing a
    generator that a second reader would find spent.
    """
    corpus = list(sources)
    assert corpus, (
        "the census was handed an empty corpus, so the inventory below is "
        "an assertion about nothing. Check the paths the caller derived: a "
        "wrong root globs no files and every net in the suite returns {}."
    )
    for one in (control,) if isinstance(control, str) else control:
        proof = sum(1 for node in all_nodes(parse(one)) if sees(node))
        assert proof, (
            "the census predicate matched nothing in this control, so the "
            "inventory below is indistinguishable from what a switched-off "
            f"net returns. Control: {one!r}"
        )
    built = census(corpus, sees, key)
    assert built == expected, f"{message} Found: {built}"


# --- the partition: what a walk saw, and what it could not decide ---------


@dataclass(frozen=True)
class Sites:
    """A walk's complete answer about one corpus.

    Every CANDIDATE lands in exactly one half. There is no third "was not
    looked at" bucket: an unresolvable callee becomes a row in
    ``undecided`` rather than an absence in ``seen``.

    WHAT THIS DOES NOT SAY, and an earlier draft did. It does not say
    that a walk which could not look cannot read clean. That claim is
    about CANDIDACY, which this class does not own: a call the classifier
    decides is somebody else's is in neither half, correctly, and the
    partition cannot tell a correct decision from a wrong one.

    Round 3 of review constructed the wrong one. Four lines of ordinary
    code, ``class _Meter: load = os.getloadavg``, made the bare-name
    over-match answer confidently for a receiver it had never seen, so a
    genuinely undecidable ``mod.load(handle)`` was DECIDED to be somebody
    else's and left both halves. It also left ``tests/test_toml_readers``'
    own ``guarded``, ``unguarded`` and ``parses`` inventories, taking
    that file from 1 failed to 37 passed. That specific hole is closed:
    :class:`Origin` labels the guess and :func:`_classify_call` treats a
    guess outside the target set as undecided rather than as a decision.

    So the claim that is true, and it is the one worth making: the
    partition is total over candidacy, and candidacy is decided by
    :func:`calls_to`'s two questions, whose own residuals are pinned. A
    third hole in resolution would produce a third clean report, and the
    only defence against that is that resolution is now in ONE place with
    its own tests rather than in eleven.
    """

    seen: tuple[str, ...] = ()
    undecided: tuple[str, ...] = ()

    def __add__(self, other: Sites) -> Sites:
        return Sites(self.seen + other.seen, self.undecided + other.undecided)

    def sorted(self) -> Sites:
        """The same answer in a stable order.

        Both migration lanes of #324 asked for this independently: rows
        accumulated with ``+`` across a corpus come out in file-iteration
        order, so a pin churns for a reason that is not the guard's
        subject. Sorting is the cheap half of that; the expensive half is
        that a row carries a line number at all, which a caller strips
        for itself when it wants a pin an edit above the site cannot
        break.
        """
        return Sites(tuple(sorted(self.seen)), tuple(sorted(self.undecided)))

    def without_line_numbers(self) -> Sites:
        """The same answer keyed by module and expression, deduplicated.

        The other half of what both lanes asked for, and the one that
        matters more: a pin carrying a line number fails when an unrelated
        edit lands above the site, which trains a reader to update the
        number without reading the row. Both lanes wrote a local copy of
        this, and rebasing this branch onto a moved main failed four pins
        on line numbers alone, none of which was the guard's subject.

        DEDUPLICATED THROUGH A SET, so a pin built on it CANNOT COUNT.
        Two identical expressions in one module collapse to one row, and
        deleting one of them moves nothing. That is the right trade for a
        pin whose subject is "which modules do this, and how", and the
        wrong one for a pin whose subject is "how many", which is what
        ``assert_census`` is for. A guard that needs both pins both.

        Use it for a package-wide inventory. Keep the line numbers where
        the site is the answer, as ``tests/test_toml_readers.py`` does for
        two parses on one line.
        """
        return Sites(_dropped(self.seen), _dropped(self.undecided))


def _dropped(rows: Iterable[str]) -> tuple[str, ...]:
    """Rows without their line number. ``a/b.py:12 x`` and ``12 x`` both
    lose the number; a row that never carried one is returned as it is."""
    out = set()
    for row in rows:
        head, _, rest = row.partition(" ")
        if head.isdigit():
            out.add(rest or head)
        else:
            out.add(f"{head.rsplit(':', 1)[0]} {rest}".rstrip())
    return tuple(sorted(out))


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
