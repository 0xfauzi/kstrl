"""Chunk 7 (TUI rewrite): renderer inversion + console assembly.

The load-bearing test is the round trip: every UI-protocol method
called on EventBridgeUI, rendered back through UIBackedRenderer onto a
PlainUI, must produce BYTE-IDENTICAL output to calling the same method
on a PlainUI directly. That single property is the no-regression proof
for every imperative call site the console swap touches.
"""

from __future__ import annotations

import io
from pathlib import Path

from kstrl import events as ev
from kstrl.output import Console, build_console
from kstrl.render import UIBackedRenderer, plain_renderer
from kstrl.ui.bridge import EventBridgeUI
from kstrl.ui.plain import PlainUI

# (method, args) covering all 14 protocol methods' render paths.
_CALLS: list[tuple[str, tuple[str, ...]]] = [
    ("title", ("Ralph",)),
    ("section", ("Startup",)),
    ("subsection", ("Git / Branch",)),
    ("hr", ()),
    ("kv", ("Root", "/tmp/project")),
    ("startup_art", ()),
    ("info", ("plain info line",)),
    ("ok", ("all checks passed",)),
    ("warn", ("something odd",)),
    ("err", ("something broke",)),
    ("channel_header", ("GUARD", "Disallowed changes")),
    ("stream_line", ("GIT", "On branch main")),
    ("stream_line", ("AI", "agent says hi")),  # no transcript -> event
]


def _drive(ui: object) -> None:
    for method, args in _CALLS:
        getattr(ui, method)(*args)


class TestInversionRoundTrip:
    def test_bridge_render_output_byte_identical_to_direct(self) -> None:
        direct_buf = io.StringIO()
        direct = PlainUI(no_color=True, file=direct_buf)
        _drive(direct)

        rendered_buf = io.StringIO()
        renderer = UIBackedRenderer(PlainUI(no_color=True, file=rendered_buf))
        bus = ev.EventBus(ev.CallbackSink(renderer.handle))
        bridge = EventBridgeUI(bus)  # no transcript: AI lines render too
        _drive(bridge)

        assert rendered_buf.getvalue() == direct_buf.getvalue()
        assert rendered_buf.getvalue() != ""

    def test_round_trip_survives_serialization(self, tmp_path: Path) -> None:
        """bridge -> events.jsonl -> read back -> renderer == direct.
        Proves a recorded run replays to the same terminal bytes."""
        direct_buf = io.StringIO()
        _drive(PlainUI(no_color=True, file=direct_buf))

        events_file = tmp_path / "events.jsonl"
        bus = ev.EventBus(ev.JsonlSink(events_file))
        _drive(EventBridgeUI(bus))
        bus.close()

        replay_buf = io.StringIO()
        renderer = UIBackedRenderer(PlainUI(no_color=True, file=replay_buf))
        for event in ev.read_events(events_file):
            renderer.handle(event)

        assert replay_buf.getvalue() == direct_buf.getvalue()

    def test_semantic_events_render_nothing(self) -> None:
        buf = io.StringIO()
        renderer = UIBackedRenderer(PlainUI(no_color=True, file=buf))
        for event in [
            ev.RunStarted(project="p", components=1),
            ev.PhaseStarted(component="a", phase="verify", attempt=1),
            ev.PhaseCompleted(component="a", phase="verify", passed=True),
            ev.ComponentUsage(component="a", phase="engineer", calls=1),
            ev.FindingRecorded(component="a", phase="review"),
            ev.WorkerHeartbeat(component="a", pid=1),
            ev.RunCompleted(completed=1),
            ev.UnknownEvent(type_name="future_thing"),
        ]:
            renderer.handle(ev.EventBus().emit(event))
        assert buf.getvalue() == ""  # no double-narration, ever

    def test_unknown_log_kind_still_surfaces_text(self) -> None:
        buf = io.StringIO()
        renderer = UIBackedRenderer(PlainUI(no_color=True, file=buf))
        renderer.handle(ev.EventBus().emit(
            ev.Log(kind="holo_display", text="important message"),
        ))
        assert "important message" in buf.getvalue()


class TestBuildConsole:
    def test_assembly_and_synchronous_render(self) -> None:
        console = build_console("plain", no_color=True)
        assert isinstance(console, Console)
        assert isinstance(console.prompter, PlainUI)
        buf = io.StringIO()
        # Repoint the concrete UI at a buffer to observe rendering.
        console.prompter._file = buf  # type: ignore[attr-defined]
        console.ui.info("hello")
        assert "hello" in buf.getvalue()

    def test_bus_reachable_for_run_factory_discovery(self) -> None:
        console = build_console("plain", no_color=True)
        assert console.ui.bus is console.bus

    def test_gum_alias_resolves(self) -> None:
        # "gum" is a vestigial alias for rich; resolution must not crash
        # on non-tty test environments (falls back per get_ui rules).
        console = build_console("gum", no_color=True)
        assert console.prompter is not None

    def test_plain_renderer_helper(self) -> None:
        renderer = plain_renderer(no_color=True)
        assert isinstance(renderer.ui, PlainUI)
