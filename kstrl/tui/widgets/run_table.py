"""Run browser table for the home shell (TUI surface D1).

One row per discovered run, newest first. Stable polls update cells in
place; structural changes rebuild the row order while retaining the
selected run. D1 renders what RunRef alone knows - kind, liveness, age;
D2 joins the folded summaries (comps, tokens, cost) into the remaining
columns, which render the honest dim dot until then.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import DataTable

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.tui.runs import RunRef

COLUMNS = ("", "run", "kind", "age")


def _age(mtime: float, now: float) -> str:
    seconds = max(0, int(now - mtime))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d"


def _status_cell(ref: RunRef) -> Text:
    if ref.live:
        return Text("●", style=f"bold {theme.ACCENT}")
    if ref.completed:
        return Text("✓", style=f"bold {theme.SUCCESS}")
    return Text(theme.EMPTY_CELL, style=theme.MUTED)


def _row_values(ref: RunRef, now: float) -> tuple[Text | str, ...]:
    return (
        _status_cell(ref),
        Text(theme.short_run_id(ref.run_id), style="bold"),
        Text(ref.kind or "run", style=theme.MUTED),
        Text(_age(ref.mtime, now), style=theme.MUTED, justify="right"),
    )


class RunTable(DataTable[Text | str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False
        for column in COLUMNS:
            self.add_column(column, key=column or "status")

    def update_runs(self, refs: list[RunRef]) -> None:
        now = time.time()
        desired = [ref.run_id for ref in refs]
        current = [str(key.value) for key in self.rows]
        selected = (
            current[self.cursor_row]
            if 0 <= self.cursor_row < len(current) else None
        )
        order_changed = current != desired
        if order_changed:
            self.clear()
        for ref in refs:
            values = _row_values(ref, now)
            if ref.run_id in self.rows:
                for key, value in zip(
                    ("status", *COLUMNS[1:]), values, strict=True,
                ):
                    self.update_cell(ref.run_id, key, value)
            else:
                self.add_row(*values, key=ref.run_id)
        if order_changed and selected in desired:
            self.move_cursor(row=desired.index(selected))
