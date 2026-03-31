"""Main menu screen - the landing screen for Ralph TUI."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from ralph.config import load_config
from ralph.models import agent_display_name, detect_installed_agents
from ralph.prd import load_prd

LOGO = """\
 ____       _       _
|  _ \\ __ _| |_ __ | |__
| |_) / _` | | '_ \\| '_ \\
|  _ < (_| | | |_) | | | |
|_| \\_\\__,_|_| .__/|_| |_|
             |_|"""


class MainMenuScreen(Screen):
    """Landing screen with mode selection and project overview."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("1", "select_new_project", "New Project"),
        ("2", "select_existing_project", "Existing Project"),
        ("3", "select_run", "Run"),
        ("4", "select_understand", "Understand"),
        ("5", "select_prd", "PRD"),
        ("6", "select_status", "Status"),
        ("7", "select_config", "Settings"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center():
            with Vertical(id="main-menu"):
                yield Static(LOGO, id="main-menu-logo")
                yield Static("Agentic Loop Harness", id="main-menu-subtitle")

                yield ListView(
                    ListItem(Label("[1]  New Project"), id="menu-new-project"),
                    ListItem(Label("[2]  Add to Existing Project"), id="menu-existing-project"),
                    ListItem(Label("[3]  Run Feature Loop"), id="menu-run"),
                    ListItem(Label("[4]  Run Codebase Understanding"), id="menu-understand"),
                    ListItem(Label("[5]  Create / Edit PRD"), id="menu-prd"),
                    ListItem(Label("[6]  View Status"), id="menu-status"),
                    ListItem(Label("[7]  Settings"), id="menu-config"),
                    id="main-menu-list",
                )

                yield Static(id="project-info")

        yield Footer()

    def on_mount(self) -> None:
        self._refresh_project_info()

    def _refresh_project_info(self) -> None:
        info = self.query_one("#project-info", Static)
        cwd = Path.cwd()
        lines: list[str] = [f"[bold]Project[/bold]  {cwd.name}"]

        try:
            config = load_config()
            agent_desc = agent_display_name(config.agent.type, config.agent.model)
            lines.append(f"[bold]Agent[/bold]    {agent_desc}")
        except Exception:
            installed = detect_installed_agents()
            agents = ", ".join(installed) if installed else "none detected"
            lines.append(f"[bold]Agents[/bold]   {agents}")

        try:
            prd = load_prd(cwd / "scripts" / "ralph" / "prd.json")
            lines.append(
                f"[bold]PRD[/bold]      {prd.total_stories} stories  "
                f"[green]{prd.passing_stories} pass[/green]  "
                f"[red]{prd.failing_stories} fail[/red]"
            )
        except Exception:
            lines.append("[bold]PRD[/bold]      [dim]not found[/dim]")

        info.update("\n".join(lines))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        actions = {
            "menu-new-project": self.action_select_new_project,
            "menu-existing-project": self.action_select_existing_project,
            "menu-run": self.action_select_run,
            "menu-understand": self.action_select_understand,
            "menu-prd": self.action_select_prd,
            "menu-status": self.action_select_status,
            "menu-config": self.action_select_config,
        }
        action = actions.get(item_id)
        if action:
            action()

    def action_select_new_project(self) -> None:
        self.app.push_screen("new_project_wizard")

    def action_select_existing_project(self) -> None:
        self.app.push_screen("init_wizard")

    def action_select_run(self) -> None:
        self.app.push_screen("run_config")

    def action_select_understand(self) -> None:
        self.app.push_screen("run_config_understand")

    def action_select_prd(self) -> None:
        self.app.push_screen("prd_wizard")

    def action_select_status(self) -> None:
        self.app.push_screen("status")

    def action_select_config(self) -> None:
        self.app.push_screen("config")

    def action_quit(self) -> None:
        self.app.exit()
