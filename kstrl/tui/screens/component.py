"""Component detail screen (design pass).

Structure fix from the critique: the findings table and transcript
used to float untitled in dead space. Now each region has a 1-line
panel title in the shared title grammar ("findings", "engineer
transcript"), the findings title carries the bookkeeping count the
table hides, and the transcript title shows the follow state. The
header uses the theme's status color instead of hardcoded yellow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Static

from kstrl.tui import theme
from kstrl.tui.messages import StateChanged
from kstrl.tui.widgets.evidence import EvidencePanel
from kstrl.tui.widgets.findings_table import FindingsTable
from kstrl.tui.widgets.phase_timeline import PhaseTimeline
from kstrl.tui.widgets.transcript import TranscriptTail

if TYPE_CHECKING:
    from kstrl.manifest import Manifest
    from kstrl.reducer import ComponentState, RunState


class ComponentScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("f", "toggle_follow", "Follow"),
    ]

    def __init__(self, component_id: str) -> None:
        super().__init__()
        self.component_id = component_id
        self._following = True

    def compose(self) -> ComposeResult:
        yield Static(id="component-header")
        yield PhaseTimeline(id="phase-timeline")
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
        self._update_transcript_title()
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
        glyph, color = theme.status_glyph(comp.status)
        header = Text()
        header.append(
            f" {self.component_id} ",
            style=f"bold {theme.BACKGROUND} on {theme.ACCENT}",
        )
        if comp.title:
            header.append(f"  {comp.title}", style="bold")
        header.append(f"  {glyph} {comp.status}", style=f"bold {color}")
        if comp.attempt > 1:
            header.append(f"  attempt {comp.attempt}", style=theme.MUTED)
        self.query_one("#component-header", Static).update(header)
        self.query_one(PhaseTimeline).update_state(comp)
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
        self.query_one("#findings-title", Static).update(title)
        manifest_comp = (
            manifest.get_component(self.component_id)
            if manifest is not None else None
        )
        self.query_one(EvidencePanel).update_state(comp, manifest_comp)

    def _update_transcript_title(self) -> None:
        title = Text("engineer transcript", style="bold")
        if self._following:
            title.append("  ● following", style=theme.ACCENT)
        else:
            title.append("  ⏸ paused", style=theme.MUTED)
        title.append("  (f toggles)", style=theme.MUTED)
        self.query_one("#transcript-title", Static).update(title)

    def feed_transcript(self, lines: list[str]) -> None:
        self.query_one(TranscriptTail).feed_lines(lines)

    def action_toggle_follow(self) -> None:
        self._following = self.query_one(TranscriptTail).toggle_follow()
        self._update_transcript_title()

    def on_state_changed(self, message: StateChanged) -> None:
        # Manifest join is injected by the app poll; message-only
        # refresh covers state (manifest may lag one poll harmlessly).
        self.refresh_state(message.state, None)
