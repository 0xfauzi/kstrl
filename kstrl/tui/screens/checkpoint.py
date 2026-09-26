"""Checkpoint modal: approve a component's PR before it is merged (PR E).

Turns the checkpoint from a rubber stamp into an inspection surface:
the bounded diff excerpt, both finding streams, and the attempt's
spend - everything PromptRequest.checkpoint carries (PR A). Dismissal
values: 0 Approve / 1 Reject / 2 Retry / None leave-pending (Esc).
Wired to actually ANSWER the interaction channel in PR F; dash mode
never opens it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Group
from rich.padding import Padding
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from kstrl.tui import theme
from kstrl.tui.widgets.cost_meter import at_least

if TYPE_CHECKING:
    from kstrl.interaction import PromptRequest


def readable_diff(diff: str) -> list[str]:
    """The diff's file names and changed lines, without git's headers (#433 G7).

    A file's header (``diff --git``, ``index 980e96d..79a0275 100644``,
    the mode and rename lines, ``---``/``+++``) runs from its ``diff --git``
    line to its first ``@@`` hunk; it becomes one ``file <path>`` line.
    """
    lines: list[str] = []
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header = True
            lines.append(f"file {line.rsplit(' b/', 1)[-1]}")
        elif line.startswith("@@"):
            in_header = False
            lines.append(line)
        elif not in_header:
            lines.append(line)
    return lines


def _diff_style(line: str) -> str:
    """A ``readable_diff`` line's style: file names, additions, removals."""
    if line.startswith("file "):
        return f"bold {theme.ACCENT}"
    if line.startswith("+"):
        return theme.SUCCESS
    return theme.ERROR if line.startswith("-") else theme.MUTED


def _findings_block(title: str, findings: tuple[object, ...]) -> Group:
    """Each finding's text wraps under itself, after the severity tag (#433 H8)."""
    heading = Text(title.lower(), style=f"bold {theme.ACCENT}")
    if not findings:
        return Group(heading, Text("  none\n", style=theme.MUTED))
    rows = []
    for finding in findings:
        severity = getattr(finding, "severity", "")
        style = theme.ERROR if severity in ("critical", "high", "fail") else theme.WARNING
        said = Text()
        said.append(f"{getattr(finding, 'location', '')}  ", style="bold")
        said.append(str(getattr(finding, "explanation", "")))
        rows.append((Text(f"[{severity}]", style=f"bold {style}"), said))
    return Group(heading, Padding(theme.label_rows(rows), (0, 0, 1, 2)))


#: What each answer makes the pipeline do (#433 H9), read from
#: ``Pipeline._phase_checkpoint`` and the branch after it in kstrl/pipeline.py:
#: Reject is ``fail(..., check="hitl_reject")``, returned before ``_phase_pr``,
#: so the component is FAILED, its dependents are cascade-skipped, nothing is
#: pushed and no retry is counted; Retry is ``retry_or_fail(..., check="hitl_retry")``
#: with "Human reviewer requested changes at PR checkpoint" added to the
#: engineer's context, PENDING with one more retry used while retries remain,
#: else FAILED; Approve goes on to ``_phase_pr``, which pushes the branch,
#: opens the PR and merges it, and the component is COMPLETED only on a
#: confirmed merge (R0.2). tests/test_pipeline.py pins the first two
#: (test_checkpoint_reject_fails_component, test_checkpoint_retry_consumes_a_retry).
CHOICE_EFFECTS = {
    "Approve": "pushes {branch}, opens its PR and merges it; {cid} completes only once "
    "the merge is confirmed.",
    "Reject": "{cid} fails and its dependents are skipped; nothing is pushed.",
    "Retry": "the engineer runs {cid} again with a note that a human reviewer asked for "
    "changes; no reason is passed on. Uses one retry; with none left, {cid} fails as on Reject.",
}


def choice_effects(request: PromptRequest) -> Group:
    """What each offered answer does, for the answers the pipeline defines."""
    ctx = request.checkpoint
    cid = request.component_id or (ctx.component_id if ctx else "") or "the component"
    branch = ctx.branch if ctx is not None and ctx.branch else "the branch"
    rows = [
        (Text(label, style="bold"), Text(CHOICE_EFFECTS[label].format(cid=cid, branch=branch)))
        for label in (option.split(" (")[0] for option in request.options)
        if label in CHOICE_EFFECTS
    ]
    heading = Text("what each choice does", style=f"bold {theme.MUTED}")
    return Group(heading, Padding(theme.label_rows(rows), (0, 0, 0, 2)))


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
        dialog.border_title = "approve before merge"
        with dialog:
            yield Label(self.request.header, id="checkpoint-question")
            if ctx is not None:
                summary = Text()
                if ctx.branch:
                    summary.append("branch ", style=theme.MUTED)
                    summary.append(ctx.branch, style="bold")
                if ctx.usage is not None and ctx.usage.calls:
                    bound = bool(ctx.usage.unreported_calls)
                    summary.append("  ·  spend ", style=theme.MUTED)
                    summary.append(
                        f"{at_least(f'{ctx.usage.total_tokens:,}', bound)} tok, "
                        f"{at_least(f'${ctx.usage.cost_usd:.2f}', bound)}",
                        style="bold",
                    )
                yield Static(summary, id="checkpoint-summary")
                with VerticalScroll(id="checkpoint-body"):
                    yield Static(
                        _findings_block(
                            "Review findings",
                            ctx.review_findings,
                        )
                    )
                    yield Static(
                        _findings_block(
                            "Security findings",
                            ctx.security_findings,
                        )
                    )
                    diff_text = Text()
                    diff_text.append("diff\n", style=f"bold {theme.ACCENT}")
                    if ctx.diff_excerpt:
                        for line in readable_diff(ctx.diff_excerpt):
                            diff_text.append(line + "\n", style=_diff_style(line))
                    else:
                        diff_text.append(
                            "  (no diff captured)\n",
                            style=theme.MUTED,
                        )
                    yield Static(diff_text)
            yield Static(choice_effects(self.request), id="checkpoint-effects")
            with Horizontal(id="checkpoint-buttons"):
                # Quiet buttons; the TCSS gives choice-0 the single
                # accent treatment (one primary action per surface).
                for index, option in enumerate(self.request.options):
                    label = option.split(" (")[0]
                    yield Button(
                        f"{label} ({index + 1})",
                        id=f"choice-{index}",
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
