"""Findings table: the typed adversarial finding stream (PR E)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.widgets import DataTable

if TYPE_CHECKING:
    from ralph_py.reducer import ComponentState

_SEVERITY_STYLES = {
    "critical": "bold red",
    "high": "red",
    "fail": "red",
    "medium": "yellow",
    "advisory": "yellow",
    "low": "dim",
}

COLUMNS = ("phase", "severity", "category", "location", "model", "att")


class FindingsTable(DataTable[Text | str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        for column in COLUMNS:
            self.add_column(column, key=column)
        self._rendered = 0
        self._snapshot: list[dict[str, Any]] = []

    def update_state(self, comp: ComponentState) -> None:
        findings: list[dict[str, Any]] = comp.recent_findings
        if findings[:self._rendered] != self._snapshot:
            # The bounded list rolled over (possibly at the same length),
            # or a rebuilt event snapshot replaced prior rows.
            self.clear()
            self._rendered = 0
        for finding in findings[self._rendered:]:
            severity = str(finding.get("severity", ""))
            self.add_row(
                str(finding.get("phase", "")),
                Text(severity, style=_SEVERITY_STYLES.get(severity, "")),
                str(finding.get("category", "")),
                str(finding.get("location", "")),
                str(finding.get("model", "") or "-"),
                str(finding.get("attempt", "")),
            )
        self._rendered = len(findings)
        self._snapshot = [dict(finding) for finding in findings]
