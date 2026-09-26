"""Forming-DAG table for the decompose screen (TUI surface C5).

Rows appear as the architect's RunPlan folds - no manifest read
needed. Tiers come from a local cycle-tolerant Kahn over the folded
deps (mirrors Manifest.compute_tiers but works before the manifest
exists); a component caught in a dependency cycle renders a warning
marker instead of failing the screen - the plain-mode DAG validation
warnings remain the authoritative complaint.

Same render policy as the component board: diff row updates, never
clear()+rebuild per poll (spike finding 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import DataTable

from kstrl.tui import theme
from kstrl.tui.state import planned_component_ids

if TYPE_CHECKING:
    from kstrl.reducer import ComponentState, RunState

COLUMNS = ("component", "tier", "deps", "prd")
_CYCLE_TIER = -1


def compute_tiers(components: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """Kahn layering over {id: deps}; cycle members get _CYCLE_TIER.

    Deps pointing outside the mapping are ignored (a forming plan can
    reference components whose rows have not folded yet)."""
    remaining = {cid: {d for d in deps if d in components} for cid, deps in components.items()}
    tiers: dict[str, int] = {}
    tier = 0
    while remaining:
        ready = sorted(cid for cid, deps in remaining.items() if not (deps - set(tiers)))
        if not ready:
            for cid in remaining:
                tiers[cid] = _CYCLE_TIER
            break
        for cid in ready:
            tiers[cid] = tier
            del remaining[cid]
        tier += 1
    return tiers


#: Cells the table keeps for itself: its padding and the scrollbar.
_TABLE_CHROME = 4
#: A title shorter than this is dropped rather than shown as a stub.
_MIN_TITLE = 8


@dataclass(frozen=True)
class _Fit:
    """How many cells the title and the deps text may use."""

    title: int
    deps: int


def fit_columns(comps: list[ComponentState], width: int) -> _Fit:
    """Room for each row's title and deps so the table fits ``width``.

    At 80 columns the component-and-title column pushed deps past the
    edge and the table scrolled sideways (#433 F1). The title gives way
    first, since the id names the component; then the deps text.
    """
    ids = max((len(comp.component_id) for comp in comps), default=0)
    deps = max((len(", ".join(comp.deps)) for comp in comps), default=0)
    fixed = len("cycle!") + len("prd") + len(COLUMNS) * 2 + _TABLE_CHROME
    room = width - fixed - max(ids, len("component"))
    deps_cells = max(len("deps"), min(deps, room))
    title = room - deps_cells - 2
    return _Fit(title=title if title >= _MIN_TITLE else 0, deps=deps_cells)


def _shorten(text: str, cells: int) -> str:
    return text if len(text) <= cells else text[: max(1, cells - 1)] + "…"


def _row_values(
    comp: ComponentState,
    tier: int,
    prd_written: bool,
    fit: _Fit | None = None,
) -> tuple[Text | str, ...]:
    name = Text(comp.component_id)
    if comp.title and (fit is None or fit.title):
        title = comp.title if fit is None else _shorten(comp.title, fit.title)
        name.append(f"  {title}", style=theme.MUTED)
    if tier == _CYCLE_TIER:
        tier_cell = Text("cycle!", style=f"bold {theme.WARNING}", justify="right")
    else:
        tier_cell = Text(str(tier), justify="right")
    deps = ", ".join(comp.deps)
    if fit is not None:
        deps = _shorten(deps, fit.deps)
    return (
        name,
        tier_cell,
        Text(deps) if deps else Text(theme.EMPTY_CELL, style=theme.MUTED),
        Text("✓", style=theme.SUCCESS)
        if prd_written
        else Text(theme.EMPTY_CELL, style=theme.MUTED),
    )


class DagTable(DataTable[Text | str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False
        self._laid_out_for = 0
        self._add_columns()

    def _add_columns(self) -> None:
        for column in COLUMNS:
            self.add_column(column, key=column)

    def _screen_width(self) -> int:
        width = self.app.size.width if self.is_attached else 0
        return width or 120

    def _relayout(self, width: int) -> None:
        """Columns only widen on update, so a new width is a rebuild."""
        if width == self._laid_out_for:
            return
        self.clear(columns=True)
        self._add_columns()
        self._laid_out_for = width

    def update_state(self, state: RunState) -> None:
        # The architect's own pseudo-row is filtered out; a COMPONENT the
        # architect named `architect` is not. Those were one string until
        # #281, and this table had its own declaration of it, so a real
        # component with that name vanished from the DAG entirely.
        #
        # Resolved per run rather than pinned to the constant, or a
        # pre-#281 dir's stale pseudo-row renders as a graph node.
        order = planned_component_ids(state)
        deps_map = {cid: state.components[cid].deps for cid in order if cid in state.components}
        tiers = compute_tiers(deps_map)
        prds = {a["component"] for a in state.artifacts if a.get("label") == "prd"}
        desired = [cid for cid in order if cid in state.components]
        width = self._screen_width()
        self._relayout(width)
        fit = fit_columns([state.components[cid] for cid in desired], width)
        current = [str(key.value) for key in self.rows]

        # A rewritten event stream can replace the plan. Remove rows that
        # disappeared, and rebuild only when surviving rows changed order.
        # The normal forming-plan path remains append/update-only.
        for cid in current:
            if cid not in desired:
                self.remove_row(cid)
        current = [str(key.value) for key in self.rows]
        if current != desired[: len(current)]:
            self.clear()
        for cid in order:
            comp = state.components.get(cid)
            if comp is None:
                continue
            self._write_row(cid, _row_values(comp, tiers.get(cid, 0), cid in prds, fit))

    def _write_row(self, cid: str, values: tuple[Text | str, ...]) -> None:
        """Update the row in place, widening each column, or add it."""
        if cid not in self.rows:
            self.add_row(*values, key=cid)
            return
        for key, value in zip(COLUMNS, values, strict=True):
            self.update_cell(cid, key, value, update_width=True)
