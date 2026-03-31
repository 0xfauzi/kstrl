"""Pre-conversation config for interactive feature planning.

Collects model, initial prompt, and/or markdown file path before
launching the conversation screen.
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
    TextArea,
)

from ralph.tui.widgets.model_selector import ModelSelector


class FeatureConfigScreen(Screen):
    """Collect initial context before starting a planning conversation."""

    BINDINGS = [
        ("escape", "back", "Back"),
    ]

    def __init__(self, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._error = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center(classes="wizard-outer"):
            with Vertical(id="new-project-container"):
                yield Static("Interactive Feature Planning", classes="title")
                yield Static(
                    "[dim]Describe what you want to build. An AI PM will "
                    "review your spec and ask probing questions before "
                    "generating a PRD.[/dim]",
                    classes="help-text",
                )
                yield Vertical(id="fc-body")
                with Horizontal(id="new-project-nav"):
                    yield Button("Back", id="fc-back", variant="default")
                    yield Button(
                        "Start Conversation",
                        id="fc-start",
                        variant="primary",
                    )
        yield Footer()

    def on_mount(self) -> None:
        self._render_form()

    def _render_form(self) -> None:
        body = self.query_one("#fc-body", Vertical)
        body.remove_children()

        body.mount(Label("Agent and model"))
        body.mount(ModelSelector(id="fc-model-selector"))

        body.mount(Label("Describe your feature (optional)"))
        body.mount(
            Static(
                "[dim]A few sentences about what you want to build. "
                "You can also provide this interactively in the "
                "conversation.[/dim]",
                classes="help-text",
            )
        )
        body.mount(
            TextArea("", id="fc-prompt", tab_behavior="indent")
        )

        body.mount(Label("Markdown spec file (optional)"))
        body.mount(
            Static(
                "[dim]Path to an existing spec or requirements "
                "document. Leave empty to skip.[/dim]",
                classes="help-text",
            )
        )
        body.mount(
            Input(
                value="",
                id="fc-file",
                placeholder="/path/to/spec.md",
            )
        )

        if self._error:
            body.mount(Static(f"[red]{self._error}[/red]"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fc-back":
            self.app.pop_screen()
        elif event.button.id == "fc-start":
            self._start_conversation()

    def _start_conversation(self) -> None:
        prompt = ""
        file_path = ""

        try:
            prompt = self.query_one("#fc-prompt", TextArea).text.strip()
        except Exception:
            pass

        try:
            file_path = self.query_one("#fc-file", Input).value.strip()
        except Exception:
            pass

        # Validate file exists if provided
        if file_path:
            p = Path(file_path).expanduser().resolve()
            if not p.exists():
                self._error = f"File not found: {p}"
                self._render_form()
                return
            if not p.is_file():
                self._error = f"Not a file: {p}"
                self._render_form()
                return

        # At least one input required
        if not prompt and not file_path:
            self._error = (
                "Provide a feature description, a spec file, or both."
            )
            self._render_form()
            return

        # Get model selection
        agent_type = ""
        model = ""
        try:
            selector = self.query_one("#fc-model-selector", ModelSelector)
            agent_type = selector.agent_type
            model = selector.model
        except Exception:
            pass

        from ralph.tui.screens.feature_conversation import (
            FeatureConversationScreen,
        )

        self.app.pop_screen()
        self.app.push_screen(
            FeatureConversationScreen(
                initial_prompt=prompt,
                initial_file=file_path,
                agent_type=agent_type or "claude",
                model=model or "sonnet",
            )
        )

    def action_back(self) -> None:
        self.app.pop_screen()
