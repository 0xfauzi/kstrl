"""Overview screen: the run board + activity feed (design pass).

Layout: masthead (1) / checkpoint banner (0-1) / board (content-sized,
capped) / "activity" panel title / live feed (fills the rest) /
footer. The feed is what replaced the critique's 85% dead space.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, Static

from kstrl.tui import theme
from kstrl.tui.delivery import (
    CI_NOT_READ,
    Delivery,
    integration_summary,
    merge_lines,
    merges_of,
    read_ci,
    release_note,
)
from kstrl.tui.integration_view import IntegrationReview, read_integration_review
from kstrl.tui.messages import DeliveryRead, StateChanged
from kstrl.tui.serve_view import ServeState, read_serve_state, short_item_id
from kstrl.tui.widgets.activity import ActivityFeed
from kstrl.tui.widgets.component_table import ComponentTable
from kstrl.tui.widgets.cost_meter import CostMeter
from kstrl.tui.widgets.header import RunHeader, meter_width, topbar_header
from kstrl.tui.widgets.safe_mode_chip import SafeModeBanner

if TYPE_CHECKING:
    from kstrl import events as ev
    from kstrl.ci_state import CiLedger
    from kstrl.reducer import RunState
    from kstrl.safemode import SafeModeReason


class CheckpointBanner(Static):
    """Shown while any component has an unresolved checkpoint. In dash
    (observe-only) mode it points at the factory terminal; embedded
    mode swaps the hint for the modal keybinding."""

    def update_state(self, state: RunState, *, observe_only: bool) -> None:
        open_components = [
            comp.component_id for comp in state.components.values() if comp.checkpoint_open
        ]
        if not open_components:
            self.display = False
            return
        self.display = True
        names = ", ".join(sorted(open_components))
        hint = "answer in the `ks factory` terminal" if observe_only else "press c to answer"
        self.update(f"◆ checkpoint pending: {names} - {hint}")


#: How often the delivery row re-reads the integration files and the
#: serve queue. Operator time, like the safe-mode check, and on a thread.
DELIVERY_INTERVAL_SECONDS = 2.0


def serve_note(serve: ServeState | None, run_id: str) -> str:
    """The header's word on ``ks serve`` for this run (#433 M1)."""
    if serve is None:
        return ""
    parts = []
    item = serve.item_for_run(run_id)
    if item is not None:
        parts.append(f"ks serve {short_item_id(item.item_id)}")
    elif serve.in_flight:
        running = serve.in_flight[0]
        parts.append(f"ks serve running {short_item_id(running.item_id)}")
    if serve.queued:
        parts.append(f"{len(serve.queued)} queued")
    return " · ".join(parts)


def read_run_delivery(
    root_dir: Path, run_dir: Path, fix_status: dict[str, str]
) -> tuple[IntegrationReview | None, ServeState | None]:
    """The two file reads the delivery row needs; run on a worker thread."""
    from kstrl.tui.runs import factory_lock_held, newest_factory_run_id

    newest = newest_factory_run_id(root_dir)
    review = read_integration_review(root_dir, run_dir, fix_status)
    return review, read_serve_state(root_dir, newest, factory_lock_held(root_dir))


class OverviewScreen(Screen[None]):
    BINDINGS = [Binding("i", "integration", "Integration review")]

    def __init__(self, *, observe_only: bool) -> None:
        super().__init__()
        self.observe_only = observe_only
        # Events arriving before compose mounts the feed (the app's
        # catch-up poll) buffer here and flush in on_mount - the run's
        # history must still narrate on attach.
        self._pending_feed: list[ev.Event] = []
        self._integration: IntegrationReview | None = None
        self._serve: ServeState | None = None
        self._ci: CiLedger | None = None
        self._ci_problem = CI_NOT_READ
        self._delivery_read = False
        self._reading_delivery = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield RunHeader(id="run-header")
            yield CostMeter(id="cost-meter")
        yield SafeModeBanner(id="safe-mode-banner")
        yield CheckpointBanner(id="checkpoint-banner")
        yield ComponentTable(id="component-table")
        yield Static(id="delivery-row")
        yield Static("activity", id="activity-title")
        yield ActivityFeed(id="activity-feed")
        yield Footer()

    def update_safe_mode(
        self,
        reasons: list[SafeModeReason] | None,
    ) -> None:
        """Duck-typed contract the app calls; ignored while unmounted.

        A banner and no chip, and that was measured rather than chosen:
        the topbar is one line, and on the standard 120-column fixture
        the header (41 cells) and the cost meter (79) already want 126
        before anything is added. A chip there cost the run its own
        state label. Degraded is impossible to miss on the banner, `m`
        is always in the footer, and the panel is the surface that
        distinguishes not-checked from checked-and-clear.
        """
        banner = next(iter(self.query(SafeModeBanner)), None)
        if banner is not None:
            banner.update_reasons(reasons)

    def on_mount(self) -> None:
        self.query_one(CheckpointBanner).display = False
        # Replay the last completed check rather than starting hidden.
        # A screen mounted after the message (home -> run) would
        # otherwise show no warning until the next interval, so an
        # active degradation vanished for as long as five seconds.
        self.update_safe_mode(getattr(self.app, "_safe_mode_reasons", None))
        if self._pending_feed:
            self.query_one(ActivityFeed).feed_events(self._pending_feed)
            self._pending_feed = []
        self.query_one("#delivery-row", Static).display = False
        self._read_delivery()
        self.set_interval(DELIVERY_INTERVAL_SECONDS, self._read_delivery)

    # -- delivery row (#433 F9, M1, M3) ---------------------------------------

    def _store_state(self) -> RunState | None:
        store = getattr(self.app, "store", None)
        return store.state if store is not None else None

    def _read_delivery(self) -> None:
        """Re-read the integration files and the queue off the event loop."""
        run_dir = getattr(self.app, "run_dir", None)
        root = getattr(self.app, "root_dir", None)
        state = self._store_state()
        if self._reading_delivery or not isinstance(run_dir, Path) or not isinstance(root, Path):
            return
        # Snapshot on this thread: the poll mutates the state in place.
        fix_status = {cid: comp.status for cid, comp in state.components.items()} if state else {}
        self._reading_delivery = True

        def _work() -> None:
            try:
                review, serve = read_run_delivery(root, run_dir, fix_status)
            except Exception:  # noqa: BLE001 - a broken file must not kill the board
                review, serve = None, None
            # Its own read: an unreadable ledger is every commit's unknown,
            # and never hides the review (#433 G11).
            self.post_message(DeliveryRead(review, serve, *read_ci(root)))

        self.run_worker(_work, thread=True, group="delivery")

    def on_delivery_read(self, message: DeliveryRead) -> None:
        self._reading_delivery = False
        self._integration = message.integration
        self._serve = message.serve
        self._ci, self._ci_problem = message.ci, message.ci_problem
        self._delivery_read = True
        self.refresh_bindings()
        state = self._store_state()
        if state is not None:
            self.refresh_state(state)

    def _render_delivery(self, state: RunState) -> None:
        row = self.query_one("#delivery-row", Static)
        if state.kind != "factory" or not self._delivery_read:
            row.display = False
            return
        # Glyphs only: the words for each verdict are on the review screen.
        integration = integration_summary(self._integration, short=True)
        if self._integration is not None:
            integration.append("  i opens it", style=theme.MUTED)
        delivery = Delivery(
            run_id=state.run_id,
            merges=merges_of(state),
            release_ref=state.release_ref,
            release_withheld=state.release_withheld,
            integration=self._integration,
            ci=self._ci,
            ci_problem=self._ci_problem,
        )
        # A section of its own under the board, titled like "activity"
        # (#433 G10), not two lines flush under the table.
        title = Text("delivery", style=f"bold {theme.MUTED}")
        title.append(release_note(delivery), style=theme.MUTED)
        # #delivery-row pads two cells a side and each line is indented two.
        lines = [integration, *merge_lines(delivery, time.time(), self.size.width - 6)]
        row.update(Text("\n").join([title, *(Text("  ") + line for line in lines)]))
        row.display = True

    def check_action(self, action: str, _parameters: tuple[object, ...]) -> bool | None:
        if action == "integration":
            return self._integration is not None
        return True

    def action_integration(self) -> None:
        if self._integration is None:
            return
        from kstrl.tui.screens.integration import IntegrationScreen

        run_dir = getattr(self.app, "run_dir", None)
        run_id = run_dir.name if isinstance(run_dir, Path) else ""
        self.app.push_screen(IntegrationScreen(self._integration, run_id))

    @property
    def ready(self) -> bool:
        return next(iter(self.query(ActivityFeed)), None) is not None

    def refresh_state(self, state: RunState) -> None:
        if not self.ready:
            return
        try:
            self._update_topbar(state)
            self.query_one(ComponentTable).update_state(state)
            self._render_delivery(state)
            self.query_one(CheckpointBanner).update_state(
                state,
                observe_only=self.observe_only,
            )
        except NoMatches:
            # A late StateChanged can arrive while the screen is being torn
            # down: `ready` still sees the feed (composed last, removed
            # late) but RunHeader (composed first) is already gone. Dropping
            # the update is safe - these are observability writes, not
            # control flow.
            return

    def feed_events(self, batch: list[ev.Event]) -> None:
        feed = next(iter(self.query(ActivityFeed)), None)
        if feed is None:
            self._pending_feed.extend(batch)
            return
        feed.feed_events(batch)

    def tick_ages(self, state: RunState) -> None:
        if not self.ready:
            return
        try:
            self._update_topbar(state)
            self.query_one(ComponentTable).tick_ages(state)
        except NoMatches:
            # Same teardown race as refresh_state: a timer-driven tick can
            # fire after RunHeader is removed. Drop it.
            return

    def _update_topbar(self, state: RunState) -> None:
        run_dir = getattr(self.app, "run_dir", None)
        note = serve_note(self._serve, run_dir.name if isinstance(run_dir, Path) else "")
        header = topbar_header(state, self.app, self.size.width, note)
        self.query_one(RunHeader).update(header)
        self.query_one(CostMeter).update_state(state, meter_width(header, self.size.width))

    def on_resize(self) -> None:
        store = getattr(self.app, "store", None)
        if store is not None:
            self.refresh_state(store.state)

    def on_state_changed(self, message: StateChanged) -> None:
        self.refresh_state(message.state)
