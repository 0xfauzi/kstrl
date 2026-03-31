"""Configuration screen - visual editor for ralph.toml settings."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    Static,
)

from ralph.config import load_config, save_config
from ralph.tui.widgets.model_selector import ModelSelector


class ConfigScreen(Screen):
    """Visual editor for ralph.toml configuration."""

    BINDINGS = [
        ("escape", "back", "Back"),
        ("ctrl+s", "save", "Save"),
    ]

    def __init__(self, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._config = load_config()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="config-container"):
            yield Static("Settings", classes="title")
            yield Static("")

            # Agent section
            with Vertical(classes="config-section"):
                yield Static("Agent", classes="config-section-title")
                yield ModelSelector(id="model-selector")

            # Run section
            with Vertical(classes="config-section"):
                yield Static("Run", classes="config-section-title")
                yield Label("Max iterations:")
                yield Input(
                    value=str(self._config.run.max_iterations),
                    id="max-iterations",
                    type="integer",
                )
                yield Label("Sleep seconds:")
                yield Input(
                    value=str(self._config.run.sleep_seconds),
                    id="sleep-seconds",
                    type="integer",
                )
                yield Checkbox("Interactive mode", self._config.run.interactive, id="interactive")

            # Paths section
            with Vertical(classes="config-section"):
                yield Static("Paths", classes="config-section-title")
                yield Label("Prompt file:")
                yield Input(value=self._config.paths.prompt, id="prompt-path")
                yield Label("PRD file:")
                yield Input(value=self._config.paths.prd, id="prd-path")
                yield Label("Allowed paths (comma-separated):")
                yield Input(
                    value=", ".join(self._config.paths.allowed),
                    id="allowed-paths",
                )

            # Git section
            with Vertical(classes="config-section"):
                yield Static("Git", classes="config-section-title")
                yield Label("Branch override (empty = from PRD):")
                yield Input(value=self._config.git.branch, id="git-branch")
                yield Checkbox("Auto checkout", self._config.git.auto_checkout, id="auto-checkout")

            # Actions
            yield Static("")
            yield Button("Save", id="save-btn", variant="primary")
            yield Static("", id="save-status")

        yield Footer()

    def on_model_selector_changed(self, event: ModelSelector.Changed) -> None:
        self._config.agent.type = event.agent_type
        self._config.agent.model = event.model

    def _collect_form_data(self) -> None:
        """Collect all form values into config."""
        try:
            self._config.run.max_iterations = int(self.query_one("#max-iterations", Input).value)
        except ValueError:
            pass
        try:
            self._config.run.sleep_seconds = int(self.query_one("#sleep-seconds", Input).value)
        except ValueError:
            pass
        self._config.run.interactive = self.query_one("#interactive", Checkbox).value
        self._config.paths.prompt = self.query_one("#prompt-path", Input).value
        self._config.paths.prd = self.query_one("#prd-path", Input).value

        allowed_str = self.query_one("#allowed-paths", Input).value
        self._config.paths.allowed = [
            p.strip() for p in allowed_str.split(",") if p.strip()
        ]

        self._config.git.branch = self.query_one("#git-branch", Input).value
        self._config.git.auto_checkout = self.query_one("#auto-checkout", Checkbox).value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            self.action_save()

    def action_save(self) -> None:
        self._collect_form_data()
        toml_path = Path.cwd() / "ralph.toml"
        save_config(self._config, toml_path)
        status = self.query_one("#save-status", Static)
        status.update(f"[green]Saved to {toml_path}[/green]")

    def action_back(self) -> None:
        self.app.pop_screen()
