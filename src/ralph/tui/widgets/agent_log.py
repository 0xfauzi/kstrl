"""Agent output log widget - displays streaming agent output with visual hierarchy.

Visual hierarchy uses only indentation, spacing, color, and text weight.
No unicode symbols - only ASCII characters that render everywhere.

Hierarchy (most to least prominent):
1. AI text     - left-aligned, bold white, the content users scan for
2. Thinking    - indented 6 chars, dim italic, clearly background reasoning
3. Tool calls  - indented 6 chars, dim, compact summaries
4. Errors      - left-aligned, red, bold
5. System/info - indented 4 chars, dim, minimal
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
            # AI text after anything else gets a blank line
            if curr == LineRole.AI:
                self.write(Text(""))
            # Starting a thinking block gets a blank line
            elif curr == LineRole.THINK and prev != LineRole.THINK:
                self.write(Text(""))
            # Tool after AI gets a blank line
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
        """AI text - most prominent. Left-aligned, bold."""
        text = Text()
        text.append(line, style="bold")
        self.write(text)

    def _write_think(self, line: str) -> None:
        """Thinking - subdued. Indented, dim italic."""
        text = Text()
        text.append("      ", style="")
        text.append(line, style="dim italic")
        self.write(text)

    def _write_tool(self, line: str) -> None:
        """Tool calls - compact. Indented, dim."""
        text = Text()
        text.append("      ", style="")
        text.append(line, style="dim")
        self.write(text)

    def _write_guard(self, line: str) -> None:
        """Guard violations - red, prominent."""
        text = Text()
        text.append(line, style="bold red")
        self.write(text)

    def _write_git(self, line: str) -> None:
        """Git operations."""
        text = Text()
        text.append("      ", style="")
        text.append(line, style="dim blue")
        self.write(text)

    def _write_sys(self, line: str) -> None:
        """System messages - very dim."""
        text = Text()
        text.append("    ", style="")
        text.append(line, style="dim")
        self.write(text)

    def _write_default(self, line: str) -> None:
        """Fallback."""
        text = Text()
        text.append(line, style="dim")
        self.write(text)

    def write_separator(self, label: str) -> None:
        """Write a prominent separator line between iterations."""
        self.write(Text(""))
        line = Text()
        width = 60
        pad = max(0, width - len(label) - 4)
        line.append("-- ", style="bold")
        line.append(label, style="bold")
        line.append(" " + "-" * pad, style="bold")
        self.write(line)
        self.write(Text(""))
        self._last_role = None

    def write_info(self, message: str) -> None:
        """Informational message."""
        text = Text()
        text.append("    ", style="")
        text.append(message, style="dim")
        self.write(text)

    def write_success(self, message: str) -> None:
        """Success message."""
        text = Text()
        text.append(message, style="bold green")
        self.write(text)

    def write_error(self, message: str) -> None:
        """Error message."""
        text = Text()
        text.append(message, style="bold red")
        self.write(text)
