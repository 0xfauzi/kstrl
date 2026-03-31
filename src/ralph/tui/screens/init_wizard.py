"""Init wizard screen - guided setup for existing (brownfield) projects."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Center, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Static,
)

from ralph.config import RalphConfig, save_config
from ralph.models import detect_installed_agents
from ralph.prompt import scaffold_project
from ralph.tui.widgets.model_selector import ModelSelector


class InitWizardScreen(Screen):
    """Guided project initialization wizard for existing codebases."""

    BINDINGS = [
        ("escape", "back", "Back"),
    ]

    def __init__(self, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._step = 0
        self._target_dir = str(Path.cwd())
        self._error = ""
        self._done = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center(classes="wizard-outer"):
            with Vertical(id="init-container"):
                yield Static("Add Ralph to Existing Project", classes="title")
                yield Static(
                    self._progress_dots(),
                    id="init-step-label",
                )
                yield Vertical(id="init-step")
                with Horizontal(id="new-project-nav"):
                    yield Button("Back", id="init-back", variant="default", disabled=True)
                    yield Button("Next", id="init-next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._render_step()

    def _progress_dots(self) -> str:
        total = 2
        parts: list[str] = []
        for i in range(total):
            if i == self._step:
                parts.append("[bold]()[/bold]")
            elif i < self._step:
                parts.append("[green]o[/green]")
            else:
                parts.append("[dim]o[/dim]")
        return "  ".join(parts) + f"    Step {self._step + 1} of {total}"

    def _render_step(self) -> None:
        step_container = self.query_one("#init-step", Vertical)
        step_label = self.query_one("#init-step-label", Static)
        next_btn = self.query_one("#init-next", Button)
        back_btn = self.query_one("#init-back", Button)

        step_container.remove_children()
        step_label.update(self._progress_dots())

        back_btn.disabled = self._step == 0
        next_btn.disabled = False
        next_btn.label = "Initialize" if self._step == 1 else "Next"

        if self._step == 0:
            step_container.mount(Label("Target directory"))
            step_container.mount(
                Input(
                    value=self._target_dir,
                    id="target-dir",
                    placeholder="/path/to/project",
                )
            )
            step_container.mount(
                Static(
                    "[dim]The directory containing your existing codebase. "
                    "Ralph will create a scripts/ralph/ directory here.[/dim]",
                    classes="help-text",
                )
            )
            if self._error:
                step_container.mount(Static(f"[red]{self._error}[/red]"))
        elif self._step == 1:
            step_container.mount(Label("Select your AI agent"))
            step_container.mount(ModelSelector(id="init-model-selector"))

            installed = detect_installed_agents()
            if not installed:
                step_container.mount(
                    Static(
                        "[yellow]No agents detected. "
                        "Install claude or codex first.[/yellow]"
                    )
                )
            else:
                step_container.mount(
                    Static(
                        "[dim]Tip: after setup, run 'ralph understand 10' "
                        "to map your codebase before writing a PRD.[/dim]",
                        classes="help-text",
                    )
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "init-done":
            self.app.pop_screen()
            return

        if event.button.id == "init-back":
            if self._step > 0:
                self._step -= 1
                self._error = ""
                self._render_step()
        elif event.button.id == "init-next":
            if self._step == 0:
                try:
                    self._target_dir = self.query_one("#target-dir", Input).value
                except Exception:
                    pass

                target = Path(self._target_dir).resolve()
                if not target.exists():
                    self._error = f"Directory does not exist: {target}"
                    self._render_step()
                    return
                if not target.is_dir():
                    self._error = f"Not a directory: {target}"
                    self._render_step()
                    return

                self._error = ""
                self._step = 1
                self._render_step()
            elif self._step == 1:
                self._run_init()

    def _run_init(self) -> None:
        next_btn = self.query_one("#init-next", Button)
        next_btn.disabled = True

        target = Path(self._target_dir).resolve()
        step_container = self.query_one("#init-step", Vertical)
        step_container.remove_children()

        step_container.mount(Static(f"Initializing {target}..."))
        step_container.mount(Static(""))

        messages = scaffold_project(target)
        for msg in messages:
            if msg.startswith("Created"):
                step_container.mount(Static(f"  [green]{msg}[/green]"))
            else:
                step_container.mount(Static(f"  [dim]{msg}[/dim]"))

        config = RalphConfig()
        try:
            selector = self.query_one("#init-model-selector", ModelSelector)
            config.agent.type = selector.agent_type
            config.agent.model = selector.model
        except Exception:
            installed = detect_installed_agents()
            if "claude" in installed:
                config.agent.type = "claude"
                step_container.mount(
                    Static("  [dim]Auto-detected agent: claude[/dim]")
                )
            elif "codex" in installed:
                config.agent.type = "codex"
                step_container.mount(
                    Static("  [dim]Auto-detected agent: codex[/dim]")
                )

        toml_path = target / "ralph.toml"
        if not toml_path.exists():
            save_config(config, toml_path)
            step_container.mount(Static("  [green]Created: ralph.toml[/green]"))
        else:
            step_container.mount(Static("  [dim]Exists: ralph.toml[/dim]"))

        step_container.mount(Static(""))
        step_container.mount(Static("[bold green]Setup complete[/bold green]"))
        step_container.mount(Static(""))
        step_container.mount(Static("Next steps:"))
        step_container.mount(Static("  1. ralph understand 10   Map your codebase"))
        step_container.mount(Static("  2. ralph prd create      Create your PRD"))
        step_container.mount(Static("  3. ralph run 25          Run the feature loop"))

        self._done = True
        self.query_one("#init-step-label", Static).update(
            "[green]o[/green]  [green]o[/green]    Complete"
        )

        # Replace nav buttons with Done
        nav = self.query_one("#new-project-nav", Horizontal)
        nav.remove_children()
        nav.mount(Button("Done", id="init-done", variant="primary"))

    def action_back(self) -> None:
        self.app.pop_screen()
