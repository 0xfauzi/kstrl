"""A gate's whole stored output, wrapped and scrollable (#433 advice 2.5).

The component detail and the failure queue show the last lines of a
failed gate's output (#462). The decisive line is often above them, and
a line wider than the pane lost its end. This screen shows the file from
the path the event names, every line wrapped, bounded to its last
``FULL_BYTES`` and saying so when it is cut.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Static

from kstrl.tui import theme
from kstrl.tui.widgets.component_detail import read_tail, shown_path
from kstrl.tui.widgets.context_bar import ContextBar
from kstrl.tui.widgets.reflow_log import ReflowLog

#: The most of a log this screen reads, from its end.
FULL_BYTES = 1_048_576
MAX_LINES = 20_000


def read_log(path: str) -> tuple[list[str], str]:
    """(lines, note): the last ``FULL_BYTES`` of the file, and what was cut."""
    try:
        text, size = read_tail(path, FULL_BYTES)
    except OSError as exc:
        return [], f"not readable: {exc}"
    lines = text.splitlines()
    if size > FULL_BYTES:
        return lines[1:], f"the last {FULL_BYTES // 1024} KB of {size // 1024} KB"
    return lines, f"{len(lines)} line(s)"


class GateLogScreen(Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, path: str, what: str, root_dir: Path | None = None) -> None:
        super().__init__()
        self._path = path
        self._what = what
        self._root_dir = root_dir

    def compose(self) -> ComposeResult:
        yield ContextBar("output", self._what)
        yield Static(id="gate-log-path")
        yield ReflowLog(id="gate-log", max_lines=MAX_LINES, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        lines, note = read_log(self._path)
        head = Text(shown_path(self._path, self._root_dir))
        head.append(f"  {note}", style=theme.MUTED)
        self.query_one("#gate-log-path", Static).update(head)
        log = self.query_one("#gate-log", ReflowLog)
        for line in lines:
            log.write_source(Text(line))
