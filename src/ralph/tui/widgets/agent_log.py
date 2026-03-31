"""Agent output log widget - displays streaming agent output with visual hierarchy.

Design principles:
- AI text output is the most prominent (white, clear spacing)
- Thinking is subdued (dim italic, vertical bar prefix)
- Tool calls are compact (dim, one-line summaries)
- Errors are red and prominent
- System/info messages are minimal
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
        # Add spacing between different role types for visual grouping
        if self._last_role is not None and self._last_role != output.role:
            # Transition from tool -> ai or think -> ai gets a blank line
            if output.role == LineRole.AI and self._last_role in (
                LineRole.TOOL, LineRole.THINK,
            ):
                self.write(Text(""))
            # Transition into thinking gets a blank line
            elif output.role == LineRole.THINK and self._last_role != LineRole.THINK:
                self.write(Text(""))

        self._last_role = output.role

        if output.role == LineRole.AI:
            self._write_ai(output.line)
        elif output.role == LineRole.THINK:
            self._write_think(output.line)
        elif output.role == LineRole.TOOL:
            self._write_tool(output.line)
        elif output.role == LineRole.GUARD:
            self._write_guard(output.line)
        elif output.role == LineRole.GIT:
            self._write_git(output.line)
        elif output.role == LineRole.SYS:
            self._write_sys(output.line)
        else:
            self._write_default(output.line)

    def _write_ai(self, line: str) -> None:
        """AI text output - most prominent, white, clear."""
        text = Text()
        text.append("  ", style="")
        text.append(line, style="bold")
        self.write(text)

    def _write_think(self, line: str) -> None:
        """Thinking - subdued, italic, with vertical bar prefix."""
        text = Text()
        text.append("  \u2502 ", style="dim magenta")
        text.append(line, style="dim italic")
        self.write(text)

    def _write_tool(self, line: str) -> None:
        """Tool calls - compact, dim, monospace feel."""
        text = Text()
        text.append("  \u25aa ", style="dim yellow")
        text.append(line, style="dim")
        self.write(text)

    def _write_guard(self, line: str) -> None:
        """Guard violations - red, prominent."""
        text = Text()
        text.append("  ! ", style="bold red")
        text.append(line, style="red")
        self.write(text)

    def _write_git(self, line: str) -> None:
        """Git operations - blue accent."""
        text = Text()
        text.append("  \u25aa ", style="dim blue")
        text.append(line, style="dim blue")
        self.write(text)

    def _write_sys(self, line: str) -> None:
        """System messages - very dim."""
        text = Text()
        text.append("    ", style="")
        text.append(line, style="dim")
        self.write(text)

    def _write_default(self, line: str) -> None:
        """Fallback for unknown roles."""
        text = Text()
        text.append("  ", style="")
        text.append(line, style="dim")
        self.write(text)

    def write_info(self, message: str) -> None:
        """Write an informational message."""
        text = Text()
        text.append("  ", style="")
        text.append(message, style="dim")
        self.write(text)

    def write_success(self, message: str) -> None:
        """Write a success message."""
        text = Text()
        text.append("  \u2713 ", style="bold green")
        text.append(message, style="green")
        self.write(text)

    def write_error(self, message: str) -> None:
        """Write an error message."""
        text = Text()
        text.append("  \u2717 ", style="bold red")
        text.append(message, style="red")
        self.write(text)
