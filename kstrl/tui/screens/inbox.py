"""Inbox screen: triage what is waiting on a human (R8.3).

Master-detail over ``.kstrl/inbox.jsonl`` with the REAL decision path -
the same ``Inbox`` methods ``ks inbox`` calls, so a decision made here is
indistinguishable from one made at the CLI and lands in the same
append-only log.

One-key actions in the spirit of a triage queue: ``a`` approve, ``r``
reject, ``s`` snooze, ``o`` toggle decided items. Reject opens a comment
prompt rather than accepting a bare "no" - a rejection nobody can explain
later is not a decision.

Requeue is deliberately CLI-only (``ks inbox retry``): it mutates the
manifest, and a keystroke away from a component reset is the kind of
thing that should cost one more deliberate step.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from kstrl.inbox import Inbox, InboxConfig, InboxError, InboxItem
from kstrl.tui.widgets.context_bar import ContextBar

_PRIORITY_STYLE = {"high": "bold red", "normal": "", "low": "dim"}


def priority_marker(priority: str) -> Text:
    """A one-cell severity cue; low priority stays visually quiet."""
    glyph = {"high": "!", "normal": "•", "low": "·"}.get(priority, "•")
    return Text(glyph, style=_PRIORITY_STYLE.get(priority, ""))


class InboxScreen(Screen[None]):
    """Triage surface for exceptions awaiting a decision."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("a", "approve", "approve"),
        Binding("r", "reject", "reject"),
        Binding("s", "snooze", "snooze"),
        Binding("o", "toggle_decided", "show/hide decided"),
        Binding("f5", "refresh", "refresh"),
    ]

    def __init__(self, root_dir: Any = None) -> None:
        super().__init__()
        from pathlib import Path

        self._root = Path(root_dir) if root_dir else Path.cwd()
        self._show_decided = False
        self._items: list[InboxItem] = []

    # -- composition -------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield ContextBar("inbox")
        with Horizontal():
            yield DataTable(id="inbox-table")
            yield Static("", id="inbox-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#inbox-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("", "kind", "title", "status")
        self.action_refresh()

    # -- data --------------------------------------------------------------
    def _inbox(self) -> Inbox:
        return Inbox(self._root, InboxConfig.load(self._root))

    def action_refresh(self) -> None:
        box = self._inbox()
        self._items = box.items() if self._show_decided else box.open_items()
        table = self.query_one("#inbox-table", DataTable)
        table.clear()
        for item in self._items:
            repeat = f" x{item.occurrences}" if item.occurrences > 1 else ""
            table.add_row(
                priority_marker(str(item.priority)),
                str(item.kind),
                f"{item.title}{repeat}",
                "" if item.is_open else str(item.status),
            )
        self._render_detail()

    def action_toggle_decided(self) -> None:
        self._show_decided = not self._show_decided
        self.action_refresh()

    def _selected(self) -> InboxItem | None:
        table = self.query_one("#inbox-table", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self._items)):
            return None
        return self._items[row]

    def _render_detail(self) -> None:
        detail = self.query_one("#inbox-detail", Static)
        item = self._selected()
        if item is None:
            detail.update(
                Text("Inbox clear: nothing is waiting on you.", style="dim")
                if not self._items
                else Text("")
            )
            return
        lines = Text()
        lines.append(f"{item.title}\n", style="bold")
        lines.append(f"{item.kind}  priority={item.priority}\n", style="dim")
        if item.component:
            lines.append(f"component: {item.component}\n")
        if item.occurrences > 1:
            lines.append(f"seen {item.occurrences}x\n")
        if item.decided_by:
            lines.append(
                f"decided by {item.decided_by} at {item.decided_at}\n",
                style="dim",
            )
        if item.decision_comment:
            lines.append(f"comment: {item.decision_comment}\n", style="dim")
        if item.detail:
            lines.append(f"\n{item.detail}\n")
        for key, value in item.evidence.items():
            lines.append(f"  {key}: {value}\n", style="dim")
        detail.update(lines)

    def on_data_table_row_highlighted(self, _event: object) -> None:
        self._render_detail()

    # -- actions -----------------------------------------------------------
    def _decide(self, action: str, comment: str = "") -> None:
        item = self._selected()
        if item is None:
            return
        box = self._inbox()
        try:
            if action == "approve":
                box.approve(item.id, actor=self._actor(), comment=comment)
            elif action == "reject":
                box.reject(item.id, actor=self._actor(), comment=comment)
            else:
                box.snooze(item.id, actor=self._actor())
        except InboxError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"{action}d: {item.title}")
        self.action_refresh()

    @staticmethod
    def _actor() -> str:
        import os

        return os.environ.get("USER") or os.environ.get("USERNAME") or "operator"

    def action_approve(self) -> None:
        self._decide("approve")

    def action_snooze(self) -> None:
        self._decide("snooze")

    def action_reject(self) -> None:
        """Reject needs a reason, so this prompts rather than acting."""
        item = self._selected()
        if item is None:
            return
        from kstrl.interaction import PromptKind, PromptRequest
        from kstrl.tui.screens.options import OptionsModal

        reasons = (
            "not a real problem",
            "needs a spec change",
            "will fix by hand",
        )

        def _handle(choice: int | None) -> None:
            if choice is not None and 0 <= choice < len(reasons):
                self._decide("reject", comment=reasons[choice])

        self.app.push_screen(
            OptionsModal(
                PromptRequest(
                    kind=PromptKind.GUARD,
                    header=f"Reject: {item.title}",
                    options=reasons,
                    default=0,
                )
            ),
            _handle,
        )


__all__ = ["InboxScreen", "priority_marker"]
