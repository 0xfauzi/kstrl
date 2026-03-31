"""Init wizard screen - guided project setup."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
)

from ralph.config import RalphConfig, save_config
from ralph.models import detect_installed_agents
from ralph.prompt import scaffold_project
from ralph.tui.widgets.model_selector import ModelSelector


class InitWizardScreen(Screen):
    """Guided project initialization wizard."""

    BINDINGS = [
        ("escape", "back", "Back"),
    ]

    def __init__(self, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._step = 0
        self._target_dir = str(Path.cwd())
        self._project_type = "greenfield"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center():
            with Vertical(id="init-container"):
                yield Static("Project Setup", classes="title")
                yield Static("Step 1 of 3: Target Directory", id="init-step-label")
                yield Vertical(id="init-step")
                yield Button("Next", id="init-next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._render_step()

    def _render_step(self) -> None:
        step_container = self.query_one("#init-step", Vertical)
        step_label = self.query_one("#init-step-label", Static)
        next_btn = self.query_one("#init-next", Button)

        step_container.remove_children()

        step_titles = [
            "Step 1 of 3: Target Directory",
            "Step 2 of 3: Project Type",
            "Step 3 of 3: Agent Selection",
        ]
        step_label.update(step_titles[self._step])
        next_btn.label = "Initialize" if self._step == 2 else "Next"

        if self._step == 0:
            step_container.mount(Label("Target directory:"))
            step_container.mount(
                Input(value=self._target_dir, id="target-dir", placeholder="/path/to/project")
            )
        elif self._step == 1:
            step_container.mount(Label("Is this an existing codebase?"))
            step_container.mount(
                RadioSet(
                    RadioButton(
                        "Greenfield (new project)",
                        value=self._project_type == "greenfield",
                    ),
                    RadioButton(
                        "Brownfield (existing code)",
                        value=self._project_type == "brownfield",
                    ),
                    id="project-type",
                )
            )
        elif self._step == 2:
            step_container.mount(Label("Select your AI agent:"))
            step_container.mount(ModelSelector(id="init-model-selector"))

            installed = detect_installed_agents()
            if not installed:
                step_container.mount(
                    Static("[yellow]No agents detected. Install claude or codex first.[/yellow]")
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "init-next":
            return

        if self._step == 0:
            try:
                self._target_dir = self.query_one("#target-dir", Input).value
            except Exception:
                pass
            self._step = 1
            self._render_step()
        elif self._step == 1:
            radio_set = self.query_one("#project-type", RadioSet)
            if radio_set.pressed_index == 1:
                self._project_type = "brownfield"
            else:
                self._project_type = "greenfield"
            self._step = 2
            self._render_step()
        elif self._step == 2:
            self._run_init()

    def _run_init(self) -> None:
        target = Path(self._target_dir).resolve()
        step_container = self.query_one("#init-step", Vertical)
        step_container.remove_children()

        step_container.mount(Static(f"Initializing {target}..."))

        # Scaffold files
        messages = scaffold_project(target)
        for msg in messages:
            if msg.startswith("Created"):
                step_container.mount(Static(f"[green]{msg}[/green]"))
            else:
                step_container.mount(Static(f"[dim]{msg}[/dim]"))

        # Create ralph.toml
        config = RalphConfig()
        try:
            selector = self.query_one("#init-model-selector", ModelSelector)
            config.agent.type = selector.agent_type
            config.agent.model = selector.model
        except Exception:
            installed = detect_installed_agents()
            if "claude" in installed:
                config.agent.type = "claude"
            elif "codex" in installed:
                config.agent.type = "codex"

        toml_path = target / "ralph.toml"
        if not toml_path.exists():
            save_config(config, toml_path)
            step_container.mount(Static("[green]Created: ralph.toml[/green]"))

        step_container.mount(Static(""))
        step_container.mount(Static("[bold green]Setup complete![/bold green]"))

        if self._project_type == "brownfield":
            step_container.mount(Static(
                "\nRecommended next step: run codebase understanding first.\n"
                "  ralph understand 10"
            ))
        else:
            step_container.mount(Static(
                "\nNext steps:\n"
                "  ralph prd create    # Create your PRD\n"
                "  ralph run 25        # Run the feature loop"
            ))

        self.query_one("#init-next", Button).disabled = True
        self.query_one("#init-step-label", Static).update("Setup Complete")

    def action_back(self) -> None:
        self.app.pop_screen()
