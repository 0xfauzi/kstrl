"""KstrlTuiApp: the dashboard application (stage 3 PR D/E).

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

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable

from kstrl.interaction import (
    PromptKind,
    PromptRequest,
    QueueInteractionChannel,
)
from kstrl.tui.bridge import OrchestratorHandle
from kstrl.tui.messages import StateChanged
from kstrl.tui.screens.checkpoint import CheckpointModal
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.screens.overview import OverviewScreen
from kstrl.tui.screens.quit import QuitModal
from kstrl.tui.state import StateStore
from kstrl.tui.tail import RunTailer, TextTailer
from kstrl.tui.theme import KSTRL_THEME

DEFAULT_POLL_INTERVAL = 0.2  # measured, spike G1

# Bottom-first initial screen stack; None = the default overview.
ScreenStackFactory = Callable[[], list[Screen[None]]]


class Mode(StrEnum):
    DASH = "dash"
    EMBEDDED = "embedded"


class KstrlTuiApp(App[int]):
    CSS_PATH = "styles.tcss"
    TITLE = "kstrl"
    BINDINGS = [
        Binding("q", "quit_or_detach", "Detach"),
        # Spike finding 1: raw mode delivers ctrl+c as a KEY - unbound
        # it does nothing at all.
        Binding("ctrl+c", "quit_or_detach", show=False),
        Binding("c", "answer_checkpoint", "Checkpoint", show=False),
    ]

    def __init__(
        self,
        *,
        run_dir: Path,
        root_dir: Path,
        mode: Mode = Mode.DASH,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        channel: QueueInteractionChannel | None = None,
        orchestrator: OrchestratorHandle | None = None,
        screen_factory: ScreenStackFactory | None = None,
    ) -> None:
        super().__init__()
        self.run_dir = run_dir
        self.root_dir = root_dir
        self.mode = mode
        self.poll_interval = poll_interval
        self.channel = channel
        self.orchestrator = orchestrator
        self.screen_factory = screen_factory
        self.tailer = RunTailer(run_dir)
        self.store = StateStore(root_dir, run_id=run_dir.name)
        self._transcript_tailers: dict[str, TextTailer] = {}
        self._pending_prompts: dict[str, PromptRequest] = {}
        self._stopping = False

    def on_mount(self) -> None:
        self.register_theme(KSTRL_THEME)
        self.theme = "kstrl"
        if self.screen_factory is not None:
            for screen in self.screen_factory():
                self.push_screen(screen)
        else:
            self.push_screen(OverviewScreen(
                observe_only=self.mode is Mode.DASH,
            ))
        self.set_interval(self.poll_interval, self._poll)
        self.set_interval(1.0, self._tick_ages)
        if self.mode is Mode.EMBEDDED:
            self.set_interval(0.5, self._check_orchestrator)
            if self.channel is not None:
                # Attach HERE, not before app.run(): call_from_thread on
                # a not-yet-running app raises, which would degrade the
                # request to its non-interactive default. Until attach,
                # can_prompt() is False and a checkpoint proceeds
                # NOT_PROMPTED - exactly the non-TTY semantics.
                self.channel.attach(
                    lambda req: self.call_from_thread(
                        self.on_prompt_request, req,
                    ),
                )
        self._poll()  # catch-up fold before the first frame settles

    # -- data flow -----------------------------------------------------------

    def _poll(self) -> None:
        """Screen dispatch is duck-typed on a small contract so any
        screen (component detail, decompose, home-launched views) can
        opt in without the app enumerating types:

        - ``transcript_component`` (str): a detail screen; when
          ``ready``, gets ``refresh_state(state, manifest)`` directly
          and a bounded transcript tail for that ONE component
          (finding 3 - never all components at once).
        - otherwise: receives the StateChanged message.
        - ``feed_events(batch)``: narrated event batches (curated by
          humanize(); truncation rebuilds replay history into state
          but must not re-narrate it into the feed).
        """
        chunk = self.tailer.poll_events()
        if chunk.truncated:
            self.store.reset()
        changed = self.store.apply_events(chunk.events)
        screen_stack = self.screen_stack
        if not screen_stack:
            return
        screen = screen_stack[-1]
        component_id = str(getattr(screen, "transcript_component", ""))
        ready = bool(getattr(screen, "ready", True))
        if changed:
            if component_id:
                if ready:
                    screen.refresh_state(  # type: ignore[attr-defined]
                        self.store.state, self.store.manifest(),
                    )
            else:
                screen.post_message(StateChanged(self.store.state))
            feed = getattr(screen, "feed_events", None)
            if feed is not None and not chunk.truncated:
                feed(chunk.events)
        if component_id and ready:
            feed_transcript = getattr(screen, "feed_transcript", None)
            if feed_transcript is not None:
                feed_transcript(
                    self._transcript_tailer(component_id).poll(),
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
        screen_stack = self.screen_stack
        if not screen_stack:
            return
        tick = getattr(screen_stack[-1], "tick_ages", None)
        if tick is not None:
            tick(self.store.state)

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

    # -- embedded mode (PR F) ------------------------------------------------

    def _check_orchestrator(self) -> None:
        handle = self.orchestrator
        if handle is None or not handle.done():
            return
        self._poll()  # final drain before leaving the screen
        self.exit(
            130 if handle.stop.is_set() else handle.exit_code,
        )

    def on_prompt_request(self, request: PromptRequest) -> None:
        """The interaction channel's notify callback (call_from_thread -
        the sole orchestrator-to-UI crossing, spike-G4 validated)."""
        self._pending_prompts[request.request_id] = request
        self._open_prompt(request)

    def _open_prompt(self, request: PromptRequest) -> None:
        if self.channel is None:
            return

        def _resolve(choice: int | None) -> None:
            if choice is None:
                # Left pending (Esc): the banner keeps pointing at it;
                # press c to reopen. The orchestrator stays blocked -
                # that is what a checkpoint IS.
                self.notify(
                    "checkpoint left pending - press c to answer",
                    severity="warning",
                )
                return
            if self.channel is not None and self.channel.resolve(
                request.request_id, choice,
            ):
                self._pending_prompts.pop(request.request_id, None)

        if request.kind is PromptKind.CHECKPOINT and request.checkpoint:
            self.push_screen(CheckpointModal(request), _resolve)
        else:
            # Generic prompts get the lean options modal: the
            # checkpoint modal's a/r/t bindings hardcode three options
            # and mis-resolve a 2-option CONFIRM (choice out of range
            # leaves the prompt pending with the modal gone).
            self.push_screen(OptionsModal(request), _resolve)

    def action_answer_checkpoint(self) -> None:
        if isinstance(self.screen, CheckpointModal):
            return
        for request in self._pending_prompts.values():
            self._open_prompt(request)
            return

    def action_quit_or_detach(self) -> None:
        if self.mode is Mode.DASH:
            # Detach immediately; the run (if live) is not ours to stop.
            self.exit(0)
            return
        handle = self.orchestrator
        if handle is None:
            self.exit(0)
            return
        if self._stopping or handle.stop.is_set():
            # Second request escalates to force (mirrors the second
            # Ctrl-C signal semantics of PR B).
            handle.stop.request("forced from TUI", force=True)
            self.exit(130)
            return

        def _confirmed(stop_run: bool | None) -> None:
            if not stop_run:
                return
            self._stopping = True
            handle.stop.request("stopped from TUI")
            self.notify("shutting down - waiting for cleanup", timeout=10)

        self.push_screen(QuitModal(), _confirmed)
