"""What swallows an exception at a given point, and what it says.

Split out of ``tests/helpers/encodingwalk.py`` when that file crossed the
800-line ratchet, along the cut the code already made: "what can swallow
an exception here" is a different question from "what does this call
read", and #344 round 4 is entirely about the first one.

THE LESSON, because it outlives the shapes that produced it. The walk
used to enumerate ``ast.Try``. THAT IS A SPELLING, NOT THE PROPERTY. The
property is "a construct that swallows exceptions here", and at least
three things have it: ``try``, ``try/except*`` - a different node type
entirely - and a ``with`` whose context manager's ``__exit__`` returns
true, of which ``contextlib.suppress(OSError)`` is #320's own defect
written without the word ``try``.

Three review rounds each found the walk clearing a live escape, and each
time the fix was another case. Round 4 changed the rule instead:
:class:`Verdict` has three answers and no fourth, and ``unproven`` never
collapses into ``clear``. A construct this module cannot READ makes the
site undecided, which costs one row on ``kstrl/`` and buys an invariant
that does not need a tenth patch.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from tests.helpers.astwalk import (
    Bindings,
    all_nodes,
    folded_str,
    handler_clauses,
    leaf_name,
    own_nodes,
    try_body_nodes,
)
from tests.helpers.encodingrules import answers_for_io, clause_fault, covers_the_decode

#: The three constructs that can swallow an exception raised in their
#: body. A UNION and not ``ast.AST``, so mypy makes the ladder below
#: handle every member: the fourth spelling somebody adds cannot be
#: quietly ignored the way ``ast.TryStar`` was.
Swallower = ast.Try | ast.TryStar | ast.withitem


@dataclass(frozen=True)
class Verdict:
    """What the swallow ladder says about one decode site.

    Three answers and no fourth, which is the same shape :class:`Scan`
    keeps: ``clear`` is a PROOF, ``fault`` is a reason to report, and
    ``unproven`` is a reason nobody can act on yet and so a reason to be
    undecided. Option A is exactly the rule that the third answer never
    collapses into the first.
    """

    kind: str
    why: str = ""


CLEAR = Verdict("clear")
KEEP_LOOKING = Verdict("keep-looking")


def starts_at(node: Swallower) -> int:
    """The line a swallower starts on, which is how INNERMOST is decided.

    ``ast.withitem`` is neither a statement nor an expression and carries
    NO ``lineno``. A ``getattr(node, "lineno", 0)`` default therefore
    sorted every ``with`` item to line 0, putting it OUTERMOST - so an
    outer ``try`` that covers the decode answered first and cleared a
    read sitting inside an unprovable context manager, which is the exact
    escape :func:`with_verdict` exists to refuse. Measured while writing
    this: ``try/except (OSError, UnicodeDecodeError)`` around
    ``with tempfile.TemporaryDirectory()`` around the read came back
    ``clear=2`` on the broken key and ``undecided`` on this one.

    The item's context expression is the line the reader sees, so it is
    the line the ladder uses.
    """
    return node.context_expr.lineno if isinstance(node, ast.withitem) else node.lineno


def body_ids(node: ast.AST) -> set[int]:
    """The ids of the nodes a construct's BODY owns, stopping at a nested
    function for the reason :func:`try_body_nodes` gives."""
    if isinstance(node, ast.Try | ast.TryStar):
        return {id(child) for child in try_body_nodes(node)}
    owned: list[ast.AST] = []
    for statement in getattr(node, "body", []):
        owned.append(statement)
        if not isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            owned.extend(own_nodes(statement))
    return {id(child) for child in owned}


def swallowers(tree: ast.Module) -> list[tuple[Swallower, set[int]]]:
    """Every construct that could swallow an exception raised in its body,
    with the ids of the nodes that body owns.

    #344 round 4 found the hole this closes, and it is a lesson worth
    more than the shape that revealed it. The old universe was
    ``ast.Try``. THAT IS A SPELLING, NOT THE PROPERTY. The property is
    "a construct that swallows exceptions here", and at least three
    things have it:

    - ``ast.Try``, the spelling everybody thinks of
    - ``ast.TryStar``, which is a different node type entirely
    - a ``with`` whose context manager's ``__exit__`` returns true, of
      which ``contextlib.suppress(OSError)`` is #320's own defect written
      without the word ``try``

    ``suppress`` is a live idiom in this repo - ``procdispose.py:281``
    and ``config_preflight.py:537`` - so this is not hypothetical, and
    the 20-site census that searched for ``except OSError`` would not
    have found a site written the other way either. Measured on
    ``kstrl/`` at the time of writing: 0 ``TryStar``, 2 ``suppress``,
    neither of them around a text read, and 3 non-``open`` ``with``
    statements around one.

    A ``with`` is entered as a swallower unless the walk can PROVE it is
    not one, and the only thing it proves is an ``open`` call, because
    that is the object it already resolved. Everything else is
    ``unproven``. That is option A: the guard would rather name three
    rows a reader can work through than clear a construct it never read.
    """
    found: list[tuple[Swallower, set[int]]] = []
    for node in all_nodes(tree):
        if isinstance(node, ast.Try | ast.TryStar):
            found.append((node, body_ids(node)))
        elif isinstance(node, ast.With | ast.AsyncWith):
            owned = body_ids(node)
            found.extend((item, owned) for item in node.items)
    return found


def with_verdict(item: ast.withitem, table: Bindings) -> Verdict:
    """What one ``with`` item does to an exception raised in its body."""
    call = item.context_expr
    if isinstance(call, ast.Call) and leaf_name(call.func) == "open":
        # The one construct this walk has already resolved: a text
        # stream, whose __exit__ closes the file and returns None.
        return KEEP_LOOKING
    if isinstance(call, ast.Call) and leaf_name(call.func) == "suppress":
        names = {
            got for arg in call.args if (got := folded_str(arg) or exception_name(arg)) is not None
        }
        if len(names) != len(call.args):
            return Verdict(
                "unproven",
                "sits inside a suppress() whose arguments this walk cannot name",
            )
        if covers_the_decode(names):
            return CLEAR
        if answers_for_io(names):
            return Verdict(
                "fault",
                f"sits inside suppress({sorted(names)[0]}) with nothing covering "
                "UnicodeDecodeError, which is a ValueError and is swallowed with it",
            )
        return KEEP_LOOKING
    return Verdict(
        "unproven",
        f"sits inside `with {ast.unparse(call)[:40]}`, whose __exit__ this walk cannot "
        "prove does not swallow the decode",
    )


def exception_name(node: ast.expr) -> str | None:
    """The identifier a ``suppress`` argument spells, or None."""
    return leaf_name(node)


def guard_verdict(
    node: ast.AST, swallowers: list[tuple[Swallower, set[int]]], table: Bindings
) -> Verdict:
    """The first enclosing construct that ANSWERS for this decode, and
    what it says.

    OUTWARD FROM THE INNERMOST, not the innermost alone. An inner ``try``
    that already covers the decode means the decode never reaches an
    outer ``except OSError``, so the outer one is not an escape; and an
    inner ``try`` catching only ``JSONDecodeError`` does not stop the
    decode reaching an outer ``except OSError`` that has no clause for
    it. Only the innermost construct that ANSWERS decides, either way.

    The empty ladder returns ``CLEAR`` and that is a PROOF rather than
    an absence: :func:`swallowers` enumerates the constructs that could
    swallow, so nothing enclosing means nothing can. That is the twelve
    "no handler anywhere" rows in ``kstrl/``, and it is deliberately not
    interprocedural: a caller that wraps this function in its own
    ``except OSError`` is the caller's site to answer for, and this walk
    says so rather than pretending to see it.
    """
    holding = sorted(
        (statement for statement, owned in swallowers if id(node) in owned),
        key=starts_at,
        reverse=True,
    )
    for statement in holding:
        if isinstance(statement, ast.withitem):
            verdict = with_verdict(statement, table)
        else:
            found = clause_fault(handler_clauses(statement, table))
            verdict = (
                KEEP_LOOKING
                if found == "keep-looking"
                else (CLEAR if found is None else Verdict("fault", found))
            )
        if verdict.kind != "keep-looking":
            return verdict
    return CLEAR


def verdict_parts(verdict: Verdict) -> tuple[str | None, str | None]:
    """A :class:`Verdict` split into the two fields :class:`Read` keeps.

    ``fault`` is a reason to report and ``unproven`` a reason to be
    undecided, and NEITHER of them is ``clear``. Keeping them apart here
    rather than folding ``unproven`` into ``guard_fault`` is what lets
    ``reported`` stay the actionable list: an unproven row asks a reader
    to look at a construct, a reported row names a defect.
    """
    if verdict.kind == "fault":
        return verdict.why, None
    if verdict.kind == "unproven":
        return None, verdict.why
    return None, None
