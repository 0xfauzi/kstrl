"""Context bar: the shared masthead for non-run screens (design 2).

HARD-WON: never store ``self._context`` on a widget - it shadows
MessagePump._context(), the internal Textual uses to process every
message, and the widget dies SILENTLY (children never mount, pilots
hang). Attribute names here are _bar_-prefixed for that reason.

The run screens carry the run masthead (RunHeader + CostMeter on a
surface band). Every OTHER screen was naked - config, evolve, forms,
retry, triage floated in the void with no answer to "where am I".
The context bar is the same surface band grammar: brand chip (amber
reverse) > screen name (bold) > muted context left; right slot for
screen-specific facts.

Both slots buffer: a screen's on_mount may call set_right/set_context
BEFORE this widget's own compose has mounted its children (the same
mount-ordering trap the activity feed hit in the first design pass),
so setters store the value and compose/on_mount render whatever is
pending - never a query into unmounted children.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from kstrl.tui import theme


def brand_chip() -> Text:
    # Built by append, not Text(str, style=...): a constructor style
    # becomes the BASE style and bleeds into every later append.
    chip = Text()
    chip.append(
        " ◍ kstrl ", style=f"bold {theme.BACKGROUND} on {theme.ACCENT}",
    )
    return chip


class ContextBar(Horizontal):
    DEFAULT_CLASSES = "context-bar"

    def __init__(
        self, screen_name: str, context: str = "",
        right: Text | str = "",
    ) -> None:
        super().__init__()
        self._screen_name = screen_name
        self._bar_context = context
        self._right = right

    def _left_text(self) -> Text:
        left = brand_chip()
        left.append("  ")
        left.append(self._screen_name, style="bold")
        if self._bar_context:
            left.append(f"  {self._bar_context}", style=theme.MUTED)
        return left

    def compose(self) -> ComposeResult:
        yield Static(self._left_text(), classes="context-left")
        yield Static(self._right, classes="context-right")

    def set_context(self, context: str) -> None:
        self._bar_context = context
        left = next(iter(self.query(".context-left")), None)
        if left is not None:
            left.update(self._left_text())  # type: ignore[attr-defined]

    def set_right(self, right: Text | str) -> None:
        self._right = right
        target = next(iter(self.query(".context-right")), None)
        if target is not None:
            target.update(right)  # type: ignore[attr-defined]
