"""Decompose run screens: the live architect view + spec triage (C5).

DecomposeScreen is the decompose-kind counterpart of ComponentScreen:
attempt strip, the forming DAG (rows appear as the RunPlan folds),
the architect's streaming transcript, and a spec-issue strip that
deepens into SpecTriageScreen. Everything renders from the folded
RunState - files are the record.

SpecTriageScreen is read-only in v1: blockers already halted the run
(the banner says so and points at the durable artifact); non-blockers
never gate, so there is no decision to prompt for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from kstrl.tui import theme
from kstrl.tui.messages import StateChanged
from kstrl.tui.widgets.dag_table import ARCHITECT_ID, DagTable
from kstrl.tui.widgets.header import RunHeader
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
    architect = state.components.get(ARCHITECT_ID)
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
            parts.append((
                f"{counts[severity]} {severity}",
                _SEVERITY_STYLES[severity],
            ))
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
    architect = state.components.get(ARCHITECT_ID)
    if architect is None:
        text.append("waiting for the architect...", style=theme.MUTED)
        return text
    glyph, color = theme.status_glyph(architect.status)
    text.append(f"{glyph} ", style=f"bold {color}")
    text.append("architect ", style="bold")
    text.append(architect.status, style=color)
    attempts = [
        p for p in architect.phase_history
        if p.get("phase") == "decompose"
    ]
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
    architect = state.components.get(ARCHITECT_ID)
    if architect is None or architect.status != "completed":
        text.append("✗ decompose did not complete", style=f"bold {theme.ERROR}")
        return text
    planned = [c for c in state.plan_order if c != ARCHITECT_ID]
    prds = [a for a in state.artifacts if a.get("label") == "prd"]
    manifest = next(
        (a for a in state.artifacts if a.get("label") == "manifest"), None,
    )
    text.append("✓ ", style=f"bold {theme.SUCCESS}")
    text.append(f"{len(planned)} component(s)", style="bold")
    text.append(f" · {len(prds)} PRD(s)", style=theme.MUTED)
    if manifest is not None:
        text.append(f" · manifest {manifest.get('path', '')}",
                    style=theme.MUTED)
    return text


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
        self.transcript_component = ARCHITECT_ID
        self._following = True

    def compose(self) -> ComposeResult:
        yield RunHeader(id="decompose-header")
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

    def refresh_state(
        self, state: RunState, manifest: Manifest | None,
    ) -> None:
        del manifest  # the folded plan is the source; no manifest join
        if not self.ready:
            return
        self.query_one(RunHeader).update_state(state)
        self.query_one("#attempt-strip", Static).update(_attempt_strip(state))
        self.query_one(DagTable).update_state(state)
        self.query_one("#issues-strip", Static).update(_issue_strip(state))
        summary = _summary(state)
        summary_widget = self.query_one("#decompose-summary", Static)
        summary_widget.display = summary is not None
        if summary is not None:
            summary_widget.update(summary)

    def tick_ages(self, state: RunState) -> None:
        if self.ready:
            self.query_one(RunHeader).update_state(state)

    def feed_transcript(self, lines: list[str]) -> None:
        self.query_one(TranscriptTail).feed_lines(lines)

    def _update_transcript_title(self) -> None:
        title = Text("architect transcript", style="bold")
        if self._following:
            title.append("  ● following", style=theme.ACCENT)
        else:
            title.append("  ⏸ paused", style=theme.MUTED)
        title.append("  (f toggles)", style=theme.MUTED)
        self.query_one(
            "#decompose-transcript-title", Static,
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

    COLUMNS = ("severity", "kind", "summary", "location")

    def compose(self) -> ComposeResult:
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
        for column in self.COLUMNS:
            table.add_column(column, key=column)
        store = getattr(self.app, "store", None)
        if store is not None:
            self._refresh(store.state)

    def on_state_changed(self, message: StateChanged) -> None:
        self._refresh(message.state)

    def _refresh(self, state: RunState) -> None:
        if not self.ready:
            return
        self._issues = sorted(
            state.spec_issues,
            key=lambda i: _SEVERITY_ORDER.get(i.get("severity", ""), 9),
        )
        banner = self.query_one("#triage-banner", Static)
        architect = state.components.get(ARCHITECT_ID)
        halted = (
            bool(state.spec_issue_counts.get("blocker"))
            and architect is not None and architect.status == "failed"
        )
        if halted:
            artifact = next(
                (a.get("path", "") for a in state.artifacts
                 if a.get("label") == "spec_issues"),
                "scripts/kstrl/spec-issues.json",
            )
            banner.display = True
            banner.update(Text(
                f"✗ decompose halted - resolve the spec and re-run · {artifact}",
                style=f"bold {theme.ERROR}",
            ))
        else:
            banner.display = False
        table = self.query_one(DataTable)
        table.clear()
        for issue in self._issues:
            severity = issue.get("severity", "")
            table.add_row(
                Text(severity,
                     style=_SEVERITY_STYLES.get(severity, theme.MUTED)),
                Text(issue["kind"]) if issue.get("kind")
                else Text(theme.EMPTY_CELL, style=theme.MUTED),
                Text(issue.get("summary", "")),
                Text(issue["location"]) if issue.get("location")
                else Text(theme.EMPTY_CELL, style=theme.MUTED),
            )
        if self._issues:
            self._show_detail(0)
        else:
            self.query_one("#triage-detail", Static).update(
                Text("no spec issues recorded", style=theme.MUTED),
            )

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted,
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
