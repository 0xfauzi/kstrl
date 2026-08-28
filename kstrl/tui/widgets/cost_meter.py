"""Cost meter: the R3.1 rollup with honest lower-bound semantics.

The "+" marker is load-bearing: token/cost figures are CLI
self-reports, and a total is a LOWER BOUND whenever some call did not
report the figure it is denominated in (H4: totals are only as honest
as their coverage). The meter must never turn an honest number into a
false one.

R8: one marker per AXIS. Coverage is per axis because adapters are -
codex reports a token total and no cost, claude can report a cost with
no usage dict - and the run this concept came from had full token
coverage and partial cost coverage at the same time.

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


def _pressure_style(pct: int) -> str:
    return (
        f"bold {theme.ERROR}"
        if pct >= 90
        else f"bold {theme.WARNING}"
        if pct >= 70
        else theme.MUTED
    )


def render_cost_meter(state: RunState) -> Text:
    # Per axis, not per run (R8 review finding 1). The old single marker
    # keyed on unreported_calls, which counts calls that reported
    # NOTHING - so a cross-family reviewer reporting tokens and no cost
    # left it at 0 and this meter rendered a cost total covering one
    # role as exact, against a cap that bounded one role. The two axes
    # genuinely differ: on the measured run tokens were fully covered
    # while cost was not, so one shared marker is wrong either way.
    token_marker = "+" if state.tokens_are_lower_bound else ""
    cost_marker = "+" if state.cost_is_lower_bound else ""
    text = Text()
    text.append(
        f"{format_tokens(state.total_tokens)}{token_marker}",
        style="bold",
    )
    text.append(" tok", style=theme.MUTED)
    text.append(" · ", style=theme.MUTED)
    text.append(f"${state.cost_usd:.2f}{cost_marker}", style="bold")
    # Both ceilings can be configured, and they measure different things
    # (total_tokens counts cache reads at par), so each gets its own
    # pressure reading rather than one anonymous "% of cap".
    #
    # The percentage carries the marker too: it is what an operator
    # reads as headroom, and "50% of cost cap" on a total that counts
    # half the calls overstates the headroom by exactly the amount
    # nobody measured.
    if state.max_total_tokens:
        pct = min(
            100,
            int(100 * state.total_tokens / state.max_total_tokens),
        )
        text.append(" · ", style=theme.MUTED)
        text.append(
            f"{pct}%{token_marker} of token cap",
            style=_pressure_style(pct),
        )
    if state.max_cost_usd:
        cost_pct = min(100, int(100 * state.cost_usd / state.max_cost_usd))
        text.append(" · ", style=theme.MUTED)
        text.append(
            f"{cost_pct}%{cost_marker} of cost cap",
            style=_pressure_style(cost_pct),
        )
    axes = [
        name
        for name, marked in (
            ("tokens", token_marker),
            ("cost", cost_marker),
        )
        if marked
    ]
    if axes:
        # The legend names which axes are short - and stops there. The
        # uncovered magnitude is a TOKEN count (state.coverage_gaps
        # carries it, the activity feed prints it); converting it to
        # dollars for a tidier masthead would put an invented price on
        # the surface the operator watches.
        text.append(
            f"  + lower bound ({', '.join(axes)})",
            style=f"italic {theme.MUTED}",
        )
    if state.run_id:
        text.append("  run ", style=theme.MUTED)
        text.append(theme.short_run_id(state.run_id), style=theme.MUTED)
    return text


class CostMeter(Static):
    def update_state(self, state: RunState) -> None:
        self.update(render_cost_meter(state))
