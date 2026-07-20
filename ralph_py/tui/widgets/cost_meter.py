"""Cost meter: the R3.1 rollup with honest lower-bound semantics.

The "+" marker is load-bearing: token/cost figures are CLI
self-reports, and whenever unreported_calls > 0 every total is a
LOWER BOUND (H4: totals are only as honest as their coverage). The
meter must never turn an honest number into a false one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from ralph_py.reducer import RunState


def _format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.2f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def render_cost_meter(state: RunState) -> Text:
    text = Text()
    marker = "+" if state.unreported_calls else ""
    text.append("tokens ", style="dim")
    text.append(f"{_format_tokens(state.total_tokens)}{marker}", style="bold")
    if state.max_total_tokens:
        pct = min(
            100, int(100 * state.total_tokens / state.max_total_tokens),
        )
        style = "bold red" if pct >= 90 else (
            "bold yellow" if pct >= 70 else "dim"
        )
        text.append(
            f" / {_format_tokens(state.max_total_tokens)} ({pct}%)",
            style=style,
        )
    text.append("   cost ", style="dim")
    text.append(f"${state.cost_usd:.2f}{marker}", style="bold")
    if state.unreported_calls:
        text.append(
            f"   [{state.unreported_calls} call(s) unreported: lower bound]",
            style="italic dim",
        )
    return text


class CostMeter(Static):
    def update_state(self, state: RunState) -> None:
        self.update(render_cost_meter(state))
