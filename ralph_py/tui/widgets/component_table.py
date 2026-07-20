"""Component board: one row per component, diff-updated.

Spike finding 3 made the render policy binding: rows are updated in
place (update_cell), never clear()+rebuilt per poll - full rebuilds at
storm rates measurably starve keyboard input.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import DataTable

if TYPE_CHECKING:
    from ralph_py.reducer import ComponentState, RunState

STATUS_GLYPHS = {
    "pending": (".", "dim"),
    "running": (">", "yellow"),
    "verifying": ("~", "cyan"),
    "completed": ("+", "green"),
    "merge_pending": ("=", "magenta"),
    "failed": ("x", "red"),
    "skipped": ("-", "dim"),
}

COLUMNS = ("", "component", "status", "phase", "att", "iter",
           "age", "tokens", "cost")


def _age(ts: float, now: float) -> str:
    if ts <= 0:
        return "-"
    seconds = max(0, int(now - ts))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _row_values(comp: ComponentState, now: float) -> tuple[Text | str, ...]:
    glyph, style = STATUS_GLYPHS.get(comp.status, ("?", "dim"))
    marker = "+" if comp.unreported_calls else ""
    checkpoint = " !" if comp.checkpoint_open else ""
    return (
        Text(glyph, style=style),
        Text(f"{comp.component_id}{checkpoint}"),
        Text(comp.status, style=style),
        comp.phase or "-",
        str(comp.attempt or 1),
        str(comp.iteration or "-"),
        _age(comp.last_event_ts, now),
        f"{comp.total_tokens}{marker}" if comp.total_tokens else "-",
        f"${comp.cost_usd:.2f}{marker}" if comp.cost_usd else "-",
    )


class ComponentTable(DataTable[Text | str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        for column in COLUMNS:
            self.add_column(column, key=column or "glyph")

    def update_state(self, state: RunState) -> None:
        now = time.time()
        order = state.plan_order or sorted(state.components)
        for comp_id in order:
            comp = state.components.get(comp_id)
            if comp is None:
                continue
            values = _row_values(comp, now)
            if comp_id in self.rows:
                for key, value in zip(
                    ("glyph", *COLUMNS[1:]), values, strict=True,
                ):
                    self.update_cell(comp_id, key, value)
            else:
                self.add_row(*values, key=comp_id)

    def tick_ages(self, state: RunState) -> None:
        """1s label-only refresh of the age column."""
        now = time.time()
        for comp_id, comp in state.components.items():
            if comp_id in self.rows:
                self.update_cell(
                    comp_id, "age", _age(comp.last_event_ts, now),
                )
