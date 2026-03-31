"""Interactive feature planning conversation screen.

Chat-like interface where a PM agent reviews the user's feature spec,
asks probing questions, and eventually generates a PRD.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input

from ralph.agent import (
    AgentOutput,
    LineRole,
    reset_stream_state,
    run_conversation_agent,
    run_prd_generation,
)
from ralph.conversation import (
    PRD_JSON_SCHEMA,
    ConversationMessage,
    build_conversation_prompt,
    build_generation_prompt,
    parse_prd_from_json_output,
    response_has_ready_marker,
)
from ralph.tui.widgets.agent_log import AgentLogWidget


class FeatureConversationScreen(Screen):
    """Chat-like screen for interactive feature planning."""

    BINDINGS = [
        ("escape", "back", "Back"),
    ]

    def __init__(
        self,
        initial_prompt: str = "",
        initial_file: str = "",
        agent_type: str = "claude",
        model: str = "sonnet",
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self._initial_prompt = initial_prompt
        self._initial_file = initial_file
        self._agent_type = agent_type
        self._model = model
        self._messages: list[ConversationMessage] = []
        self._agent_busy = False
        self._accumulated_response = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield AgentLogWidget(
            id="conv-log", wrap=True, highlight=True, markup=True,
        )
        with Horizontal(id="conv-input-bar"):
            yield Input(
                placeholder="Type your response...",
                id="conv-input",
            )
            yield Button("Send", id="conv-send", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#conv-log", AgentLogWidget)
        log.write_info("Starting interactive feature planning session")
        log.write_info(f"Agent: {self._agent_type} ({self._model})")
        log.write(Text(""))

        self._build_initial_context()
        self._send_to_agent()

    def _build_initial_context(self) -> None:
        """Build the first user message from prompt and/or file."""
        parts: list[str] = []

        if self._initial_file:
            file_path = Path(self._initial_file).expanduser().resolve()
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(
                    f"Here is my feature specification:\n\n{content}"
                )

        if self._initial_prompt:
            parts.append(self._initial_prompt)

        first_message = "\n\n".join(parts) if parts else (
            "I want to build a new feature. Let me describe it."
        )

        # Display what we're sending
        log = self.query_one("#conv-log", AgentLogWidget)
        log.write_separator("You")
        for line in first_message.split("\n")[:10]:
            log.write(Text(f"  {line}", style="bold green"))
        if first_message.count("\n") > 10:
            log.write(Text(
                f"  ... ({first_message.count(chr(10)) - 10} more lines)",
                style="dim green",
            ))

        self._messages.append(
            ConversationMessage(role="user", content=first_message)
        )

    def _send_to_agent(self) -> None:
        """Send the full conversation to Claude and stream the response."""
        self._agent_busy = True
        self._accumulated_response = ""
        self._disable_input()

        reset_stream_state()

        log = self.query_one("#conv-log", AgentLogWidget)
        log.write(Text(""))
        log.write_separator("PM")

        prompt = build_conversation_prompt(self._messages)
        self.run_worker(
            self._agent_worker(prompt),
            name="conv-agent",
            thread=True,
            exclusive=True,
        )

    async def _agent_worker(self, prompt: str) -> None:
        """Worker: stream Claude response and post to UI."""
        try:
            async for output in run_conversation_agent(
                model=self._model,
                prompt=prompt,
                cwd=Path.cwd(),
            ):
                self.app.call_from_thread(self._on_agent_line, output)
        except Exception as e:
            self.app.call_from_thread(self._on_agent_error, str(e))

        self.app.call_from_thread(self._on_agent_done)

    def _on_agent_line(self, output: AgentOutput) -> None:
        """Handle a single streamed line from the agent."""
        log = self.query_one("#conv-log", AgentLogWidget)
        log.write_agent_line(output)

        if output.role == LineRole.AI:
            self._accumulated_response += output.line + "\n"

    def _on_agent_error(self, error: str) -> None:
        log = self.query_one("#conv-log", AgentLogWidget)
        log.write_error(f"Agent error: {error}")

    def _on_agent_done(self) -> None:
        """Agent finished responding. Check for ready marker or re-enable input."""
        self._agent_busy = False
        response = self._accumulated_response.strip()

        if response:
            self._messages.append(
                ConversationMessage(role="assistant", content=response)
            )

        # Check if the PM agent signaled it's ready to generate
        if response_has_ready_marker(response):
            log = self.query_one("#conv-log", AgentLogWidget)
            log.write(Text(""))
            log.write_info(
                "PM is satisfied. Generating PRD with structured output..."
            )
            self._generate_prd()
        else:
            self._enable_input()

    def _generate_prd(self) -> None:
        """Launch the structured PRD generation call."""
        self._agent_busy = True
        self._disable_input()
        self.run_worker(
            self._prd_generation_worker(),
            name="prd-gen",
            thread=True,
            exclusive=True,
        )

    async def _prd_generation_worker(self) -> None:
        """Worker: call Claude with --json-schema to generate PRD."""
        try:
            prompt = build_generation_prompt(self._messages)
            raw = await run_prd_generation(
                model=self._model,
                prompt=prompt,
                json_schema=PRD_JSON_SCHEMA,
                cwd=Path.cwd(),
            )
            self.app.call_from_thread(self._on_prd_generated, raw)
        except Exception as e:
            self.app.call_from_thread(self._on_agent_error, str(e))
            self.app.call_from_thread(self._enable_input)

    def _on_prd_generated(self, raw_output: str) -> None:
        """Handle the structured PRD generation result."""
        self._agent_busy = False
        log = self.query_one("#conv-log", AgentLogWidget)

        prd = parse_prd_from_json_output(raw_output)
        if prd is not None:
            log.write(Text(""))
            log.write_success(
                f"PRD generated: {prd.total_stories} stories, "
                f"branch: {prd.branch_name}"
            )
            log.write_info("Opening review screen...")

            from ralph.tui.screens.prd_review import PRDReviewScreen
            self.app.push_screen(PRDReviewScreen(prd=prd))
        else:
            log.write_error("Failed to generate valid PRD. Try again.")
            log.write_info(
                "Type 'generate' to retry, or continue the conversation."
            )
            self._enable_input()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "conv-send":
            self._submit_user_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "conv-input":
            self._submit_user_input()

    def _submit_user_input(self) -> None:
        """Handle user pressing Send or Enter."""
        if self._agent_busy:
            return

        inp = self.query_one("#conv-input", Input)
        user_text = inp.value.strip()
        if not user_text:
            return

        inp.value = ""

        # Display user message
        log = self.query_one("#conv-log", AgentLogWidget)
        log.write(Text(""))
        log.write_separator("You")
        log.write(Text(f"  {user_text}", style="bold green"))

        self._messages.append(
            ConversationMessage(role="user", content=user_text)
        )

        self._send_to_agent()

    def _disable_input(self) -> None:
        try:
            self.query_one("#conv-input", Input).disabled = True
            self.query_one("#conv-send", Button).disabled = True
        except Exception:
            pass

    def _enable_input(self) -> None:
        try:
            inp = self.query_one("#conv-input", Input)
            inp.disabled = False
            inp.focus()
            self.query_one("#conv-send", Button).disabled = False
        except Exception:
            pass

    def action_back(self) -> None:
        self.app.pop_screen()
