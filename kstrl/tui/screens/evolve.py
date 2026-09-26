"""Evolve screen: failure patterns and experiment trends (D4).

Two tabs over the evolution layer's on-disk records:
- patterns: get_cross_run_patterns over the journal.
- trends: the last experiments.tsv rows with retry-rate bars and
  R3.1 lower-bound markers on token/cost cells.

The proposals tab and its apply modal went with the proposal generator
(#217 Slice 1). The screen reads; it writes nothing.

#433 F12: a summary line above the tabs says what the patterns tab found
and carries the learning-readiness lines ``ks evolve`` prints (built by
``kstrl.evolve_report``, read off the UI thread). An empty tab says so
in one sentence instead of showing a bare table header.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane

from kstrl.evolution import EvolutionConfig, EvolutionJournal, FailurePattern
from kstrl.tui import theme
from kstrl.tui.widgets.config_problem import ConfigProblemBanner
from kstrl.tui.widgets.context_bar import ContextBar

TREND_ROWS = 14
_BAR_BLOCKS = "▁▂▃▄▅▆▇"

NO_PATTERNS = (
    "No recurring failure patterns across recent runs. "
    "Run more factory sessions to accumulate data."
)
NO_TRENDS = "No experiments recorded yet. Run `ks factory` first."


def readiness_block(summary: Text, lines: list[str]) -> Group:
    """The patterns summary, then the readiness lines with a hanging indent.

    ``ks evolve`` indents each line two spaces; a line that wraps keeps
    that indent here instead of running back to column 0.
    """
    parts: list[RenderableType] = [summary, Text("learning readiness", style="bold")]
    parts.extend(Padding(Text(line.strip(), style=theme.MUTED), (0, 0, 0, 2)) for line in lines)
    return Group(*parts)


class ReadinessReady(Message):
    """The readiness lines, computed on a worker thread."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__()
        self.lines = lines


def patterns_summary(patterns: list[FailurePattern], config: EvolutionConfig) -> Text:
    """One sentence on what the patterns tab holds."""
    text = Text()
    if patterns:
        text.append(f"{len(patterns)} recurring failure pattern(s)", style=f"bold {theme.WARNING}")
    else:
        text.append("No recurring failure patterns", style="bold")
    text.append(
        f" in the last {config.lookback_runs} runs"
        f" (a pattern recurs in {config.min_pattern_frequency} or more runs)",
        style=theme.MUTED,
    )
    return text


def retry_bar(rate: float) -> str:
    """A one-cell bar for a 0..1 retry rate; empty stays empty."""
    if not math.isfinite(rate) or rate <= 0:
        return theme.EMPTY_CELL
    index = min(len(_BAR_BLOCKS) - 1, int(rate * len(_BAR_BLOCKS)))
    return _BAR_BLOCKS[index]


class EvolveScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "reload", "Reload", show=False),
    ]

    PATTERN_COLUMNS = ("check", "code", "runs", "components", "category")
    TREND_COLUMNS = ("run", "done", "failed", "retry", "tok", "cost")

    def compose(self) -> ComposeResult:
        yield ContextBar("evolve", "failure patterns and trends")
        # Above the tabs, not inside one: an unreadable [evolution]
        # section empties patterns AND trends, so it belongs to neither.
        yield ConfigProblemBanner()
        # Also above the tabs, and for the same reason: a repaired
        # journal write is about the file both journal-backed tabs read.
        # A separate widget rather than a second use of the banner
        # above, which prefixes "configuration unreadable": the config
        # is fine here and the journal was torn (#333).
        yield Static(id="evolve-repairs")
        yield Static(id="evolve-summary")
        with TabbedContent(id="evolve-tabs"):
            with TabPane("patterns", id="tab-patterns"):
                yield Static(NO_PATTERNS, id="patterns-empty", classes="empty-state")
                yield DataTable(id="patterns-table")
            with TabPane("trends", id="tab-trends"):
                yield Static(NO_TRENDS, id="trends-empty", classes="empty-state")
                yield DataTable(id="trends-table")
        yield Footer()

    @property
    def ready(self) -> bool:
        return next(iter(self.query(TabbedContent)), None) is not None

    def on_mount(self) -> None:
        for table_id, columns in (
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
        root = getattr(self.app, "root_dir", None)
        return root if root is not None else Path.cwd()

    def reload(self) -> None:
        self._load_patterns_and_trends(self._root_dir())

    def _load_patterns_and_trends(self, root_dir: Path) -> None:
        """Both journal-backed tabs, or the reason neither can be shown.

        Guarded because this screen is reachable from the home shell,
        which is not a click command and so never runs the entry check
        that would have stopped `ks evolve` (#289). Degrading to two
        empty tables would be worse than the traceback it replaces:
        "no patterns yet" is a real state on this screen.
        """
        config = self.query_one(ConfigProblemBanner).load(EvolutionConfig.load, root_dir)
        patterns_table = self.query_one("#patterns-table", DataTable)
        patterns_table.clear()
        trends_table = self.query_one("#trends-table", DataTable)
        trends_table.clear()
        summary = self.query_one("#evolve-summary", Static)
        summary.display = config is not None
        if config is None:
            self._show_repairs(None)
            # The banner says why; "no patterns" would be a claim about a
            # journal nobody read.
            self._show_empty_states(read=False)
            return
        journal = EvolutionJournal(config)
        self._show_repairs(journal)
        patterns = journal.get_cross_run_patterns(lookback_runs=config.lookback_runs)
        for pattern in patterns:
            patterns_table.add_row(
                Text(pattern.check_name, style="bold"),
                Text(pattern.error_signature),
                Text(str(pattern.frequency), justify="right"),
                Text(str(len(pattern.affected_components)), justify="right"),
                Text(pattern.category, style=theme.MUTED),
            )
        for row in journal.get_experiment_trends(last_n=TREND_ROWS):
            trends_table.add_row(*self._trend_cells(row))
        self._show_empty_states()
        self._summary = patterns_summary(patterns, config)
        self.query_one("#evolve-summary", Static).update(self._summary)
        self.run_worker(
            lambda: self._readiness_worker(journal, config, patterns, root_dir),
            thread=True,
            group="evolve-readiness",
        )

    def _show_empty_states(self, *, read: bool = True) -> None:
        for name in ("patterns", "trends"):
            table = self.query_one(f"#{name}-table", DataTable)
            table.display = table.row_count > 0
            self.query_one(f"#{name}-empty", Static).display = read and table.row_count == 0

    def _readiness_worker(
        self,
        journal: EvolutionJournal,
        config: EvolutionConfig,
        patterns: list[FailurePattern],
        root_dir: Path,
    ) -> None:
        from kstrl.evolve_report import readiness_lines

        try:
            lines = readiness_lines(journal, config, patterns, root_dir)
        except Exception as exc:  # noqa: BLE001 - a broken read must not take the screen down
            lines = [f"  learning readiness not measured: {type(exc).__name__}: {exc}"]
        self.post_message(ReadinessReady(lines))

    def on_readiness_ready(self, message: ReadinessReady) -> None:
        summary = self.query_one("#evolve-summary", Static)
        summary.update(readiness_block(self._summary, message.lines))

    def _show_repairs(self, journal: EvolutionJournal | None) -> None:
        """The count of repaired journal writes, or nothing at zero (#333).

        #312's argument for writing a durable ``journal_repair`` row at
        all was that under the TUI the logger warning goes to
        ``orchestrator.log`` where nobody is looking. That argument names
        the TUI operator, and until now this screen was the one surface
        that built an ``EvolutionJournal``, read the journal for its
        patterns tab, and said nothing about the rows.

        Silent at zero, which is the same choice ``ks evolve --status``
        makes and for the same reason: a line that prints on every
        healthy journal is a line an operator learns to skip. The facts
        are the CLI's facts, because they are facts about the file
        rather than about a surface, and since #352 round 2 they are one
        STRING as well: ``EvolutionJournal.repair_summary``. What is NOT
        shared is the helper, because ``cli._echo_journal_repairs``
        writes through ``UI`` in the click module, so this is a second
        renderer of one measurement, not a second measurement.

        Takes the journal rather than a count so the count and the path
        printed beside it cannot come from two different journals. None
        means the config did not resolve, which the banner above has
        already said; this line hides rather than showing a stale count
        from before a ``reload``.

        COST, and it is a whole extra read of the journal rather than
        nothing: ``repair_summary`` goes through ``_read_all_entries``
        while the patterns tab beside it goes through
        ``_read_journal_entries``, so this screen reads the file twice
        per load and per ``r``, on the Textual event loop. Measured
        here, 20 calls each: 0.016 ms at 1 line, 0.097 ms at 100,
        1.12 ms at 989 (194 KiB, the largest real journal #333 found),
        and on a 10,000-line journal 9.3 ms for the repair read against
        9.2 ms for the patterns read, so the screen pays 18.5 ms where
        one read would cost 9.3. Linear in the file.

        The lookback window is NOT why it is a second read, and that
        sentence used to be here and was wrong (#352 round 2, F5).
        ``get_cross_run_patterns`` goes through ``_read_journal_entries``,
        which calls ``_read_all_entries()`` and applies its window in
        MEMORY. The two figures being within a few percent of each other
        is that window costing nothing at the read.

        Kept as a second read anyway, and this is the real reason: the
        two are methods on the same journal called from two places on
        this screen, so folding them means either passing the entries
        into ``_show_repairs`` or giving ``EvolutionJournal`` a cached
        read. The first makes this method's own argument a list somebody
        else read, which is the thing the paragraph above rejects for
        the count and the path. The second changes the surface every
        other caller of the journal sees, for a screen that reloads on
        one keypress. Neither is worth 9.3 ms at a file size no journal
        here has reached; the cost is written down so the next reader
        decides on the number rather than on the sentence.
        """
        line = self.query_one("#evolve-repairs", Static)
        summary = None if journal is None else journal.repair_summary()
        if summary is None:
            line.display = False
            return
        line.display = True
        line.update(Text(f"▲ {summary}", style=theme.WARNING))

    @staticmethod
    def _trend_cells(row: dict[str, Any]) -> tuple[Text | str, ...]:
        def _num(key: str) -> Text:
            value = str(row.get(key, "") or "")
            if not value:
                return Text(theme.EMPTY_CELL, style=theme.MUTED, justify="right")
            return Text(value, justify="right")

        run_id = str(row.get("run_id", ""))
        short = run_id.rsplit("-", 1)[-1] if run_id else theme.EMPTY_CELL
        try:
            rate = float(row.get("retry_rate", "") or 0)
        except ValueError:
            rate = 0.0
        if not math.isfinite(rate):
            rate = 0.0
        # Unreported calls make token/cost totals lower bounds (R3.1).
        try:
            unreported = int(float(row.get("unreported_calls", "") or 0))
        except (OverflowError, ValueError):
            unreported = 0
        marker = "+" if unreported else ""
        tokens = str(row.get("total_tokens", "") or "")
        cost = str(row.get("total_cost_usd", "") or "")
        return (
            Text(short, style="bold"),
            _num("completed"),
            _num("failed"),
            Text(f"{retry_bar(rate)} {rate:.2f}" if rate else theme.EMPTY_CELL, justify="right"),
            Text(f"{tokens}{marker}", justify="right")
            if tokens
            else Text(theme.EMPTY_CELL, style=theme.MUTED, justify="right"),
            Text(f"${cost}{marker}", justify="right")
            if cost
            else Text(theme.EMPTY_CELL, style=theme.MUTED, justify="right"),
        )

    def action_reload(self) -> None:
        self.reload()
