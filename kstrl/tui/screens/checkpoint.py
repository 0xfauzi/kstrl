"""E6 checkpoint modal (PR E).

Turns the checkpoint from a rubber stamp into an inspection surface:
the bounded diff excerpt, both finding streams, and the attempt's
spend - everything PromptRequest.checkpoint carries (PR A). Dismissal
values: 0 Approve / 1 Reject / 2 Retry / None leave-pending (Esc).
Wired to actually ANSWER the interaction channel in PR F; dash mode
never opens it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.interaction import PromptRequest


def _findings_block(title: str, findings: tuple[object, ...]) -> Text:
    text = Text()
    text.append(f"{title.lower()}\n", style=f"bold {theme.ACCENT}")
    if not findings:
        text.append("  none\n", style=theme.MUTED)
        return text
    for finding in findings:
        severity = getattr(finding, "severity", "")
        location = getattr(finding, "location", "")
        explanation = getattr(finding, "explanation", "")
        style = (
            theme.ERROR if severity in ("critical", "high", "fail")
            else theme.WARNING
        )
        text.append(f"  [{severity}] ", style=f"bold {style}")
        text.append(f"{location}  ", style="bold")
        text.append(f"{explanation}\n")
    return text


class CheckpointModal(ModalScreen[int | None]):
    BINDINGS = [
        Binding("a", "decide(0)", "Approve"),
        Binding("r", "decide(1)", "Reject"),
        Binding("t", "decide(2)", "Retry"),
        Binding("1", "decide(0)", show=False),
        Binding("2", "decide(1)", show=False),
        Binding("3", "decide(2)", show=False),
        Binding("escape", "leave_pending", "Later"),
    ]

    def __init__(self, request: PromptRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        ctx = self.request.checkpoint
        dialog = Vertical(id="checkpoint-dialog")
        dialog.border_title = "E6 checkpoint"
        with dialog:
            yield Label(self.request.header, id="checkpoint-question")
            if ctx is not None:
                summary = Text()
                if ctx.branch:
                    summary.append("branch ", style=theme.MUTED)
                    summary.append(ctx.branch, style="bold")
                if ctx.usage is not None and ctx.usage.calls:
                    marker = "+" if ctx.usage.unreported_calls else ""
                    summary.append("  ·  spend ", style=theme.MUTED)
                    summary.append(
                        f"{ctx.usage.total_tokens:,}{marker} tok, "
                        f"${ctx.usage.cost_usd:.2f}{marker}",
                        style="bold",
                    )
                yield Static(summary, id="checkpoint-summary")
                with VerticalScroll(id="checkpoint-body"):
                    yield Static(_findings_block(
                        "Review findings", ctx.review_findings,
                    ))
                    yield Static(_findings_block(
                        "Security findings", ctx.security_findings,
                    ))
                    diff_text = Text()
                    diff_text.append("diff\n", style=f"bold {theme.ACCENT}")
                    if ctx.diff_excerpt:
                        for line in ctx.diff_excerpt.splitlines():
                            style = (
                                theme.SUCCESS if line.startswith("+")
                                else theme.ERROR if line.startswith("-")
                                else theme.MUTED
                            )
                            diff_text.append(line + "\n", style=style)
                    else:
                        diff_text.append(
                            "  (no diff captured)\n", style=theme.MUTED,
                        )
                    yield Static(diff_text)
            with Horizontal(id="checkpoint-buttons"):
                # Quiet buttons; the TCSS gives choice-0 the single
                # accent treatment (one primary action per surface).
                for index, option in enumerate(self.request.options):
                    label = option.split(" (")[0]
                    yield Button(
                        f"{label} ({index + 1})", id=f"choice-{index}",
                    )

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

    def action_leave_pending(self) -> None:
        self.dismiss(None)
