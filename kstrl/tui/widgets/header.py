"""Masthead: brand, project, state chip, elapsed (design pass).

Hierarchy fix from the critique: the old header gave the widest, most
prominent slot to the full run id - the least useful element. Now the
eye lands on brand -> project -> state; the run id is a short dim
suffix on the meter side (theme.short_run_id).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

from kstrl.tui import theme
from kstrl.tui.run_status import (
    RUN_STATE_STYLE,
    RUNNING,
    UNKNOWN,
    age_phrase,
    finished_word,
)
from kstrl.tui.runs import run_is_live

if TYPE_CHECKING:
    from kstrl.reducer import RunState


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def app_project(app: object) -> str | None:
    """The directory the TUI was opened on, which names the project (#433 F3).

    A run records the manifest's ``projectName``, and a daemon names each
    run it starts ``queue-<id>``; the masthead showed that queue name as
    the project.
    """
    root = getattr(app, "root_dir", None)
    return root.resolve().name if isinstance(root, Path) else None


def app_live(app: object) -> bool | None:
    """Whether the app's run is live; None when the app has no run.

    A run this app launched is live until it finishes. One it observes
    uses the home table's rule (``runs.run_is_live``).
    """
    run = getattr(app, "run_context", None)
    root = getattr(app, "root_dir", None)
    if run is None or not isinstance(root, Path):
        return None
    handle = getattr(run, "handle", None)
    if handle is not None and not handle.done():
        return True
    return run_is_live(run.run_dir, root)


def _state_chip(state: RunState, live: bool | None) -> tuple[str, str, float]:
    """(word, style, elapsed seconds) for the header's state."""
    if state.finished:
        word = finished_word(state)
        glyph, color = RUN_STATE_STYLE[word]
        return f"{glyph} {word}", f"bold {color}", state.last_event_ts - state.started_ts
    if live is False:
        glyph, color = RUN_STATE_STYLE[UNKNOWN]
        return f"{glyph} {UNKNOWN}", f"bold {color}", state.last_event_ts - state.started_ts
    glyph, color = RUN_STATE_STYLE[RUNNING]
    elapsed = (time.time() - state.started_ts) if state.started_ts else 0.0
    return f"{glyph} {RUNNING}", f"bold {color}", elapsed


def render_header(
    state: RunState,
    project: str | None = None,
    *,
    live: bool | None = None,
    compact: bool = False,
    serve_note: str = "",
) -> Text:
    """Brand, project, the run's state word, elapsed, last event age.

    ``live`` False turns an unfinished run's word from running to
    unknown (#433 F4). ``compact`` leaves out the last-event age, which
    the board's time column also shows per component, so the spend and
    its cap still fit at 80 columns.
    """
    text = Text()
    text.append(" ◍ kstrl ", style=f"bold {theme.BACKGROUND} on {theme.ACCENT}")
    text.append("  ")
    text.append(project or state.project or "(no project)", style="bold")
    if state.kind != "factory":
        # Non-factory kinds name themselves; the board is otherwise
        # identical, and a factory run stays visually unchanged.
        text.append(f"  {state.kind}", style=f"bold {theme.STEEL}")
    text.append("  ")
    chip, style, elapsed = _state_chip(state, live)
    text.append(chip, style=style)
    if not (compact and serve_note):
        # At 80 columns the ks serve note (Q1) outranks the clock.
        text.append(f"  {_format_elapsed(max(0.0, elapsed))}", style=theme.MUTED)
    if not state.finished and state.last_event_ts and not compact:
        # Q6: a run in flight says how long ago it last wrote (#433).
        quiet = age_phrase(time.time() - state.last_event_ts)
        text.append(f"  last event {quiet} ago", style=theme.MUTED)
    if serve_note:
        # M1 (#433): work ks serve runs or holds that this TUI did not start.
        text.append(f"  {serve_note}", style=f"bold {theme.STEEL}")
    return text


#: Cells the topbar spends on padding: the header's 0 1 and the meter's 0 2.
TOPBAR_PADDING = 6


#: Below this terminal width the header is rendered ``compact``.
COMPACT_TOPBAR_BELOW = 100


def topbar_header(state: RunState, app: object, total_width: int, serve_note: str = "") -> Text:
    """The header for a run screen's topbar at ``total_width`` columns."""
    return render_header(
        state,
        app_project(app),
        live=None if state.finished else app_live(app),
        compact=total_width < COMPACT_TOPBAR_BELOW,
        serve_note=serve_note,
    )


def meter_width(header: Text, total_width: int) -> int:
    """What the cost meter may use once the header has what it needs.

    The header (project, state, elapsed) is what an operator reads first,
    so it is never squeezed; the meter drops whole segments to fit the
    rest (#433: at 80 columns the meter used to push the run's own state
    off the line).
    """
    return max(0, total_width - header.cell_len - TOPBAR_PADDING)


class RunHeader(Static):
    """One-line run summary; re-rendered on StateChanged and the 1s
    age ticker (label-only updates - no layout churn)."""

    def update_state(self, state: RunState) -> None:
        self.update(topbar_header(state, self.app, self.app.size.width))
