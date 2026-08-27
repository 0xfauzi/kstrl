#!/usr/bin/env python3
"""Fail when production code imports or uses a mocking primitive.

A fake in a production path answers where the real component would refuse. That
is the rule. The interesting part is how it is checked.

WHY THIS IS NOT A pygrep HOOK
-----------------------------
The obvious spelling is a line regex over
`unittest\\.mock|MagicMock|AsyncMock|mock\\.patch|@patch\\b|monkeypatch\\.`, and
that is how this rule started life in another repo, whose config asserts that
requiring the CALL or the IMPORT means "prose does not trip it". Measured across
two other repos, that assertion does not hold: after excluding tests it still
reported six hits, and all six were docstrings and comments EXPLAINING mock
behaviour, e.g. a comment reading "coerces to 1 via MagicMock.__index__". A line
regex cannot tell code from prose, because by the time it runs, both are text.

So this parses instead. `ast` never sees a comment, and a docstring is a plain
string expression rather than a Name or an Import, so prose is invisible here by
construction rather than by a pattern that hopes to miss it.

Flags: importing unittest.mock in any spelling; referring to MagicMock,
AsyncMock, NonCallableMock, Mock or PropertyMock; calling mock.patch or
patch.object; the @patch decorator; and touching pytest's monkeypatch fixture.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MOCK_NAMES = frozenset(
    {
        "MagicMock",
        "AsyncMock",
        "NonCallableMock",
        "NonCallableMagicMock",
        "PropertyMock",
        "create_autospec",
        "mock_open",
        "seal",
    }
)
MOCK_MODULES = frozenset({"unittest.mock", "mock"})


def emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")


class Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in MOCK_MODULES:
                self.hits.append((node.lineno, f"imports {alias.name}"))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module in MOCK_MODULES:
            names = ", ".join(a.name for a in node.names)
            self.hits.append((node.lineno, f"imports {names} from {module}"))
        elif module == "unittest" and any(a.name == "mock" for a in node.names):
            self.hits.append((node.lineno, "imports mock from unittest"))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in MOCK_NAMES:
            self.hits.append((node.lineno, f"uses {node.id}"))
        elif node.id == "monkeypatch":
            self.hits.append((node.lineno, "uses pytest's monkeypatch fixture"))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in MOCK_NAMES:
            self.hits.append((node.lineno, f"uses {node.attr}"))
        elif (
            isinstance(node.value, ast.Name)
            and node.value.id in {"mock", "patch"}
            and node.attr in {"patch", "object", "dict", "multiple"}
        ):
            self.hits.append((node.lineno, f"calls {node.value.id}.{node.attr}"))
        self.generic_visit(node)


def check(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(errors="replace"), filename=str(path))
    except SyntaxError:
        # Not this hook's job to report; ruff and the compiler both will.
        return []
    finder = Finder()
    finder.visit(tree)
    seen: set[tuple[int, str]] = set()
    out: list[str] = []
    for lineno, what in finder.hits:
        if (lineno, what) in seen:
            continue
        seen.add((lineno, what))
        out.append(f"  {path}:{lineno}: {what}")
    return out


def main(argv: list[str]) -> int:
    problems: list[str] = []
    for name in argv:
        path = Path(name)
        if path.suffix == ".py" and path.is_file():
            problems.extend(check(path))
    if not problems:
        return 0
    emit("Mocking primitives in production code:")
    emit("")
    for p in sorted(problems):
        emit(p)
    emit("")
    emit("A fake here answers where the real component would refuse. Move it into")
    emit("the tests, or inject the real seam so the test can supply its own double.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
