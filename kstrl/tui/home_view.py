"""Text the home screen renders, kept out of the screen module (#433).

Increment 2 made home an operator queue (``operator_queue``): the
sections below the masthead are needs you, active, delivery and history,
in that order, and each helper here renders one of them.

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
    from kstrl.tui.operator_queue import ActiveRow, NeedsYouRow, OperatorQueue
    from kstrl.tui.runs import RunRef
    from kstrl.tui.serve_view import ServeState

#: One key per launcher command, each a single keypress.
COMMAND_KEYS = "1234567890"

#: Below this width the launcher column gives way to ``command_strip``.
#: The queue sections need the width more than the launcher does, so the
#: column shows only on a wide terminal (#433 increment 2).
NARROW_BELOW = 160

#: The most rows the needs-you and active tables show; the title counts
#: the rest.
SECTION_ROWS = 4


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
        text.append("needs you: ", style=f"bold {theme.MUTED}")
        text.append("nothing is waiting on you", style=theme.MUTED)
        return text
    text.append("needs you: ", style="bold")
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


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def section_title(name: str, count: int, shown: int) -> Text:
    text = Text(name, style=f"bold {theme.MUTED}")
    if count > shown:
        text.append(f"  {count}, {shown} shown", style=theme.MUTED)
    return text


def fit_rows(rows: list[list[Text]], width: int, flex: int) -> list[list[Text]]:
    """Shorten column ``flex`` so every row fits ``width`` without scrolling.

    A column is as wide as its widest cell, plus one cell of padding a
    side; the table pads one a side and keeps one for its scrollbar. So
    the room left for ``flex`` is what the OTHER columns' widest cells
    leave, which a per-row sum cannot see (#433: the active table
    scrolled sideways at 80 columns).
    """
    if not rows:
        return rows
    columns = len(rows[0])
    widest = [max(row[i].cell_len for row in rows) for i in range(columns)]
    room = width - sum(w for i, w in enumerate(widest) if i != flex) - 2 * columns - 4
    room = max(12, room)
    for row in rows:
        cell = row[flex]
        if cell.cell_len > room:
            row[flex] = Text(_fit(cell.plain, room), style=cell.style)
    return rows


def needs_cells(row: NeedsYouRow) -> list[Text]:
    """[glyph, what, action] for one needs-you row; fit with ``fit_rows``."""
    from kstrl.tui.operator_queue import DECISION

    decision = row.kind == DECISION
    color = theme.WARNING if decision else theme.ERROR
    glyph = Text("◆" if decision else "✗", style=f"bold {color}")
    return [glyph, Text(row.what), Text(row.action, style=theme.ACCENT)]


def active_cells(row: ActiveRow, *, narrow: bool = False) -> list[Text]:
    """[glyph, source and id, state, detail] for one active row."""
    from kstrl.tui.serve_view import short_item_id

    serve = row.source == "ks serve"
    label = short_item_id(row.label) if serve else theme.short_run_id(row.label)
    source = Text(f"{row.source} ", style=theme.STEEL if serve else theme.MUTED)
    source.append(label, style="bold")
    queued = row.state.startswith("queued")
    glyph = Text("○" if queued else "●", style=theme.MUTED if queued else f"bold {theme.ACCENT}")
    state = Text(row.state, style=theme.MUTED if queued else theme.ACCENT)
    if row.state == "interrupted":
        # Not moving: its daemon and lease holder are gone (serve_view).
        glyph, state = Text("▲", style=theme.WARNING), Text(row.state, style=theme.WARNING)
    detail = row.short_detail if narrow and row.short_detail else row.detail
    return [glyph, source, state, Text(detail, style=theme.MUTED)]


def serve_phrase(serve: ServeState | None, *, pid: bool = True) -> Text:
    """``ks serve running (pid 4242)``, ``not running``, or nothing."""
    from kstrl.tui.serve_view import RUNNING

    text = Text()
    if serve is None:
        return text
    text.append("ks serve ", style=theme.MUTED)
    if serve.daemon == RUNNING:
        text.append(f"running (pid {serve.daemon_pid})" if pid else "running", style=theme.ACCENT)
    else:
        text.append(serve.daemon, style=theme.MUTED if serve.daemon != "unknown" else theme.WARNING)
    if serve.problem:
        text.append(f" · {serve.problem}", style=theme.WARNING)
    return text


def active_empty(queue: OperatorQueue | None) -> Text:
    text = Text("active", style=f"bold {theme.MUTED}")
    if queue is None:
        text.append("  reading...", style=theme.MUTED)
    elif not queue.active:
        text.append("  nothing is running", style=theme.MUTED)
    return text


def delivery_text(queue: OperatorQueue | None, width: int) -> Text:
    """The newest finished factory run's integration, merges and CI (Q7, Q8)."""
    from kstrl.tui.delivery import integration_summary, merge_summary

    text = Text("delivery", style=f"bold {theme.MUTED}")
    if queue is None:
        text.append("  reading...", style=theme.MUTED)
        return text
    delivery = queue.delivery
    if delivery is None:
        text.append("  no finished factory run yet", style=theme.MUTED)
        return text
    text.append(f"  run {theme.short_run_id(delivery.run_id)}", style=theme.MUTED)
    text.append("\n  ")
    text.append_text(integration_summary(delivery.integration, short=width < 110))
    text.append("\n  ")
    text.append_text(merge_summary(delivery, short=width < 110))
    return text


def history_note(run_id: str, word: str, queue: OperatorQueue | None) -> str:
    """Why a failed run is history: its successor, or that it is current."""
    from kstrl.tui.run_status import FAILED

    if word != FAILED or queue is None:
        return ""
    successor = queue.superseded.get(run_id)
    if successor:
        return f"superseded by {theme.short_run_id(successor)}"
    if any(row.run_id == run_id for row in queue.needs_you):
        return "current: see needs you"
    return ""
