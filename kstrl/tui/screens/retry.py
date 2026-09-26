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
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from kstrl.config_preflight import SURFACE_REJECTIONS, raise_if_defect
from kstrl.interaction import PromptKind, PromptRequest
from kstrl.launch import FactoryLaunch
from kstrl.retry_plan import (
    RESUME_REFUSAL,
    RetryError,
    plan_resume,
    prepare_retry,
    preview_retry,
)
from kstrl.tui import theme
from kstrl.tui.messages import FailuresRead, ScopeRead
from kstrl.tui.operator_queue import FailureEntry, failure_queue
from kstrl.tui.retry_scope import RetryScope, retry_scope
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.widgets.component_detail import gate_log_excerpt, shown_path
from kstrl.tui.widgets.context_bar import ContextBar
from kstrl.ui.plain import PlainUI

#: How many recent runs the failure queue folds; the home run list's cap.
FAILURE_RUN_LIMIT = 15

if TYPE_CHECKING:
    from kstrl.manifest import Component, Manifest
    from kstrl.retry_plan import ResumePlan, RetryPreview


def _load_manifest(root_dir: Path) -> tuple[Manifest | None, Path]:
    from kstrl.manifest import Manifest

    manifest_file = root_dir / "scripts" / "kstrl" / "manifest.json"
    if not manifest_file.exists():
        return None, manifest_file
    try:
        return Manifest.load(manifest_file), manifest_file
    except (OSError, ValueError):
        return None, manifest_file


def _carry_problem(plan: ResumePlan | None, problems: list[str]) -> str | None:
    """Why the TUI cannot carry the retry's own resume plan, or None when it can.

    FactoryLaunch carries no cost ceiling as a field, but the TUI launch
    path (kstrl/tui/session.py -> kstrl/launch.py::assemble_factory_configs
    -> FactoryConfig.load) loads the same env/kstrl.toml ceiling
    plan_resume resolved, so a ceiling above 0 is not laundered away and
    needs no refusal here. `ks factory` recorded flags are a different
    story: FactoryLaunch has no field for them, and re-entering through
    the CLI is the only path that can replay them (#436), so the TUI
    must refuse when the record carries any.
    """
    if plan is None:
        return "; ".join(problems)
    if plan.argv:
        return "the recorded run's flags cannot be carried through the TUI"
    return None


def _nothing_to_retry(manifest: Manifest | None, manifest_file: Path) -> str:
    if manifest is None:
        return f"Nothing to retry: there is no readable manifest at {manifest_file}."
    return f"Nothing to retry: no component in {manifest_file.name} is in the failed state."


#: Failure queue columns (#433, advice-r1 section 3).
COLUMNS = ("run", "component", "cause", "tries", "failed", "recovery")


def _when(ts: float) -> str:
    if ts <= 0:
        return theme.EMPTY_CELL
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


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
    ]

    COLUMNS = COLUMNS

    def __init__(self, select: str = "") -> None:
        super().__init__()
        #: The component to put the cursor on (home's needs-you row).
        self._select = select
        self._entries: list[FailureEntry] = []
        self._scoping = False
        self._scope: RetryScope | None = None

    def compose(self) -> ComposeResult:
        yield ContextBar("retry", "failure queue: what failed, and what a retry would do")
        yield Static("failures", id="retry-title")
        yield DataTable(id="retry-table")
        yield Static(id="retry-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = False
        for column in COLUMNS:
            table.add_column(column, key=column)
        self.reload()

    def _root_dir(self) -> Path:
        root = getattr(self.app, "root_dir", None)
        return root if root is not None else Path.cwd()

    def reload(self) -> None:
        """The manifest's failures now; the runs' failures when the worker lands."""
        manifest, manifest_file = _load_manifest(self._root_dir())
        self._manifest = manifest
        self._manifest_file = manifest_file
        self._scope = None
        entries = failure_queue(manifest, [], {}) if manifest is not None else []
        self._show_entries(entries)
        root = self._root_dir()

        def _work() -> None:
            try:
                queue = _read_failure_queue(root)
            except Exception:  # noqa: BLE001 - the manifest rows stay up
                return
            self.post_message(FailuresRead(queue))

        self.run_worker(_work, thread=True, group="failures", exclusive=True)

    def on_failures_read(self, message: FailuresRead) -> None:
        self._show_entries(message.entries)

    def _show_entries(self, entries: list[FailureEntry]) -> None:
        table = self.query_one(DataTable)
        selected = self._selected()
        self._entries = entries
        table.clear()
        width = max(80, self.app.size.width)
        for entry in entries:
            table.add_row(*_entry_cells(entry, width), key=f"{entry.run_id}/{entry.component_id}")
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
        if action == "retry_selected":
            entry = self._selected()
            return entry is not None and entry.retryable
        return True

    def _show_detail(self, index: int) -> None:
        if not (0 <= index < len(self._entries)):
            return
        entry = self._entries[index]
        detail = _entry_head(entry)
        comp = self._manifest.get_component(entry.component_id) if self._manifest else None
        if entry.retryable and comp is not None:
            _append_evidence(detail, entry, comp)
        _append_gate_output(detail, entry, self._root_dir())
        scope = self._scope
        if scope is not None and scope.component_id == entry.component_id:
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

        def _work() -> None:
            try:
                scope = retry_scope(
                    root,
                    manifest,
                    cid,
                    carry=lambda: _runs_under(root, manifest, manifest_file),
                )
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
        problem = self._carry_problem_for(latest, latest_file, component_id)
        if problem is not None:
            self.app.notify(
                f"{RESUME_REFUSAL}: {problem} - use 'ks retry {component_id}' instead",
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

    def _carry_problem_for(
        self,
        manifest: Manifest,
        manifest_file: Path,
        component_id: str,
    ) -> str | None:
        """`_carry_problem` against this screen's own resume plan (#436).

        `plan_resume` loads `FactoryConfig` (the ceiling `_ceiling_problems`
        checks against) and lets a config rejection propagate, the same as
        `assemble_factory_configs` - so this call needs the same guard
        `kstrl/tui/session.py::_prepare_factory` puts around that one,
        or a broken kstrl.toml would take the Textual event loop down
        instead of being reported (#289's defect class).
        """
        from kstrl.cli import factory as factory_command

        try:
            plan, problems = plan_resume(
                self._root_dir(),
                manifest,
                manifest_file,
                factory_command,
                max_cost_usd=None,
                max_parallel=None,
                keep_worktrees_on_failure=False,
            )
        except SURFACE_REJECTIONS as exc:
            raise_if_defect(exc)
            return f"failed to load configuration: {exc}"
        return _carry_problem(plan, problems)


def _recovery_word(entry: FailureEntry) -> str:
    if entry.successor:
        return f"superseded by {theme.short_run_id(entry.successor)}"
    return entry.recovery


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


def _runs_under(root: Path, manifest: Manifest, manifest_file: Path) -> tuple[str, str]:
    """(what the relaunch runs under, why the TUI cannot carry it)."""
    from kstrl.cli import factory as factory_command

    try:
        plan, problems = plan_resume(
            root,
            manifest,
            manifest_file,
            factory_command,
            max_cost_usd=None,
            max_parallel=None,
            keep_worktrees_on_failure=False,
        )
    except SURFACE_REJECTIONS as exc:
        raise_if_defect(exc)
        return "", f"failed to load configuration: {exc}"
    problem = _carry_problem(plan, problems)
    if problem is not None or plan is None:
        return "", problem or "no resume plan"
    ceiling = f"${plan.max_cost_usd:g} cost ceiling" if plan.max_cost_usd > 0 else "no cost ceiling"
    return f"{ceiling}, {plan.max_parallel} in parallel", ""


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


def _entry_cells(entry: FailureEntry, width: int) -> tuple[Text, ...]:
    style = "" if entry.retryable else theme.MUTED
    run = theme.short_run_id(entry.run_id) if entry.run_id else theme.EMPTY_CELL
    name_style = f"bold {theme.ERROR}" if entry.retryable else theme.MUTED
    return (
        Text(run, style=theme.MUTED),
        Text(entry.component_id, style=name_style),
        Text(_fit(entry.cause or theme.EMPTY_CELL, max(16, width - 78)), style=style),
        Text(str(entry.attempts), justify="right", style=style),
        Text(_when(entry.failed_ts), style=theme.MUTED),
        Text(_recovery_word(entry), style=theme.SUCCESS if entry.retryable else theme.MUTED),
    )


def _entry_head(entry: FailureEntry) -> Text:
    detail = Text()
    detail.append(entry.component_id, style=f"bold {theme.ERROR}")
    if entry.run_id:
        detail.append(f"  run {theme.short_run_id(entry.run_id)}", style=theme.MUTED)
    detail.append(f"  {entry.recovery}", style=theme.SUCCESS if entry.retryable else theme.MUTED)
    detail.append(f"\n{entry.cause or 'no cause recorded'}")
    return detail


def _append_evidence(detail: Text, entry: FailureEntry, comp: Component) -> None:
    if comp.error and comp.error not in entry.cause:
        detail.append(f"\n{comp.error[:300]}", style=theme.MUTED)
    for label, value in (("worktree", comp.evidence_worktree), ("debug", comp.evidence_debug_dir)):
        if value:
            detail.append(f"\n{label}  ", style=f"bold {theme.ACCENT}")
            detail.append(value)


def _append_gate_output(detail: Text, entry: FailureEntry, root: Path) -> None:
    for path in entry.gate_logs[:1]:
        detail.append("\noutput  ", style=f"bold {theme.ACCENT}")
        detail.append(shown_path(path, root))
        for line in (gate_log_excerpt(path, 4) or [])[-4:]:
            detail.append(f"\n  {line}", style=theme.MUTED)
