"""Which construct a node belongs to, and what a handler catches.

Both answer a question three guards asked badly: ``own_nodes`` stops
at a nested scope so a helper defined in a ``try`` is not credited to
it, and :class:`Clause` says when it could not name what is caught."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from tests.helpers.astwalk.corpus import all_nodes
from tests.helpers.astwalk.resolve import Bindings

# --- scope ----------------------------------------------------------------


def own_nodes(node: ast.AST) -> list[ast.AST]:
    """Every node belonging to this scope, stopping at a nested function.

    So a helper DEFINED inside a ``try`` and called elsewhere is not
    credited to it. ``ClassDef`` is deliberately not a stop: a method body
    belongs to its class, and the function stop keeps the attribution
    innermost.
    """
    found: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        found.append(child)
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            found.extend(own_nodes(child))
    return found


def try_body_nodes(node: ast.Try | ast.TryStar) -> list[ast.AST]:
    """Every node in this ``try``'s BODY, stopping at a nested function.

    ``ast.TryStar`` is accepted because ``try/except*`` is a different
    NODE TYPE with the same body, and a guard that types this parameter
    as ``ast.Try`` alone silently declines to look inside one. #344 round
    4 found that shape in the encoding walk. There are none in ``kstrl/``
    today, which is exactly why nothing would have failed.

    :func:`own_nodes` is the boundary; this is what applies it to a LIST
    of statements rather than to one node, and what stops a ``def``
    written at the top of the body being descended into. The distinction
    matters because a plain ``ast.walk`` credits a ``try`` with guarding
    a call that lives in a function defined in its body and called
    somewhere else, under a handler that will never see its exception.

    Two guards wanted exactly this - ``tests/test_toml_readers.py`` for
    the parse it attributes to a handler ladder, and
    ``tests/helpers/encodingwalk.py`` for the read it attributes to
    one - and wrote it twice before it was hoisted here.
    """
    found: list[ast.AST] = []
    for statement in node.body:
        found.append(statement)
        if not isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            found.extend(own_nodes(statement))
    return found


def scopes(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """Every scope in a module and its qualified name.

    ``<module>``, then ``build``, ``EvolutionJournal.append_entries``,
    ``_prepare.build.target``. The qualified name lets an exemption table
    name ONE closure rather than every function of that name in the file.
    """
    found: list[tuple[ast.AST, str]] = [(tree, "<module>")]
    _walk_scopes(tree, "", found)
    return found


def _walk_scopes(node: ast.AST, prefix: str, found: list[tuple[ast.AST, str]]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            qualified = f"{prefix}{child.name}"
            found.append((child, qualified))
            _walk_scopes(child, f"{qualified}.", found)
        elif isinstance(child, ast.ClassDef):
            _walk_scopes(child, f"{prefix}{child.name}.", found)
        else:
            _walk_scopes(child, prefix, found)


def declared_in(tree: ast.Module, class_name: str, method: str) -> set[int]:
    """The lines of one method of one class, resolved through the class.

    An exemption resolved by function NAME alone gives a free pass to an
    unrelated method that shares it, which is what round 1 of #327
    shipped. Located by walking, so editing the file above it is free.
    """
    for node in all_nodes(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name == method:
                return set(range(item.lineno, (item.end_lineno or item.lineno) + 1))
    return set()


# --- try/except -----------------------------------------------------------


@dataclass(frozen=True)
class Clause:
    """One ``except`` clause: what it catches, and whether that is known.

    ``decided`` is what stops the skip direction. A handler whose type
    this walk cannot name yields an EMPTY ``names``, and an empty set
    reads exactly like "catches nothing", which is the worst possible
    misreading of "catches something I could not see".

    ``names`` and ``origins`` answer two different questions and cannot
    contradict each other, which is the test the deleted ``Bindings``
    field failed: ``names`` is what the clause is CALLED, one leaf per
    part, and ``origins`` is where the resolver could place it, one
    dotted origin per part it could. A part the table cannot place has a
    name and no origin. Ask ``names`` about a builtin, which has no
    import to resolve; ask ``origins`` about anything a module had to
    obtain, because the name alone is a spelling anybody can rebind.
    """

    names: frozenset[str]
    decided: bool
    lineno: int
    origins: frozenset[str] = frozenset()


def handler_clauses(node: ast.Try | ast.TryStar, table: Bindings | None = None) -> list[Clause]:
    """The clauses of one ``try``, IN ORDER, resolved through ``table``.

    ``ast.TryStar`` counts, for the reason :func:`try_body_nodes` gives:
    ``except*`` is a spelling of the same property, and a signature that
    excludes it is a hole nothing in a repo without one can detect.

    Order is load-bearing: a broad clause above a narrow one makes the
    narrow one unreachable, so a guard that sorts cannot tell a correct
    ladder from a dead one. A bare ``except:`` reads as ``BaseException``,
    which is what it catches, and is not an undecidable handler.

    ``table`` is what a DOTTED clause is resolved against, and leaving it
    out means nothing dotted resolves, so every dotted clause is
    undecided. That default is fail-closed on purpose. Round 2 of #324
    measured the alternative: reading a dotted clause's LEAF name moved
    ``except shim.Exception``, ``except shim.SURFACE_REJECTIONS`` and
    ``except (ValueError, shim.Exception)`` from reported to CLEARED in
    ``tests/test_tui_config_walk.py``, where the pre-#324 walk reported
    all three. A migration that narrows a guard is the defect this issue
    records, so a dotted name is only its leaf when the resolver can
    place it. On ``kstrl/`` itself the two answers agree: all 114 dotted
    clause parts there resolve, none of them to a neutralising name.

    ``Clause.origins`` comes from the same table and is the other half of
    that correction. Round 2 of #324 measured a module-level
    ``SURFACE_REJECTIONS = (ValueError,)`` in ``kstrl/tui/screens/`` with
    an unguarded config load under it: the TUI guard matched the clause
    by SPELLING, cleared the load, and its whole file stayed green at 49
    passed. A name is not an identity, so a guard whose neutralising set
    is a project constant reads ``origins`` and gets the import back.
    """
    resolver = table if table is not None else Bindings()
    return [_clause(handler, resolver) for handler in node.handlers]


def _clause(handler: ast.ExceptHandler, table: Bindings) -> Clause:
    if handler.type is None:
        return Clause(frozenset({"BaseException"}), True, handler.lineno, frozenset())
    parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = {_clause_name(part, table) for part in parts}
    placed = [table.origin_of(part) for part in parts]
    origins = {found.dotted for found in placed if found is not None and not found.guessed}
    return Clause(
        frozenset(name for name in names if name is not None),
        None not in names,
        handler.lineno,
        frozenset(origins),
    )


def _clause_name(part: ast.expr, table: Bindings) -> str | None:
    """What one clause part catches, or None when it cannot be named.

    ASK THE RESOLVER FIRST, EVEN FOR A BARE NAME. What stood here
    returned ``part.id`` and said in its own docstring that nothing can
    rebind ``Exception`` without an assignment the resolver would see. An
    IMPORT is not an assignment: ``from json import JSONDecodeError as
    Exception`` leaves this reading ``Exception``, the ``tomllib`` guard
    green at 37 passed, and a real ``TOMLDecodeError`` escaping at run
    time, which is #318's shipped defect. ``origins`` held
    ``json.JSONDecodeError`` the whole time and the code did not look.
    Round 3 of review found it, and it is this branch's own "a disclosure
    whose wording does not reach the shape" finding turned back on it.

    A GUESS IS NOT A NAME. An origin from the bare-name over-match
    answers for any receiver in the module, so
    ``class X: Exc = builtins.Exception`` would make ``except other.Exc:``
    read as broad for a guard whose rule is "``Exception`` exactly".
    Naming a clause is a decision, so a guessed origin leaves the clause
    undecided instead.

    A dotted name is its leaf only when the resolver can place it, so
    ``except shim.Exception`` in a module that never bound ``shim`` is
    nothing at all.
    """
    found = table.origin_of(part)
    if found is None:
        return part.id if isinstance(part, ast.Name) else None
    if found.guessed:
        return None
    return found.dotted.rsplit(".", 1)[-1]
