"""Run browser table for the home shell (TUI surface D1/D2).

One row per discovered run, newest first, diff-updated like the
component board (never clear()+rebuild). Ref-only columns (kind,
liveness, age) render immediately; the folded summary columns (comps,
tok, cost) render the honest dim dot until the D2 worker posts
SummariesReady - and keep R3.1's "+" lower-bound marker whenever the
run had unreported calls.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import DataTable

from kstrl.tui import theme
from kstrl.tui.widgets.cost_meter import format_tokens

if TYPE_CHECKING:
    from kstrl.tui.home_data import RunSummary
    from kstrl.tui.runs import RunRef

COLUMNS = ("", "run", "kind", "age", "comps", "tok", "cost")


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


def _status_cell(ref: RunRef, summary: RunSummary | None) -> Text:
    if ref.live:
        return Text("●", style=f"bold {theme.ACCENT}")
    if summary is not None and summary.outcome == "failed":
        return Text("✗", style=f"bold {theme.ERROR}")
    if ref.completed:
        return Text("✓", style=f"bold {theme.SUCCESS}")
    return Text(theme.EMPTY_CELL, style=theme.MUTED)


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
        Text(f"{format_tokens(summary.total_tokens)}{marker}",
             justify="right")
        if summary.total_tokens else _dot()
    )
    cost = (
        Text(f"${summary.cost_usd:.2f}{marker}", justify="right")
        if summary.cost_usd else _dot()
    )
    return comps, tok, cost


def _row_values(
    ref: RunRef, summary: RunSummary | None, now: float,
) -> tuple[Text | str, ...]:
    return (
        _status_cell(ref, summary),
        Text(theme.short_run_id(ref.run_id), style="bold"),
        Text(ref.kind or "run", style=theme.MUTED),
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
        seen = set()
        for ref in refs:
            seen.add(ref.run_id)
            values = _row_values(ref, summaries.get(ref.run_id), now)
            if ref.run_id in self.rows:
                for key, value in zip(
                    ("status", *COLUMNS[1:]), values, strict=True,
                ):
                    self.update_cell(ref.run_id, key, value)
            else:
                self.add_row(*values, key=ref.run_id)
        stale = [k for k in self.rows if str(k.value) not in seen]
        for row_key in stale:
            self.remove_row(row_key)
