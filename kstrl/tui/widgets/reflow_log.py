"""A log that wraps its lines again when its width changes (#433 F11).

``RichLog`` wraps a line once, at the width the log has when the line is
written, and keeps those strips. Two things went wrong with that:

- It renders at least ``min_width`` (78) cells wide, so at 80 columns a
  wrapped line still ran past a pane narrower than 78 and the log
  scrolled sideways.
- Lines written before the first layout, or before the terminal was
  widened, stayed wrapped at the old width: a transcript opened at 120
  columns kept 80-cell lines.

``ReflowLog`` keeps what it was given (bounded like the log) and writes
it all again when the width it wrapped at changes.
"""

from __future__ import annotations

from collections import deque

from rich.console import RenderableType
from textual.events import Resize
from textual.widgets import RichLog


class ReflowLog(RichLog):
    def __init__(self, *, max_lines: int, **kwargs: object) -> None:
        super().__init__(
            max_lines=max_lines,
            wrap=True,
            min_width=1,
            **kwargs,  # type: ignore[arg-type]
        )
        self._sources: deque[RenderableType] = deque(maxlen=max_lines)
        #: The content width the current strips were wrapped at; 0 before
        #: the first layout.
        self.wrapped_at = 0

    def write_source(self, content: RenderableType) -> None:
        """Write ``content`` and keep it for a later rewrap."""
        self._sources.append(content)
        self.write(content)

    def on_resize(self, event: Resize) -> None:
        # RichLog's own on_resize also runs (Textual calls each class's
        # handler) and flushes the writes deferred before the first size.
        width = self.scrollable_content_region.width
        if not width or width == self.wrapped_at:
            return
        first = self.wrapped_at == 0
        self.wrapped_at = width
        if first:
            return
        self.clear()
        for content in self._sources:
            self.write(content, scroll_end=False)
        if self.auto_scroll:
            self.scroll_end(animate=False, immediate=False, x_axis=False)
