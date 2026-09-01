"""Fold a ``kstrl/`` expression to the string it is KNOWN to evaluate to.

Shared by the two guards over the journal's failure signatures -
``tests/test_check_name_enrolment.py``, which resolves the four
producers and asserts the category table carries what they emit, and
``tests/test_signature_spellings.py``, which enumerates no node types
and pins every signature-shaped string in the package. They ask
different questions of the same folding, and #339 review is the reason
there is only one copy of it.

Not ``tests.test_journal_one_writer.folded_str``, which answers a
narrower question with the same word. That one is strict: any
undecidable piece makes the whole answer ``None``, and a bare ``Name``
is always undecidable, because it is looking for a filename and a
partial filename is not one. A check name is a PREFIX, so a partial
answer is often a complete one - ``f"contract:tier_{n}"`` names its
check as plainly as a literal does - and package-level constants have to
resolve, because ``verify`` builds its gate names out of
``gateparse.GATE_TEST``. Hence :data:`HOLE` and :class:`Folded`.

That is a difference in the ANSWER, not in the rules, and #339 review
measured how small it is: over the 22,738 ``kstrl/`` expressions
containing no ``ast.Name``, ``folded_str`` and a strict reading of
``fold`` (``None`` whenever the result carries a :data:`HOLE`) agree on
every one, and over all 119,605 expressions the only 471 disagreements
are the deliberate name-resolution branch. So the strict answer is a
two-line wrapper over this one and the two SHOULD converge. They are not
merged here because ``test_journal_one_writer.py`` guards #312 and
changing what it folds is that guard's own measurement to make, not a
side effect of a categorisation fix. Read this paragraph as a pointer to
that work, not as a reason the copies must stay.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

KSTRL_DIR = Path(__file__).resolve().parent.parent.parent / "kstrl"

#: Stands in for a piece of a string the walk cannot decide.
#:
#: TWO NULs, not one. #339 review measured the obvious choice wrong:
#: ``kstrl/`` holds three literals containing a single ``\x00``
#: (``git.py``, ``pipeline.py``, ``workqueue.py``), so a one-NUL marker
#: is a claim about the package that the package already contradicts.
#: A doubled NUL appears in no literal there today, and
#: ``tests/test_signature_spellings.py`` asserts that rather than
#: leaving it as a claim - which is the whole standard this file exists
#: to meet.
HOLE = "\x00\x00"


@dataclass(frozen=True)
class Folded:
    """A string expression's value, with a marker for each unknown piece.

    ``text`` is what the interpreter would produce with every
    undecidable piece replaced by :data:`HOLE`. ``first_hole`` is the
    expression that produced the leftmost of those, and it is what makes
    an unknown piece answerable rather than merely flagged: the one
    consumer that rescues a hole needs the NODE, not the fact that there
    was one.

    Only the first, measured: 4,496 folds carry a hole, 5,485 holes in
    all, and no reader can reach any hole but the leftmost, because a
    check name is what precedes the first colon.
    """

    text: str
    first_hole: ast.expr | None


def _is_str_constant(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _string_bindings(node: ast.stmt) -> dict[str, str]:
    """``{name: value}`` for one statement, empty unless it binds a str.

    Handles the annotated form as well as the plain one. #339 review
    measured what that is worth today and the honest answer is nothing:
    ``kstrl/`` has three module-level annotated string constants
    (``agents/base.ARCHITECT_ROLE``, ``names.ROLE_KEY_PREFIX``,
    ``serve.SPAWNED_RUN_KIND``) and none is a check name, so deleting
    the branch leaves the census identical. An earlier version of this
    docstring claimed ``gateparse`` needed it; ``gateparse`` declares
    ``GATE_TEST`` and friends with a plain ``ast.Assign``. The branch
    stays because ``ast.Assign``-only readers are a logged defect class
    in this repo (``assignment_parts`` in
    ``tests/test_journal_one_writer.py`` carries the same note for the
    same reason), but it is future-proofing, not a live requirement.
    """
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and _is_str_constant(node.value):
            return {node.target.id: node.value.value}  # type: ignore[union-attr]
        return {}
    if not isinstance(node, ast.Assign) or not _is_str_constant(node.value):
        return {}
    return {t.id: node.value.value for t in node.targets if isinstance(t, ast.Name)}  # type: ignore[attr-defined]


def called_name(func: ast.expr) -> str:
    """The bare callable name, through an attribute access.

    ``verify.CheckResult(...)`` and ``CheckResult(...)`` are the same
    construction to this walk. No call site uses the qualified form
    today, which is exactly why an ``ast.Name``-only reader would not
    fail when one is added.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


@lru_cache(maxsize=1)
def parsed_modules() -> tuple[tuple[str, ast.Module], ...]:
    """Every ``kstrl/`` module, parsed once for the whole session.

    Measured before caching: the census re-parsed all 124 files on each
    of its five calls and parsed the package a second time inside the
    constant collector, costing 1.52 s, about 70 percent of the
    scope-related test files' total runtime.
    """
    return tuple(
        (str(path.relative_to(KSTRL_DIR.parent)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(KSTRL_DIR.rglob("*.py"))
    )


def _positional_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """A function's positional parameters, receiver excluded.

    Excluded so the answer indexes a call's ``args`` directly: the
    receiver of ``self.fail(...)`` is in ``node.func``, not in
    ``node.args``.
    """
    names = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
    return names[1:] if names[:1] in (["self"], ["cls"]) else names


def _annotated_fields(node: ast.ClassDef) -> list[str]:
    """A dataclass's fields, in the order its constructor takes them."""
    return [
        stmt.target.id
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]


@lru_cache(maxsize=1)
def _positional_parameters_by_name() -> dict[str, list[list[str]]]:
    """Every definition in ``kstrl/``, as ``name -> parameter lists``.

    ONE pass, indexed, rather than a walk per question. #339 review
    measured the per-key version at 6 cache misses in a four-file run,
    each re-walking all 127 modules to find one ``def``: 241 ms on a
    quiet machine and 581 ms under load, of which 124 ms landed inside
    ``_census`` alone. This pass costs 58 ms once for all 1,775 names
    and is flat in the number of questions asked.

    A LIST of parameter lists per name, not one: two definitions can
    share a name, and which of them a call site meant is exactly what
    :func:`parameter_index` refuses to guess.
    """
    found: dict[str, list[list[str]]] = {}
    for _rel, tree in parsed_modules():
        for node in ast.walk(tree):
            name = getattr(node, "name", None)
            if not isinstance(name, str):
                continue
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                found.setdefault(name, []).append(_positional_parameters(node))
            elif isinstance(node, ast.ClassDef):
                found.setdefault(name, []).append(_annotated_fields(node))
    return found


def parameter_index(name: str, parameter: str) -> int | None:
    """Where ``parameter`` sits positionally in ``name``'s definition.

    Read off the ``def`` (or the dataclass body), never written down.
    #339 review measured the hand-written version: four integers next to
    a walk that parses the very tree that states them, and six of eight
    off-by-one mutations to them left the guards green. An index is a
    FACT about the definition, so deriving it deletes both the integers
    and the argument for what they should be.

    ``None`` when no ``kstrl/`` definition of that name has the
    parameter, or when two definitions disagree about where it sits. The
    first is what keeps ``feature_cmd``'s one-argument local ``fail``
    out of the census by construction rather than by a special case; the
    second is the safe end of an ambiguity, because a guard that guessed
    between two positions would read the wrong argument and report a
    confident wrong name.
    """
    positions = {
        names.index(parameter)
        for names in _positional_parameters_by_name().get(name, ())
        if parameter in names
    }
    return positions.pop() if len(positions) == 1 else None


@lru_cache(maxsize=1)
def module_level_strings() -> dict[str, dict[str, str]]:
    """Module-level ``NAME = "literal"`` bindings, keyed by module."""
    return {
        rel: {k: v for node in tree.body for k, v in _string_bindings(node).items()}
        for rel, tree in parsed_modules()
    }


def unambiguous_pool(per_module: dict[str, dict[str, str]]) -> dict[str, str]:
    """Constants that mean ONE thing across the whole package.

    A check name is usually defined in one module and used in another:
    ``verify`` builds its gates from ``gateparse.GATE_TEST``. So the
    walk cannot resolve names against the using module alone. Pooling
    every module's constants into one flat dict is the obvious fix and
    is wrong in a way nothing would report: two modules binding the same
    name to different values would resolve to whichever file sorts last,
    silently attributing one module's check name to another's.

    So a name is poolable only when every definition of it agrees. A
    genuinely ambiguous name resolves to nothing and, if it is a check
    name, surfaces as a blind site rather than as the wrong answer.
    There are no collisions today; this is here so the walk has a reason
    to be correct rather than a coincidence.
    """
    values: dict[str, set[str]] = {}
    for constants in per_module.values():
        for name, value in constants.items():
            values.setdefault(name, set()).add(value)
    return {name: next(iter(v)) for name, v in values.items() if len(v) == 1}


@lru_cache(maxsize=1)
def pool() -> dict[str, str]:
    return unambiguous_pool(module_level_strings())


def _fold_part(node: ast.expr, own: dict[str, str]) -> Folded:
    """A piece of a larger string: never ``None``, a hole at worst."""
    folded = fold(node, own)
    return folded if folded is not None else Folded(HOLE, node)


def _fold_join(nodes: list[ast.expr], own: dict[str, str]) -> Folded:
    parts = [_fold_part(n, own) for n in nodes]
    return Folded(
        "".join(p.text for p in parts),
        next((p.first_hole for p in parts if p.first_hole is not None), None),
    )


def _fold_name(node: ast.Name, own: dict[str, str]) -> Folded | None:
    """A module-level string constant, defining module first."""
    value = own.get(node.id) or pool().get(node.id)
    return Folded(value, None) if value is not None else None


def _fold_placeholder(node: ast.FormattedValue, own: dict[str, str]) -> Folded:
    """``!r`` and a format spec both change the result, so only the plain
    placeholder folds; the rest is a hole, not a guess."""
    if node.conversion == -1 and node.format_spec is None:
        return _fold_part(node.value, own)
    return Folded(HOLE, node)


def _fold_concat(node: ast.BinOp, own: dict[str, str]) -> Folded | None:
    """``"check" + ":code"``, and nothing else that uses an operator.

    At least one side has to be evidently a string, or ``a + b`` on two
    unknowns would fold to two holes and read as a string expression.

    Measured: 125 ``BinOp``/``Add`` nodes in ``kstrl/`` fold, and none of
    them is a signature today, so this branch is here for the shape
    rather than for a site. ``tests/test_signature_spellings.py`` has a
    fixture for it, because a branch nothing exercises is a branch
    nobody knows is broken - and a concatenation is exactly what
    somebody writes to get past a string search (#327 F9).
    """
    if not isinstance(node.op, ast.Add):
        return None
    if fold(node.left, own) is None and fold(node.right, own) is None:
        return None
    return _fold_join([node.left, node.right], own)


def fold(node: ast.AST, own: dict[str, str]) -> Folded | None:
    """What this expression is known to evaluate to, or ``None``.

    ``None`` means "not evidently a string expression at all", which is
    a different answer from "a string with an unknown piece" - the
    latter is a :data:`HOLE`. Only the shapes that can be decided
    without an interpreter are handled; a call, a subscript, a
    ``%``-format or a ``.join`` is ``None`` here and the caller records
    a blind site rather than pretending.

    Split across five functions for the same reason
    ``test_journal_one_writer.folded_str`` is split across four: the
    recursion costs 18 on the cognitive gate in one, and that hook fails
    rather than advises.
    """
    if isinstance(node, ast.Constant):
        return Folded(node.value, None) if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return _fold_name(node, own)
    if isinstance(node, ast.JoinedStr):
        return _fold_join(node.values, own)
    if isinstance(node, ast.FormattedValue):
        return _fold_placeholder(node, own)
    if isinstance(node, ast.BinOp):
        return _fold_concat(node, own)
    return None


#: What a journal signature looks like. The head is a check name, so it
#: is a lower-case identifier; the code is a ``signature_slug`` (lower
#: alphanumerics and hyphens), a linter rule, or a ``Finding.category``,
#: and it may carry further colons - ``pr:coverage-unverified:no-diffstat``
#: is real. Whitespace and path separators are what this excludes, which
#: is what keeps ordinary prose, URLs and traceback lines out. A
#: :data:`HOLE` is allowed in the CODE and never in the head.
_SIGNATURE_SHAPE = re.compile(r"[a-z][a-z0-9_]*:[A-Za-z0-9_.:\x00-]+")


def folded_nodes(tree: ast.AST, own: dict[str, str]) -> Iterator[Folded]:
    """Every subexpression of ``tree`` that folds to a string.

    One walk for both guards. #339 review found them shipping a copy
    each - the net in ``tests/test_signature_spellings.py`` and
    ``_names_in`` in ``tests/test_check_name_enrolment.py`` - which is
    the exact "two copies that can be widened apart" hazard this module
    was created to remove, reintroduced inside the same change. The
    enrolment guard's extra step, rescuing an unresolved head from the
    call sites, layers on top of the yielded value rather than needing a
    walk of its own.
    """
    for node in ast.walk(tree):
        folded = fold(node, own)
        if folded is not None:
            yield folded


def signature_head(folded: Folded) -> str | None:
    """The check name of a folded signature, or ``None``.

    ``split_signature`` takes everything before the FIRST colon, so the
    code being unknown costs nothing: ``f"contract:tier_{n}"`` names its
    check as plainly as a literal does, and the walk this replaces
    folded it to nothing because one piece was unknown.
    """
    if not _SIGNATURE_SHAPE.fullmatch(folded.text):
        return None
    head = folded.text.partition(":")[0]
    return head


@dataclass(frozen=True)
class Scope:
    """One node's enclosing function, and the dotted path naming it."""

    qualname: str
    function: ast.FunctionDef | ast.AsyncFunctionDef | None


def scoped_nodes(tree: ast.Module) -> Iterator[tuple[ast.AST, Scope]]:
    """Every node of one module, paired with the function enclosing it.

    A generator rather than the ``{id(node): Scope}`` map this started
    as. #339 review measured that map at 159,548 entries across the
    package to answer 71 questions, and it cost more than the memory: it
    needed a paragraph arguing that no node is collected underneath it
    so no id is reused, and a ``.get(..., <module scope>)`` fallback at
    every call site that could not fire. Yielding the pair deletes the
    dict, the id keying, the argument and the fallbacks.
    """

    def descend(
        node: ast.AST, path: tuple[str, ...], scope: Scope
    ) -> Iterator[tuple[ast.AST, Scope]]:
        for child in ast.iter_child_nodes(node):
            yield child, scope
            if isinstance(child, ast.ClassDef):
                yield from descend(child, path + (child.name,), scope)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                inner = Scope(".".join(path + (child.name,)), child)
                yield from descend(child, path + (child.name,), inner)
            else:
                yield from descend(child, path, scope)

    yield from descend(tree, (), Scope("<module>", None))
