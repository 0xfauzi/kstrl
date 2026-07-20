"""Component detail screen (PR E)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Static

from ralph_py.tui.messages import StateChanged
from ralph_py.tui.widgets.evidence import EvidencePanel
from ralph_py.tui.widgets.findings_table import FindingsTable
from ralph_py.tui.widgets.phase_timeline import PhaseTimeline
from ralph_py.tui.widgets.transcript import TranscriptTail

if TYPE_CHECKING:
    from ralph_py.manifest import Manifest
    from ralph_py.reducer import ComponentState, RunState


class ComponentScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("f", "toggle_follow", "Follow"),
    ]

    def __init__(self, component_id: str) -> None:
        super().__init__()
        self.component_id = component_id

    def compose(self) -> ComposeResult:
        yield Static(id="component-header")
        yield PhaseTimeline(id="phase-timeline")
        yield FindingsTable(id="findings-table")
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

    def refresh_state(
        self, state: RunState, manifest: Manifest | None,
    ) -> None:
        comp: ComponentState | None = state.components.get(self.component_id)
        if comp is None:
            return
        header = Text()
        header.append(f" {self.component_id} ", style="bold reverse")
        if comp.title:
            header.append(f"  {comp.title}", style="bold")
        header.append(f"  {comp.status}", style="yellow")
        if comp.findings_count:
            header.append(f"  {comp.findings_count} finding(s)", style="dim")
        self.query_one("#component-header", Static).update(header)
        self.query_one(PhaseTimeline).update_state(comp)
        self.query_one(FindingsTable).update_state(comp)
        manifest_comp = (
            manifest.get_component(self.component_id)
            if manifest is not None else None
        )
        self.query_one(EvidencePanel).update_state(comp, manifest_comp)

    def feed_transcript(self, lines: list[str]) -> None:
        self.query_one(TranscriptTail).feed_lines(lines)

    def action_toggle_follow(self) -> None:
        following = self.query_one(TranscriptTail).toggle_follow()
        self.notify(
            "following transcript" if following else "follow paused",
            timeout=1.5,
        )

    def on_state_changed(self, message: StateChanged) -> None:
        # Manifest join is injected by the app poll; message-only
        # refresh covers state (manifest may lag one poll harmlessly).
        self.refresh_state(message.state, None)
