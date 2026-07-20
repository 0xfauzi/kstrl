"""Renderers: project the event stream back onto a terminal surface.

Chunk 7 of the TUI rewrite. :class:`UIBackedRenderer` is the exact
inverse of :class:`~ralph_py.ui.bridge.EventBridgeUI`'s method-to-event
mapping, so a bridge -> bus -> renderer round trip produces terminal
bytes IDENTICAL to calling the concrete UI directly - the
no-regression proof for the ~390 imperative call sites.

Semantic events (phase brackets, PR lifecycle, usage, ...) render
NOTHING here by design: while the imperative Log narration coexists
with them, rendering both would double-print. The Textual dashboard is
the surface that renders semantic events.
"""

from __future__ import annotations

from typing import Protocol

from ralph_py.events import Event, Log
from ralph_py.ui.base import UI
from ralph_py.ui.plain import PlainUI


class Renderer(Protocol):
    """Consumes stamped events; renders (or ignores) each."""

    def handle(self, event: Event) -> None: ...


class UIBackedRenderer:
    """Renders Log events onto a concrete UI implementation."""

    def __init__(self, ui: UI) -> None:
        self.ui = ui

    def handle(self, event: Event) -> None:  # noqa: C901 - flat dispatch
        if not isinstance(event, Log):
            return  # semantic events: the dashboard's job, not ours
        ui = self.ui
        kind = event.kind
        if kind == "line":
            if event.severity == "ok":
                ui.ok(event.text)
            elif event.severity == "warn":
                ui.warn(event.text)
            elif event.severity == "error":
                ui.err(event.text)
            else:
                ui.info(event.text)
        elif kind == "kv":
            ui.kv(event.key, event.text)
        elif kind == "section":
            ui.section(event.text)
        elif kind == "subsection":
            ui.subsection(event.text)
        elif kind == "title":
            ui.title(event.text)
        elif kind == "hr":
            ui.hr()
        elif kind == "channel":
            ui.channel_header(event.key, event.text)
        elif kind == "stream":
            ui.stream_line(event.key, event.text)
        elif kind == "startup_art":
            ui.startup_art()
        else:
            # Forward-compat: an unknown Log kind still surfaces its text.
            ui.info(event.text)


def plain_renderer(
    no_color: bool = False, ascii_only: bool = False,
) -> UIBackedRenderer:
    """The CI/pipe/non-TTY surface: line output via PlainUI."""
    return UIBackedRenderer(PlainUI(no_color=no_color, ascii_only=ascii_only))
