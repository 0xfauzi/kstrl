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


def render_timeline(comp: ComponentState) -> Text:
    text = Text()
    if not comp.phase_history and not comp.phase:
        text.append("no phases yet", style=theme.MUTED)
        return text
    for entry in comp.phase_history:
        passed = bool(entry.get("passed"))
        glyph = "✓" if passed else "✗"
        color = theme.SUCCESS if passed else theme.ERROR
        chip = Text()
        chip.append(f" {entry.get('phase', '?')} ", style="bold")
        chip.append(glyph, style=f"bold {color}")
        duration = entry.get("duration_seconds") or 0.0
        if duration:
            chip.append(f" {duration:.0f}s", style=theme.MUTED)
        chip.append(" ")
        chip.stylize(f"on {theme.PANEL}")
        text.append_text(chip)
        text.append("  ")
    current = comp.phase
    current_finished = any(
        entry.get("phase") == current
        and entry.get("attempt") == comp.attempt
        for entry in comp.phase_history
    )
    if current and comp.status in ("running", "verifying") and (
        not current_finished
    ):
        chip = Text()
        chip.append(f" {current} ", style=f"bold {theme.BACKGROUND}")
        chip.append("● ", style=theme.BACKGROUND)
        chip.stylize(f"on {theme.ACCENT}")
        text.append_text(chip)
    return text


class PhaseTimeline(Static):
    def update_state(self, comp: ComponentState) -> None:
        self.update(render_timeline(comp))
