"""Cost meter: the R3.1 rollup with honest lower-bound semantics.

The "+" marker is load-bearing: token/cost figures are CLI
self-reports, and whenever unreported_calls > 0 every total is a
LOWER BOUND (H4: totals are only as honest as their coverage). The
meter must never turn an honest number into a false one.

Design pass: compact grammar (`12.4k+ tok · $1.87+ · 40% of cap`),
cap pressure colored only when it matters (>=70%), the short run id
as a dim suffix so the masthead stays about the work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.reducer import RunState


def format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.2f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def render_cost_meter(state: RunState) -> Text:
    text = Text()
    marker = "+" if state.unreported_calls else ""
    text.append(
        f"{format_tokens(state.total_tokens)}{marker}", style="bold",
    )
    text.append(" tok", style=theme.MUTED)
    text.append(" · ", style=theme.MUTED)
    text.append(f"${state.cost_usd:.2f}{marker}", style="bold")
    if state.max_total_tokens:
        pct = min(
            100, int(100 * state.total_tokens / state.max_total_tokens),
        )
        style = (
            f"bold {theme.ERROR}" if pct >= 90
            else f"bold {theme.WARNING}" if pct >= 70
            else theme.MUTED
        )
        text.append(" · ", style=theme.MUTED)
        text.append(f"{pct}% of cap", style=style)
    if state.unreported_calls:
        text.append("  + lower bound", style=f"italic {theme.MUTED}")
    if state.run_id:
        text.append("  run ", style=theme.MUTED)
        text.append(theme.short_run_id(state.run_id), style=theme.MUTED)
    return text


class CostMeter(Static):
    def update_state(self, state: RunState) -> None:
        self.update(render_cost_meter(state))
