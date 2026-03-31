"""Agent output log widget - displays streaming agent output with role classification."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import RichLog

from ralph.agent import AgentOutput, LineRole

# Role -> (tag_style, line_style) for the log display
ROLE_STYLES: dict[LineRole, tuple[str, str]] = {
    LineRole.AI: ("bold white", ""),
    LineRole.THINK: ("magenta", "dim italic"),
    LineRole.TOOL: ("bold yellow", ""),
    LineRole.SYS: ("dim", "dim"),
    LineRole.PROMPT: ("bold cyan", "dim"),
    LineRole.GIT: ("bold blue", ""),
    LineRole.GUARD: ("bold red", "bold red"),
    LineRole.USER: ("bold cyan", ""),
    LineRole.UNKNOWN: ("", "dim"),
}

# Compact lowercase tag labels
ROLE_LABELS: dict[LineRole, str] = {
    LineRole.AI: "ai",
    LineRole.THINK: "think",
    LineRole.TOOL: "tool",
    LineRole.SYS: "sys",
    LineRole.PROMPT: "prompt",
    LineRole.GIT: "git",
    LineRole.GUARD: "guard",
    LineRole.USER: "user",
    LineRole.UNKNOWN: "",
}


class AgentLogWidget(RichLog):
    """RichLog-based agent output display with role-classified coloring."""

    DEFAULT_CSS = ""

    def write_agent_line(self, output: AgentOutput) -> None:
        """Write a classified agent output line to the log."""
        tag_style, line_style = ROLE_STYLES.get(output.role, ("", ""))
        tag = ROLE_LABELS.get(output.role, "")

        text = Text()
        text.append(f" {tag:>6} ", style=tag_style)
        text.append(" ", style="dim")
        text.append(output.line, style=line_style)

        self.write(text)

    def write_info(self, message: str) -> None:
        """Write an informational message."""
        text = Text()
        text.append("   info ", style="dim")
        text.append(" ", style="dim")
        text.append(message, style="dim")
        self.write(text)

    def write_success(self, message: str) -> None:
        """Write a success message."""
        text = Text()
        text.append("     ok ", style="bold green")
        text.append(" ", style="dim")
        text.append(message, style="green")
        self.write(text)

    def write_error(self, message: str) -> None:
        """Write an error message."""
        text = Text()
        text.append("  error ", style="bold red")
        text.append(" ", style="dim")
        text.append(message, style="red")
        self.write(text)
