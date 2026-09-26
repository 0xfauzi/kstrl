"""Component board: one row per component, diff-updated (design pass).

Spike finding 3 keeps the render policy binding: rows are updated in
place (update_cell), never clear()+rebuilt per poll.

Design decisions from the critique:
- Status glyphs come from the theme (unicode, user decision) - the
  ASCII punctuation set was invisible at a glance.
- Numeric columns are right-aligned; empty data is a dim midpoint dot,
  never "-" (a dash column reads as broken data).
- A pending component says WHAT it is waiting on - the DAG is known;
  make the board explain itself.
- An open checkpoint marks the row with an accent diamond; the banner
  carries the call to action.

#433 increment 1:
- The phase column gives every row that is not moving a reason: what a
  pending row waits on, that a carried row was not run here (F10), the
  failed phase and its cause (F7), why a row was skipped.
- The ``time`` column says how long a finished component took, and how
  long ago a running one last wrote an event; it no longer measures a
  component that finished two days ago against the clock (F5).
- Tokens are compact (``7.29M``) like every other table, and at a
  narrow terminal the try, iter and tokens columns are dropped whole
  rather than cut at the edge (F1). The phase text is shortened with an
  ellipsis to what is left; enter opens the full cause.
- Every cell update widens its column, so a cell that starts as the dot
  and later holds a number is never cut (F1).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import DataTable

from kstrl.tui import theme
from kstrl.tui.agent_health import agent_health
from kstrl.tui.run_status import age_phrase, component_took, failed_cause
from kstrl.tui.widgets.cost_meter import format_tokens

if TYPE_CHECKING:
    from kstrl.reducer import ComponentState, RunState

FULL_COLUMNS = ("", "component", "status", "phase", "try", "iter", "time", "tokens", "cost")
COMPACT_COLUMNS = ("", "component", "status", "phase", "time", "cost")
#: Added after ``phase`` while a component is moving in an unfinished
#: run: its last output and its process (#433 M2).
AGENT_COLUMN = "agent"
#: Kept for callers that index the full layout.
COLUMNS = FULL_COLUMNS

#: Below this terminal width the board drops try, iter and tokens.
COMPACT_BELOW = 110
#: The phase text never shrinks below this many cells.
MIN_PHASE_WIDTH = 12
#: Cells the table cannot give to columns: its own padding (0 1), the
#: vertical scrollbar, and one spare so a growing number does not tip
#: the row into a horizontal scrollbar.
_TABLE_CHROME = 4

_MOVING = ("running", "verifying")
#: Statuses that did work in this run, so a duration means something. A
#: skipped component never ran: its one event is the skip.
_TERMINAL = ("completed", "failed", "merge_pending", "awaiting_approval")


def time_cell_text(comp: ComponentState, now: float) -> str:
    """How long a component took, or has taken so far while it moves.

    One meaning for the whole column. It used to hold a duration on a
    finished row and ``21s ago`` (the last event's age) on a running one,
    under the one header ``time`` (#433). How recently a running agent
    wrote is the ``agent`` column's job now, labelled ``output``.
    """
    if comp.carried or not comp.started_ts:
        return theme.EMPTY_CELL
    if comp.status in _MOVING:
        return age_phrase(now - comp.started_ts)
    if comp.status in _TERMINAL:
        return age_phrase(component_took(comp))
    return theme.EMPTY_CELL


def _dim(value: str) -> Text:
    return Text(value, style=theme.MUTED, justify="right")


def _num(value: str) -> Text:
    return Text(value, justify="right")


def _blockers(comp: ComponentState, state: RunState) -> list[str]:
    return [
        dep
        for dep in comp.deps
        if state.components.get(dep) is None or state.components[dep].status != "completed"
    ]


def phase_reason(comp: ComponentState, state: RunState) -> tuple[str, str]:
    """The phase cell's text and style: a phase, or the reason for a state."""
    if comp.status == "pending" and state.kind == "decompose":
        # A decompose run plans components; it never builds them, so
        # "waiting on" would promise a start that will not come (#433).
        return "planned; ks factory builds it", theme.MUTED
    if comp.status == "pending" and _blockers(comp, state):
        return f"waiting on {', '.join(_blockers(comp, state))}", theme.MUTED
    # Pending rows are "carried" too until the run reaches them: only the
    # scope record exists. Carried means a status an EARLIER run set.
    if comp.carried and comp.status != "pending":
        return "carried from an earlier run", theme.MUTED
    if comp.status == "failed":
        return failed_cause(comp) or "failed", theme.ERROR
    if comp.status == "skipped" and comp.error:
        return comp.error, theme.MUTED
    if not comp.phase:
        return theme.EMPTY_CELL, theme.MUTED
    moving = comp.status in _MOVING
    return comp.phase, theme.status_glyph(comp.status)[1] if moving else ""


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def _phase_cell(comp: ComponentState, state: RunState, width: int) -> Text:
    text, style = phase_reason(comp, state)
    return Text(_fit(text, width), style=style)


def _tokens(comp: ComponentState) -> Text:
    if not comp.total_tokens:
        return _dim(theme.EMPTY_CELL)
    marker = "+" if comp.tokens_are_lower_bound else ""
    return _num(f"{format_tokens(comp.total_tokens)}{marker}")


def _cost(comp: ComponentState) -> Text:
    if not comp.cost_usd:
        return _dim(theme.EMPTY_CELL)
    return _num(f"${comp.cost_usd:.2f}{'+' if comp.cost_is_lower_bound else ''}")


def _time(comp: ComponentState, now: float) -> Text:
    value = time_cell_text(comp, now)
    return _dim(value) if value == theme.EMPTY_CELL else _num(value)


def _agent_cell(
    comp: ComponentState, state: RunState, run_dir: Path | None, now: float, short: bool
) -> Text:
    if state.finished or comp.status not in _MOVING:
        # Left-aligned like the text it stands in for.
        return Text(theme.EMPTY_CELL, style=theme.MUTED)
    health = agent_health(run_dir, comp, now)
    style = theme.ERROR if health.process == "exited" else theme.MUTED
    return Text(health.text(short=short, pid=False), style=style)


def _cells(
    comp: ComponentState,
    state: RunState,
    now: float,
    phase_width: int,
    run_dir: Path | None = None,
    short: bool = False,
) -> dict[str, Text]:
    glyph, color = theme.status_glyph(comp.status)
    name = Text(comp.component_id)
    if comp.checkpoint_open:
        name.append("  ◆", style=theme.ACCENT)
    return {
        "glyph": Text(glyph, style=f"bold {color}"),
        "component": name,
        "status": Text(comp.status, style=color),
        "phase": _phase_cell(comp, state, phase_width),
        "try": _num(str(comp.attempt)) if comp.attempt else _dim(theme.EMPTY_CELL),
        "iter": _num(str(comp.iteration)) if comp.iteration else _dim(theme.EMPTY_CELL),
        "time": _time(comp, now),
        "tokens": _tokens(comp),
        "cost": _cost(comp),
        "agent": _agent_cell(comp, state, run_dir, now, short),
    }


def _key(column: str) -> str:
    return column or "glyph"


def _fixed_width(columns: tuple[str, ...], cells: list[dict[str, Text]]) -> int:
    """Cells every column except phase needs, with each column's padding."""
    total = 0
    for column in columns:
        key = _key(column)
        if key == "phase":
            total += 2
            continue
        widest = max((cell[key].cell_len for cell in cells), default=0)
        total += max(widest, len(column)) + 2
    return total


def _columns_for(state: RunState, compact: bool) -> tuple[str, ...]:
    """The layout for the width, with ``agent`` while anything moves."""
    columns: tuple[str, ...] = COMPACT_COLUMNS if compact else FULL_COLUMNS
    if state.finished or not any(c.status in _MOVING for c in state.components.values()):
        return columns
    at = columns.index("phase") + 1
    return (*columns[:at], AGENT_COLUMN, *columns[at:])


class ComponentTable(DataTable[Text | str]):
    #: The run whose transcripts the agent column reads. A board that is
    #: not the app's own run (the home preview) sets it; None falls back
    #: to the app's run directory.
    run_dir: Path | None = None

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.show_cursor = True
        self._columns_shown: tuple[str, ...] = ()
        self._set_columns(FULL_COLUMNS, 0)

    def reset(self) -> None:
        """Forget rows AND column widths: the next update lays out afresh."""
        self.clear(columns=True)
        self._columns_shown = ()

    def _phase_too_wide(self, phase_width: int) -> bool:
        column = next((c for c in self.columns.values() if c.key.value == "phase"), None)
        return column is not None and phase_width > 0 and column.content_width > phase_width

    def _set_columns(self, columns: tuple[str, ...], phase_width: int) -> None:
        """Lay the columns out again when the layout or the room changes.

        A column only ever widens on update, so a phase column sized for
        a wider terminal (or a previous run) would push the row past the
        edge; that, and a change of layout, is a rebuild. The never-clear
        rule guards live polls, not a resize.
        """
        if columns == self._columns_shown and not self._phase_too_wide(phase_width):
            return
        self.clear(columns=True)
        for column in columns:
            self.add_column(column, key=_key(column))
        self._columns_shown = columns

    def _screen_width(self) -> int:
        width = self.app.size.width if self.is_attached else 0
        return width or 120

    def _run_dir(self) -> Path | None:
        if self.run_dir is not None:
            return self.run_dir
        run_dir = getattr(self.app, "run_dir", None) if self.is_attached else None
        return run_dir if isinstance(run_dir, Path) else None

    def update_state(self, state: RunState) -> None:
        now = time.time()
        width = self._screen_width()
        compact = width < COMPACT_BELOW
        columns = _columns_for(state, compact)
        run_dir = self._run_dir()
        order = state.plan_order or sorted(state.components)
        comps = [state.components[cid] for cid in order if cid in state.components]
        rough = [_cells(comp, state, now, 200, run_dir, compact) for comp in comps]
        phase_width = max(MIN_PHASE_WIDTH, width - _TABLE_CHROME - _fixed_width(columns, rough))
        self._set_columns(columns, phase_width)
        for comp in comps:
            cells = _cells(comp, state, now, phase_width, run_dir, compact)
            values = [cells[_key(column)] for column in columns]
            if comp.component_id in self.rows:
                for column, value in zip(columns, values, strict=True):
                    self.update_cell(comp.component_id, _key(column), value, update_width=True)
            else:
                self.add_row(*values, key=comp.component_id)

    def tick_ages(self, state: RunState) -> None:
        """1s label-only refresh of the time and agent columns."""
        if "time" not in self._columns_shown:
            return
        now = time.time()
        agent = AGENT_COLUMN in self._columns_shown
        run_dir = self._run_dir() if agent else None
        short = "tokens" not in self._columns_shown
        for comp_id, comp in state.components.items():
            if comp_id not in self.rows:
                continue
            self.update_cell(comp_id, "time", _time(comp, now), update_width=True)
            if agent:
                cell = _agent_cell(comp, state, run_dir, now, short)
                self.update_cell(comp_id, AGENT_COLUMN, cell, update_width=True)
