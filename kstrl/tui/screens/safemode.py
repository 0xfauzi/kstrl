"""Safe-mode panel: the reasons behind the masthead chip.

The chip can only say how many and which sources. This says what each
signal actually reported, in the signal's own words, and names the
runbook section that recovers it - the same three fields
``kstrl.safemode.SafeModeReason`` carries, because inventing a fifth
wording for a state that already has one is how surfaces drift.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from kstrl.safemode import SafeModeReason
from kstrl.tui import theme


def render_reason(reason: SafeModeReason) -> Text:
    text = Text()
    text.append(f" {reason.source} ", style=f"bold {theme.BACKGROUND} on {theme.WARNING}")
    text.append("  ")
    text.append(reason.detail)
    text.append(f"\n  see {reason.recovery}", style=theme.MUTED)
    return text


def panel_title(reasons: list[SafeModeReason] | None) -> str:
    """Three states, three titles. This is the surface that must never
    conflate them: the banner is hidden while nominal, so the panel is
    where "not checked yet" and "checked and clear" are told apart."""
    if reasons is None:
        return "safe mode: not checked yet"
    if reasons:
        return f"safe mode: {len(reasons)} reason(s)"
    return "safe mode: nominal"


class SafeModePanel(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("m", "close", show=False),
        Binding("q", "close", show=False),
    ]

    def __init__(self, reasons: list[SafeModeReason] | None) -> None:
        super().__init__()
        self._panel_reasons = reasons

    def compose(self) -> ComposeResult:
        reasons = self._panel_reasons
        dialog = Vertical(id="safemode-dialog")
        dialog.border_title = panel_title(reasons)
        with dialog:
            if reasons is None:
                yield Label(
                    "The background check has not reported yet. It runs a "
                    "few seconds after the dashboard opens and every few "
                    "seconds after that.",
                    id="safemode-empty",
                )
            elif not reasons:
                yield Label(
                    "Every signal is clear: the control directory is "
                    "trusted, the autonomy ladder is at the level it "
                    "earned, the queue is running, and the last finished "
                    "factory run skipped no adversarial phase.",
                    id="safemode-empty",
                )
            else:
                with VerticalScroll(id="safemode-reasons"):
                    for index, reason in enumerate(reasons):
                        yield Static(
                            render_reason(reason),
                            classes="safemode-reason",
                            id=f"safemode-reason-{index}",
                        )

    def action_close(self) -> None:
        self.dismiss(None)
