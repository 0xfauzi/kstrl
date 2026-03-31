"""Configuration screen - visual editor for ralph.toml settings."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
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
        self._toml_exists = Path("ralph.toml").exists()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="config-container"):
            yield Static("Settings", classes="title")

            if not self._toml_exists:
                yield Static(
                    "[yellow]No ralph.toml found.[/yellow] "
                    "Showing defaults. Save to create the file.",
                    classes="help-text",
                )

            # Agent section
            with Vertical(classes="config-section"):
                yield Static("Agent", classes="config-section-title")
                yield Static(
                    "Select which AI agent and model to use.",
                    classes="config-help",
                )
                yield ModelSelector(id="model-selector")

            # Run section
            with Vertical(classes="config-section"):
                yield Static("Run", classes="config-section-title")
                yield Static(
                    "Control loop execution behavior.",
                    classes="config-help",
                )
                yield Label("Max iterations")
                yield Input(
                    value=str(self._config.run.max_iterations),
                    id="max-iterations",
                    type="integer",
                    placeholder="e.g. 10",
                )
                yield Label("Sleep between iterations (seconds)")
                yield Input(
                    value=str(self._config.run.sleep_seconds),
                    id="sleep-seconds",
                    type="integer",
                    placeholder="e.g. 2",
                )
                yield Checkbox(
                    "Interactive mode (pause after each iteration)",
                    self._config.run.interactive,
                    id="interactive",
                )

            # Paths section
            with Vertical(classes="config-section"):
                yield Static("Paths", classes="config-section-title")
                yield Static(
                    "File locations for prompts, PRD, and path restrictions.",
                    classes="config-help",
                )
                yield Label("Prompt file")
                yield Input(
                    value=self._config.paths.prompt,
                    id="prompt-path",
                    placeholder="scripts/ralph/prompt.md",
                )
                yield Label("PRD file")
                yield Input(
                    value=self._config.paths.prd,
                    id="prd-path",
                    placeholder="scripts/ralph/prd.json",
                )
                yield Label("Allowed paths (comma-separated, empty = unrestricted)")
                yield Input(
                    value=", ".join(self._config.paths.allowed),
                    id="allowed-paths",
                    placeholder="src/, tests/",
                )

            # Git section
            with Vertical(classes="config-section"):
                yield Static("Git", classes="config-section-title")
                yield Static(
                    "Branch management and checkout behavior.",
                    classes="config-help",
                )
                yield Label("Branch override (empty = use PRD branch name)")
                yield Input(
                    value=self._config.git.branch,
                    id="git-branch",
                    placeholder="ralph/my-feature",
                )
                yield Checkbox(
                    "Auto-checkout branch before running",
                    self._config.git.auto_checkout,
                    id="auto-checkout",
                )

            # Actions
            yield Button("Save", id="save-btn", variant="primary")
            yield Static("", id="save-status")

        yield Footer()

    def on_model_selector_changed(self, event: ModelSelector.Changed) -> None:
        self._config.agent.type = event.agent_type
        self._config.agent.model = event.model

    def _collect_form_data(self) -> list[str]:
        """Collect all form values into config. Returns list of errors."""
        errors: list[str] = []

        max_iter_str = self.query_one("#max-iterations", Input).value
        try:
            val = int(max_iter_str)
            if val < 1:
                errors.append("Max iterations must be at least 1")
            else:
                self._config.run.max_iterations = val
        except ValueError:
            errors.append(f"Invalid max iterations: '{max_iter_str}'")

        sleep_str = self.query_one("#sleep-seconds", Input).value
        try:
            val = int(sleep_str)
            if val < 0:
                errors.append("Sleep seconds cannot be negative")
            else:
                self._config.run.sleep_seconds = val
        except ValueError:
            errors.append(f"Invalid sleep seconds: '{sleep_str}'")

        self._config.run.interactive = self.query_one(
            "#interactive", Checkbox
        ).value
        self._config.paths.prompt = self.query_one("#prompt-path", Input).value
        self._config.paths.prd = self.query_one("#prd-path", Input).value

        allowed_str = self.query_one("#allowed-paths", Input).value
        self._config.paths.allowed = [
            p.strip() for p in allowed_str.split(",") if p.strip()
        ]

        self._config.git.branch = self.query_one("#git-branch", Input).value
        self._config.git.auto_checkout = self.query_one(
            "#auto-checkout", Checkbox
        ).value

        return errors

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            self.action_save()

    def action_save(self) -> None:
        errors = self._collect_form_data()
        status = self.query_one("#save-status", Static)

        if errors:
            status.update("[red]" + "\n".join(errors) + "[/red]")
            return

        toml_path = Path.cwd() / "ralph.toml"
        save_config(self._config, toml_path)
        self._toml_exists = True
        status.update(f"[green]Saved to {toml_path}[/green]")

    def action_back(self) -> None:
        self.app.pop_screen()
