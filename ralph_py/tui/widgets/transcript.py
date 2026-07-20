"""Transcript tail pane (PR E).

Spike finding 3 is binding here: ONE component's transcript, bounded
buffer, follow-toggleable. The pane pulls via feed_lines from the
app's poll (only while its screen is on top - the app gates that), so
a backgrounded detail screen costs nothing.
"""

from __future__ import annotations

from textual.widgets import RichLog

MAX_BUFFER_LINES = 1000


class TranscriptTail(RichLog):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            max_lines=MAX_BUFFER_LINES, wrap=False, highlight=False,
            **kwargs,  # type: ignore[arg-type]
        )
        self.follow = True

    def feed_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        self.auto_scroll = self.follow
        for line in lines:
            self.write(line)

    def toggle_follow(self) -> bool:
        self.follow = not self.follow
        self.auto_scroll = self.follow
        return self.follow
