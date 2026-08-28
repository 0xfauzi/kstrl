"""Safe-mode panel: the reasons behind the masthead banner and chip.

The banner can only say how many and which sources. This says what each
signal reported, in the signal's own words, and names the runbook
section that recovers it - the same three fields
``kstrl.safemode.SafeModeReason`` carries, because inventing a fifth
wording for a state that already has one is how surfaces drift.

The panel updates in place. It used to take its reasons at construction
and never look again, so opening it before the first background check
finished left it reading "not checked yet" for the life of the session,
and a later check left it showing reasons that had already cleared. A
panel whose whole job is to be the precise surface cannot be the stale
one.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from kstrl.safemode import SafeModeReason
from kstrl.tui import theme


def panel_title(reasons: list[SafeModeReason] | None) -> str:
    """Three states, three titles. This is the surface that must never
    conflate them: the banner is hidden while nominal, so the panel is
    where "not checked yet" and "checked and clear" are told apart."""
    if reasons is None:
        return "safe mode: not checked yet"
    if reasons:
        return f"safe mode: {len(reasons)} reason(s)"
    return "safe mode: nominal"


def render_body(reasons: list[SafeModeReason] | None) -> Text:
    text = Text()
    if reasons is None:
        text.append(
            "The background check has not reported yet.", style=theme.MUTED,
        )
        return text
    if not reasons:
        text.append("Every signal is clear.\n", style=theme.SUCCESS)
        text.append(
            "Control directory trusted, autonomy at its earned level, "
            "queue running, and the last finished factory run skipped no "
            "adversarial phase.",
            style=theme.MUTED,
        )
        return text
    for index, reason in enumerate(reasons):
        if index:
            text.append("\n\n")
        text.append(
            f" {reason.source} ",
            style=f"bold {theme.BACKGROUND} on {theme.WARNING}",
        )
        text.append("\n")
        text.append(reason.detail)
        text.append(f"\n see {reason.recovery}", style=theme.MUTED)
    return text


class SafeModePanel(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("f2", "close", show=False),
        Binding("q", "close", show=False),
    ]

    def __init__(self, reasons: list[SafeModeReason] | None) -> None:
        super().__init__()
        self._panel_reasons = reasons

    def compose(self) -> ComposeResult:
        dialog = Vertical(id="safemode-dialog")
        dialog.border_title = panel_title(self._panel_reasons)
        with dialog:
            with VerticalScroll(id="safemode-scroll"):
                yield Static(
                    render_body(self._panel_reasons), id="safemode-body",
                )

    def on_mount(self) -> None:
        # Replay the last completed check. The broadcast only reaches a
        # panel that is open when a check LANDS; a panel opened after
        # the last one finished would otherwise sit on whatever it was
        # constructed with until the next interval.
        self.update_safe_mode(getattr(self.app, "_safe_mode_reasons", None))

    def update_safe_mode(
        self, reasons: list[SafeModeReason] | None,
    ) -> None:
        """Duck-typed contract the app broadcasts to every live screen."""
        self._panel_reasons = reasons
        dialog = next(iter(self.query("#safemode-dialog")), None)
        body = next(iter(self.query("#safemode-body")), None)
        if dialog is None or body is None:
            return  # mid-mount; compose will render the new value
        dialog.border_title = panel_title(reasons)
        assert isinstance(body, Static)
        body.update(render_body(reasons))

    def action_close(self) -> None:
        self.dismiss(None)
