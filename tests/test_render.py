"""Chunk 7 (TUI rewrite): renderer inversion, replayed from disk.

Every UI-protocol method called on EventBridgeUI, written to events.jsonl,
read back and rendered through UIBackedRenderer onto a PlainUI must produce
BYTE-IDENTICAL output to calling the same method on a PlainUI directly.
That single property is the no-regression proof for every imperative call
site the console swap touches, and it proves a recorded run replays to the
same terminal bytes.
"""

from __future__ import annotations

import io
from pathlib import Path

from kstrl import events as ev
from kstrl.render import UIBackedRenderer
from kstrl.ui.bridge import EventBridgeUI
from kstrl.ui.plain import PlainUI

# (method, args) covering all 14 protocol methods' render paths.
_CALLS: list[tuple[str, tuple[str, ...]]] = [
    ("title", ("kstrl",)),
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
        assert replay_buf.getvalue() != ""
