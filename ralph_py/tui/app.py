"""RalphTuiApp: the dashboard application (stage 3 PR D/E).

Render policy (all measured in the Stage 0 spike, RESULTS.md):
- Poll at 0.2s (p95 tail latency ~240ms at 1x AND 10x event rates for
  <1.3% CPU); post StateChanged only when events actually arrived.
- Diff row updates; the transcript pane tails ONE component (the top
  screen's) with a bounded buffer - full rebuilds and multi-component
  floods measurably starve input (finding 3).
- ctrl+c is BOUND explicitly: raw mode delivers it as a key event, not
  SIGINT (finding 1). SIGTERM handling is the embed layer's job
  (finding 2, PR F).

DASH mode is observe-only: q detaches immediately and the run is
untouched. EMBEDDED mode (PR F) overrides the quit path with the
graceful-shutdown flow.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from textual.app import App
from textual.binding import Binding
from textual.widgets import DataTable

from ralph_py.tui.messages import StateChanged
from ralph_py.tui.screens.component import ComponentScreen
from ralph_py.tui.screens.overview import OverviewScreen
from ralph_py.tui.state import StateStore
from ralph_py.tui.tail import RunTailer, TextTailer

DEFAULT_POLL_INTERVAL = 0.2  # measured, spike G1


class Mode(StrEnum):
    DASH = "dash"
    EMBEDDED = "embedded"


class RalphTuiApp(App[int]):
    CSS_PATH = "styles.tcss"
    TITLE = "ralph"
    BINDINGS = [
        Binding("q", "quit_or_detach", "Detach"),
        # Spike finding 1: raw mode delivers ctrl+c as a KEY - unbound
        # it does nothing at all.
        Binding("ctrl+c", "quit_or_detach", show=False),
    ]

    def __init__(
        self,
        *,
        run_dir: Path,
        root_dir: Path,
        mode: Mode = Mode.DASH,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        super().__init__()
        self.run_dir = run_dir
        self.root_dir = root_dir
        self.mode = mode
        self.poll_interval = poll_interval
        self.tailer = RunTailer(run_dir)
        self.store = StateStore(root_dir, run_id=run_dir.name)
        self._transcript_tailers: dict[str, TextTailer] = {}

    def on_mount(self) -> None:
        self.push_screen(OverviewScreen(
            observe_only=self.mode is Mode.DASH,
        ))
        self.set_interval(self.poll_interval, self._poll)
        self.set_interval(1.0, self._tick_ages)
        self._poll()  # catch-up fold before the first frame settles

    # -- data flow -----------------------------------------------------------

    def _poll(self) -> None:
        chunk = self.tailer.poll_events()
        if chunk.truncated:
            self.store.reset()
        changed = self.store.apply_events(chunk.events)
        screen = self.screen
        if changed:
            if isinstance(screen, ComponentScreen) and screen.ready:
                screen.refresh_state(self.store.state, self.store.manifest())
            elif not isinstance(screen, ComponentScreen):
                screen.post_message(StateChanged(self.store.state))
        # Transcript: ONLY the top component screen's component
        # (finding 3 - never all components at once).
        if isinstance(screen, ComponentScreen) and screen.ready:
            screen.feed_transcript(
                self._transcript_tailer(screen.component_id).poll(),
            )

    def _transcript_tailer(self, component_id: str) -> TextTailer:
        tailer = self._transcript_tailers.get(component_id)
        if tailer is None:
            tailer = TextTailer(
                self.run_dir / "components" / component_id / "engineer.log",
            )
            self._transcript_tailers[component_id] = tailer
        return tailer

    def _tick_ages(self) -> None:
        screen = self.screen
        if isinstance(screen, OverviewScreen):
            screen.tick_ages(self.store.state)

    # -- navigation ----------------------------------------------------------

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected,
    ) -> None:
        if not isinstance(self.screen, OverviewScreen):
            return
        if event.row_key.value is None:
            return
        self.open_component(str(event.row_key.value))

    def open_component(self, component_id: str) -> None:
        # The screen fills itself in on_mount (compose must run first).
        self.push_screen(ComponentScreen(component_id))

    def action_quit_or_detach(self) -> None:
        # DASH: detach immediately; the run (if live) is not ours to
        # stop. EMBEDDED overrides this action in PR F.
        self.exit(0)
