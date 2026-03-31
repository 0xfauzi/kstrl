"""Status screen - read-only view of project state, PRD summary, and agent info."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from ralph.config import config_to_display, load_config
from ralph.git_ops import current_branch, is_git_repo
from ralph.models import detect_installed_agents
from ralph.prd import load_prd
from ralph.tui.widgets.story_table import StoryTableWidget


class StatusScreen(Screen):
    """Read-only overview of project status."""

    BINDINGS = [
        ("escape", "back", "Back"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="status-container"):
            yield Static("Project Status", classes="title")
            yield Static("", id="config-summary")
            yield Static("", id="git-summary")
            yield Static("", id="agent-summary")
            yield Static("", id="prd-summary")
            yield StoryTableWidget(id="story-table")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        cwd = Path.cwd()

        # Config summary
        try:
            config = load_config()
            display = config_to_display(config)
            config_lines = "\n".join(f"  {k}: {v}" for k, v in display.items())
            self.query_one("#config-summary", Static).update(
                f"[bold]Configuration[/bold]\n{config_lines}"
            )
        except Exception as e:
            self.query_one("#config-summary", Static).update(
                f"[yellow]Config: {e}[/yellow]"
            )

        # Git summary
        if is_git_repo(cwd):
            branch = current_branch(cwd)
            self.query_one("#git-summary", Static).update(
                f"\n[bold]Git[/bold]\n  Branch: {branch}"
            )
        else:
            self.query_one("#git-summary", Static).update(
                "\n[dim]Not a git repository[/dim]"
            )

        # Agent summary
        installed = detect_installed_agents()
        agent_str = ", ".join(installed) if installed else "[yellow]none found[/yellow]"
        self.query_one("#agent-summary", Static).update(
            f"\n[bold]Installed Agents[/bold]\n  {agent_str}"
        )

        # PRD summary
        try:
            config = load_config()
            prd = load_prd(cwd / config.paths.prd)
            self.query_one("#prd-summary", Static).update(
                f"\n[bold]PRD[/bold]\n"
                f"  Branch: {prd.branch_name}\n"
                f"  Stories: {prd.total_stories} total, "
                f"{prd.passing_stories} passing, {prd.failing_stories} failing"
            )
            next_story = prd.next_story()
            if next_story:
                self.query_one("#prd-summary", Static).update(
                    self.query_one("#prd-summary", Static).renderable  # type: ignore[arg-type]
                )

            story_table = self.query_one("#story-table", StoryTableWidget)
            story_table.update_stories(prd)
        except Exception as e:
            self.query_one("#prd-summary", Static).update(
                f"\n[dim]PRD: {e}[/dim]"
            )

    def action_refresh(self) -> None:
        self._refresh()

    def action_back(self) -> None:
        self.app.pop_screen()
