"""Retry surface: pick a failed component, preview, relaunch (D6).

The table lists the manifest's FAILED components with their evidence
paths. `r` renders preview_retry (non-mutating) in the confirm modal;
on confirm, prepare_retry does the real mutation (reset + worktree/
branch cleanup + save, narrated into the new session's log) and the
factory relaunches through the D6 session seam.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from kstrl.interaction import PromptKind, PromptRequest
from kstrl.launch import FactoryLaunch
from kstrl.retry_plan import RetryError, prepare_retry, preview_retry
from kstrl.tui import theme
from kstrl.tui.screens.options import OptionsModal
from kstrl.ui.plain import PlainUI

if TYPE_CHECKING:
    from kstrl.manifest import Component, Manifest


def _load_manifest(root_dir: Path) -> tuple[Manifest | None, Path]:
    from kstrl.manifest import Manifest

    manifest_file = root_dir / "scripts" / "kstrl" / "manifest.json"
    if not manifest_file.exists():
        return None, manifest_file
    try:
        return Manifest.load(manifest_file), manifest_file
    except (OSError, ValueError):
        return None, manifest_file


def _failed_components(manifest: Manifest) -> list[Component]:
    return [c for c in manifest.components if c.status == "failed"]


class RetryScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "retry_selected", "Retry"),
    ]

    COLUMNS = ("component", "phase", "check", "retries", "evidence")

    def compose(self) -> ComposeResult:
        yield Static("failed components", id="retry-title")
        yield DataTable(id="retry-table")
        yield Static(id="retry-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = False
        for column in self.COLUMNS:
            table.add_column(column, key=column)
        self.reload()

    def _root_dir(self) -> Path:
        root = getattr(self.app, "root_dir", None)
        return root if root is not None else Path.cwd()

    def reload(self) -> None:
        manifest, manifest_file = _load_manifest(self._root_dir())
        self._manifest = manifest
        self._manifest_file = manifest_file
        self._failed = _failed_components(manifest) if manifest else []
        table = self.query_one(DataTable)
        table.clear()
        for comp in self._failed:
            evidence = comp.evidence_worktree or comp.evidence_debug_dir
            table.add_row(
                Text(comp.id, style="bold"),
                comp.failed_phase or Text(theme.EMPTY_CELL, style=theme.MUTED),
                comp.failed_check or Text(theme.EMPTY_CELL, style=theme.MUTED),
                Text(str(comp.retries), justify="right"),
                evidence or Text(theme.EMPTY_CELL, style=theme.MUTED),
                key=comp.id,
            )
        detail = self.query_one("#retry-detail", Static)
        if manifest is None:
            detail.update(Text(
                f"no manifest at {manifest_file} - nothing to retry",
                style=theme.MUTED,
            ))
        elif not self._failed:
            detail.update(Text(
                "no failed components - nothing to retry",
                style=theme.MUTED,
            ))
        elif self._failed:
            self._show_detail(0)

    def _show_detail(self, index: int) -> None:
        if not (0 <= index < len(self._failed)):
            return
        comp = self._failed[index]
        detail = Text()
        detail.append(comp.id, style=f"bold {theme.ERROR}")
        if comp.error:
            detail.append(f"  {comp.error[:160]}", style=theme.MUTED)
        for label, value in (
            ("worktree", comp.evidence_worktree),
            ("debug", comp.evidence_debug_dir),
        ):
            if value:
                detail.append(f"\n{label}  ", style=f"bold {theme.ACCENT}")
                detail.append(value)
        detail.append("\n(r) retry - resets the component, removes the "
                      "failed attempt's worktree/branch, re-enters the "
                      "factory", style=theme.MUTED)
        self.query_one("#retry-detail", Static).update(detail)

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted,
    ) -> None:
        if event.cursor_row is not None and event.cursor_row >= 0:
            self._show_detail(event.cursor_row)

    def action_retry_selected(self) -> None:
        table = self.query_one(DataTable)
        if not table.row_count or table.cursor_row is None:
            return
        if not (0 <= table.cursor_row < len(self._failed)):
            return
        manifest = self._manifest
        if manifest is None:
            return
        comp = self._failed[table.cursor_row]
        try:
            preview = preview_retry(manifest, comp.id)
        except ValueError as exc:
            self.app.notify(str(exc), severity="error")
            return
        dependents = (
            ", ".join(preview.reset_dependents)
            if preview.reset_dependents else "none"
        )
        header = (
            f"Retry '{comp.id}'? Resets it (+ dependents: {dependents}), "
            "removes the failed attempt's worktree and branch, and "
            "re-enters the factory."
        )

        def _resolved(choice: int | None) -> None:
            if choice != 0:
                return
            # Reload at commit time. An external factory or editor can
            # update the manifest while the confirmation modal is open;
            # never save the stale object captured by the screen.
            latest, latest_file = _load_manifest(self._root_dir())
            if latest is None:
                self.app.notify(
                    f"retry failed: cannot load {latest_file}",
                    severity="error",
                )
                self.reload()
                return
            try:
                latest_preview = preview_retry(latest, comp.id)
            except ValueError as exc:
                self.app.notify(
                    f"retry plan changed: {exc}", severity="warning",
                )
                self.reload()
                return
            if latest_preview != preview:
                self.app.notify(
                    "retry plan changed since the preview; review it again",
                    severity="warning",
                )
                self.reload()
                return
            # Keep preparation narration off the alternate screen; the
            # confirmation already presented the same retry plan.
            narration = io.StringIO()
            try:
                prepare_retry(
                    latest, comp.id, latest_file,
                    self._root_dir(), PlainUI(no_color=True, file=narration),
                )
            except (
                OSError, ValueError, RetryError, subprocess.SubprocessError,
            ) as exc:
                self.app.notify(f"retry failed: {exc}", severity="error")
                self.reload()
                return
            launch = getattr(self.app, "launch", None)
            if launch is not None:
                launch(FactoryLaunch(manifest_path=latest_file))

        self.app.push_screen(
            OptionsModal(PromptRequest(
                kind=PromptKind.CONFIRM,
                header=header,
                options=("Start retry", "Cancel"),
                default=1,
            )),
            _resolved,
        )
