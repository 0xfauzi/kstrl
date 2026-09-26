"""Every DataTable cell update in the TUI widens its column (#433 F1).

Textual's ``DataTable.update_cell`` and ``update_cell_at`` default to
``update_width=False``: the column keeps the width it had. The home run
table added each row with a "·" placeholder while its summary was
folded off-thread, then updated the cells in place, so ``18.51M`` was
drawn in a column one cell wide plus padding and read ``18.``. Every
table in ``kstrl/tui`` had the same call.

Two layers, so the guard fails red rather than going blind:

1. Every call to ``update_cell`` or ``update_cell_at`` under
   ``kstrl/tui`` passes ``update_width=True`` as a literal keyword, and
   no other mention of either name exists: an alias, a partial or a
   getattr is flagged where it is written. Before this, an alias call
   that replaced a compliant call kept the census count and passed both
   layers.
2. A census of every place either name is mentioned at all: an
   attribute, a bare name, or a string constant (``getattr(t,
   "update_cell")``), so a new call site is a visible count change.

Disclosed blind spot: a name built at run time (``"update_" + "cell"``)
is seen by neither layer.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TUI = ROOT / "kstrl" / "tui"
NAMES = frozenset({"update_cell", "update_cell_at"})

#: path under kstrl/tui -> how many times the file mentions either name.
EXPECTED_MENTIONS: dict[str, int] = {
    "widgets/component_table.py": 2,
    "widgets/dag_table.py": 1,
    "widgets/run_table.py": 1,
}


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _widens(node: ast.Call) -> bool:
    return any(
        kw.arg == "update_width" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in node.keywords
    )


def _spells(node: ast.AST) -> bool:
    return (
        (isinstance(node, ast.Attribute) and node.attr in NAMES)
        or (isinstance(node, ast.Name) and node.id in NAMES)
        or (isinstance(node, ast.Constant) and node.value in NAMES)
    )


def narrow_updates(tree: ast.AST) -> list[int]:
    """Lines of an update call without update_width=True, and of any other
    mention of either name.

    Closed by construction: the only compliant way to spell either name
    is as the callee of a call that passes the keyword. An alias
    (``cell = t.update_cell``), a ``getattr`` or a ``partial`` is flagged
    where it is written, so a swap that keeps the census count cannot
    hide a narrow call behind a new name.
    """
    callees: set[int] = set()
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node) in NAMES:
            callees.add(id(node.func))
            if not _widens(node):
                lines.append(node.lineno)
    lines.extend(
        node.lineno for node in ast.walk(tree) if _spells(node) and id(node) not in callees
    )
    return sorted(lines)


def mentions(tree: ast.AST) -> int:
    """Every attribute, name or string constant that spells either name."""
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in NAMES:
            count += 1
        elif isinstance(node, ast.Name) and node.id in NAMES:
            count += 1
        elif isinstance(node, ast.Constant) and node.value in NAMES:
            count += 1
    return count


def _trees() -> dict[str, ast.Module]:
    return {
        str(path.relative_to(TUI)): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(TUI.rglob("*.py"))
    }


def test_every_update_in_the_tui_widens_its_column() -> None:
    offenders = {name: lines for name, tree in _trees().items() if (lines := narrow_updates(tree))}
    assert offenders == {}


def test_the_census_of_update_mentions_is_unchanged() -> None:
    census = {name: n for name, tree in _trees().items() if (n := mentions(tree))}
    assert census == EXPECTED_MENTIONS


class TestControls:
    """Each layer, run on source that must trip it."""

    def test_layer_one_flags_a_call_without_the_keyword(self) -> None:
        tree = ast.parse(
            "t.update_cell(r, c, v)\n"
            "t.update_cell(r, c, v, update_width=False)\n"
            "t.update_cell_at(xy, v)\n"
            "t.update_cell(r, c, v, update_width=True)\n"
        )
        assert narrow_updates(tree) == [1, 2, 3]

    def test_layer_one_flags_every_mention_that_is_not_a_compliant_callee(self) -> None:
        tree = ast.parse(
            "f = t.update_cell\n"
            "g = getattr(t, 'update_cell_at')\n"
            "from functools import partial\n"
            "h = partial(DataTable.update_cell, t)\n"
        )
        assert narrow_updates(tree) == [1, 2, 4]
        assert mentions(tree) == 3

    def test_an_alias_that_keeps_the_census_count_is_flagged(self) -> None:
        """A compliant call swapped for an alias call leaves one mention."""
        tree = ast.parse("cell = self.update_cell\ncell(r, c, v)\n")
        assert mentions(tree) == 1
        assert narrow_updates(tree) == [1]
