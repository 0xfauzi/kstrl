"""Run dashboard header widget - shows iteration counter, story, and timer."""

from __future__ import annotations

import time

from textual.reactive import reactive
from textual.widgets import Static


class RunHeader(Static):
    """Header bar for the run dashboard showing iteration, story, and elapsed time."""

    iteration = reactive(0)
    max_iterations = reactive(0)
    current_story = reactive("")
    elapsed = reactive(0.0)
    _start_time: float = 0.0

    DEFAULT_CSS = ""

    def on_mount(self) -> None:
        self._start_time = time.monotonic()
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self.elapsed = time.monotonic() - self._start_time

    def render(self) -> str:
        minutes = int(self.elapsed) // 60
        seconds = int(self.elapsed) % 60
        time_str = f"{minutes:02d}:{seconds:02d}"

        iter_str = f"Iteration {self.iteration}/{self.max_iterations}"
        story_str = self.current_story or "-"

        return f"  {iter_str}   |   Story: {story_str}   |   {time_str}"

    def set_iteration(self, iteration: int, max_iterations: int) -> None:
        self.iteration = iteration
        self.max_iterations = max_iterations

    def set_story(self, story_id: str) -> None:
        self.current_story = story_id

    def reset_timer(self) -> None:
        self._start_time = time.monotonic()
