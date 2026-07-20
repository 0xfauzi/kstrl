"""Phase timeline: the component's journey with verdicts (PR E)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from ralph_py.reducer import ComponentState


def render_timeline(comp: ComponentState) -> Text:
    text = Text()
    if not comp.phase_history and not comp.phase:
        text.append("no phases yet", style="dim")
        return text
    for i, entry in enumerate(comp.phase_history):
        if i:
            text.append("  ->  ", style="dim")
        verdict = "✓" if entry.get("passed") else "✗"
        style = "green" if entry.get("passed") else "red"
        duration = entry.get("duration_seconds") or 0.0
        text.append(f"{entry.get('phase', '?')} {verdict}", style=style)
        if duration:
            text.append(f" {duration:.0f}s", style="dim")
    current = comp.phase
    finished_phases = {e.get("phase") for e in comp.phase_history}
    if current and comp.status in ("running", "verifying") and (
        current not in finished_phases
    ):
        if comp.phase_history:
            text.append("  ->  ", style="dim")
        text.append(f"{current} …", style="bold yellow")
    return text


class PhaseTimeline(Static):
    def update_state(self, comp: ComponentState) -> None:
        self.update(render_timeline(comp))
