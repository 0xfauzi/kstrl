"""Quit confirmation for embedded mode (stage 3 PR F)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class QuitModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Stop run"),
        Binding("n", "keep", "Keep running"),
        Binding("escape", "keep", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-dialog"):
            yield Label("Stop the run?", id="quit-question")
            yield Label(
                "In-flight agents will be group-killed, worktrees "
                "cleaned, the manifest flushed; the run exits 130. "
                "Press q again after confirming to force-kill.",
                id="quit-detail",
            )
            with Horizontal(id="quit-buttons"):
                yield Button("Stop (y)", id="stop", variant="error")
                yield Button("Keep running (n)", id="keep", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "stop")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_keep(self) -> None:
        self.dismiss(False)
