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

#433 E1: approve, reject and snooze are in the footer only while an open
item is selected; an empty inbox offered all three. The table is hidden
while it has no rows, leaving the one sentence that says the inbox is
clear. With no root given, the screen reads the app's root, like every
other home screen, rather than the process's working directory.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from rich.console import Group
from rich.padding import Padding
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from kstrl.inbox import Inbox, InboxConfig, InboxError, InboxItem, ItemStatus
from kstrl.statedir import ControlStateError
from kstrl.tui import theme
from kstrl.tui.inbox_consequences import Consequences, consequences, kind_label
from kstrl.tui.run_status import age_phrase
from kstrl.tui.widgets.config_problem import ConfigProblemBanner
from kstrl.tui.widgets.context_bar import ContextBar

_PRIORITY_STYLE = {"high": "bold red", "normal": "", "low": "dim"}
_DECISIONS = frozenset({"approve", "reject", "snooze"})


#: Evidence keys in the detail's words; any other key loses its underscores.
EVIDENCE_LABELS = {
    "pr": "PR",
    "head_sha": "PR head",
    "open_findings": "open findings",
    "evidence": "evidence file",
}


def item_age(item: InboxItem, now: float | None = None) -> str:
    """How long ago the item was last raised, from its own timestamps."""
    for stamp in (item.last_seen_at, item.created_at):
        try:
            moment = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return age_phrase((time.time() if now is None else now) - moment.timestamp())
    return theme.EMPTY_CELL


def evidence_line(key: str, value: object) -> str:
    """``PR head: 87c3e2efbe2c``: a label, and a list as a list (#433 G6)."""
    label = EVIDENCE_LABELS.get(key, key.replace("_", " "))
    shown = ", ".join(str(v) for v in value) if isinstance(value, list | tuple) else str(value)
    return f"{label}: {shown}"


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
        Binding("o", "toggle_decided", "decided"),
        Binding("f5", "refresh", "refresh"),
    ]

    def __init__(self, root_dir: Any = None, select: str = "") -> None:
        super().__init__()
        #: The item to put the cursor on at mount (home's needs-you row).
        self._select = select
        from pathlib import Path

        self._root_arg = Path(root_dir) if root_dir else None
        self._show_decided = False
        self._items: list[InboxItem] = []
        #: Manifest status per component, None when the manifest could not
        #: be read; what a merge-gate decision acts on (#433 Q4).
        self._statuses: dict[str, str] | None = {}
        self._snooze_hours = InboxConfig().snooze_hours

    # -- composition -------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield ContextBar("inbox")
        yield ConfigProblemBanner()
        # List above detail. Side by side, the table was given no width
        # and rendered one cell wide, so the list of items could not be
        # seen at any terminal size (#433 E1).
        yield DataTable(id="inbox-table")
        # Scrolls: at 80x24 the last choice ran past the footer (#433 G6).
        with VerticalScroll(id="inbox-detail-scroll"):
            yield Static("", id="inbox-detail")
        yield Footer()

    @property
    def _root(self) -> Any:
        from pathlib import Path

        if self._root_arg is not None:
            return self._root_arg
        return getattr(self.app, "root_dir", None) or Path.cwd()

    def on_mount(self) -> None:
        table = self.query_one("#inbox-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("", "kind", "title", "age", "status")
        self.action_refresh()

    # -- data --------------------------------------------------------------
    def _inbox(self) -> Inbox | None:
        """The inbox, or None with the banner saying why (#289).

        Re-read per call rather than cached: this screen outlives edits
        to kstrl.toml, which is the same reason the config screen has a
        refresh action. That is also why the guard is here and not only
        at mount - the file can break between two keystrokes.
        """
        config = self.query_one(ConfigProblemBanner).load(InboxConfig.load, self._root)
        return None if config is None else Inbox(self._root, config)

    def action_refresh(self) -> None:
        self._redraw(self._inbox())

    def _redraw(self, box: Inbox | None) -> None:
        """The table and detail for a box already loaded, or for None.

        Split from the load so a caller holding the answer does not
        throw it away and load again to get the same one.
        """
        table = self.query_one("#inbox-table", DataTable)
        table.clear()
        if box is None:
            self._items = []
            self._render_detail()
            return
        self._items = box.items() if self._show_decided else box.open_items()
        self._snooze_hours = box.config.snooze_hours
        self._statuses = self._manifest_statuses()
        for item in self._items:
            repeat = f" x{item.occurrences}" if item.occurrences > 1 else ""
            table.add_row(
                priority_marker(str(item.priority)),
                kind_label(item.kind),
                f"{item.title}{repeat}",
                item_age(item),
                str(ItemStatus.OPEN) if item.is_open else str(item.status),
            )
        table.display = bool(self._items)
        if self._select:
            ids = [item.id for item in self._items]
            if self._select in ids:
                table.move_cursor(row=ids.index(self._select))
            self._select = ""
        self._render_detail()

    def _manifest_statuses(self) -> dict[str, str] | None:
        """Component statuses; None when a manifest exists and cannot be read."""
        from pathlib import Path

        from kstrl.tui.operator_queue import load_manifest

        manifest, problem = load_manifest(Path(self._root))
        if problem:
            return None
        return {comp.id: comp.status for comp in manifest.components} if manifest else {}

    def _consequences(self, item: InboxItem) -> Consequences:
        status = None if self._statuses is None else self._statuses.get(item.component, "absent")
        return consequences(item, status, self._snooze_hours)

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
        self.refresh_bindings()
        detail = self.query_one("#inbox-detail", Static)
        item = self._selected()
        if item is None:
            # "Inbox clear" is a claim about the log. It must never be
            # made from an empty list that only means the config would
            # not load, so the banner is asked rather than a second
            # copy of its answer being kept beside it.
            unreadable = self.query_one(ConfigProblemBanner).problem is not None
            detail.update(
                Text("Inbox clear: nothing is waiting on you.", style="dim")
                if not self._items and not unreadable
                else Text("")
            )
            return
        lines = Text()
        lines.append(f"{item.title}\n", style="bold")
        seen = f" · seen {item.occurrences} times" if item.occurrences > 1 else ""
        lines.append(
            f"{kind_label(item.kind)} · {item.priority} priority · raised {item_age(item)} ago"
            f"{seen}\n",
            style="dim",
        )
        if item.component:
            lines.append(f"component: {item.component}\n")
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
            lines.append(f"  {evidence_line(key, value)}\n", style="dim")
        if not item.is_open:
            detail.update(lines)
            return
        detail.update(Group(lines, choices_text(self._consequences(item))))

    def on_data_table_row_highlighted(self, _event: object) -> None:
        self._render_detail()

    def check_action(self, action: str, _parameters: tuple[object, ...]) -> bool | None:
        """Decisions are offered only for a selected item that is open,
        and only the ones whose effect is known (#433 Q4)."""
        if action in _DECISIONS:
            item = self._selected()
            return item is not None and item.is_open and self._consequences(item).allows(action)
        return True

    # -- actions -----------------------------------------------------------
    def _decide(self, action: str, comment: str = "") -> None:
        item = self._selected()
        if item is None or not self._consequences(item).allows(action):
            return
        box = self._inbox()
        if box is None:
            # The file broke since the list was drawn. Render that
            # state from the answer already in hand.
            self._redraw(None)
            return
        try:
            if action == "approve":
                box.approve(item.id, actor=self._actor(), comment=comment)
            elif action == "reject":
                box.reject(item.id, actor=self._actor(), comment=comment)
            else:
                box.snooze(item.id, actor=self._actor())
        except (InboxError, ControlStateError) as exc:
            # ControlStateError is a RuntimeError, so it escapes an
            # InboxError clause: every decide reaches Inbox._append,
            # which takes the control lock, and an uncaught one here
            # takes the Textual event loop down rather than telling the
            # operator their keystroke did nothing.
            self.notify(str(exc), severity="error")
            return
        self.notify(f"{action}d: {item.title}")
        # _redraw, not action_refresh: this holds the Inbox already and
        # `box.items()` re-reads the log from disk, so the post-decision
        # state renders identically with one config load instead of two.
        # Throwing the answer away to load it again is the exact waste
        # _redraw was split out to prevent, and on a broken kstrl.toml
        # it doubled the os.environ scrub window per keystroke.
        self._redraw(box)

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


def choices_text(choices: Consequences) -> Group:
    """What each offered key does, from the code that reads the decision;
    each sentence wraps under itself, not back at the left edge (#433 H7)."""
    keys = {"approve": "a", "reject": "r", "snooze": "s"}
    rows = [
        (Text(f"{keys[choice]} {choice}:", style=f"bold {theme.ACCENT}"), Text(sentence))
        for choice, sentence in choices.offered
    ]
    notes = [
        Text(said, style=style)
        for said, style in ((choices.withheld, theme.WARNING), (choices.note, theme.MUTED))
        if said
    ]
    return Group(
        Text("\nwhat each choice does", style=f"bold {theme.MUTED}"),
        Padding(Group(theme.label_rows(rows), *notes), (0, 0, 0, 2)),
    )


__all__ = ["InboxScreen", "priority_marker"]
