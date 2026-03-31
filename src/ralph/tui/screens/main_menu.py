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


class MainMenuScreen(Screen):
    """Landing screen with mode selection and project overview."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("1", "select_init", "Init"),
        ("2", "select_run", "Run"),
        ("3", "select_understand", "Understand"),
        ("4", "select_prd", "PRD"),
        ("5", "select_status", "Status"),
        ("6", "select_config", "Settings"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center():
            with Vertical(id="main-menu"):
                yield Static("Ralph", id="main-menu-title")
                yield Static("Agentic Loop Harness", id="main-menu-subtitle")

                yield ListView(
                    ListItem(Label("[1] New Project Setup"), id="menu-init"),
                    ListItem(Label("[2] Run Feature Loop"), id="menu-run"),
                    ListItem(Label("[3] Run Codebase Understanding"), id="menu-understand"),
                    ListItem(Label("[4] Create / Edit PRD"), id="menu-prd"),
                    ListItem(Label("[5] View Status"), id="menu-status"),
                    ListItem(Label("[6] Settings"), id="menu-config"),
                    id="main-menu-list",
                )

                yield Static(id="project-info")

        yield Footer()

    def on_mount(self) -> None:
        self._refresh_project_info()

    def _refresh_project_info(self) -> None:
        info = self.query_one("#project-info", Static)
        cwd = Path.cwd()
        lines = [f"Project: {cwd}"]

        try:
            config = load_config()
            agent_desc = agent_display_name(config.agent.type, config.agent.model)
            lines.append(f"Agent: {agent_desc}")
        except Exception:
            installed = detect_installed_agents()
            lines.append(
                f"Agents: {', '.join(installed) if installed else 'none detected'}"
            )

        try:
            prd = load_prd(cwd / "scripts" / "ralph" / "prd.json")
            lines.append(
                f"PRD: {prd.total_stories} stories "
                f"({prd.passing_stories} passing, {prd.failing_stories} failing)"
            )
        except Exception:
            lines.append("PRD: not found")

        info.update("\n".join(lines))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id == "menu-init":
            self.action_select_init()
        elif item_id == "menu-run":
            self.action_select_run()
        elif item_id == "menu-understand":
            self.action_select_understand()
        elif item_id == "menu-prd":
            self.action_select_prd()
        elif item_id == "menu-status":
            self.action_select_status()
        elif item_id == "menu-config":
            self.action_select_config()

    def action_select_init(self) -> None:
        self.app.push_screen("init_wizard")

    def action_select_run(self) -> None:
        self.app.push_screen("run_dashboard")

    def action_select_understand(self) -> None:
        self.app.push_screen("run_dashboard_understand")

    def action_select_prd(self) -> None:
        self.app.push_screen("prd_wizard")

    def action_select_status(self) -> None:
        self.app.push_screen("status")

    def action_select_config(self) -> None:
        self.app.push_screen("config")

    def action_quit(self) -> None:
        self.app.exit()
