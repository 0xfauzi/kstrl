"""Lean options modal for generic prompts (TUI surface A3).

Every non-checkpoint PromptRequest (feature review gate, evolve apply,
retry confirm, guard/iteration prompts) lands here: header + one
button per option, digits 1..n decide, Enter takes the default,
Esc leaves the prompt pending (the request stays open; press c to
reopen - identical semantics to the checkpoint modal).

The checkpoint modal is NOT reused for these: its a/r/t bindings
hardcode three options, and on a 2-option CONFIRM the third key
dismissed with an out-of-range choice - the channel rejected it,
leaving the orchestrator blocked with the modal gone (D9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

if TYPE_CHECKING:
    from kstrl.interaction import PromptRequest

MAX_KEYED_OPTIONS = 9


class OptionsModal(ModalScreen[int | None]):
    # Enter is NOT bound at screen level: the default button holds
    # focus, so Enter presses it - a screen binding would double-fire.
    BINDINGS = [
        Binding("escape", "leave_pending", "Later"),
        *[
            Binding(str(n + 1), f"decide({n})", show=False)
            for n in range(MAX_KEYED_OPTIONS)
        ],
    ]

    def __init__(self, request: PromptRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        dialog = Vertical(id="options-dialog")
        dialog.border_title = self.request.kind.value
        with dialog:
            yield Label(self.request.header, id="options-question")
            with Horizontal(id="options-buttons"):
                for index, option in enumerate(self.request.options):
                    label = option.split(" (")[0]
                    button = Button(
                        f"{label} ({index + 1})", id=f"choice-{index}",
                    )
                    if index == self.request.default:
                        button.add_class("default-choice")
                    yield button

    def on_mount(self) -> None:
        buttons = list(self.query(Button))
        default = self.request.default
        if 0 <= default < len(buttons):
            buttons[default].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("choice-"):
            return
        try:
            choice = int(button_id.removeprefix("choice-"))
        except ValueError:
            return
        self.action_decide(choice)

    def action_decide(self, choice: int) -> None:
        if 0 <= choice < len(self.request.options):
            self.dismiss(choice)

    def action_take_default(self) -> None:
        self.action_decide(self.request.default)

    def action_leave_pending(self) -> None:
        self.dismiss(None)
