"""Findings table: the typed adversarial finding stream (design pass).

Critique fix: phase-skip and infrastructure records used to sit in the
table styled exactly like real findings - noise dressed as signal. Now
the table carries REAL findings only; bookkeeping records are counted
and surfaced by the panel title (the screen reads hidden_count), and
empty cells render as the dim theme dot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.widgets import DataTable

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.reducer import ComponentState

_SEVERITY_STYLES = {
    "critical": f"bold {theme.ERROR}",
    "high": theme.ERROR,
    "fail": theme.ERROR,
    "medium": theme.WARNING,
    "advisory": theme.WARNING,
    "low": theme.MUTED,
}

# Bookkeeping categories (E3-infra / R1.2): they mark non-execution,
# not defects, and belong in the panel title, not the table.
_BOOKKEEPING_CATEGORIES = {"phase_skipped", "infrastructure_error"}

COLUMNS = ("phase", "severity", "category", "location", "model", "try")


def _is_bookkeeping(finding: dict[str, Any]) -> bool:
    return str(finding.get("category", "")) in _BOOKKEEPING_CATEGORIES


class FindingsTable(DataTable[Text | str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False
        for column in COLUMNS:
            self.add_column(column, key=column)
        self._snapshot: list[dict[str, Any]] = []
        self.hidden_count = 0

    def update_state(self, comp: ComponentState) -> None:
        real = [f for f in comp.recent_findings if not _is_bookkeeping(f)]
        self.hidden_count = len(comp.recent_findings) - len(real)
        if real == self._snapshot:
            return
        self.clear()
        for finding in real:
            severity = str(finding.get("severity", ""))
            model = str(finding.get("model", "") or "")
            location = str(finding.get("location", "") or "")
            self.add_row(
                str(finding.get("phase", "")),
                Text(severity, style=_SEVERITY_STYLES.get(severity, "")),
                str(finding.get("category", "")),
                location or Text(theme.EMPTY_CELL, style=theme.MUTED),
                model or Text(theme.EMPTY_CELL, style=theme.MUTED),
                Text(str(finding.get("attempt", "")), justify="right"),
            )
        self._snapshot = [dict(finding) for finding in real]
