"""Run browser table for the home shell (TUI surface D1/D2).

One row per discovered run, newest first. Stable polls update cells in
place; structural changes rebuild the row order while retaining the
selected run. Ref-only columns (kind, liveness, age) render immediately;
the folded summary columns (state, comps, tok, cost) render the honest dim
dot until the D2 worker posts SummariesReady, and keep R3.1's "+"
lower-bound marker whenever the run had unreported calls.

#433: the state column says a word (tui.run_status) instead of leaving a
glyph to carry it, and every cell update widens its column. A cell that
starts as the one-character dot and later holds ``18.51M`` kept the
dot's width, so the table showed ``18.`` and ``$19.`` (F1).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import DataTable

from kstrl.tui import theme
from kstrl.tui.run_status import RUN_STATE_STYLE, RUNNING
from kstrl.tui.widgets.cost_meter import format_tokens

if TYPE_CHECKING:
    from kstrl.tui.home_data import RunSummary
    from kstrl.tui.runs import RunRef

COLUMNS = ("", "run", "kind", "state", "age", "comps", "tok", "cost", "note")
#: Below COMPACT_BELOW columns: cost and component counts go first (advice-r1).
COMPACT_COLUMNS = ("", "run", "kind", "state", "age", "note")
COMPACT_BELOW = 110


def _age(mtime: float, now: float) -> str:
    seconds = max(0, int(now - mtime))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d"


def _dot() -> Text:
    return Text(theme.EMPTY_CELL, style=theme.MUTED, justify="right")


def _state_of(ref: RunRef, summary: RunSummary | None) -> str:
    """The run's state word, or "" while its summary is still folding."""
    if ref.live:
        return RUNNING
    return summary.state if summary is not None else ""


def _state_cells(ref: RunRef, summary: RunSummary | None) -> tuple[Text, Text]:
    word = _state_of(ref, summary)
    if not word:
        return Text(theme.EMPTY_CELL, style=theme.MUTED), Text(theme.EMPTY_CELL, style=theme.MUTED)
    glyph, color = RUN_STATE_STYLE[word]
    return Text(glyph, style=f"bold {color}"), Text(word, style=color)


def _summary_cells(summary: RunSummary | None) -> tuple[Text, Text, Text]:
    if summary is None:
        return _dot(), _dot(), _dot()
    comps = Text(
        f"{summary.components_done}/{summary.components_total}",
        justify="right",
        style=theme.ERROR if summary.components_failed else "",
    )
    if summary.components_failed:
        # A word with the count: "0/2 1✗" was a glyph an operator had to
        # decode (#433 round 1).
        comps.append(f", {summary.components_failed} failed", style=theme.ERROR)
    marker = "+" if summary.tokens_lower_bound else ""
    tok = (
        Text(f"{format_tokens(summary.total_tokens)}{marker}", justify="right")
        if summary.total_tokens
        else _dot()
    )
    cost = Text(f"${summary.cost_usd:.2f}{marker}", justify="right") if summary.cost_usd else _dot()
    return comps, tok, cost


def _row_values(
    ref: RunRef,
    summary: RunSummary | None,
    now: float,
    note: str = "",
) -> dict[str, Text | str]:
    glyph, word = _state_cells(ref, summary)
    comps, tok, cost = _summary_cells(summary)
    return {
        "status": glyph,
        "run": Text(theme.short_run_id(ref.run_id), style="bold"),
        "kind": Text(ref.kind or "run", style=theme.MUTED),
        "state": word,
        "age": Text(_age(ref.mtime, now), style=theme.MUTED, justify="right"),
        "comps": comps,
        "tok": tok,
        "cost": cost,
        "note": Text(note, style=theme.MUTED),
    }


def _key(column: str) -> str:
    return column or "status"


class RunTable(DataTable[Text | str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False
        self._columns_shown: tuple[str, ...] = ()
        self._set_columns(COLUMNS)

    def _set_columns(self, columns: tuple[str, ...]) -> None:
        if columns == self._columns_shown:
            return
        self.clear(columns=True)
        for column in columns:
            self.add_column(column, key=_key(column))
        self._columns_shown = columns

    def update_runs(
        self,
        refs: list[RunRef],
        summaries: dict[str, RunSummary] | None = None,
        notes: dict[str, str] | None = None,
    ) -> None:
        now = time.time()
        summaries = summaries or {}
        notes = notes or {}
        columns = self._columns_for_width()
        selected_before = self._selected_run()
        self._set_columns(columns)
        desired = [ref.run_id for ref in refs]
        current = [str(key.value) for key in self.rows]
        order_changed = current != desired
        if order_changed:
            self.clear()
        for ref in refs:
            cells = _row_values(ref, summaries.get(ref.run_id), now, notes.get(ref.run_id, ""))
            self._put_row(ref.run_id, [cells[_key(column)] for column in columns], columns)
        if order_changed and selected_before in desired:
            self.move_cursor(row=desired.index(selected_before))

    def _put_row(self, run_id: str, values: list[Text | str], columns: tuple[str, ...]) -> None:
        """Update the row in place (every cell widening its column), or add it."""
        if run_id not in self.rows:
            self.add_row(*values, key=run_id)
            return
        for column, value in zip(columns, values, strict=True):
            self.update_cell(run_id, _key(column), value, update_width=True)

    def _columns_for_width(self) -> tuple[str, ...]:
        width = self.app.size.width if self.is_attached else 120
        return COMPACT_COLUMNS if width < COMPACT_BELOW else COLUMNS

    def _selected_run(self) -> str | None:
        current = [str(key.value) for key in self.rows]
        return current[self.cursor_row] if 0 <= self.cursor_row < len(current) else None
