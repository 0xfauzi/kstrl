"""Home screen: project identity, the run browser, and commands (D1).

The `ks` no-args landing surface. Everything renders from disk
discovery (tui.runs) and the project's own files; opening a run
delegates to the app's open_run, which builds an observe context and
pushes the kind-appropriate stack. Returning here (escape/q) tears
that context down via on_screen_resume - no matter which path popped.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, OptionList, Static
from textual.widgets.option_list import Option

from kstrl.config import resolve_config_file
from kstrl.tui import theme
from kstrl.tui.home_data import (
    HomeStats,
    RunSummary,
    SummaryCache,
    gather_stats,
)
from kstrl.tui.messages import SummariesReady
from kstrl.tui.runs import RunRef, discover_runs
from kstrl.tui.widgets.cost_meter import format_tokens
from kstrl.tui.widgets.run_table import RunTable

if TYPE_CHECKING:
    pass

HOME_POLL_INTERVAL = 2.0
HOME_RUN_LIMIT = 15


@dataclass(frozen=True)
class HomeCommand:
    command_id: str
    title: str
    description: str


# The launcher grows with the wave: D3 config, D4 evolve, D5 init,
# D6 launch forms all append here.
HOME_COMMANDS: list[HomeCommand] = [
    HomeCommand(
        "dash", "dashboard",
        "open the newest run (enter on a row opens that run)",
    ),
    HomeCommand(
        "config", "config",
        "resolved configuration with per-value sources",
    ),
    HomeCommand(
        "evolve", "evolve",
        "harness proposals, failure patterns, experiment trends",
    ),
    HomeCommand(
        "init", "init",
        "scaffold kstrl into a project (wizard)",
    ),
]


def _git_branch(root_dir: Path) -> str:
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root_dir, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _project_name(root_dir: Path) -> str:
    manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
    if manifest_path.exists():
        try:
            from kstrl.manifest import Manifest

            return Manifest.load(manifest_path).project_name
        except (OSError, ValueError):
            pass
    return root_dir.name


def _masthead(root_dir: Path, branch: str, project: str) -> Text:
    text = Text()
    text.append(" ◍ kstrl ", style=f"bold {theme.BACKGROUND} on {theme.ACCENT}")
    text.append("  ")
    text.append(project or "(no project)", style="bold")
    if branch:
        text.append(f"  {branch}", style=theme.STEEL)
    toml_path = resolve_config_file(root_dir)
    if toml_path.exists():
        text.append(f"  {toml_path.name} ✓", style=theme.MUTED)
    else:
        text.append("  no kstrl.toml - run ks init", style=theme.WARNING)
    return text


def _stats_line(stats: HomeStats) -> Text:
    text = Text()
    last = stats.last
    if last is None:
        text.append("no finished runs yet", style=theme.MUTED)
    else:
        glyphs = {
            "live": ("●", theme.ACCENT),
            "done": ("✓", theme.SUCCESS),
            "failed": ("✗", theme.ERROR),
            "stale": (theme.EMPTY_CELL, theme.MUTED),
        }
        glyph, color = glyphs.get(last.outcome, (theme.EMPTY_CELL, theme.MUTED))
        text.append("last run ", style=theme.MUTED)
        text.append(f"{glyph} {last.outcome}", style=f"bold {color}")
        text.append(
            f" {last.components_done}/{last.components_total}",
            style="bold",
        )
        marker = "+" if last.tokens_lower_bound else ""
        if last.total_tokens:
            text.append(" · ", style=theme.MUTED)
            text.append(f"{format_tokens(last.total_tokens)}{marker} tok")
        if last.cost_usd:
            text.append(" · ", style=theme.MUTED)
            text.append(f"${last.cost_usd:.2f}{marker}")
    if stats.pending_proposals:
        text.append("   ", style=theme.MUTED)
        text.append(
            f"▲ {stats.pending_proposals} proposal(s) pending",
            style=theme.WARNING,
        )
    return text


class HomeScreen(Screen[None]):
    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._refs: dict[str, RunRef] = {}
        self._summaries: dict[str, RunSummary] = {}
        self._cache = SummaryCache()
        self._summarizing = False

    def compose(self) -> ComposeResult:
        yield Static(id="home-masthead")
        yield Static(id="home-stats")
        yield Static("runs", id="home-runs-title")
        yield RunTable(id="home-runs")
        yield Static("commands", id="home-commands-title")
        yield OptionList(id="home-commands")
        yield Footer()

    @property
    def ready(self) -> bool:
        return next(iter(self.query(RunTable)), None) is not None

    def on_mount(self) -> None:
        root_dir = self._root_dir()
        self.query_one("#home-masthead", Static).update(_masthead(
            root_dir, _git_branch(root_dir), _project_name(root_dir),
        ))
        commands = self.query_one(OptionList)
        for command in HOME_COMMANDS:
            label = Text(command.title, style="bold")
            label.append(f"  {command.description}", style=theme.MUTED)
            commands.add_option(Option(label, id=command.command_id))
        self.refresh_runs()
        self.set_interval(HOME_POLL_INTERVAL, self.refresh_runs)

    def _root_dir(self) -> Path:
        return getattr(self.app, "root_dir", Path.cwd())

    def refresh_runs(self) -> None:
        if not self.ready:
            return
        root_dir = self._root_dir()
        refs = discover_runs(root_dir)[:HOME_RUN_LIMIT]
        self._refs = {ref.run_id: ref for ref in refs}
        self.query_one(RunTable).update_runs(refs, self._summaries)
        title = Text("runs", style=f"bold {theme.MUTED}")
        if not refs:
            title.append("  none yet - run a command below",
                         style=theme.MUTED)
        self.query_one("#home-runs-title", Static).update(title)
        if not self._summarizing:
            # Folding every listed run is file IO + reducer work: off
            # the UI thread, with "·" cells until the message lands.
            self._summarizing = True
            self.run_worker(
                lambda: self._compute_summaries(list(refs), root_dir),
                thread=True,
            )

    def _compute_summaries(
        self, refs: list[RunRef], root_dir: Path,
    ) -> None:
        try:
            summaries = self._cache.refresh(refs)
            stats = gather_stats(
                root_dir, summaries,
                refs[0].run_id if refs else "",
            )
        except Exception:  # noqa: BLE001 - a broken run dir must not kill home
            summaries, stats = {}, HomeStats(None, 0)
        self.post_message(SummariesReady(summaries, stats))

    def on_summaries_ready(self, message: SummariesReady) -> None:
        self._summarizing = False
        self._summaries = message.summaries
        if self.ready:
            self.query_one(RunTable).update_runs(
                list(self._refs.values()), self._summaries,
            )
            self.query_one("#home-stats", Static).update(
                _stats_line(message.stats),
            )

    def on_screen_resume(self) -> None:
        # Whatever path popped back here, the observed run is done
        # with: tear its context down and re-discover.
        close_run = getattr(self.app, "close_run", None)
        if close_run is not None:
            close_run()
        self.refresh_runs()

    def action_refresh(self) -> None:
        self.refresh_runs()

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected,
    ) -> None:
        event.stop()
        if event.row_key.value is None:
            return
        ref = self._refs.get(str(event.row_key.value))
        if ref is None:
            return
        open_run = getattr(self.app, "open_run", None)
        if open_run is not None:
            open_run(ref)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected,
    ) -> None:
        event.stop()
        if event.option_id == "dash":
            refs = list(self._refs.values())
            if refs:
                open_run = getattr(self.app, "open_run", None)
                if open_run is not None:
                    open_run(refs[0])
            else:
                self.app.notify("no runs yet", severity="warning")
        elif event.option_id == "config":
            from kstrl.tui.screens.config import ConfigScreen

            self.app.push_screen(ConfigScreen())
        elif event.option_id == "evolve":
            from kstrl.tui.screens.evolve import EvolveScreen

            self.app.push_screen(EvolveScreen())
        elif event.option_id == "init":
            from kstrl.tui.screens.init_wizard import InitWizardScreen

            self.app.push_screen(InitWizardScreen())
