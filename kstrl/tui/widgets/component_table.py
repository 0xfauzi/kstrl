"""Component board: one row per component, diff-updated (design pass).

Spike finding 3 keeps the render policy binding: rows are updated in
place (update_cell), never clear()+rebuilt per poll.

Design decisions from the critique:
- Status glyphs come from the theme (unicode, user decision) - the
  ASCII punctuation set was invisible at a glance.
- Numeric columns are right-aligned; empty data is a dim midpoint dot,
  never "-" (a dash column reads as broken data).
- A pending component says WHAT it is waiting on - the DAG is known;
  make the board explain itself.
- An open checkpoint marks the row with an accent diamond; the banner
  carries the call to action.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import DataTable

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.reducer import ComponentState, RunState

COLUMNS = ("", "component", "status", "phase", "try", "iter",
           "age", "tokens", "cost")

_NUMERIC_KEYS = {"try", "iter", "age", "tokens", "cost"}


def _age(ts: float, now: float) -> str:
    if ts <= 0:
        return theme.EMPTY_CELL
    seconds = max(0, int(now - ts))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _dim(value: str) -> Text:
    return Text(value, style=theme.MUTED, justify="right")


def _num(value: str) -> Text:
    return Text(value, justify="right")


def _phase_cell(comp: ComponentState, state: RunState) -> Text:
    if comp.status == "pending" and comp.deps:
        blockers = [
            dep for dep in comp.deps
            if state.components.get(dep) is None
            or state.components[dep].status != "completed"
        ]
        if blockers:
            return Text(
                f"waiting on {', '.join(blockers)}", style=theme.MUTED,
            )
    if not comp.phase:
        return Text(theme.EMPTY_CELL, style=theme.MUTED)
    glyph_style = theme.status_glyph(comp.status)[1]
    return Text(comp.phase, style=glyph_style if comp.status in (
        "running", "verifying",
    ) else "")


def _row_values(
    comp: ComponentState, state: RunState, now: float,
) -> tuple[Text | str, ...]:
    glyph, color = theme.status_glyph(comp.status)
    marker = "+" if comp.unreported_calls else ""
    name = Text(comp.component_id)
    if comp.checkpoint_open:
        name.append("  ◆", style=theme.ACCENT)
    age = _age(comp.last_event_ts, now)
    return (
        Text(glyph, style=f"bold {color}"),
        name,
        Text(comp.status, style=color),
        _phase_cell(comp, state),
        _num(str(comp.attempt)) if comp.attempt else _dim(theme.EMPTY_CELL),
        _num(str(comp.iteration)) if comp.iteration else _dim(theme.EMPTY_CELL),
        _dim(age) if age == theme.EMPTY_CELL else _num(age),
        _num(f"{comp.total_tokens:,}{marker}")
        if comp.total_tokens else _dim(theme.EMPTY_CELL),
        _num(f"${comp.cost_usd:.2f}{marker}")
        if comp.cost_usd else _dim(theme.EMPTY_CELL),
    )


class ComponentTable(DataTable[Text | str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.show_cursor = True
        for column in COLUMNS:
            self.add_column(column, key=column or "glyph")

    def update_state(self, state: RunState) -> None:
        now = time.time()
        order = state.plan_order or sorted(state.components)
        for comp_id in order:
            comp = state.components.get(comp_id)
            if comp is None:
                continue
            values = _row_values(comp, state, now)
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
                age = _age(comp.last_event_ts, now)
                self.update_cell(
                    comp_id, "age",
                    _dim(age) if age == theme.EMPTY_CELL else _num(age),
                )
