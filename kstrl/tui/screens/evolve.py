"""Evolve screen: proposals, failure patterns, experiment trends (D4).

Three tabs over the evolution layer's on-disk records:
- proposals: master-detail over .kstrl/proposals/prop-*.md with the
  REAL apply path (B1's engine). `a` opens the confirm modal; the
  modal IS the confirmation, so apply_proposal runs with an
  always-yes seam. Non-convention proposals keep the honest manual
  message - no false "applied" claims (R6.3).
- patterns: get_cross_run_patterns over the journal.
- trends: the last experiments.tsv rows with retry-rate bars and
  R3.1 lower-bound markers on token/cost cells.

Propose-from-TUI is deliberately absent in v1: `ks evolve` remains
the generator; this screen reads, triages, and applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane

from kstrl.evolution import EvolutionConfig, EvolutionJournal
from kstrl.interaction import PromptKind, PromptRequest
from kstrl.proposals import Proposal, apply_proposal, list_proposals
from kstrl.tui import theme
from kstrl.tui.screens.options import OptionsModal

if TYPE_CHECKING:
    from pathlib import Path

TREND_ROWS = 14
_BAR_BLOCKS = "▁▂▃▄▅▆▇"


def retry_bar(rate: float) -> str:
    """A one-cell bar for a 0..1 retry rate; empty stays empty."""
    if rate <= 0:
        return theme.EMPTY_CELL
    index = min(len(_BAR_BLOCKS) - 1, int(rate * len(_BAR_BLOCKS)))
    return _BAR_BLOCKS[index]


def _proposal_detail(proposal: Proposal) -> Text:
    text = Text()
    text.append(f"{proposal.display_id} ", style=f"bold {theme.ACCENT}")
    text.append(proposal.title, style="bold")
    text.append(f"\n{proposal.path}", style=theme.MUTED)
    text.append("\ntype ", style=theme.MUTED)
    text.append(proposal.type or "?")
    text.append("  target ", style=theme.MUTED)
    text.append(proposal.target or "?")
    if proposal.convention:
        text.append("\n\nsuggested change\n", style=f"bold {theme.ACCENT}")
        text.append(proposal.convention)
    if proposal.applied:
        text.append(f"\n\n✓ applied {proposal.applied}",
                    style=f"bold {theme.SUCCESS}")
    elif proposal.is_convention:
        text.append("\n\n(a) apply - appends to CLAUDE.md Agent Learnings",
                    style=theme.MUTED)
    else:
        text.append(
            "\n\nautomated apply only covers convention-type proposals "
            "(target claude_md); review the file and apply manually",
            style=theme.WARNING,
        )
    return text


class EvolveScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "apply_selected", "Apply"),
        Binding("r", "reload", "Reload", show=False),
    ]

    PROPOSAL_COLUMNS = ("id", "title", "type", "target", "applied")
    PATTERN_COLUMNS = ("check", "code", "runs", "components", "category")
    TREND_COLUMNS = ("run", "done", "failed", "retry", "tok", "cost")

    def __init__(self) -> None:
        super().__init__()
        self._proposals: list[Proposal] = []

    def compose(self) -> ComposeResult:
        with TabbedContent(id="evolve-tabs"):
            with TabPane("proposals", id="tab-proposals"):
                with Horizontal(id="proposals-split"):
                    yield DataTable(id="proposals-table")
                    yield Static(id="proposal-detail")
            with TabPane("patterns", id="tab-patterns"):
                yield DataTable(id="patterns-table")
            with TabPane("trends", id="tab-trends"):
                yield DataTable(id="trends-table")
        yield Footer()

    @property
    def ready(self) -> bool:
        return next(iter(self.query(TabbedContent)), None) is not None

    def on_mount(self) -> None:
        for table_id, columns in (
            ("#proposals-table", self.PROPOSAL_COLUMNS),
            ("#patterns-table", self.PATTERN_COLUMNS),
            ("#trends-table", self.TREND_COLUMNS),
        ):
            table = self.query_one(table_id, DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = False
            for column in columns:
                table.add_column(column, key=column)
        self.reload()

    def _root_dir(self) -> Path:
        from pathlib import Path as _Path

        root = getattr(self.app, "root_dir", None)
        return root if root is not None else _Path.cwd()

    def reload(self) -> None:
        root_dir = self._root_dir()
        self._load_proposals(root_dir)
        self._load_patterns_and_trends(root_dir)

    def _load_proposals(self, root_dir: Path) -> None:
        self._proposals = list_proposals(root_dir / ".kstrl" / "proposals")
        table = self.query_one("#proposals-table", DataTable)
        table.clear()
        for proposal in self._proposals:
            applied = (
                Text("✓", style=f"bold {theme.SUCCESS}")
                if proposal.applied
                else Text(theme.EMPTY_CELL, style=theme.MUTED)
            )
            table.add_row(
                Text(proposal.display_id, style="bold"),
                proposal.title,
                proposal.type or Text(theme.EMPTY_CELL, style=theme.MUTED),
                proposal.target or Text(theme.EMPTY_CELL, style=theme.MUTED),
                applied,
                key=proposal.path.name,
            )
        detail = self.query_one("#proposal-detail", Static)
        if self._proposals:
            self._show_detail(0)
        else:
            detail.update(Text(
                "no proposals yet - run `ks evolve` after a few factory "
                "runs to generate them",
                style=theme.MUTED,
            ))

    def _load_patterns_and_trends(self, root_dir: Path) -> None:
        journal = EvolutionJournal(EvolutionConfig.load(root_dir))
        patterns_table = self.query_one("#patterns-table", DataTable)
        patterns_table.clear()
        for pattern in journal.get_cross_run_patterns():
            patterns_table.add_row(
                Text(pattern.check_name, style="bold"),
                pattern.error_signature,
                Text(str(pattern.frequency), justify="right"),
                Text(str(len(pattern.affected_components)), justify="right"),
                Text(pattern.category, style=theme.MUTED),
            )
        trends_table = self.query_one("#trends-table", DataTable)
        trends_table.clear()
        for row in journal.get_experiment_trends(last_n=TREND_ROWS):
            trends_table.add_row(*self._trend_cells(row))

    @staticmethod
    def _trend_cells(row: dict[str, Any]) -> tuple[Text | str, ...]:
        def _num(key: str) -> Text:
            value = str(row.get(key, "") or "")
            if not value:
                return Text(theme.EMPTY_CELL, style=theme.MUTED,
                            justify="right")
            return Text(value, justify="right")

        run_id = str(row.get("run_id", ""))
        short = run_id.rsplit("-", 1)[-1] if run_id else theme.EMPTY_CELL
        try:
            rate = float(row.get("retry_rate", "") or 0)
        except ValueError:
            rate = 0.0
        # Unreported calls make token/cost totals lower bounds (R3.1).
        try:
            unreported = int(float(row.get("unreported_calls", "") or 0))
        except ValueError:
            unreported = 0
        marker = "+" if unreported else ""
        tokens = str(row.get("total_tokens", "") or "")
        cost = str(row.get("total_cost_usd", "") or "")
        return (
            Text(short, style="bold"),
            _num("completed"),
            _num("failed"),
            Text(f"{retry_bar(rate)} {rate:.2f}" if rate else theme.EMPTY_CELL,
                 justify="right"),
            Text(f"{tokens}{marker}", justify="right") if tokens
            else Text(theme.EMPTY_CELL, style=theme.MUTED, justify="right"),
            Text(f"${cost}{marker}", justify="right") if cost
            else Text(theme.EMPTY_CELL, style=theme.MUTED, justify="right"),
        )

    # -- proposals master-detail + apply ------------------------------------

    def _selected_proposal(self) -> Proposal | None:
        table = self.query_one("#proposals-table", DataTable)
        if not table.row_count or table.cursor_row is None:
            return None
        index = table.cursor_row
        if 0 <= index < len(self._proposals):
            return self._proposals[index]
        return None

    def _show_detail(self, index: int) -> None:
        if 0 <= index < len(self._proposals):
            self.query_one("#proposal-detail", Static).update(
                _proposal_detail(self._proposals[index]),
            )

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted,
    ) -> None:
        if event.data_table.id != "proposals-table":
            return
        if event.cursor_row is not None and event.cursor_row >= 0:
            self._show_detail(event.cursor_row)

    def action_reload(self) -> None:
        self.reload()

    def action_apply_selected(self) -> None:
        proposal = self._selected_proposal()
        if proposal is None:
            return
        if proposal.applied:
            self.app.notify(
                f"{proposal.display_id} already applied at "
                f"{proposal.applied}",
            )
            return
        if not proposal.is_convention:
            self.app.notify(
                "automated apply only covers convention-type proposals "
                f"(target claude_md); review {proposal.path} and apply "
                "manually",
                severity="warning",
            )
            return

        def _resolved(choice: int | None) -> None:
            if choice != 0:
                return
            # The modal WAS the confirmation.
            outcome = apply_proposal(
                proposal, self._root_dir(), confirm=lambda _: True,
            )
            self.app.notify(
                outcome.message,
                severity="information" if outcome.status == "applied"
                else "error",
            )
            self._load_proposals(self._root_dir())

        self.app.push_screen(
            OptionsModal(PromptRequest(
                kind=PromptKind.CONFIRM,
                header=(
                    f"{proposal.display_id}: append this convention to "
                    f"CLAUDE.md Agent Learnings?  \"{proposal.convention}\""
                ),
                options=("Apply", "Cancel"),
                default=1,
            )),
            _resolved,
        )
