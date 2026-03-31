"""Agent output log widget - displays streaming agent output with visual hierarchy.

Visual hierarchy uses indentation, spacing, color, and text weight.

Hierarchy (most to least prominent):
1. AI text     - left-aligned, bold, white - the content users scan for
2. Thinking    - indented, italic, cyan - background reasoning
3. Tool calls  - indented, yellow - what the agent is doing
4. Git         - indented, blue - branch/commit operations
5. Guard       - left-aligned, bold red - violations
6. System/info - indented, dim - metadata
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import RichLog

from ralph.agent import AgentOutput, LineRole


class AgentLogWidget(RichLog):
    """RichLog-based agent output display with role-based visual hierarchy."""

    DEFAULT_CSS = ""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._last_role: LineRole | None = None

    def write_agent_line(self, output: AgentOutput) -> None:
        """Write a classified agent output line with appropriate styling."""
        prev = self._last_role
        curr = output.role

        # Insert blank lines at role transitions for visual grouping
        if prev is not None and prev != curr:
            if curr == LineRole.AI:
                self.write(Text(""))
            elif curr == LineRole.THINK and prev != LineRole.THINK:
                self.write(Text(""))
            elif curr == LineRole.TOOL and prev == LineRole.AI:
                self.write(Text(""))

        self._last_role = curr

        if curr == LineRole.AI:
            self._write_ai(output.line)
        elif curr == LineRole.THINK:
            self._write_think(output.line)
        elif curr == LineRole.TOOL:
            self._write_tool(output.line)
        elif curr == LineRole.GUARD:
            self._write_guard(output.line)
        elif curr == LineRole.GIT:
            self._write_git(output.line)
        elif curr == LineRole.SYS:
            self._write_sys(output.line)
        else:
            self._write_default(output.line)

    def _write_ai(self, line: str) -> None:
        """AI text - most prominent. Bold white."""
        text = Text()
        text.append(line, style="bold")
        self.write(text)

    def _write_think(self, line: str) -> None:
        """Thinking - cyan italic, indented."""
        text = Text()
        text.append("      ", style="")
        text.append(line, style="italic cyan")
        self.write(text)

    def _write_tool(self, line: str) -> None:
        """Tool calls - yellow, indented."""
        text = Text()
        text.append("      ", style="")
        text.append(line, style="yellow")
        self.write(text)

    def _write_guard(self, line: str) -> None:
        """Guard violations - bold red."""
        text = Text()
        text.append(line, style="bold red")
        self.write(text)

    def _write_git(self, line: str) -> None:
        """Git operations - blue, indented."""
        text = Text()
        text.append("      ", style="")
        text.append(line, style="blue")
        self.write(text)

    def _write_sys(self, line: str) -> None:
        """System messages - dim, indented."""
        text = Text()
        text.append("    ", style="")
        text.append(line, style="dim")
        self.write(text)

    def _write_default(self, line: str) -> None:
        """Fallback - dim."""
        text = Text()
        text.append(line, style="dim")
        self.write(text)

    def write_separator(self, label: str) -> None:
        """Write a prominent separator line between iterations."""
        self.write(Text(""))
        line = Text()
        width = 60
        pad = max(0, width - len(label) - 4)
        line.append("-- ", style="bold magenta")
        line.append(label, style="bold magenta")
        line.append(" " + "-" * pad, style="bold magenta")
        self.write(line)
        self.write(Text(""))
        self._last_role = None

    def write_info(self, message: str) -> None:
        """Informational message - dim."""
        text = Text()
        text.append("    ", style="")
        text.append(message, style="dim")
        self.write(text)

    def write_success(self, message: str) -> None:
        """Success message - bold green."""
        text = Text()
        text.append(message, style="bold green")
        self.write(text)

    def write_error(self, message: str) -> None:
        """Error message - bold red."""
        text = Text()
        text.append(message, style="bold red")
        self.write(text)
