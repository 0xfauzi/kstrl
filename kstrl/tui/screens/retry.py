"""Retry surface: the failure queue, a retry's scope, relaunch (D6, #433).

The table is a failure queue (``operator_queue.failure_queue``): every
failed component of the recent runs, the current ones (still FAILED in
the manifest) first with their recovery, then the ones a later run
superseded, naming it. The manifest's failures show at once; the run
columns fill when the worker has folded the runs.

``r`` works out the selected retry's scope on a worker
(``retry_scope``): where it starts, what it resets, the worktree and
branch it removes, what it keeps and what the relaunch runs under. The
confirmation is offered only when every part is known. On confirm,
``prepare_retry`` does the real mutation and its narration is shown,
warnings first: a process the #537 sweep killed in the evidence worktree
used to go to a discarded buffer. The factory then relaunches through
the D6 session seam.

#433 E2: with nothing to retry the screen says so once, in one sentence,
shows no empty table, and does not offer ``r``.
"""

from __future__ import annotations

import io
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from kstrl.interaction import PromptKind, PromptRequest
from kstrl.launch import FactoryLaunch
from kstrl.retry_plan import RESUME_REFUSAL, RetryError, prepare_retry, preview_retry
from kstrl.tui import theme
from kstrl.tui.home_view import fit_rows
from kstrl.tui.messages import FailuresRead, ScopeRead
from kstrl.tui.operator_queue import FailureEntry, failure_queue
from kstrl.tui.retry_carry import VALUE_NOTE, Carry, read_carry
from kstrl.tui.retry_scope import RetryScope, retry_scope
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.widgets.component_detail import gate_log_excerpt, shown_path
from kstrl.tui.widgets.context_bar import ContextBar
from kstrl.ui.plain import PlainUI

#: How many recent runs the failure queue folds; the home run list's cap.
FAILURE_RUN_LIMIT = 15

if TYPE_CHECKING:
    from kstrl.manifest import Component, Manifest
    from kstrl.retry_plan import RetryPreview


def _load_manifest(root_dir: Path) -> tuple[Manifest | None, Path]:
    from kstrl.manifest import Manifest

    manifest_file = root_dir / "scripts" / "kstrl" / "manifest.json"
    if not manifest_file.exists():
        return None, manifest_file
    try:
        return Manifest.load(manifest_file), manifest_file
    except (OSError, ValueError):
        return None, manifest_file


def _nothing_to_retry(manifest: Manifest | None, manifest_file: Path) -> str:
    if manifest is None:
        return f"Nothing to retry: there is no readable manifest at {manifest_file}."
    return f"Nothing to retry: no component in {manifest_file.name} is in the failed state."


#: Failure queue columns (#433, advice-r1 section 3).
COLUMNS = ("run", "component", "cause", "tries", "failed", "recovery")
#: Below this width the "failed" column gives way (the detail names the
#: time), so the recovery column is never cut (#433 G2).
NARROW_BELOW = 100
#: The recovery word when only ``ks retry`` can carry the retry (#433 G1).
CLI_ONLY = "retry via CLI"


def columns_for(width: int) -> tuple[str, ...]:
    return tuple(c for c in COLUMNS if width >= NARROW_BELOW or c != "failed")


def _when(ts: float) -> str:
    if ts <= 0:
        return theme.EMPTY_CELL
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def scope_header(scope: RetryScope) -> str:
    """The confirmation question: the retry and its whole scope."""
    lines = [f"Retry '{scope.component_id}'?"]
    lines.extend(f"{line.label}: {line.value}" for line in scope.lines if line.label != "stays out")
    preview = scope.preview
    if preview is not None and preview.not_in_retry:
        lines.append("Not in this retry:")
        lines.extend(f"  {line}" for line in preview.not_in_retry)
    return "\n".join(lines)


def scope_text(scope: RetryScope) -> Text:
    text = Text()
    text.append("retry scope", style=f"bold {theme.ACCENT}")
    for line in scope.lines:
        text.append(f"\n  {line.label:<11}", style=theme.MUTED)
        text.append(line.value, style="" if line.known else f"bold {theme.WARNING}")
    if scope.unknown:
        text.append(
            f"\nnot offered: the effect on {', '.join(scope.unknown)} is unknown",
            style=f"bold {theme.WARNING}",
        )
    elif scope.refusal:
        text.append(f"\nnot offered here: {scope.refusal}", style=f"bold {theme.WARNING}")
    return text


def narration_lines(narration: str) -> list[str]:
    """``prepare_retry``'s narration, blank lines dropped."""
    return [line.rstrip() for line in narration.splitlines() if line.strip()]


class RetryScreen(Screen[None]):
    """The failure queue: every failed component of the recent runs.

    Current failures (the manifest still records them FAILED) come first,
    with the recovery the retry offers; failures a later run superseded
    follow, naming that run, and offer nothing. ``r`` works out the
    selected retry's scope off the event loop and offers it only when
    every part of that scope is known (``retry_scope``).
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "retry_selected", "Review retry"),
        Binding("o", "open_output", "Full output"),
    ]

    COLUMNS = COLUMNS

    def __init__(self, select: str = "") -> None:
        super().__init__()
        #: The component to put the cursor on (home's needs-you row).
        self._select = select
        self._entries: list[FailureEntry] = []
        self._scoping = False
        self._scope: RetryScope | None = None
        #: Whether this surface can carry a retry; None until read.
        self._carry: Carry | None = None

    def compose(self) -> ComposeResult:
        yield ContextBar("retry", "failure queue: what failed, and what a retry would do")
        yield Static("failures", id="retry-title")
        yield DataTable(id="retry-table")
        with VerticalScroll(id="retry-detail-scroll"):
            yield Static(id="retry-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = False
        self.reload()

    def on_resize(self) -> None:
        if self._entries:
            self._show_entries(self._entries)

    def _root_dir(self) -> Path:
        root = getattr(self.app, "root_dir", None)
        return root if root is not None else Path.cwd()

    def reload(self) -> None:
        """The manifest's failures now; the runs' failures when the worker lands."""
        manifest, manifest_file = _load_manifest(self._root_dir())
        self._manifest = manifest
        self._manifest_file = manifest_file
        self._scope = None
        self._carry = None
        entries = failure_queue(manifest, [], {}) if manifest is not None else []
        self._show_entries(entries)
        root = self._root_dir()

        def _work() -> None:
            try:
                queue = _read_failure_queue(root)
            except Exception:  # noqa: BLE001 - the manifest rows stay up
                return
            self.post_message(FailuresRead(queue, _read_queue_carry(root, queue)))

        self.run_worker(_work, thread=True, group="failures", exclusive=True)

    def on_failures_read(self, message: FailuresRead) -> None:
        self._carry = message.carry
        self._show_entries(message.entries)

    def _show_entries(self, entries: list[FailureEntry]) -> None:
        table = self.query_one(DataTable)
        selected = self._selected()
        self._entries = entries
        # Columns too: which ones show depends on the width (#433 G2).
        table.clear(columns=True)
        width = self.app.size.width
        columns = columns_for(width)
        for column in columns:
            table.add_column(column, key=column)
        rows = fit_rows(
            [_entry_cells(entry, columns, self._carry) for entry in entries],
            width,
            flex=columns.index("cause"),
            headers=columns,
        )
        for entry, row in zip(entries, rows, strict=True):
            table.add_row(*row, key=f"{entry.run_id}/{entry.component_id}")
        self._show_counts(entries)
        table.display = bool(entries)
        self.query_one("#retry-title", Static).display = bool(entries)
        self.refresh_bindings()
        self._restore_cursor(table, selected)
        if entries:
            self._show_detail(table.cursor_row or 0)
        else:
            self.query_one("#retry-detail", Static).update(
                Text(_nothing_to_retry(self._manifest, self._manifest_file), style=theme.MUTED)
            )

    def _show_counts(self, entries: list[FailureEntry]) -> None:
        current = sum(1 for entry in entries if entry.retryable)
        right = Text()
        if current:
            right.append(f"✗ {current} to retry", style=theme.ERROR)
        self.query_one(ContextBar).set_right(right)
        title = Text("failures", style=f"bold {theme.MUTED}")
        past = len(entries) - current
        title.append(f"  {current} current, {past} superseded or past", style=theme.MUTED)
        self.query_one("#retry-title", Static).update(title)

    def _restore_cursor(self, table: DataTable[Text], selected: FailureEntry | None) -> None:
        """Keep the row the operator was on; else the one home asked for."""
        if selected is not None:
            wanted = [(selected.run_id, selected.component_id)]
        elif self._select:
            wanted = [
                (e.run_id, e.component_id) for e in self._entries if e.component_id == self._select
            ][:1]
        else:
            return
        keys = [(e.run_id, e.component_id) for e in self._entries]
        if wanted and wanted[0] in keys:
            table.move_cursor(row=keys.index(wanted[0]))

    def _selected(self) -> FailureEntry | None:
        table = next(iter(self.query(DataTable)), None)
        row = table.cursor_row if table is not None else None
        if row is None or not (0 <= row < len(self._entries)):
            return None
        return self._entries[row]

    def check_action(self, action: str, _parameters: tuple[object, ...]) -> bool | None:
        entry = self._selected()
        if action == "retry_selected":
            # Withheld when this surface cannot carry the retry (#433 G1).
            refused = self._carry is not None and bool(self._carry.refusal)
            return entry is not None and entry.retryable and not refused
        if action == "open_output":
            return entry is not None and bool(entry.gate_logs)
        return True

    def _show_detail(self, index: int) -> None:
        if not (0 <= index < len(self._entries)):
            return
        entry = self._entries[index]
        detail = _entry_head(entry, self._carry)
        comp = self._manifest.get_component(entry.component_id) if self._manifest else None
        if entry.retryable and comp is not None:
            _append_evidence(detail, entry, comp)
        scope = self._scope
        scoped = scope is not None and scope.component_id == entry.component_id
        carry = self._carry
        cli_only = entry.retryable and carry is not None and bool(carry.refusal)
        # Once the scope or the CLI command is on screen it is what the
        # operator reads; the gate output keeps its path, o opens it whole.
        _append_gate_output(detail, entry, self._root_dir(), lines=0 if scoped or cli_only else 4)
        if cli_only and carry is not None:
            detail.append_text(cli_retry_text(carry, entry.component_id))
        elif scope is not None and scoped:
            detail.append("\n")
            detail.append_text(scope_text(scope))
        elif entry.retryable:
            detail.append(
                "\nr works out what a retry would do, before it is offered", style=theme.MUTED
            )
        self.query_one("#retry-detail", Static).update(detail)

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        if event.cursor_row is not None and event.cursor_row >= 0:
            # r is offered only on a row a retry can act on.
            self.refresh_bindings()
            self._show_detail(event.cursor_row)

    def action_retry_selected(self) -> None:
        entry = self._selected()
        manifest = self._manifest
        if entry is None or not entry.retryable or manifest is None or self._scoping:
            return
        self._scoping = True
        self.query_one("#retry-detail", Static).update(
            Text(f"working out what retrying {entry.component_id} would do...", style=theme.MUTED)
        )
        root, manifest_file = self._root_dir(), self._manifest_file
        cid = entry.component_id

        def _carry() -> tuple[str, str]:
            carry = read_carry(root, manifest, manifest_file)
            return carry.runs_under, carry.refusal

        def _work() -> None:
            try:
                scope = retry_scope(root, manifest, cid, carry=_carry)
            except Exception as exc:  # noqa: BLE001 - reported, never a crash
                scope = RetryScope(
                    cid, (), None, refusal=f"the scope could not be worked out: {exc}"
                )
            self.post_message(ScopeRead(scope))

        self.run_worker(_work, thread=True, group="scope")

    def on_scope_read(self, message: ScopeRead) -> None:
        self._scoping = False
        scope = message.scope
        self._scope = scope
        entry = self._selected()
        if entry is not None:
            self._show_detail(self._entries.index(entry))
        if scope.refusal and not scope.unknown:
            self.app.notify(
                f"{RESUME_REFUSAL}: {scope.refusal} - use 'ks retry {scope.component_id}' instead",
                severity="error",
            )
            # Read again so the row, the detail and r agree (#433 G1).
            self.reload()
            return
        if not scope.offerable or scope.preview is None:
            self.app.notify(
                f"retry not offered: the effect on {', '.join(scope.unknown)} is unknown",
                severity="warning",
            )
            return
        preview = scope.preview

        def _resolved(choice: int | None) -> None:
            if choice == 0:
                self._confirm_retry(scope.component_id, preview)

        self.app.push_screen(
            OptionsModal(
                PromptRequest(
                    kind=PromptKind.CONFIRM,
                    header=scope_header(scope),
                    options=("Start retry", "Cancel"),
                    default=1,
                )
            ),
            _resolved,
        )

    def _confirm_retry(self, component_id: str, preview: RetryPreview) -> None:
        # Reload at commit time. An external factory or editor can update
        # the manifest while the confirmation modal is open; never save
        # the stale object captured by the screen.
        latest, latest_file = _load_manifest(self._root_dir())
        if latest is None:
            self.app.notify(f"retry failed: cannot load {latest_file}", severity="error")
            self.reload()
            return
        try:
            latest_preview = preview_retry(latest, component_id)
        except ValueError as exc:
            self.app.notify(f"retry plan changed: {exc}", severity="warning")
            self.reload()
            return
        if latest_preview != preview:
            self.app.notify(
                "retry plan changed since the preview; review it again",
                severity="warning",
            )
            self.reload()
            return
        carry = read_carry(self._root_dir(), latest, latest_file)
        if carry.refusal:
            self.app.notify(
                f"{RESUME_REFUSAL}: {carry.refusal} - use '{carry.command(component_id)}' instead",
                severity="error",
            )
            self.reload()
            return
        # #537's handoff: the narration used to go to a discarded buffer,
        # so a process the sweep killed in the evidence worktree was never
        # shown here. It is kept and shown, warnings first.
        narration = io.StringIO()
        try:
            prepare_retry(
                latest,
                component_id,
                latest_file,
                self._root_dir(),
                PlainUI(no_color=True, file=narration),
            )
        except (OSError, ValueError, RetryError, subprocess.SubprocessError) as exc:
            said = "\n".join(narration_lines(narration.getvalue()))
            self.app.notify(f"retry failed: {exc}\n{said}".rstrip(), severity="error", timeout=30)
            self.reload()
            return
        _notify_narration(self.app, narration_lines(narration.getvalue()))
        launch = getattr(self.app, "launch", None)
        if launch is not None:
            launch(FactoryLaunch(manifest_path=latest_file))

    def action_open_output(self) -> None:
        entry = self._selected()
        if entry is None or not entry.gate_logs:
            return
        from kstrl.tui.screens.gate_log import GateLogScreen

        what = f"{entry.component_id}: {entry.cause or 'failed gate'}"
        self.app.push_screen(GateLogScreen(entry.gate_logs[0], what, self._root_dir()))


def _recovery_word(entry: FailureEntry, carry: Carry | None) -> str:
    if entry.successor:
        return f"superseded by {theme.short_run_id(entry.successor)}"
    if entry.retryable and carry is not None and carry.refusal:
        return CLI_ONLY
    return entry.recovery


def _recovery_style(entry: FailureEntry, carry: Carry | None) -> str:
    if not entry.retryable:
        return theme.MUTED
    return theme.WARNING if _recovery_word(entry, carry) == CLI_ONLY else theme.SUCCESS


def cli_retry_text(carry: Carry, component_id: str) -> Text:
    """Why this screen cannot run the retry and the command that can (#433 G1)."""
    text = Text()
    text.append("\nretry from the CLI", style=f"bold {theme.WARNING}")
    text.append(": this screen cannot carry it", style=theme.WARNING)
    text.append(f"\n  {carry.refusal}", style=theme.MUTED)
    text.append(f"\n  {carry.command(component_id)}", style="bold")
    if carry.needs_value:
        text.append(f"\n  {VALUE_NOTE}", style=theme.MUTED)
    return text


def _notify_narration(app: object, lines: list[str]) -> None:
    """What ``prepare_retry`` did, as one notification that outlives the
    screen change to the relaunched board. Warnings (a process the sweep
    killed, a census that could not run) lead and raise the severity."""
    warnings = [line for line in lines if line.startswith(("WARN:", "ERROR:"))]
    rest = [line for line in lines if line not in warnings]
    notify = getattr(app, "notify", None)
    if notify is None or not lines:
        return
    notify(
        "\n".join([*warnings, *rest]),
        title="retry prepared",
        severity="warning" if warnings else "information",
        timeout=30 if warnings else 10,
    )


def _read_failure_queue(root: Path) -> list[FailureEntry]:
    """Fold the recent runs and join them to the manifest (worker thread)."""
    from kstrl.tui.home_data import SummaryCache
    from kstrl.tui.runs import discover_runs

    refs = discover_runs(root)[:FAILURE_RUN_LIMIT]
    cache = SummaryCache()
    cache.refresh(refs)
    states = {ref.run_id: state for ref in refs if (state := cache.state_for(ref.run_id))}
    manifest, _ = _load_manifest(root)
    return failure_queue(manifest, refs, states)


def _read_queue_carry(root: Path, entries: list[FailureEntry]) -> Carry | None:
    """Whether this surface can carry a retry, when one is offered (worker thread).

    None when no entry is retryable. An answer that could not be read is a
    refusal, never None: None would leave r offered and the row green.
    """
    if not any(entry.retryable for entry in entries):
        return None
    manifest, manifest_file = _load_manifest(root)
    if manifest is None:
        return None
    try:
        return read_carry(root, manifest, manifest_file)
    except Exception as exc:  # noqa: BLE001 - shown as the refusal, never a crash
        return Carry(refusal=f"could not tell whether this screen can carry it: {exc!r}")


def _entry_cells(entry: FailureEntry, columns: tuple[str, ...], carry: Carry | None) -> list[Text]:
    """One row, cells in ``columns`` order; ``fit_rows`` shortens the cause."""
    style = "" if entry.retryable else theme.MUTED
    run = theme.short_run_id(entry.run_id) if entry.run_id else theme.EMPTY_CELL
    name_style = f"bold {theme.ERROR}" if entry.retryable else theme.MUTED
    cells = {
        "run": Text(run, style=theme.MUTED),
        "component": Text(entry.component_id, style=name_style),
        "cause": Text(entry.cause or theme.EMPTY_CELL, style=style),
        "tries": Text(str(entry.attempts), justify="right", style=style),
        "failed": Text(_when(entry.failed_ts), style=theme.MUTED),
        "recovery": Text(_recovery_word(entry, carry), style=_recovery_style(entry, carry)),
    }
    return [cells[column] for column in columns]


def _entry_head(entry: FailureEntry, carry: Carry | None) -> Text:
    detail = Text()
    detail.append(entry.component_id, style=f"bold {theme.ERROR}")
    if entry.run_id:
        detail.append(f"  run {theme.short_run_id(entry.run_id)}", style=theme.MUTED)
    if entry.failed_ts > 0:
        detail.append(f"  failed {_when(entry.failed_ts)}", style=theme.MUTED)
    detail.append(f"  {_recovery_word(entry, carry)}", style=_recovery_style(entry, carry))
    detail.append(f"\n{entry.cause or 'no cause recorded'}")
    return detail


def _append_evidence(detail: Text, entry: FailureEntry, comp: Component) -> None:
    if comp.error and comp.error not in entry.cause:
        detail.append(f"\n{comp.error[:300]}", style=theme.MUTED)
    for label, value in (("worktree", comp.evidence_worktree), ("debug", comp.evidence_debug_dir)):
        if value:
            detail.append(f"\n{label}  ", style=f"bold {theme.ACCENT}")
            detail.append(value)


def _append_gate_output(detail: Text, entry: FailureEntry, root: Path, lines: int) -> None:
    for path in entry.gate_logs[:1]:
        detail.append("\noutput  ", style=f"bold {theme.ACCENT}")
        detail.append(shown_path(path, root))
        detail.append("  o opens it", style=theme.MUTED)
        for line in (gate_log_excerpt(path, lines) or [])[-lines:] if lines else []:
            detail.append(f"\n  {line}", style=theme.MUTED)
