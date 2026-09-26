"""Phase timeline: the component's journey as chips (design pass).

The old timeline was a flat `engineer pass -> verify pass` string -
every word the same weight, the arrows louder than the verdicts. Now
each completed phase is a chip on a panel background with a colored
verdict glyph and a dim duration; the phase currently in flight is the
one amber chip. The eye reads state, not prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.reducer import ComponentState


def _no_phases(comp: ComponentState) -> str:
    """A carried component will get no phases in this run (#433 F10)."""
    carried = comp.carried and comp.status != "pending"
    return "no phases in this run" if carried else "no phases yet"


def _attempt_label(text: Text, attempt: object) -> None:
    """``attempt 2`` before the first chip of each attempt (#433: a strip
    of engineer, verify, engineer, verify said nothing about which try
    each chip belonged to)."""
    if text.cell_len:
        text.append("  ")
    text.append(f"attempt {attempt} ", style=f"bold {theme.MUTED}")


def _chip(entry: dict[str, object]) -> Text:
    passed = bool(entry.get("passed"))
    glyph = "✓" if passed else "✗"
    color = theme.SUCCESS if passed else theme.ERROR
    chip = Text()
    chip.append(f" {entry.get('phase', '?')} ", style="bold")
    chip.append(glyph, style=f"bold {color}")
    duration = entry.get("duration_seconds") or 0.0
    if isinstance(duration, (int, float)) and duration:
        chip.append(f" {duration:.0f}s", style=theme.MUTED)
    chip.append(" ")
    chip.stylize(f"on {theme.PANEL}")
    return chip


def _current_chip(comp: ComponentState) -> Text | None:
    """The amber chip for the phase in flight, or None."""
    current = comp.phase
    finished = any(
        entry.get("phase") == current and entry.get("attempt") == comp.attempt
        for entry in comp.phase_history
    )
    if not current or comp.status not in ("running", "verifying") or finished:
        return None
    chip = Text()
    chip.append(f" {current} ", style=f"bold {theme.BACKGROUND}")
    chip.append("● ", style=theme.BACKGROUND)
    chip.stylize(f"on {theme.ACCENT}")
    return chip


def render_timeline(comp: ComponentState) -> Text:
    text = Text()
    if not comp.phase_history and not comp.phase:
        text.append(_no_phases(comp), style=theme.MUTED)
        return text
    attempts = {entry.get("attempt", 1) for entry in comp.phase_history}
    labelled = len(attempts) > 1 or comp.attempt > 1
    shown_attempt: object = None
    for entry in comp.phase_history:
        attempt = entry.get("attempt", 1)
        if labelled and attempt != shown_attempt:
            _attempt_label(text, attempt)
            shown_attempt = attempt
        text.append_text(_chip(entry))
        text.append("  ")
    current = _current_chip(comp)
    if current is not None:
        if labelled and comp.attempt != shown_attempt:
            _attempt_label(text, comp.attempt)
        text.append_text(current)
    return text


class PhaseTimeline(Static):
    def update_state(self, comp: ComponentState) -> None:
        self.update(render_timeline(comp))
