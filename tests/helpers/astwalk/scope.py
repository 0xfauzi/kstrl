"""Which construct a node belongs to, and what a handler catches.

Both answer a question three guards asked badly: ``own_nodes`` stops
at a nested scope so a helper defined in a ``try`` is not credited to
it, and :class:`Clause` says when it could not name what is caught."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from tests.helpers.astwalk.resolve import leaf_name

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
    for node in ast.walk(tree):
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
    """

    names: frozenset[str]
    decided: bool
    lineno: int


def handler_clauses(node: ast.Try) -> list[Clause]:
    """The clauses of one ``try``, IN ORDER.

    Order is load-bearing: a broad clause above a narrow one makes the
    narrow one unreachable, so a guard that sorts cannot tell a correct
    ladder from a dead one. A bare ``except:`` reads as ``BaseException``,
    which is what it catches, and is not an undecidable handler.
    """
    return [_clause(handler) for handler in node.handlers]


def _clause(handler: ast.ExceptHandler) -> Clause:
    if handler.type is None:
        return Clause(frozenset({"BaseException"}), True, handler.lineno)
    parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = {leaf_name(part) for part in parts}
    return Clause(
        frozenset(name for name in names if name is not None),
        None not in names,
        handler.lineno,
    )
