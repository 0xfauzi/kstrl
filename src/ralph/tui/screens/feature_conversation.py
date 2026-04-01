"""Interactive feature planning conversation.

Near-monochrome terminal interface. The green prompt '>' is the only
strong visual element. Everything else is just text.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

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
from ralph.tui.widgets.chat_log import ChatLogWidget


class FeatureConversationScreen(Screen):

    BINDINGS = [("escape", "back", "Back")]

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
        self._activity_lines: list[tuple[str, LineRole]] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="chat-scroll"):
            yield ChatLogWidget(
                id="chat-log", wrap=True, highlight=False, markup=False,
            )
            yield Static("", id="streaming-display")
        yield Input(placeholder="Type your response...", id="chat-input")

    def on_mount(self) -> None:
        self.query_one("#streaming-display").display = False
        log = self.query_one("#chat-log", ChatLogWidget)
        log.write_system(
            f"feature planning / {self._agent_type} / {self._model}"
        )
        self._build_initial_context()
        self._send_to_agent()

    # -- Context -----------------------------------------------------------

    def _build_initial_context(self) -> None:
        parts: list[str] = []
        if self._initial_file:
            p = Path(self._initial_file).expanduser().resolve()
            if p.exists():
                parts.append(
                    f"Here is my feature specification:\n\n"
                    f"{p.read_text(encoding='utf-8')}"
                )
        if self._initial_prompt:
            parts.append(self._initial_prompt)

        msg = "\n\n".join(parts) or "I want to build a new feature."
        self.query_one("#chat-log", ChatLogWidget).write_user_message(msg)
        self._messages.append(ConversationMessage(role="user", content=msg))

    # -- Agent -------------------------------------------------------------

    def _send_to_agent(self) -> None:
        self._agent_busy = True
        self._accumulated_response = ""
        self._activity_lines = []
        self._disable_input()
        self._show_streaming()

        reset_stream_state()
        self.run_worker(
            self._agent_worker(
                build_conversation_prompt(self._messages)
            ),
            name="conv-agent", thread=True, exclusive=True,
        )

    async def _agent_worker(self, prompt: str) -> None:
        try:
            async for output in run_conversation_agent(
                model=self._model, prompt=prompt, cwd=Path.cwd(),
            ):
                self.app.call_from_thread(self._on_agent_line, output)
        except Exception as e:
            self.app.call_from_thread(self._on_agent_error, str(e))
        self.app.call_from_thread(self._on_agent_done)

    def _on_agent_line(self, output: AgentOutput) -> None:
        if output.role == LineRole.AI:
            self._accumulated_response += output.line + "\n"
            self._update_streaming()
        elif output.role in (
            LineRole.THINK, LineRole.TOOL, LineRole.GIT, LineRole.SYS,
        ):
            self._activity_lines.append((output.line, output.role))
            self._update_streaming()

    def _on_agent_error(self, error: str) -> None:
        self.query_one("#chat-log", ChatLogWidget).write_system(
            f"error: {error}"
        )
        self._hide_streaming()
        self._enable_input()

    def _on_agent_done(self) -> None:
        self._agent_busy = False
        response = self._accumulated_response.strip()
        log = self.query_one("#chat-log", ChatLogWidget)

        if response or self._activity_lines:
            log.write_activity_summary(self._activity_lines)
            if response:
                log.write_pm_response(response)

        self._hide_streaming()

        if response:
            self._messages.append(
                ConversationMessage(role="assistant", content=response)
            )

        if response_has_ready_marker(response):
            log.write_system("generating PRD...")
            self._generate_prd()
        else:
            self._enable_input()

    # -- Streaming ---------------------------------------------------------

    def _show_streaming(self) -> None:
        d = self.query_one("#streaming-display", Static)
        d.display = True
        d.update("")

    def _hide_streaming(self) -> None:
        d = self.query_one("#streaming-display", Static)
        d.update("")
        d.display = False

    def _update_streaming(self) -> None:
        parts: list[str] = []

        # Activity: last 4 lines
        recent = self._activity_lines[-4:]
        if recent:
            from ralph.tui.widgets.chat_log import _shorten_activity
            for raw, _ in recent:
                s = _shorten_activity(raw)
                if s:
                    parts.append(f"    [dim]{s}[/dim]")

        # AI text
        ai = self._accumulated_response.strip()
        if ai:
            if parts:
                parts.append("")
            for line in ai.split("\n"):
                parts.append(f"  {line}")

        self.query_one("#streaming-display", Static).update(
            "\n".join(parts)
        )
        try:
            self.query_one("#chat-scroll", VerticalScroll).scroll_end(
                animate=False
            )
        except Exception:
            pass

    # -- PRD ---------------------------------------------------------------

    def _generate_prd(self) -> None:
        self._agent_busy = True
        self._disable_input()
        self.run_worker(
            self._prd_worker(), name="prd-gen", thread=True, exclusive=True,
        )

    async def _prd_worker(self) -> None:
        try:
            raw = await run_prd_generation(
                model=self._model,
                prompt=build_generation_prompt(self._messages),
                json_schema=PRD_JSON_SCHEMA,
                cwd=Path.cwd(),
            )
            self.app.call_from_thread(self._on_prd_generated, raw)
        except Exception as e:
            self.app.call_from_thread(self._on_agent_error, str(e))

    def _on_prd_generated(self, raw: str) -> None:
        self._agent_busy = False
        log = self.query_one("#chat-log", ChatLogWidget)
        prd = parse_prd_from_json_output(raw)
        if prd is not None:
            log.write_system(
                f"PRD: {prd.total_stories} stories, branch {prd.branch_name}"
            )
            from ralph.tui.screens.prd_review import PRDReviewScreen
            self.app.push_screen(PRDReviewScreen(prd=prd))
        else:
            log.write_system("PRD generation failed. type 'generate' to retry")
            self._enable_input()

    # -- Input -------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat-input":
            self._submit()

    def _submit(self) -> None:
        if self._agent_busy:
            return
        inp = self.query_one("#chat-input", Input)
        text = inp.value.strip()
        if not text:
            return
        inp.value = ""
        log = self.query_one("#chat-log", ChatLogWidget)
        log.write_user_message(text)
        self._messages.append(ConversationMessage(role="user", content=text))
        self._send_to_agent()

    def _disable_input(self) -> None:
        try:
            self.query_one("#chat-input", Input).disabled = True
        except Exception:
            pass

    def _enable_input(self) -> None:
        try:
            inp = self.query_one("#chat-input", Input)
            inp.disabled = False
            inp.focus()
        except Exception:
            pass

    def action_back(self) -> None:
        self.app.pop_screen()
