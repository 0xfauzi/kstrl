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
from kstrl.tui.runs import RunRef, discover_runs
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


class HomeScreen(Screen[None]):
    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._refs: dict[str, RunRef] = {}

    def compose(self) -> ComposeResult:
        yield Static(id="home-masthead")
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
        refs = discover_runs(self._root_dir())[:HOME_RUN_LIMIT]
        self._refs = {ref.run_id: ref for ref in refs}
        self.query_one(RunTable).update_runs(refs)
        title = Text("runs", style=f"bold {theme.MUTED}")
        if not refs:
            title.append("  none yet - run a command below",
                         style=theme.MUTED)
        self.query_one("#home-runs-title", Static).update(title)

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
