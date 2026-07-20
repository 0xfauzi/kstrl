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

if TYPE_CHECKING:
    from ralph_py.interaction import PromptRequest


def _findings_block(title: str, findings: tuple[object, ...]) -> Text:
    text = Text()
    text.append(f"{title}\n", style="bold underline")
    if not findings:
        text.append("  (none)\n", style="dim")
        return text
    for finding in findings:
        severity = getattr(finding, "severity", "")
        location = getattr(finding, "location", "")
        explanation = getattr(finding, "explanation", "")
        style = "red" if severity in ("critical", "high", "fail") else "yellow"
        text.append(f"  [{severity}] ", style=style)
        text.append(f"{location}  ", style="bold")
        text.append(f"{explanation}\n")
    return text


class CheckpointModal(ModalScreen[int | None]):
    BINDINGS = [
        Binding("a", "decide(0)", "Approve"),
        Binding("r", "decide(1)", "Reject"),
        Binding("t", "decide(2)", "Retry"),
        Binding("escape", "leave_pending", "Later"),
    ]

    def __init__(self, request: PromptRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        ctx = self.request.checkpoint
        with Vertical(id="checkpoint-dialog"):
            yield Label(self.request.header, id="checkpoint-question")
            if ctx is not None:
                summary = Text()
                if ctx.branch:
                    summary.append("branch ", style="dim")
                    summary.append(ctx.branch, style="bold")
                if ctx.usage is not None and ctx.usage.calls:
                    marker = "+" if ctx.usage.unreported_calls else ""
                    summary.append("   spend ", style="dim")
                    summary.append(
                        f"{ctx.usage.total_tokens}{marker} tokens, "
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
                    diff_text.append("Diff\n", style="bold underline")
                    if ctx.diff_excerpt:
                        for line in ctx.diff_excerpt.splitlines():
                            style = (
                                "green" if line.startswith("+")
                                else "red" if line.startswith("-")
                                else "dim"
                            )
                            diff_text.append(line + "\n", style=style)
                    else:
                        diff_text.append("  (no diff captured)\n", style="dim")
                    yield Static(diff_text)
            with Horizontal(id="checkpoint-buttons"):
                yield Button("Approve (a)", id="approve", variant="success")
                yield Button("Reject (r)", id="reject", variant="error")
                yield Button("Retry (t)", id="retry", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {"approve": 0, "reject": 1, "retry": 2}
        choice = choices.get(event.button.id or "")
        if choice is not None:
            self.dismiss(choice)

    def action_decide(self, choice: int) -> None:
        self.dismiss(choice)

    def action_leave_pending(self) -> None:
        self.dismiss(None)
