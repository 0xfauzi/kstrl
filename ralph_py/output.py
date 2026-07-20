"""Console assembly: the event-native replacement for get_ui().

Chunk 7 of the TUI rewrite. ``build_console`` replicates ``get_ui``'s
mode/tty resolution (including the vestigial "gum" alias and
GUM_FORCE) to pick the concrete UI, then wires:

    call sites -> EventBridgeUI -> EventBus -> CallbackSink
                                       |            |
                                       |            v
                                       |     UIBackedRenderer -> PlainUI/RichUI
                                       v
                     (run_factory attaches file sinks: events.jsonl + v1)

The CallbackSink is same-thread and synchronous, so output ordering is
exactly what the imperative call sites produce today - CliRunner
captures and shell pipelines see identical bytes (proven by the
round-trip test). Interactive prompts bypass events entirely: the
bridge delegates choose/can_prompt to the concrete UI.
"""

from __future__ import annotations

from dataclasses import dataclass

from ralph_py.events import CallbackSink, EventBus
from ralph_py.render import UIBackedRenderer
from ralph_py.ui import get_ui
from ralph_py.ui.base import UI
from ralph_py.ui.bridge import EventBridgeUI


@dataclass
class Console:
    """One command's output assembly."""

    bus: EventBus
    ui: EventBridgeUI  # what call sites use (satisfies the UI protocol)
    renderer: UIBackedRenderer
    prompter: UI  # the concrete PlainUI/RichUI underneath


def build_console(
    mode: str = "auto",
    no_color: bool = False,
    ascii_only: bool = False,
    force_rich: bool = False,
) -> Console:
    """Assemble the event-native console for one CLI command."""
    concrete = get_ui(
        mode, no_color=no_color, ascii_only=ascii_only, force_rich=force_rich,
    )
    renderer = UIBackedRenderer(concrete)
    bus = EventBus(CallbackSink(renderer.handle))
    ui = EventBridgeUI(bus, prompter=concrete)
    return Console(bus=bus, ui=ui, renderer=renderer, prompter=concrete)
