"""Chunk 5 (TUI rewrite): EventBridgeUI shim."""

from __future__ import annotations

import io

from ralph_py.events import CallbackSink, Event, EventBus, Log
from ralph_py.ui.base import UI
from ralph_py.ui.bridge import EventBridgeUI, NullPrompter, Prompter
from ralph_py.ui.plain import PlainUI


def _bridge_with_capture(
    prompter: Prompter | None = None,
    transcript_lines: list[str] | None = None,
) -> tuple[EventBridgeUI, list[Event]]:
    captured: list[Event] = []
    bus = EventBus(CallbackSink(captured.append))
    transcript = transcript_lines.append if transcript_lines is not None else None
    return EventBridgeUI(bus, prompter=prompter, transcript=transcript), captured


class TestProtocolConformance:
    def test_assignable_to_ui_protocol(self) -> None:
        bridge, _ = _bridge_with_capture()
        ui: UI = bridge  # structural check at type level
        assert ui.can_prompt() is False

    def test_plain_ui_satisfies_prompter(self) -> None:
        prompter: Prompter = PlainUI(no_color=True, file=io.StringIO())
        assert prompter.can_prompt() in (True, False)


class TestEventMapping:
    def test_exact_event_per_method(self) -> None:
        bridge, captured = _bridge_with_capture()
        bridge.title("T")
        bridge.section("S")
        bridge.subsection("SS")
        bridge.hr()
        bridge.kv("Key", "Value")
        bridge.startup_art()
        bridge.info("i")
        bridge.ok("o")
        bridge.warn("w")
        bridge.err("e")
        bridge.channel_header("GUARD", "Disallowed")
        bridge.stream_line("GIT", "git line")

        expected = [
            ("title", "info", "", "T"),
            ("section", "info", "", "S"),
            ("subsection", "info", "", "SS"),
            ("hr", "info", "", ""),
            ("kv", "info", "Key", "Value"),
            ("startup_art", "info", "", ""),
            ("line", "info", "", "i"),
            ("line", "ok", "", "o"),
            ("line", "warn", "", "w"),
            ("line", "error", "", "e"),
            ("channel", "info", "GUARD", "Disallowed"),
            ("stream", "info", "GIT", "git line"),
        ]
        assert len(captured) == len(expected)
        for event, (kind, severity, key, text) in zip(
            captured, expected, strict=True,
        ):
            assert isinstance(event, Log)
            assert (event.kind, event.severity, event.key, event.text) == (
                kind, severity, key, text,
            )


class TestTranscriptRouting:
    def test_tagged_stream_goes_to_transcript_not_bus(self) -> None:
        lines: list[str] = []
        bridge, captured = _bridge_with_capture(transcript_lines=lines)
        bridge.stream_line("AI", "agent output line")
        bridge.stream_line("GIT", "git output line")
        assert lines == ["agent output line"]
        assert len(captured) == 1  # only the GIT line became an event
        assert isinstance(captured[0], Log)
        assert captured[0].key == "GIT"

    def test_no_transcript_configured_tagged_lines_become_events(self) -> None:
        bridge, captured = _bridge_with_capture()
        bridge.stream_line("AI", "agent line")
        assert len(captured) == 1
        assert isinstance(captured[0], Log)
        assert captured[0].kind == "stream"
        assert captured[0].key == "AI"

    def test_transcript_failure_disables_not_raises(self) -> None:
        calls: list[str] = []

        def dying(line: str) -> None:
            calls.append(line)
            raise OSError("disk full")

        bus = EventBus()
        bridge = EventBridgeUI(bus, transcript=dying)
        bridge.stream_line("AI", "one")  # raises inside, swallowed
        bridge.stream_line("AI", "two")  # transcript disabled; becomes event
        assert calls == ["one"]


class TestPrompterDelegation:
    def test_choose_delegates(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, list[str], int]] = []

            def choose(self, header: str, options: list[str],
                       default: int = 0) -> int:
                self.calls.append((header, options, default))
                return 2

            def can_prompt(self) -> bool:
                return True

        rec = Recorder()
        bridge, captured = _bridge_with_capture(prompter=rec)
        assert bridge.choose("Q?", ["a", "b", "c"], 1) == 2
        assert rec.calls == [("Q?", ["a", "b", "c"], 1)]
        assert bridge.can_prompt() is True
        assert captured == []  # interaction is not an event

    def test_null_prompter_defaults(self) -> None:
        p = NullPrompter()
        assert p.can_prompt() is False
        assert p.choose("Q?", ["a", "b"], 1) == 1
