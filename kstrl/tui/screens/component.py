"""Component detail screen (design pass).

Structure fix from the critique: the findings table and transcript
used to float untitled in dead space. Now each region has a 1-line
panel title in the shared title grammar ("findings", "engineer
transcript"), the findings title carries the bookkeeping count the
table hides, and the transcript title shows the follow state. The
header uses the theme's status color instead of hardcoded yellow.

#433 increment 1:
- A failed phase says why, under the timeline: the gate's failures and,
  for the latest one, the stored gate output (F7).
- The transcript is "following" only while the component is moving in
  a run that has not finished. A finished component's transcript is
  "saved", ``f`` leaves the footer, and a component that wrote none says
  so in one sentence instead of showing an empty pane (F8).
- An empty findings table is hidden; its title already says "none".
- The transcript is filled on mount rather than at the next poll.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.padding import Padding
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Static

from kstrl.tui import theme
from kstrl.tui.agent_health import agent_health
from kstrl.tui.messages import StateChanged
from kstrl.tui.widgets.component_detail import (
    newest_gate_log,
    render_component_header,
    render_failure_detail,
    transcript_path,
    transcript_written,
)
from kstrl.tui.widgets.evidence import EvidencePanel
from kstrl.tui.widgets.findings_table import FindingsTable
from kstrl.tui.widgets.phase_timeline import PhaseTimeline
from kstrl.tui.widgets.transcript import TranscriptTail

if TYPE_CHECKING:
    from kstrl.manifest import Manifest
    from kstrl.reducer import ComponentState, RunState

_MOVING = ("running", "verifying")


def retry_route(component_id: str) -> Padding:
    """Where a failed component's retry is (#433 H12). The failure queue
    says whether a retry is offered and what it would do."""
    from kstrl.tui.screens.home import HOME_COMMANDS

    key = next(n for n, cmd in enumerate(HOME_COMMANDS, 1) if cmd.command_id == "retry")
    route = Text(
        f"{key} on home opens the failure queue; ks retry {component_id} runs one from a shell",
        style=theme.MUTED,
    )
    return Padding(
        theme.label_rows([(Text("retry", style=f"bold {theme.ACCENT}"), route)]), (0, 0, 0, 2)
    )


class ComponentScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("o", "open_output", "Full output"),
    ]

    def __init__(self, component_id: str) -> None:
        super().__init__()
        self.component_id = component_id
        # The app's duck-typed poll contract: a screen naming a
        # transcript_component gets refresh_state(state, manifest) and
        # that ONE component's transcript tail.
        self.transcript_component = component_id
        self._following = True
        #: True while the run is unfinished and the component is moving;
        #: only then does the transcript grow, so only then is "follow" a
        #: thing to toggle.
        self._live = False
        #: The newest failed gate's stored output; ``o`` opens it whole.
        self._gate_log = ""

    def compose(self) -> ComposeResult:
        yield Static(id="component-header")
        yield PhaseTimeline(id="phase-timeline")
        yield Static(id="failure-detail")
        yield Static("findings", id="findings-title")
        yield FindingsTable(id="findings-table")
        yield Static(id="transcript-title")
        yield TranscriptTail(id="transcript")
        yield EvidencePanel(id="evidence")
        yield Footer()

    @property
    def ready(self) -> bool:
        """Whether compose has mounted the widgets used by poll delivery."""
        return next(iter(self.query(TranscriptTail)), None) is not None

    def on_mount(self) -> None:
        # Initial fill happens here, not at push time - compose has not
        # run yet when the app pushes the screen. Duck-typed pull keeps
        # this module import-free of app.py.
        store = getattr(self.app, "store", None)
        if store is not None:
            self.refresh_state(store.state, store.manifest())
        run = getattr(self.app, "run_context", None)
        if run is not None:
            # The app feeds the transcript on its next poll; a finished
            # run's detail screen should not open on an empty pane.
            self.feed_transcript(run.transcript_tailer(self.component_id).poll())
        self._update_transcript_title()

    def refresh_state(
        self,
        state: RunState,
        manifest: Manifest | None,
    ) -> None:
        comp: ComponentState | None = state.components.get(self.component_id)
        if comp is None:
            return
        live = not state.finished and comp.status in _MOVING
        if live != self._live:
            self._live = live
            self.refresh_bindings()
            self._update_transcript_title()
        self._render_header(comp, live)
        self.query_one(PhaseTimeline).update_state(comp)
        route = retry_route(comp.component_id) if comp.status == "failed" else None
        failure = render_failure_detail(comp, getattr(self.app, "root_dir", None), route)
        failure_widget = self.query_one("#failure-detail", Static)
        failure_widget.display = failure is not None
        if failure is not None:
            failure_widget.update(failure)
        self._update_findings(comp)
        manifest_comp = manifest.get_component(self.component_id) if manifest is not None else None
        self.query_one(EvidencePanel).update_state(comp, manifest_comp, show_error=failure is None)
        if newest_gate_log(comp) != self._gate_log:
            self._gate_log = newest_gate_log(comp)
            self.refresh_bindings()

    def _render_header(self, comp: ComponentState, live: bool) -> None:
        """The header; a live component's carries its agent health (#433 M2).

        Only while the run is unfinished and the component moves: a
        finished run's heartbeat pid may belong to another process now.
        """
        health = agent_health(getattr(self.app, "run_dir", None), comp) if live else None
        self.query_one("#component-header", Static).update(
            render_component_header(comp, health=health)
        )

    def tick_ages(self, state: RunState) -> None:
        """1 s refresh of the header's ages (the app's age tick)."""
        comp = state.components.get(self.component_id)
        if comp is not None and self.ready:
            self._render_header(comp, not state.finished and comp.status in _MOVING)

    def _update_findings(self, comp: ComponentState) -> None:
        findings = self.query_one(FindingsTable)
        findings.update_state(comp)
        real_count = comp.findings_count - findings.hidden_count
        title = Text("findings", style="bold")
        if real_count:
            title.append(f" · {real_count}", style=theme.WARNING)
        if findings.hidden_count:
            title.append(
                f" · {findings.hidden_count} bookkeeping record(s) hidden",
                style=theme.MUTED,
            )
        if not real_count and not findings.hidden_count:
            title.append(" · none", style=theme.MUTED)
        # An empty table is only a header row; the title says "none".
        findings.display = findings.row_count > 0
        self.query_one("#findings-title", Static).update(title)

    def _update_transcript_title(self) -> None:
        tail = self.query_one(TranscriptTail)
        title = Text("engineer transcript", style="bold")
        written = tail.lines_written > 0 or transcript_written(
            transcript_path(getattr(self.app, "run_dir", None), self.component_id)
        )
        if self._live and self._following:
            title.append("  ● following", style=theme.ACCENT)
            title.append("  (f pauses)", style=theme.MUTED)
        elif self._live:
            title.append("  ⏸ paused", style=theme.MUTED)
            title.append("  (f follows)", style=theme.MUTED)
        elif written:
            title.append(f"  · saved, {tail.lines_written} line(s)", style=theme.MUTED)
        else:
            title.append(
                "  · this component wrote no engineer transcript in this run",
                style=theme.MUTED,
            )
        tail.display = written or self._live
        self.query_one("#transcript-title", Static).update(title)

    def feed_transcript(self, lines: list[str]) -> None:
        tail = self.query_one(TranscriptTail)
        before = tail.lines_written
        tail.feed_lines(lines)
        if tail.lines_written != before and not self._live:
            self._update_transcript_title()

    def check_action(self, action: str, _parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_follow":
            return self._live
        if action == "open_output":
            return bool(self._gate_log)
        return True

    def action_open_output(self) -> None:
        if not self._gate_log:
            return
        from kstrl.tui.screens.gate_log import GateLogScreen

        self.app.push_screen(
            GateLogScreen(self._gate_log, self.component_id, getattr(self.app, "root_dir", None))
        )

    def action_toggle_follow(self) -> None:
        self._following = self.query_one(TranscriptTail).toggle_follow()
        self._update_transcript_title()

    def on_state_changed(self, message: StateChanged) -> None:
        # Manifest join is injected by the app poll; message-only
        # refresh covers state (manifest may lag one poll harmlessly).
        self.refresh_state(message.state, None)
