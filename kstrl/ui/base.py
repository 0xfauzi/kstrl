"""Base UI protocol for kstrl output."""

from __future__ import annotations

from typing import Protocol

#: How much of one streamed line a terminal shows. A transcript file gets
#: the whole line: an agent's Bash command was cut at 120 characters where
#: the adapter formatted it, so a designer's transcript could not say what
#: it ran (a user run).
TERMINAL_LINE_CHARS = 300


def terminal_line(line: str, said: str = " in all") -> str:
    """``line`` as a terminal shows it: the head of its first line and how
    long it is, followed by ``said``, in at most :data:`TERMINAL_LINE_CHARS`
    characters, so a line already shortened passes unchanged."""
    first, _, rest = line.partition("\n")
    if not rest and len(first) <= TERMINAL_LINE_CHARS:
        return line
    suffix = f" ... ({len(line)} characters{said})"
    return first[: max(0, TERMINAL_LINE_CHARS - len(suffix))] + suffix


class UI(Protocol):
    """Protocol for kstrl UI implementations."""

    def title(self, text: str) -> None:
        """Display a large title."""
        ...

    def section(self, text: str) -> None:
        """Display a section header."""
        ...

    def subsection(self, text: str) -> None:
        """Display a subsection header."""
        ...

    def hr(self) -> None:
        """Display a horizontal rule."""
        ...

    def kv(self, key: str, value: str) -> None:
        """Display a key-value pair."""
        ...

    def startup_art(self) -> None:
        """Display startup animation if supported."""
        ...

    def info(self, text: str) -> None:
        """Display info message (dim)."""
        ...

    def ok(self, text: str) -> None:
        """Display success message (green)."""
        ...

    def warn(self, text: str) -> None:
        """Display warning message (yellow)."""
        ...

    def err(self, text: str) -> None:
        """Display error message (red)."""
        ...

    def channel_header(self, channel: str, title: str = "") -> None:
        """Display channel header with optional title."""
        ...

    def stream_line(self, tag: str, line: str) -> None:
        """Display a single prefixed line."""
        ...

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        """Interactive choice, returns selected index."""
        ...

    def can_prompt(self) -> bool:
        """Check if interactive prompts are available."""
        ...
