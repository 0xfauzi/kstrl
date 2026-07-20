"""Run header: project, run id, state, elapsed (stage 3 PR D)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from ralph_py.reducer import RunState


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render_header(state: RunState) -> Text:
    text = Text()
    text.append(" ralph ", style="bold reverse")
    text.append("  ")
    text.append(state.project or "(no project)", style="bold")
    if state.run_id:
        text.append("  ")
        text.append(state.run_id, style="dim")
    text.append("  ")
    if state.finished:
        text.append("finished", style="bold green")
        elapsed = state.last_event_ts - state.started_ts
    else:
        text.append("in flight", style="bold yellow")
        elapsed = (time.time() - state.started_ts) if state.started_ts else 0.0
    text.append(f"  {_format_elapsed(elapsed)}", style="dim")
    return text


class RunHeader(Static):
    """One-line run summary; re-rendered on StateChanged and the 1s
    age ticker (label-only updates - no layout churn)."""

    def update_state(self, state: RunState) -> None:
        self.update(render_header(state))
