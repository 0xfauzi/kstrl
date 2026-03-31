"""Tests for observability module."""

from __future__ import annotations

from pathlib import Path

from ralph_py.observability import NullProgressLog, ProgressLog


class TestProgressLog:
    def test_emit_creates_file(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "test.jsonl")
        log.emit("test_event")
        assert log.path.exists()

    def test_emit_appends_jsonl(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "test.jsonl")
        log.emit("event_a", component_id="comp-1")
        log.emit("event_b", component_id="comp-2", data={"key": "val"})

        events = log.read_events()
        assert len(events) == 2
        assert events[0]["event"] == "event_a"
        assert events[0]["component"] == "comp-1"
        assert events[1]["event"] == "event_b"
        assert events[1]["data"]["key"] == "val"

    def test_event_has_timestamp(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "test.jsonl")
        log.emit("test")
        events = log.read_events()
        assert "ts" in events[0]
        assert "T" in events[0]["ts"]

    def test_convenience_methods(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "test.jsonl")
        log.factory_started("demo", 5)
        log.component_started("comp-a")
        log.component_completed("comp-a", duration=10.5, iterations=3)
        log.component_failed("comp-b", error="timeout")
        log.component_retrying("comp-b", attempt=2, reason="test failure")
        log.verification_result("comp-a", passed=True, duration=5.0)
        log.review_result("comp-a", passed=True, mode="hard")
        log.contract_result(tier=0, passed=True)
        log.factory_completed(completed=3, failed=1, skipped=0, duration=120.0)

        events = log.read_events()
        assert len(events) == 9
        assert events[0]["event"] == "factory_started"
        assert events[0]["data"]["project"] == "demo"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "nested" / "dir" / "test.jsonl")
        log.emit("test")
        assert log.path.exists()

    def test_read_events_empty_file(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "test.jsonl")
        assert log.read_events() == []

    def test_read_events_missing_file(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "nonexistent.jsonl")
        assert log.read_events() == []


class TestNullProgressLog:
    def test_emit_is_noop(self) -> None:
        log = NullProgressLog()
        log.emit("test", component_id="comp", data={"key": "val"})
        # Should not raise or create any file

    def test_convenience_methods_are_noop(self) -> None:
        log = NullProgressLog()
        log.factory_started("demo", 5)
        log.component_started("comp-a")
        log.component_completed("comp-a", duration=10.0, iterations=3)
