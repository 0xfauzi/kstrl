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


def _token_segment(state: RunState, marker: str) -> Text:
    text = Text()
    text.append(f"{format_tokens(state.total_tokens)}{marker}", style="bold")
    text.append(" tok", style=theme.MUTED)
    if state.max_total_tokens:
        pct = min(100, int(100 * state.total_tokens / state.max_total_tokens))
        text.append(" · ", style=theme.MUTED)
        text.append(
            f"{pct}%{marker} of {format_tokens(state.max_total_tokens)} token cap",
            style=_pressure_style(pct),
        )
    return text


def _cost_segment(state: RunState, marker: str) -> Text:
    """Spend beside its cap: the amount, not only a percentage (#433 F6)."""
    text = Text()
    text.append(f"${state.cost_usd:.2f}{marker}", style="bold")
    if state.max_cost_usd:
        pct = min(100, int(100 * state.cost_usd / state.max_cost_usd))
        text.append(" · ", style=theme.MUTED)
        text.append(
            f"{pct}%{marker} of ${state.max_cost_usd:.2f} cost cap",
            style=_pressure_style(pct),
        )
    else:
        text.append(" · no cost cap", style=theme.MUTED)
    return text


def _cost_short(state: RunState, marker: str) -> Text:
    """``$19.24 of $78.00 cap``: the spend and the cap amount, no percentage."""
    text = Text()
    text.append(f"${state.cost_usd:.2f}{marker}", style="bold")
    if state.max_cost_usd:
        pct = min(100, int(100 * state.cost_usd / state.max_cost_usd))
        text.append(f" of ${state.max_cost_usd:.2f} cap", style=_pressure_style(pct))
    else:
        text.append(" · no cap", style=theme.MUTED)
    return text


def _legend_segment(token_marker: str, cost_marker: str) -> Text:
    axes = [name for name, marked in (("tokens", token_marker), ("cost", cost_marker)) if marked]
    if not axes:
        return Text()
    # The legend names which axes are short - and stops there. The
    # uncovered magnitude is a TOKEN count (state.coverage_gaps carries
    # it, the activity feed prints it); converting it to dollars for a
    # tidier masthead would put an invented price on the surface the
    # operator watches.
    return Text(f"+ lower bound ({', '.join(axes)})", style=f"italic {theme.MUTED}")


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

    Segments drop in a fixed order when the line does not fit (#433):
    the lower-bound legend (the ``+`` markers stay on the figures), then
    the run id, then the token figure. The spend and its cap are never
    dropped, and no segment is cut part-way; when even they do not fit,
    they are said in the short form ``$19.24 of $78.00 cap``.
    """
    token_marker = "+" if state.tokens_are_lower_bound else ""
    cost_marker = "+" if state.cost_is_lower_bound else ""
    segments = [
        # (segment, separator before it, drop rank: highest drops first)
        (_token_segment(state, token_marker), "", 1),
        (_cost_segment(state, cost_marker), " · ", 0),
        (_legend_segment(token_marker, cost_marker), "  ", 3),
        (_run_segment(state), "  ", 2),
    ]
    shown = [(text, sep, rank) for text, sep, rank in segments if text.plain]
    while width is not None and len(shown) > 1 and _joined(shown).cell_len > width:
        shown.remove(max(shown, key=lambda item: item[2]))
    if width is not None and _joined(shown).cell_len > width:
        # Last resort: the same two amounts in fewer cells.
        return _cost_short(state, cost_marker)
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
