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

COLUMNS = ("", "run", "kind", "state", "age", "comps", "tok", "cost")


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
        comps.append(f" {summary.components_failed}✗", style=theme.ERROR)
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
) -> tuple[Text | str, ...]:
    glyph, word = _state_cells(ref, summary)
    return (
        glyph,
        Text(theme.short_run_id(ref.run_id), style="bold"),
        Text(ref.kind or "run", style=theme.MUTED),
        word,
        Text(_age(ref.mtime, now), style=theme.MUTED, justify="right"),
        *_summary_cells(summary),
    )


class RunTable(DataTable[Text | str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False
        for column in COLUMNS:
            self.add_column(column, key=column or "status")

    def update_runs(
        self,
        refs: list[RunRef],
        summaries: dict[str, RunSummary] | None = None,
    ) -> None:
        now = time.time()
        summaries = summaries or {}
        desired = [ref.run_id for ref in refs]
        current = [str(key.value) for key in self.rows]
        selected = current[self.cursor_row] if 0 <= self.cursor_row < len(current) else None
        order_changed = current != desired
        if order_changed:
            self.clear()
        for ref in refs:
            values = _row_values(ref, summaries.get(ref.run_id), now)
            if ref.run_id in self.rows:
                for key, value in zip(
                    ("status", *COLUMNS[1:]),
                    values,
                    strict=True,
                ):
                    self.update_cell(ref.run_id, key, value, update_width=True)
            else:
                self.add_row(*values, key=ref.run_id)
        if order_changed and selected in desired:
            self.move_cursor(row=desired.index(selected))
