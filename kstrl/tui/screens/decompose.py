"""Decompose run screens: the live architect view + spec triage (C5).

DecomposeScreen is the decompose-kind counterpart of ComponentScreen:
attempt strip, the forming DAG (rows appear as the RunPlan folds),
the architect's streaming transcript, and a spec-issue strip that
deepens into SpecTriageScreen. Everything renders from the folded
RunState - files are the record.

SpecTriageScreen is read-only in v1: blockers already halted the run
(the banner says so and points at the durable artifact); non-blockers
never gate, so there is no decision to prompt for.

#433 F11: the triage table fits each row to the terminal and marks what
it shortened with an ellipsis; the detail pane under it prints the
highlighted issue whole, wrapped, with its location and suggestion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from kstrl.agents.base import ARCHITECT_COMPONENT
from kstrl.tui import theme
from kstrl.tui.messages import StateChanged
from kstrl.tui.state import architect_component_id, planned_component_ids
from kstrl.tui.widgets.context_bar import ContextBar
from kstrl.tui.widgets.cost_meter import CostMeter
from kstrl.tui.widgets.dag_table import DagTable
from kstrl.tui.widgets.header import RunHeader, meter_width, topbar_header
from kstrl.tui.widgets.transcript import TranscriptTail

if TYPE_CHECKING:
    from kstrl.manifest import Manifest
    from kstrl.reducer import RunState

_SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}
_SEVERITY_STYLES = {
    "blocker": f"bold {theme.ERROR}",
    "major": theme.ERROR,
    "minor": theme.WARNING,
}


def _issue_strip(state: RunState) -> Text:
    text = Text()
    counts = state.spec_issue_counts
    architect = state.components.get(architect_component_id(state))
    audit_ran = architect is not None and any(
        p.get("phase") == "audit" for p in architect.phase_history
    )
    if not counts:
        if audit_ran:
            text.append("✓ clean audit", style=f"bold {theme.SUCCESS}")
            text.append("  no spec issues", style=theme.MUTED)
        else:
            text.append("spec audit pending", style=theme.MUTED)
        return text
    text.append("▲ ", style=f"bold {theme.WARNING}")
    text.append("spec issues  ", style="bold")
    parts: list[tuple[str, str]] = []
    for severity in ("blocker", "major", "minor"):
        if counts.get(severity):
            parts.append(
                (
                    f"{counts[severity]} {severity}",
                    _SEVERITY_STYLES[severity],
                )
            )
    for other, n in sorted(counts.items()):
        if other not in _SEVERITY_ORDER:
            parts.append((f"{n} {other}", theme.MUTED))
    for index, (label, style) in enumerate(parts):
        if index:
            text.append(" · ", style=theme.MUTED)
        text.append(label, style=style)
    text.append("   (i) triage", style=theme.MUTED)
    return text


def _attempt_strip(state: RunState) -> Text:
    text = Text()
    architect = state.components.get(architect_component_id(state))
    if architect is None:
        text.append("waiting for the architect...", style=theme.MUTED)
        return text
    glyph, color = theme.status_glyph(architect.status)
    text.append(f"{glyph} ", style=f"bold {color}")
    text.append("architect ", style="bold")
    text.append(architect.status, style=color)
    attempts = [p for p in architect.phase_history if p.get("phase") == "decompose"]
    failed = sum(1 for p in attempts if not p.get("passed"))
    attempt = max(architect.attempt, 1)
    text.append(
        f"  attempt {attempt}",
        style=theme.WARNING if failed else theme.MUTED,
    )
    if failed:
        text.append(f"  · {failed} failed", style=theme.WARNING)
    return text


def _summary(state: RunState) -> Text | None:
    if not state.finished:
        return None
    text = Text()
    architect = state.components.get(architect_component_id(state))
    if architect is None or architect.status != "completed":
        text.append("✗ decompose did not complete", style=f"bold {theme.ERROR}")
        return text
    planned = planned_component_ids(state)
    prds = [a for a in state.artifacts if a.get("label") == "prd"]
    manifest = next(
        (a for a in state.artifacts if a.get("label") == "manifest"),
        None,
    )
    text.append("✓ ", style=f"bold {theme.SUCCESS}")
    text.append(f"{len(planned)} component(s)", style="bold")
    text.append(f" · {len(prds)} PRD(s)", style=theme.MUTED)
    if manifest is not None:
        text.append(f" · manifest {manifest.get('path', '')}", style=theme.MUTED)
    return text


def _shorten(text: str, cells: int) -> str:
    return text if len(text) <= cells else text[: max(1, cells - 1)] + "…"


#: Cells the triage table keeps for itself: padding and the scrollbar.
_TRIAGE_CHROME = 4
#: The summary is never shortened below this many cells.
_MIN_SUMMARY = 20


def _triage_widths(
    issues: list[dict[str, str]], width: int, location_cells: int
) -> tuple[int, int]:
    """(location, summary) cells, so the four columns fit ``width``.

    The location gives way first: the summary is what the operator
    reads, and the detail pane under the table holds both in full.
    """
    kind = max((len(issue.get("kind", "")) for issue in issues), default=4)
    longest = max((len(issue.get("location", "")) for issue in issues), default=8)
    room = width - _TRIAGE_CHROME - len("severity") - max(kind, 4) - 4 * 2
    location = max(len("location"), min(location_cells, longest, room - _MIN_SUMMARY))
    return location, max(8, room - location)


def _triage_row(issue: dict[str, str], summary_cells: int, location_cells: int) -> list[Text]:
    severity = issue.get("severity", "")
    location = issue.get("location", "")
    return [
        Text(severity, style=_SEVERITY_STYLES.get(severity, theme.MUTED)),
        Text(issue["kind"]) if issue.get("kind") else Text(theme.EMPTY_CELL, style=theme.MUTED),
        Text(_shorten(location, location_cells), style=theme.MUTED)
        if location
        else Text(theme.EMPTY_CELL, style=theme.MUTED),
        Text(_shorten(issue.get("summary", ""), summary_cells)),
    ]


class DecomposeScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("i", "open_triage", "Triage"),
        Binding("f", "toggle_follow", "Follow"),
    ]

    def __init__(self) -> None:
        super().__init__()
        # The app's duck-typed poll contract (A3): refresh_state +
        # the architect's bounded transcript tail.
        #
        # Seeded with the current key and re-resolved in refresh_state,
        # because the run's own format is not knowable until its first
        # events have folded (#281). A pre-#281 dir keeps its transcript
        # under the bare word, so tailing the new key found nothing.
        self.transcript_component = ARCHITECT_COMPONENT
        self._following = True
        #: The run has written its finish record: the transcript is saved,
        #: not growing, so there is nothing to follow (#433 F8).
        self._finished = False

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        with Horizontal(id="topbar"):
            yield RunHeader(id="decompose-header")
            yield CostMeter(id="cost-meter")
        yield Static(id="attempt-strip")
        yield Static("plan", id="dag-title")
        yield DagTable(id="dag-table")
        yield Static(id="issues-strip")
        yield Static(id="decompose-summary")
        yield Static(id="decompose-transcript-title")
        yield TranscriptTail(id="transcript")
        yield Footer()

    @property
    def ready(self) -> bool:
        return next(iter(self.query(TranscriptTail)), None) is not None

    def on_mount(self) -> None:
        self._update_transcript_title()
        store = getattr(self.app, "store", None)
        if store is not None:
            self.refresh_state(store.state, store.manifest())
        run = getattr(self.app, "run_context", None)
        if run is not None:
            # Filled now rather than at the next poll (#433 F8).
            self.feed_transcript(run.transcript_tailer(self.transcript_component).poll())

    def refresh_state(
        self,
        state: RunState,
        manifest: Manifest | None,
    ) -> None:
        del manifest  # the folded plan is the source; no manifest join
        # Before the ready guard: the tail key must track the run's
        # format even on a poll whose widgets are not up yet, and it is
        # a plain assignment that cannot raise.
        self.transcript_component = architect_component_id(state)
        if not self.ready:
            return
        if state.finished != self._finished:
            self._finished = state.finished
            self.refresh_bindings()
            self._update_transcript_title()
        try:
            self._update_topbar(state)
            self.query_one("#attempt-strip", Static).update(_attempt_strip(state))
            self.query_one(DagTable).update_state(state)
            self.query_one("#issues-strip", Static).update(_issue_strip(state))
            summary = _summary(state)
            summary_widget = self.query_one("#decompose-summary", Static)
            summary_widget.display = summary is not None
            if summary is not None:
                summary_widget.update(summary)
        except NoMatches:
            # A late StateChanged can arrive while the screen is tearing
            # down: `ready` still sees the transcript (composed last) but
            # RunHeader (composed first) is already gone. Dropping the
            # update is safe - observability writes, not control flow.
            return

    def tick_ages(self, state: RunState) -> None:
        if not self.ready:
            return
        try:
            self._update_topbar(state)
        except NoMatches:
            # Same teardown race as refresh_state: a timer-driven tick can
            # fire after RunHeader is removed. Drop it.
            return

    def _update_topbar(self, state: RunState) -> None:
        header = topbar_header(state, self.app, self.size.width)
        self.query_one(RunHeader).update(header)
        self.query_one(CostMeter).update_state(state, meter_width(header, self.size.width))

    def feed_transcript(self, lines: list[str]) -> None:
        tail = self.query_one(TranscriptTail)
        before = tail.lines_written
        tail.feed_lines(lines)
        if self._finished and tail.lines_written != before:
            self._update_transcript_title()

    def check_action(self, action: str, _parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_follow":
            return not self._finished
        return True

    def _update_transcript_title(self) -> None:
        title = Text("architect transcript", style="bold")
        if self._finished:
            lines = self.query_one(TranscriptTail).lines_written
            title.append(f"  · saved, {lines} line(s)", style=theme.MUTED)
        elif self._following:
            title.append("  ● following", style=theme.ACCENT)
            title.append("  (f pauses)", style=theme.MUTED)
        else:
            title.append("  ⏸ paused", style=theme.MUTED)
            title.append("  (f follows)", style=theme.MUTED)
        self.query_one(
            "#decompose-transcript-title",
            Static,
        ).update(title)

    def action_toggle_follow(self) -> None:
        self._following = self.query_one(TranscriptTail).toggle_follow()
        self._update_transcript_title()

    def action_open_triage(self) -> None:
        self.app.push_screen(SpecTriageScreen())


class SpecTriageScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    COLUMNS = ("severity", "kind", "location", "summary")
    #: The location column is shortened past this many cells.
    LOCATION_CELLS = 24

    def compose(self) -> ComposeResult:
        yield ContextBar("spec triage", "the architect's red-team findings")
        yield Static(id="triage-banner")
        yield Static("spec issues", id="triage-title")
        yield DataTable(id="triage-table")
        yield Static(id="triage-detail")
        yield Footer()

    @property
    def ready(self) -> bool:
        return next(iter(self.query(DataTable)), None) is not None

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = False
        store = getattr(self.app, "store", None)
        if store is not None:
            self._refresh(store.state)

    def on_state_changed(self, message: StateChanged) -> None:
        self._refresh(message.state)

    def on_resize(self) -> None:
        store = getattr(self.app, "store", None)
        if store is not None:
            self._refresh(store.state)

    def _refresh(self, state: RunState) -> None:
        if not self.ready:
            return
        self._issues = sorted(
            state.spec_issues,
            key=lambda i: _SEVERITY_ORDER.get(i.get("severity", ""), 9),
        )
        banner = self.query_one("#triage-banner", Static)
        architect = state.components.get(architect_component_id(state))
        halted = (
            bool(state.spec_issue_counts.get("blocker"))
            and architect is not None
            and architect.status == "failed"
        )
        if halted:
            artifact = next(
                (a.get("path", "") for a in state.artifacts if a.get("label") == "spec_issues"),
                "scripts/kstrl/spec-issues.json",
            )
            banner.display = True
            banner.update(
                Text(
                    f"✗ decompose halted - resolve the spec and re-run · {artifact}",
                    style=f"bold {theme.ERROR}",
                )
            )
        else:
            banner.display = False
        table = self.query_one(DataTable)
        # Columns too: a column keeps the width of its widest past cell,
        # so a narrower terminal would still get the old widths.
        table.clear(columns=True)
        for column in self.COLUMNS:
            table.add_column(column, key=column)
        location_cells, summary_cells = _triage_widths(
            self._issues, self.size.width, self.LOCATION_CELLS
        )
        for issue in self._issues:
            table.add_row(*_triage_row(issue, summary_cells, location_cells))
        if self._issues:
            self._show_detail(0)
        else:
            self.query_one("#triage-detail", Static).update(
                Text("no spec issues recorded", style=theme.MUTED),
            )

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        if event.cursor_row is not None and event.cursor_row >= 0:
            self._show_detail(event.cursor_row)

    def _show_detail(self, index: int) -> None:
        issues = getattr(self, "_issues", [])
        if not issues or index >= len(issues):
            return
        issue = issues[index]
        detail = Text()
        severity = issue.get("severity", "")
        detail.append(
            f"[{severity}] ",
            style=_SEVERITY_STYLES.get(severity, theme.MUTED),
        )
        detail.append(issue.get("summary", ""), style="bold")
        if issue.get("location"):
            detail.append(f"\nat {issue['location']}", style=theme.MUTED)
        if issue.get("suggestion"):
            detail.append("\nsuggestion  ", style=f"bold {theme.ACCENT}")
            detail.append(issue["suggestion"])
        self.query_one("#triage-detail", Static).update(detail)
