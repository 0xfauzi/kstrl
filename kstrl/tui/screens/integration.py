"""The merged-feature integration review of a run (#433 F9, M4, Q7).

Opened from the run overview's pinned integration row with ``i``. Three
parts, each from ``integration_view``:

- every round the run recorded, with its outcome and reason;
- each criterion's verdict (IC1 to IC5) from the newest round that ran;
- each IF finding with its one current disposition, joined by id from
  ``.kstrl/integration/state.json``.

The detail pane under the tables holds the selected row's full text,
which the table cells shorten.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from kstrl.tui import theme
from kstrl.tui.delivery import DISPOSITION_STYLE, OUTCOME_WORDS, VERDICT_STYLE
from kstrl.tui.integration_view import (
    CriterionVerdict,
    FindingDisposition,
    IntegrationReview,
)
from kstrl.tui.widgets.context_bar import ContextBar

_KIND_WORDS = {
    "criterion": "criterion",
    "concern": "concern",
    "register": "decision register",
    "test": "test failure",
}


def _fit(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def rounds_text(review: IntegrationReview, width: int = 0) -> Text:
    """One line per round; with ``width``, each reason is cut to fit it."""
    text = Text()
    for index, entry in enumerate(review.rounds):
        if index:
            text.append("\n")
        word, color = OUTCOME_WORDS.get(entry.outcome, (entry.outcome, theme.WARNING))
        head = f"round {entry.number} "
        text.append(head, style=theme.MUTED)
        text.append(word, style=f"bold {color}")
        if entry.reason:
            room = width - len(head) - len(word) - 2 if width else len(entry.reason)
            text.append(f"  {_fit(entry.reason, max(8, room))}", style=theme.MUTED)
    if review.state_problem:
        text.append(f"\ndispositions unknown: {review.state_problem}", style=theme.WARNING)
    return text


def _source(finding: FindingDisposition) -> str:
    if finding.story_id:
        return finding.story_id
    return _KIND_WORDS.get(finding.kind, finding.kind or "?")


def finding_detail(finding: FindingDisposition) -> Text:
    text = Text()
    text.append(f"{finding.finding_id}  ", style="bold")
    style = DISPOSITION_STYLE.get(finding.disposition, theme.WARNING)
    text.append(finding.disposition, style=f"bold {style}")
    if finding.detail:
        text.append(f": {finding.detail}", style=style)
    text.append(f"\nfrom {_source(finding)} ({_KIND_WORDS.get(finding.kind, finding.kind)})")
    text.append("\n\n")
    text.append(finding.text or "(no text recorded)")
    if finding.locations:
        text.append("\n\nlocations  ", style=theme.MUTED)
        text.append(", ".join(finding.locations))
    return text


def criterion_detail(verdict: CriterionVerdict) -> Text:
    glyph, color = VERDICT_STYLE.get(verdict.verdict, ("?", theme.WARNING))
    text = Text()
    text.append(f"{verdict.story_id}  {verdict.title}  ", style="bold")
    text.append(f"{glyph} {verdict.verdict}", style=f"bold {color}")
    text.append("\n\n")
    text.append(verdict.explanation or "(no explanation recorded)")
    return text


class IntegrationScreen(Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, review: IntegrationReview, run_id: str) -> None:
        super().__init__()
        self.review = review
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        yield ContextBar("integration review", f"run {theme.short_run_id(self.run_id)}")
        yield Static(id="integration-rounds")
        yield Static(id="integration-criteria-title", classes="panel-title")
        yield DataTable(id="integration-criteria")
        yield Static(id="integration-findings-title", classes="panel-title")
        yield DataTable(id="integration-findings")
        yield Static(id="integration-detail")
        yield Footer()

    def on_resize(self) -> None:
        # Below 30 rows the gaps go, so the detail pane keeps some height.
        self.set_class(self.size.height < 30, "tiny")
        rounds = rounds_text(self.review, max(40, self.size.width) - 4)
        self.query_one("#integration-rounds", Static).update(rounds)

    def on_mount(self) -> None:
        rounds = rounds_text(self.review, self._width() - 4)
        self.query_one("#integration-rounds", Static).update(rounds)
        self._fill_criteria()
        self._fill_findings()
        first = self.query_one("#integration-findings", DataTable)
        if first.row_count:
            first.focus()
            self._show_finding(0)
        elif self.review.criteria:
            self.query_one("#integration-criteria", DataTable).focus()
            self._show_criterion(0)

    def _width(self) -> int:
        return max(40, self.app.size.width)

    def _fill_criteria(self) -> None:
        table: DataTable[Text | str] = self.query_one("#integration-criteria", DataTable)
        table.cursor_type = "row"
        title = Text("criteria", style=f"bold {theme.MUTED}")
        if not self.review.criteria:
            title.append("  no round recorded a verdict", style=theme.MUTED)
            table.display = False
        self.query_one("#integration-criteria-title", Static).update(title)
        table.add_columns("", "criterion", "verdict", "why")
        # id 3, title 26, verdict 6, each padded 1 a side, the table's own
        # padding and its scrollbar: 48 cells that are not the "why".
        room = self._width() - 48
        for verdict in self.review.criteria:
            glyph, color = VERDICT_STYLE.get(verdict.verdict, ("?", theme.WARNING))
            table.add_row(
                Text(verdict.story_id, style="bold"),
                _fit(verdict.title, 26),
                Text(f"{glyph} {verdict.verdict}", style=color),
                Text(_fit(verdict.explanation, room), style=theme.MUTED),
                key=verdict.story_id,
            )

    def _fill_findings(self) -> None:
        table: DataTable[Text | str] = self.query_one("#integration-findings", DataTable)
        table.cursor_type = "row"
        title = Text("findings", style=f"bold {theme.MUTED}")
        if not self.review.findings:
            title.append("  the review opened none", style=theme.MUTED)
            table.display = False
        self.query_one("#integration-findings-title", Static).update(title)
        table.add_columns("", "from", "disposition", "detail")
        room = self._width() - 44
        for finding in self.review.findings:
            style = DISPOSITION_STYLE.get(finding.disposition, theme.WARNING)
            table.add_row(
                Text(finding.finding_id, style="bold"),
                _source(finding),
                Text(finding.disposition, style=f"bold {style}"),
                Text(_fit(finding.detail, room), style=theme.MUTED),
                key=finding.finding_id,
            )

    def _show_finding(self, row: int) -> None:
        if 0 <= row < len(self.review.findings):
            detail = finding_detail(self.review.findings[row])
            self.query_one("#integration-detail", Static).update(detail)

    def _show_criterion(self, row: int) -> None:
        if 0 <= row < len(self.review.criteria):
            detail = criterion_detail(self.review.criteria[row])
            self.query_one("#integration-detail", Static).update(detail)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row is None or not event.data_table.has_focus:
            return
        if event.data_table.id == "integration-findings":
            self._show_finding(event.cursor_row)
        else:
            self._show_criterion(event.cursor_row)

    def on_descendant_focus(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable) and focused.cursor_row is not None:
            if focused.id == "integration-findings":
                self._show_finding(focused.cursor_row)
            else:
                self._show_criterion(focused.cursor_row)
