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
from kstrl.tui.runcontext import RunContext
from kstrl.tui.screens.checkpoint import CheckpointModal
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.home import HomeScreen
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.screens.overview import OverviewScreen
from kstrl.tui.screens.quit import QuitModal
from kstrl.tui.state import StateStore
from kstrl.tui.theme import KSTRL_THEME

DEFAULT_POLL_INTERVAL = 0.2  # measured, spike G1

# Bottom-first initial screen stack; None = the default overview.
ScreenStackFactory = Callable[[], list[Screen[None]]]


class Mode(StrEnum):
    DASH = "dash"
    EMBEDDED = "embedded"
    HOME = "home"


class KstrlTuiApp(App[int]):
    CSS_PATH = "styles.tcss"
    TITLE = "kstrl"
    BINDINGS = [
        Binding("q", "quit_or_detach", "Detach"),
        # Spike finding 1: raw mode delivers ctrl+c as a KEY - unbound
        # it does nothing at all.
        Binding("ctrl+c", "quit_or_detach", show=False),
        Binding("c", "answer_checkpoint", "Checkpoint", show=False),
        # App-level fallback: screens with their own escape binding
        # (modals, detail screens) win; on a base screen this pops
        # toward home when there is anywhere to pop to. (Named
        # nav_back: Textual's App already owns an async action_back.)
        Binding("escape", "nav_back", show=False),
    ]

    def __init__(
        self,
        *,
        root_dir: Path,
        run_dir: Path | None = None,
        mode: Mode = Mode.DASH,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        channel: QueueInteractionChannel | None = None,
        orchestrator: OrchestratorHandle | None = None,
        screen_factory: ScreenStackFactory | None = None,
        config_report: object | None = None,
    ) -> None:
        super().__init__()
        self.root_dir = root_dir
        # Precomputed by run_home_shell BEFORE app.run() - the source
        # detection scrubs os.environ process-wide (see config_report).
        self.config_report = config_report
        self.mode = mode
        self.poll_interval = poll_interval
        self.channel = channel
        self.orchestrator = orchestrator
        self.screen_factory = screen_factory
        # CLI-scoped modes (dash/embedded) are born bound to one run;
        # HOME opens and closes contexts as the user navigates. Named
        # run_context because App.run() is Textual's entry point.
        self.run_context: RunContext | None = (
            RunContext.observe(run_dir, root_dir)
            if run_dir is not None else None
        )
        self._pending_prompts: dict[str, PromptRequest] = {}
        self._stopping = False
        # D6 launch seam: injectable so Pilot tests drive a fake
        # session; the default binds the real substrate lazily.
        self.start_session: Callable[[object], object] = (
            self._default_start_session
        )
        self._session: object | None = None
        self._session_notified = False

    def _default_start_session(self, spec: object) -> object:
        from typing import cast

        from kstrl.launch import LaunchSpec
        from kstrl.tui.session import start_run_session

        return start_run_session(cast("LaunchSpec", spec), self.root_dir)

    # Compat views for screens and tests that duck-pull off the app.
    @property
    def store(self) -> StateStore | None:
        return (
            self.run_context.store
            if self.run_context is not None else None
        )

    @property
    def run_dir(self) -> Path | None:
        return (
            self.run_context.run_dir
            if self.run_context is not None else None
        )

    def on_mount(self) -> None:
        self.register_theme(KSTRL_THEME)
        self.theme = "kstrl"
        if self.mode is Mode.HOME:
            self.push_screen(HomeScreen())
        elif self.screen_factory is not None:
            for screen in self.screen_factory():
                self.push_screen(screen)
        else:
            self.push_screen(OverviewScreen(
                observe_only=self.mode is Mode.DASH,
            ))
        self.set_interval(self.poll_interval, self._poll)
        self.set_interval(1.0, self._tick_ages)
        if self.mode is Mode.HOME:
            self.set_interval(0.5, self._check_session)
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
        run = self.run_context
        if run is None:
            return
        chunk = run.tailer.poll_events()
        if chunk.truncated:
            run.store.reset()
        changed = run.store.apply_events(chunk.events)
        screen_stack = self.screen_stack
        if not screen_stack:
            return
        screen = screen_stack[-1]
        if isinstance(screen, HomeScreen):
            return  # home renders discovery, not one run's stream
        component_id = str(getattr(screen, "transcript_component", ""))
        ready = bool(getattr(screen, "ready", True))
        if changed:
            if component_id:
                if ready:
                    screen.refresh_state(  # type: ignore[attr-defined]
                        run.store.state, run.store.manifest(),
                    )
            else:
                screen.post_message(StateChanged(run.store.state))
            feed = getattr(screen, "feed_events", None)
            if feed is not None and not chunk.truncated:
                feed(chunk.events)
        if component_id and ready:
            feed_transcript = getattr(screen, "feed_transcript", None)
            if feed_transcript is not None:
                feed_transcript(
                    run.transcript_tailer(component_id).poll(),
                )

    def _tick_ages(self) -> None:
        run = self.run_context
        screen_stack = self.screen_stack
        if run is None or not screen_stack:
            return
        tick = getattr(screen_stack[-1], "tick_ages", None)
        if tick is not None:
            tick(run.store.state)

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

    def action_nav_back(self) -> None:
        # The stack floor is 2: Textual's hidden default screen plus
        # the base screen this mode pushed (home, or the dash board).
        if len(self.screen_stack) <= 2:
            return
        below = self.screen_stack[-2]
        if isinstance(below, HomeScreen) and self.session_in_flight():
            # The run owns its board while in flight; q offers a stop.
            self.notify(
                "run in flight - press q to stop it", severity="warning",
            )
            return
        self.pop_screen()

    # -- home mode (D1) ------------------------------------------------------

    def open_run(self, ref: object) -> None:
        """Open an observe context for a discovered run and push its
        kind-appropriate screen stack over home."""
        run_dir = getattr(ref, "run_dir", None)
        kind = str(getattr(ref, "kind", "factory"))
        if run_dir is None:
            return
        from kstrl.tui.dispatch import initial_screens_for_kind

        self.run_context = RunContext.observe(
            run_dir, self.root_dir, owns_app_exit=False,
        )
        for screen in initial_screens_for_kind(kind, observe_only=True)():
            self.push_screen(screen)
        self._poll()

    def close_run(self) -> None:
        """Tear down the observe context when navigation lands back on
        home. CLI-scoped contexts (dash/embedded) are never closed,
        and an in-flight launched session keeps running (the nav
        guards keep its board up; this is only a belt-and-braces
        no-op for that state)."""
        run = self.run_context
        if run is None or run.owns_app_exit:
            return
        if run.handle is not None and not run.handle.done():
            return
        session = self._session
        if session is not None:
            close = getattr(session, "close", None)
            if close is not None:
                close()
            self._session = None
        elif run.channel is not None:
            run.channel.detach()
        if run.handle is not None:
            run.handle.join(timeout=2)
        self.run_context = None

    def session_in_flight(self) -> bool:
        run = self.run_context
        return (
            self.mode is Mode.HOME
            and run is not None
            and run.handle is not None
            and not run.handle.done()
        )

    def launch(self, spec: object) -> None:
        """Start a command session (D6) and put its board over home."""
        if self.session_in_flight():
            self.notify("a run is already in flight", severity="warning")
            return
        from kstrl.tui.session import LaunchError

        try:
            session = self.start_session(spec)
        except LaunchError as exc:
            self.notify(str(exc), severity="error")
            return
        # The seam is duck-typed so Pilot tests can inject fakes.
        from typing import Any, cast

        from kstrl.tui.dispatch import initial_screens_for_kind

        sess = cast(Any, session)
        run = RunContext.observe(
            Path(sess.run_dir), self.root_dir, owns_app_exit=False,
        )
        run.channel = sess.channel
        run.handle = sess.handle
        self.run_context = run
        self._session = session
        self._session_notified = False
        self._stopping = False
        run.channel.attach(
            lambda req: self.call_from_thread(self.on_prompt_request, req),
        )
        # Replace any form screens with the run's board stack.
        while len(self.screen_stack) > 2:
            self.pop_screen()
        kind = str(getattr(session, "kind", "factory"))
        for screen in initial_screens_for_kind(
            kind, observe_only=False,
        )():
            self.push_screen(screen)
        self._poll()

    def _check_session(self) -> None:
        """HOME-mode counterpart of _check_orchestrator: a finished
        launched session notifies and keeps the board up (post-mortem
        reading); escape then pops home and tears it down."""
        run = self.run_context
        if (
            self.mode is not Mode.HOME
            or run is None
            or run.handle is None
            or not run.handle.done()
            or self._session_notified
        ):
            return
        self._session_notified = True
        self._poll()  # final drain
        code = run.handle.exit_code
        self.notify(
            f"run finished (exit {code}) - escape returns home",
            severity="information" if code == 0 else "error",
            timeout=10,
        )

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

    def _active_channel(self) -> QueueInteractionChannel | None:
        """The embedded CLI channel, or the home-launched session's."""
        if self.channel is not None:
            return self.channel
        run = self.run_context
        return run.channel if run is not None else None

    def _open_prompt(self, request: PromptRequest) -> None:
        channel = self._active_channel()
        if channel is None:
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
            if channel.resolve(request.request_id, choice):
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
        if self.mode is Mode.HOME:
            if self.session_in_flight():
                run = self.run_context
                session_handle = run.handle if run is not None else None
                if session_handle is None:
                    return
                if self._stopping or session_handle.stop.is_set():
                    # Second request escalates to force + quit, exactly
                    # like the embedded flow.
                    session_handle.stop.request("forced from TUI", force=True)
                    self.exit(130)
                    return

                def _session_stop(stop_run: bool | None) -> None:
                    if not stop_run or session_handle is None:
                        return
                    self._stopping = True
                    session_handle.stop.request("stopped from TUI")
                    self.notify(
                        "shutting down - the board stays up until the "
                        "run finishes",
                        timeout=10,
                    )

                self.push_screen(QuitModal(), _session_stop)
                return
            if isinstance(self.screen, HomeScreen):
                self.exit(0)
                return
            # q above home pops back to it (teardown happens on the
            # home screen's resume); quitting the app is home's call.
            # Floor 2 = Textual's default screen + HomeScreen.
            while len(self.screen_stack) > 2:
                self.pop_screen()
            return
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
