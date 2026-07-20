"""Config screen: the resolved-config report as a searchable table (D3).

Renders the SAME dataset `ks config show` prints - built by
kstrl.config_report - with source-colored cells and a toml-snippet
hint for the cursor row.

THREAD HAZARD (real, documented on config_report): source detection
scrubs os.environ process-wide, so the report is computed BEFORE
app.run() (run_home_shell) and re-computed only while no launched
session is active. The dataset is static per compute, so the filter
rebuilds the table wholesale - the never-clear+rebuild render rule is
about LIVE diff updates starving input, which does not apply here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.config_report import ConfigReport

SOURCE_STYLES = {
    "flag": f"bold {theme.ACCENT}",
    "env": theme.STEEL,
    "toml": "",
    "default": theme.MUTED,
}

COLUMNS = ("section", "key", "value", "source")


class ConfigScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "back_or_clear", "Back"),
        Binding("slash", "focus_filter", "Filter", key_display="/"),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="config-root"):
            yield Input(placeholder="filter (key, section, source...)",
                        id="config-filter")
            yield Static("resolved config", id="config-title")
            yield DataTable(id="config-table")
            yield Static(id="config-hint")
            yield Footer()

    @property
    def ready(self) -> bool:
        return next(iter(self.query(DataTable)), None) is not None

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = False
        for column in COLUMNS:
            table.add_column(column, key=column)
        self._render_report()

    def _report(self) -> ConfigReport | None:
        return getattr(self.app, "config_report", None)

    def _render_report(self, needle: str = "") -> None:
        if not self.ready:
            return
        table = self.query_one(DataTable)
        table.clear()
        report = self._report()
        hint = self.query_one("#config-hint", Static)
        if report is None:
            hint.update(Text(
                "config could not be resolved - run `ks config show` "
                "for the error",
                style=theme.WARNING,
            ))
            return
        needle = needle.strip().lower()
        shown = 0
        for row in report.rows:
            haystack = f"{row.section} {row.key} {row.value} {row.source}"
            if needle and needle not in haystack.lower():
                continue
            shown += 1
            table.add_row(
                Text(row.section, style=theme.MUTED),
                Text(row.key, style="bold"),
                row.value,
                Text(row.source, style=SOURCE_STYLES.get(row.source, "")),
                key=f"{row.section}.{row.key}",
            )
        title = Text("resolved config", style=f"bold {theme.MUTED}")
        title.append(
            f"  {shown}/{len(report.rows)} value(s)", style=theme.MUTED,
        )
        self.query_one("#config-title", Static).update(title)
        self._update_hint()

    def _update_hint(self) -> None:
        report = self._report()
        hint = self.query_one("#config-hint", Static)
        if report is None:
            return
        text = Text()
        if report.toml_exists:
            text.append(str(report.toml_path), style=theme.MUTED)
        else:
            text.append(
                f"{report.toml_path} (absent - run ks init)",
                style=theme.WARNING,
            )
        table = self.query_one(DataTable)
        if table.row_count and table.cursor_row is not None:
            try:
                row_key = list(table.rows)[table.cursor_row]
            except IndexError:
                row_key = None
            if row_key is not None:
                section, _, key = str(row_key.value).partition(".")
                text.append(
                    f"   [{section}] {key} = ...", style=theme.STEEL,
                )
        hint.update(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._render_report(event.value)

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted,
    ) -> None:
        del event
        self._update_hint()

    def action_focus_filter(self) -> None:
        self.query_one(Input).focus()

    def action_back_or_clear(self) -> None:
        filter_input = self.query_one(Input)
        if filter_input.has_focus and filter_input.value:
            filter_input.value = ""
            return
        self.app.pop_screen()

    def action_refresh(self) -> None:
        # The env-scrub is process-wide: never while a launched
        # session's thread could be reading os.environ.
        run_context = getattr(self.app, "run_context", None)
        if run_context is not None and run_context.handle is not None:
            self.app.notify(
                "config refresh is disabled while a run is in flight "
                "(source detection scrubs the environment)",
                severity="warning",
            )
            return
        from kstrl.config_report import build_config_report

        root_dir = getattr(self.app, "root_dir", None)
        if root_dir is None:
            return
        try:
            self.app.config_report = (  # type: ignore[attr-defined]
                build_config_report(root_dir)
            )
        except ValueError as exc:
            self.app.notify(f"config failed to resolve: {exc}",
                            severity="error")
            return
        self._render_report(self.query_one(Input).value)
