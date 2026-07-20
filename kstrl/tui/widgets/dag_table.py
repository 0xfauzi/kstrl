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

from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import DataTable

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.reducer import ComponentState, RunState

ARCHITECT_ID = "architect"
COLUMNS = ("component", "tier", "deps", "prd")
_CYCLE_TIER = -1


def compute_tiers(components: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """Kahn layering over {id: deps}; cycle members get _CYCLE_TIER.

    Deps pointing outside the mapping are ignored (a forming plan can
    reference components whose rows have not folded yet)."""
    remaining = {
        cid: {d for d in deps if d in components}
        for cid, deps in components.items()
    }
    tiers: dict[str, int] = {}
    tier = 0
    while remaining:
        ready = sorted(
            cid for cid, deps in remaining.items()
            if not (deps - set(tiers))
        )
        if not ready:
            for cid in remaining:
                tiers[cid] = _CYCLE_TIER
            break
        for cid in ready:
            tiers[cid] = tier
            del remaining[cid]
        tier += 1
    return tiers


def _row_values(
    comp: ComponentState,
    tier: int,
    prd_written: bool,
) -> tuple[Text | str, ...]:
    name = Text(comp.component_id)
    if comp.title:
        name.append(f"  {comp.title}", style=theme.MUTED)
    if tier == _CYCLE_TIER:
        tier_cell = Text("cycle!", style=f"bold {theme.WARNING}",
                         justify="right")
    else:
        tier_cell = Text(str(tier), justify="right")
    deps = ", ".join(comp.deps)
    return (
        name,
        tier_cell,
        Text(deps) if deps else Text(theme.EMPTY_CELL, style=theme.MUTED),
        Text("✓", style=theme.SUCCESS) if prd_written
        else Text(theme.EMPTY_CELL, style=theme.MUTED),
    )


class DagTable(DataTable[Text | str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False
        for column in COLUMNS:
            self.add_column(column, key=column)

    def update_state(self, state: RunState) -> None:
        order = [cid for cid in state.plan_order if cid != ARCHITECT_ID]
        deps_map = {
            cid: state.components[cid].deps
            for cid in order if cid in state.components
        }
        tiers = compute_tiers(deps_map)
        prds = {
            a["component"]
            for a in state.artifacts if a.get("label") == "prd"
        }
        desired = [cid for cid in order if cid in state.components]
        current = [str(key.value) for key in self.rows]

        # A rewritten event stream can replace the plan. Remove rows that
        # disappeared, and rebuild only when surviving rows changed order.
        # The normal forming-plan path remains append/update-only.
        for cid in current:
            if cid not in desired:
                self.remove_row(cid)
        current = [str(key.value) for key in self.rows]
        if current != desired[:len(current)]:
            self.clear()
        for cid in order:
            comp = state.components.get(cid)
            if comp is None:
                continue
            values = _row_values(comp, tiers.get(cid, 0), cid in prds)
            if cid in self.rows:
                for key, value in zip(COLUMNS, values, strict=True):
                    self.update_cell(cid, key, value)
            else:
                self.add_row(*values, key=cid)
