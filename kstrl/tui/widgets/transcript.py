"""Transcript tail pane (PR E).

Spike finding 3 is binding here: ONE component's transcript, bounded
buffer, follow-toggleable. The pane pulls via feed_lines from the
app's poll (only while its screen is on top - the app gates that), so
a backgrounded detail screen costs nothing.

#433: lines wrap. A transcript line is usually a command or a model's
sentence, and cutting either at the right edge left no way to read the
rest; ``lines_written`` lets the screen tell a saved transcript from a
component that wrote none.
"""

from __future__ import annotations

from kstrl.tui.widgets.reflow_log import ReflowLog

MAX_BUFFER_LINES = 1000


class TranscriptTail(ReflowLog):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            max_lines=MAX_BUFFER_LINES,
            highlight=False,
            **kwargs,
        )
        self.follow = True
        self.lines_written = 0

    def feed_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        self.auto_scroll = self.follow
        for line in lines:
            self.write_source(line)
        self.lines_written += len(lines)

    def toggle_follow(self) -> bool:
        self.follow = not self.follow
        self.auto_scroll = self.follow
        return self.follow
