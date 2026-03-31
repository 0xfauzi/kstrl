"""Pre-run configuration screen.

Shown before launching the run dashboard so the user can review and
adjust iterations, model, and mode before committing to a run.
"""

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

from ralph.config import load_config
from ralph.models import agent_display_name
from ralph.prd import load_prd
from ralph.tui.widgets.model_selector import ModelSelector


class RunConfigScreen(Screen):
    """Pre-run configuration: iterations, model, and summary before starting."""

    BINDINGS = [
        ("escape", "back", "Back"),
    ]

    def __init__(
        self,
        understand_mode: bool = False,
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self.understand_mode = understand_mode
        self._error = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        mode_label = "Codebase Understanding" if self.understand_mode else "Feature Loop"
        with Center(classes="wizard-outer"):
            with Vertical(id="new-project-container"):
                yield Static(f"Run {mode_label}", classes="title")
                yield Vertical(id="run-config-body")
                with Horizontal(id="new-project-nav"):
                    yield Button("Back", id="rc-back", variant="default")
                    yield Button(
                        "Start", id="rc-start", variant="primary",
                    )
        yield Footer()

    def on_mount(self) -> None:
        self._render_form()

    def _render_form(self) -> None:
        body = self.query_one("#run-config-body", Vertical)
        body.remove_children()

        cwd = Path.cwd()
        toml_exists = (cwd / "ralph.toml").exists()

        if not toml_exists:
            body.mount(
                Static(
                    "[red]No ralph.toml found.[/red] "
                    "Run project setup first.",
                )
            )
            self.query_one("#rc-start", Button).disabled = True
            return

        config = load_config()

        # Current agent/model display
        agent_desc = agent_display_name(config.agent.type, config.agent.model)
        body.mount(Static(f"  Agent: [bold]{agent_desc}[/bold]"))
        body.mount(Static(""))

        # Model selector
        body.mount(Label("Agent and model"))
        body.mount(
            Static(
                "[dim]Change the agent or model for this run only. "
                "This does not modify ralph.toml.[/dim]",
                classes="help-text",
            )
        )
        body.mount(ModelSelector(id="rc-model-selector"))

        # Iterations
        body.mount(Label("Max iterations"))
        body.mount(
            Input(
                value=str(config.run.max_iterations),
                id="rc-iterations",
                type="integer",
                placeholder="e.g. 10",
            )
        )

        # Context summary
        body.mount(Static(""))
        if self.understand_mode:
            prompt_path = cwd / "scripts/ralph/understand_prompt.md"
            body.mount(
                Static(
                    f"  Prompt: {prompt_path.name}  "
                    f"{'[green]exists[/green]' if prompt_path.exists() else '[red]missing[/red]'}"
                )
            )
            map_path = cwd / "scripts/ralph/codebase_map.md"
            map_status = (
                "[green]exists[/green]" if map_path.exists()
                else "[dim]will be created[/dim]"
            )
            body.mount(
                Static(f"  Map:    {map_path.name}  {map_status}")
            )
        else:
            prompt_path = cwd / config.paths.prompt
            body.mount(
                Static(
                    f"  Prompt: {prompt_path.name}  "
                    f"{'[green]exists[/green]' if prompt_path.exists() else '[red]missing[/red]'}"
                )
            )
            prd_path = cwd / config.paths.prd
            try:
                prd = load_prd(prd_path)
                body.mount(
                    Static(
                        f"  PRD:    {prd.total_stories} stories  "
                        f"[green]{prd.passing_stories} pass[/green]  "
                        f"[red]{prd.failing_stories} fail[/red]"
                    )
                )
            except Exception:
                body.mount(
                    Static("  PRD:    [red]not found or invalid[/red]")
                )

        if self._error:
            body.mount(Static(""))
            body.mount(Static(f"[red]{self._error}[/red]"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rc-back":
            self.app.pop_screen()
        elif event.button.id == "rc-start":
            self._start_run()

    def _start_run(self) -> None:
        # Validate iterations
        try:
            iterations = int(
                self.query_one("#rc-iterations", Input).value
            )
            if iterations < 1:
                self._error = "Iterations must be at least 1"
                self._render_form()
                return
        except ValueError:
            self._error = "Invalid iteration count"
            self._render_form()
            return

        # Get selected model
        try:
            selector = self.query_one("#rc-model-selector", ModelSelector)
            agent_type = selector.agent_type
            model = selector.model
        except Exception:
            agent_type = ""
            model = ""

        # Pop this screen and push the dashboard with config overrides
        from ralph.tui.screens.run_dashboard import RunDashboardScreen

        self.app.pop_screen()
        dashboard = RunDashboardScreen(
            understand_mode=self.understand_mode,
            max_iterations_override=iterations,
            agent_type_override=agent_type or None,
            model_override=model or None,
        )
        self.app.push_screen(dashboard)

    def action_back(self) -> None:
        self.app.pop_screen()
