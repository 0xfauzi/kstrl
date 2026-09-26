"""Text the home screen renders, kept out of the screen module (#433).

- ``attention_line``: what is waiting on the operator, counted, with the
  key that opens it (E3). Before, the operator had to open the inbox to
  learn whether anything was in it.
- ``command_strip``: the launcher as one line of keys for a terminal too
  narrow for the launcher column (F1/F2 at 80 columns).
- ``preview_status``: the selected run's state word and the reason for
  it (F4).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from rich.text import Text

from kstrl.tui import theme
from kstrl.tui.run_status import RUN_STATE_STYLE, RUNNING, run_reason

if TYPE_CHECKING:
    from kstrl.reducer import RunState
    from kstrl.tui.home_data import HomeStats, RunSummary
    from kstrl.tui.runs import RunRef

#: One key per launcher command, each a single keypress.
COMMAND_KEYS = "1234567890"

#: Below this width the launcher column gives way to ``command_strip``.
NARROW_BELOW = 100


class _Command(Protocol):
    @property
    def command_id(self) -> str: ...

    @property
    def title(self) -> str: ...


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def attention_line(stats: HomeStats) -> Text:
    """``needs you: 2 inbox items (6) · 1 failed component (3)``."""
    text = Text()
    parts: list[tuple[str, str]] = []
    if stats.inbox_open:
        parts.append((f"{_plural(stats.inbox_open, 'inbox item')} (6)", theme.WARNING))
    if stats.failed_components:
        parts.append((f"{_plural(stats.failed_components, 'failed component')} (3)", theme.ERROR))
    if not parts:
        # "Nothing is waiting" is a claim about BOTH sources; a count that
        # could not be read (None) is not a zero (#433 E3).
        if stats.inbox_open is None or stats.failed_components is None:
            return text
        text.append("  nothing is waiting on you", style=theme.MUTED)
        return text
    text.append("  needs you: ", style="bold")
    for index, (label, style) in enumerate(parts):
        if index:
            text.append(" · ", style=theme.MUTED)
        text.append(label, style=f"bold {style}")
    return text


def command_strip(commands: Sequence[_Command], width: int) -> Text:
    """As many ``<key> <name>`` pairs as fit, then where the rest are."""
    tail = "  ^p all"
    text = Text()
    for index, command in enumerate(commands):
        piece = f"{COMMAND_KEYS[index]} {command.title}"
        room = width - 2 - text.cell_len - len(tail)
        if len(piece) + 2 > room:
            break
        if index:
            text.append("  ")
        text.append(COMMAND_KEYS[index], style=f"bold {theme.ACCENT}")
        text.append(f" {command.title}", style=theme.MUTED)
    text.append(tail, style=theme.MUTED)
    return text


def preview_status(ref: RunRef | None, summary: RunSummary | None, state: RunState) -> Text:
    """The run's state word and, for every state but completed, why."""
    word = RUNNING if ref is not None and ref.live else summary.state if summary else ""
    line = Text()
    if not word:
        line.append("folding run state...", style=theme.MUTED)
        return line
    glyph, color = RUN_STATE_STYLE[word]
    line.append(f"{glyph} {word}", style=f"bold {color}")
    reason = run_reason(word, state)
    if reason:
        line.append(f" · {reason}", style=theme.MUTED)
    return line
