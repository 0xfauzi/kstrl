"""Masthead: brand, project, state chip, elapsed (design pass).

Hierarchy fix from the critique: the old header gave the widest, most
prominent slot to the full run id - the least useful element. Now the
eye lands on brand -> project -> state; the run id is a short dim
suffix on the meter side (theme.short_run_id).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.reducer import RunState


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render_header(state: RunState) -> Text:
    text = Text()
    text.append(" ◍ kstrl ", style=f"bold {theme.BACKGROUND} on {theme.ACCENT}")
    text.append("  ")
    text.append(state.project or "(no project)", style="bold")
    if state.kind != "factory":
        # Non-factory kinds name themselves; the board is otherwise
        # identical, and a factory run stays visually unchanged.
        text.append(f"  {state.kind}", style=f"bold {theme.STEEL}")
    text.append("  ")
    if state.finished:
        text.append("✓ finished", style=f"bold {theme.SUCCESS}")
        elapsed = max(0.0, state.last_event_ts - state.started_ts)
    else:
        text.append("● in flight", style=f"bold {theme.ACCENT}")
        elapsed = (time.time() - state.started_ts) if state.started_ts else 0.0
    text.append(f"  {_format_elapsed(elapsed)}", style=theme.MUTED)
    return text


class RunHeader(Static):
    """One-line run summary; re-rendered on StateChanged and the 1s
    age ticker (label-only updates - no layout churn)."""

    def update_state(self, state: RunState) -> None:
        self.update(render_header(state))
