"""Cost meter: the R3.1 rollup with honest lower-bound semantics.

The "≥" marker is load-bearing: token/cost figures are CLI
self-reports, and a total is a LOWER BOUND whenever some call did not
report the figure it is denominated in (H4: totals are only as honest
as their coverage). The meter must never turn an honest number into a
false one. #433 G7: the marker was a trailing "+" explained by a legend
that was the first segment dropped when the line was short, so
``$4.50+`` reached the operator unexplained; ``≥$4.50`` says it itself,
on every TUI surface (``at_least``), and the legend is gone.

Every percentage of a cap uses ``cap_percent``'s one rounding rule.

R8: one marker per AXIS. Coverage is per axis because adapters are -
codex reports a token total and no cost, claude can report a cost with
no usage dict - and the run this concept came from had full token
coverage and partial cost coverage at the same time.

Design pass: compact grammar (`12.4k+ tok · $1.87+ · 40% of cap`),
cap pressure colored only when it matters (>=70%), the short run id
as a dim suffix so the masthead stays about the work.
"""

from __future__ import annotations

import math
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


def at_least(figure: str, lower_bound: bool) -> str:
    """``≥$4.50`` when ``figure`` is a lower bound, else ``figure`` (#433 G7)."""
    return f"≥{figure}" if lower_bound else figure


def cap_percent(spent: float, cap: float) -> int:
    """The percentage of ``cap`` spent: the one rule every cost surface uses.

    Rounded UP to a whole percent, so spend is not shown as less than it
    is against a cap: $19.24 of $78.00 is 24.67%, shown 25%, where the old
    truncation showed 24% (#433 advice 2.3). The percentage is first
    rounded to six decimal places, so float noise does not add a point:
    $0.07 of $7.00 computes as 1.0000000000000002 and is shown 1%, not 2%.
    That rounding is the one case this rule shows less than was spent: a
    percentage within 0.0000005 of a point above a whole percent is shown
    as that whole percent. 0% only when nothing is spent. Not capped at
    100: a run past its cap shows how far past. 0 when there is no cap.
    """
    if cap <= 0:
        return 0
    return math.ceil(round(100 * spent / cap, 6))


def cost_against_cap(spent: float, cap: float, bound: bool) -> Text:
    """``$19.24 of $78.00 cap 25%`` or ``$19.24 · no cap``: a run's spend
    with its cap and ``cap_percent`` (#433 advice 2.3), in few cells."""
    text = Text(at_least(f"${spent:.2f}", bound))
    if cap > 0:
        pct = cap_percent(spent, cap)
        text.append(f" of ${cap:.2f} cap {at_least(f'{pct}%', bound)}", style=_pressure_style(pct))
    else:
        text.append(" · no cap", style=theme.MUTED)
    return text


def _pressure_style(pct: int) -> str:
    return (
        f"bold {theme.ERROR}"
        if pct >= 90
        else f"bold {theme.WARNING}"
        if pct >= 70
        else theme.MUTED
    )


def _token_segment(state: RunState, bound: bool) -> Text:
    text = Text()
    text.append(at_least(format_tokens(state.total_tokens), bound), style="bold")
    text.append(" tok", style=theme.MUTED)
    if state.max_total_tokens:
        pct = cap_percent(state.total_tokens, state.max_total_tokens)
        text.append(" · ", style=theme.MUTED)
        text.append(
            f"{at_least(f'{pct}%', bound)} of {format_tokens(state.max_total_tokens)} token cap",
            style=_pressure_style(pct),
        )
    return text


def _cost_segment(state: RunState, bound: bool) -> Text:
    """Spend beside its cap: the amount, not only a percentage (#433 F6)."""
    text = Text()
    text.append(at_least(f"${state.cost_usd:.2f}", bound), style="bold")
    if state.max_cost_usd:
        pct = cap_percent(state.cost_usd, state.max_cost_usd)
        text.append(" · ", style=theme.MUTED)
        text.append(
            f"{at_least(f'{pct}%', bound)} of ${state.max_cost_usd:.2f} cost cap",
            style=_pressure_style(pct),
        )
    else:
        text.append(" · no cost cap", style=theme.MUTED)
    return text


def _cost_short(state: RunState, bound: bool) -> Text:
    """``$19.24 of $78.00 cap 25%``: the spend, the cap and the percentage."""
    text = cost_against_cap(state.cost_usd, state.max_cost_usd, bound)
    text.stylize("bold", 0, len(at_least(f"${state.cost_usd:.2f}", bound)))
    return text


def _run_segment(state: RunState) -> Text:
    if not state.run_id:
        return Text()
    text = Text("run ", style=theme.MUTED)
    text.append(theme.short_run_id(state.run_id), style=theme.MUTED)
    return text


def render_cost_meter(state: RunState, width: int | None = None) -> Text:
    """The meter, fitted to ``width`` cells by dropping whole segments.

    Per axis, not per run (R8 review finding 1): the old single marker
    keyed on unreported_calls, which counts calls that reported NOTHING,
    so a cost total covering one role rendered as exact. The percentage
    carries the marker too: it is what an operator reads as headroom.
    The uncovered magnitude is a TOKEN count (state.coverage_gaps carries
    it, the activity feed prints it) and is never priced here.

    Segments drop in a fixed order when the line does not fit (#433):
    the run id, then the token figure. The spend and its cap are never
    dropped, and no segment is cut part-way; when even they do not fit,
    they are said in the short form ``$19.24 of $78.00 cap 25%``.
    """
    segments = [
        # (segment, separator before it, drop rank: highest drops first)
        (_token_segment(state, state.tokens_are_lower_bound), "", 1),
        (_cost_segment(state, state.cost_is_lower_bound), " · ", 0),
        (_run_segment(state), "  ", 2),
    ]
    shown = [(text, sep, rank) for text, sep, rank in segments if text.plain]
    while width is not None and len(shown) > 1 and _joined(shown).cell_len > width:
        shown.remove(max(shown, key=lambda item: item[2]))
    if width is not None and _joined(shown).cell_len > width:
        # Last resort: the same two amounts in fewer cells.
        return _cost_short(state, state.cost_is_lower_bound)
    return _joined(shown)


def _joined(segments: list[tuple[Text, str, int]]) -> Text:
    text = Text()
    for index, (segment, sep, _rank) in enumerate(segments):
        if index:
            text.append(sep, style=theme.MUTED)
        text.append_text(segment)
    return text


class CostMeter(Static):
    def update_state(self, state: RunState, width: int | None = None) -> None:
        self.update(render_cost_meter(state, width))
