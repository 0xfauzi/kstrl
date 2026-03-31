"""Ralph TUI Application - Textual-based terminal user interface."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from ralph.tui.screens.config_screen import ConfigScreen
from ralph.tui.screens.init_wizard import InitWizardScreen
from ralph.tui.screens.main_menu import MainMenuScreen
from ralph.tui.screens.new_project_wizard import NewProjectWizardScreen
from ralph.tui.screens.prd_wizard import PRDWizardScreen
from ralph.tui.screens.run_config import RunConfigScreen
from ralph.tui.screens.status_screen import StatusScreen

CSS_PATH = Path(__file__).parent / "styles" / "app.tcss"


class RalphApp(App):
    """The Ralph TUI application."""

    TITLE = "Ralph"
    SUB_TITLE = "Agentic Loop Harness"

    CSS_PATH = str(CSS_PATH)

    SCREENS = {
        "main_menu": MainMenuScreen,
        "init_wizard": InitWizardScreen,
        "new_project_wizard": NewProjectWizardScreen,
        "run_config": lambda: RunConfigScreen(understand_mode=False),
        "run_config_understand": lambda: RunConfigScreen(understand_mode=True),
        "prd_wizard": PRDWizardScreen,
        "config": ConfigScreen,
        "status": StatusScreen,
    }

    def __init__(self, start_screen: str = "main_menu") -> None:
        super().__init__()
        self._start_screen = start_screen

    def on_mount(self) -> None:
        self.push_screen(self._start_screen)
