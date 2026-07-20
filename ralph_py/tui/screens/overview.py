"""Overview screen: the run board (stage 3 PR D)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Static

from ralph_py.tui.messages import StateChanged
from ralph_py.tui.widgets.component_table import ComponentTable
from ralph_py.tui.widgets.cost_meter import CostMeter
from ralph_py.tui.widgets.header import RunHeader

if TYPE_CHECKING:
    from ralph_py.reducer import RunState


class CheckpointBanner(Static):
    """Shown while any component has an unresolved checkpoint. In dash
    (observe-only) mode it points at the factory terminal; embedded
    mode (PR F) swaps the hint for the modal keybinding."""

    def update_state(self, state: RunState, *, observe_only: bool) -> None:
        open_components = [
            comp.component_id
            for comp in state.components.values() if comp.checkpoint_open
        ]
        if not open_components:
            self.display = False
            return
        self.display = True
        names = ", ".join(sorted(open_components))
        hint = (
            "answer in the `ralph factory` terminal"
            if observe_only else "press c to answer"
        )
        self.update(f"checkpoint pending: {names} - {hint}")


class OverviewScreen(Screen[None]):
    def __init__(self, *, observe_only: bool) -> None:
        super().__init__()
        self.observe_only = observe_only

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield RunHeader(id="run-header")
            yield CostMeter(id="cost-meter")
        yield CheckpointBanner(id="checkpoint-banner")
        yield ComponentTable(id="component-table")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(CheckpointBanner).display = False

    def refresh_state(self, state: RunState) -> None:
        self.query_one(RunHeader).update_state(state)
        self.query_one(CostMeter).update_state(state)
        self.query_one(ComponentTable).update_state(state)
        self.query_one(CheckpointBanner).update_state(
            state, observe_only=self.observe_only,
        )

    def tick_ages(self, state: RunState) -> None:
        self.query_one(RunHeader).update_state(state)
        self.query_one(ComponentTable).tick_ages(state)

    def on_state_changed(self, message: StateChanged) -> None:
        self.refresh_state(message.state)
