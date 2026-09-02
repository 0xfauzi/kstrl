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


def handler_clauses(node: ast.Try, table: Bindings | None = None) -> list[Clause]:
    """The clauses of one ``try``, IN ORDER, resolved through ``table``.

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
    origins = {table.resolve(part) for part in parts}
    return Clause(
        frozenset(name for name in names if name is not None),
        None not in names,
        handler.lineno,
        frozenset(origin for origin in origins if origin is not None),
    )


def _clause_name(part: ast.expr, table: Bindings) -> str | None:
    """What one clause part catches, or None when it cannot be named.

    A bare ``Name`` is its own answer: nothing can rebind ``Exception``
    for an ``except`` clause without an assignment the resolver would see.
    A dotted name is its leaf only when the resolver can place it, so
    ``except json.JSONDecodeError`` is ``JSONDecodeError`` and
    ``except shim.Exception`` in a module that never bound ``shim`` is
    nothing at all.
    """
    if isinstance(part, ast.Name):
        return part.id
    origin = table.resolve(part)
    return None if origin is None else origin.rsplit(".", 1)[-1]
