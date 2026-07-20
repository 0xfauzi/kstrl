"""Overview screen: the run board + activity feed (design pass).

Layout: masthead (1) / checkpoint banner (0-1) / board (content-sized,
capped) / "activity" panel title / live feed (fills the rest) /
footer. The feed is what replaced the critique's 85% dead space.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Static

from ralph_py.tui.messages import StateChanged
from ralph_py.tui.widgets.activity import ActivityFeed
from ralph_py.tui.widgets.component_table import ComponentTable
from ralph_py.tui.widgets.cost_meter import CostMeter
from ralph_py.tui.widgets.header import RunHeader

if TYPE_CHECKING:
    from ralph_py import events as ev
    from ralph_py.reducer import RunState


class CheckpointBanner(Static):
    """Shown while any component has an unresolved checkpoint. In dash
    (observe-only) mode it points at the factory terminal; embedded
    mode swaps the hint for the modal keybinding."""

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
        self.update(f"◆ checkpoint pending: {names} - {hint}")


class OverviewScreen(Screen[None]):
    def __init__(self, *, observe_only: bool) -> None:
        super().__init__()
        self.observe_only = observe_only
        # Events arriving before compose mounts the feed (the app's
        # catch-up poll) buffer here and flush in on_mount - the run's
        # history must still narrate on attach.
        self._pending_feed: list[ev.Event] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield RunHeader(id="run-header")
            yield CostMeter(id="cost-meter")
        yield CheckpointBanner(id="checkpoint-banner")
        yield ComponentTable(id="component-table")
        yield Static("activity", id="activity-title")
        yield ActivityFeed(id="activity-feed")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(CheckpointBanner).display = False
        if self._pending_feed:
            self.query_one(ActivityFeed).feed_events(self._pending_feed)
            self._pending_feed = []

    @property
    def ready(self) -> bool:
        return next(iter(self.query(ActivityFeed)), None) is not None

    def refresh_state(self, state: RunState) -> None:
        if not self.ready:
            return
        self.query_one(RunHeader).update_state(state)
        self.query_one(CostMeter).update_state(state)
        self.query_one(ComponentTable).update_state(state)
        self.query_one(CheckpointBanner).update_state(
            state, observe_only=self.observe_only,
        )

    def feed_events(self, batch: list[ev.Event]) -> None:
        feed = next(iter(self.query(ActivityFeed)), None)
        if feed is None:
            self._pending_feed.extend(batch)
            return
        feed.feed_events(batch)

    def tick_ages(self, state: RunState) -> None:
        if not self.ready:
            return
        self.query_one(RunHeader).update_state(state)
        self.query_one(ComponentTable).tick_ages(state)

    def on_state_changed(self, message: StateChanged) -> None:
        self.refresh_state(message.state)
