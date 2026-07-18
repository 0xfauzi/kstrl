"""Tests for observability module."""

from __future__ import annotations

from pathlib import Path

from ralph_py.observability import (
    NullProgressLog,
    ProgressLog,
    format_age,
    latest_run_id,
    parse_event_ts,
    read_progress_events,
    summarize_events,
)


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


class TestRunId:
    """R3.2 requirement 4: events carry the run_id."""

    def test_run_id_stamped_on_every_event(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "test.jsonl", run_id="run-123")
        log.factory_started("demo", 1)
        log.component_started("comp-a")
        log.emit("custom_event")

        events = log.read_events()
        assert len(events) == 3
        assert all(e["run_id"] == "run-123" for e in events)

    def test_no_run_id_omits_field(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "test.jsonl")
        log.emit("test")
        assert "run_id" not in log.read_events()[0]

    def test_latest_run_id_picks_last(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        ProgressLog(path, run_id="run-1").factory_started("demo", 1)
        ProgressLog(path, run_id="run-2").factory_started("demo", 1)

        events = read_progress_events(path)
        assert latest_run_id(events) == "run-2"

    def test_latest_run_id_empty_for_legacy_log(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        ProgressLog(path).emit("test")
        assert latest_run_id(read_progress_events(path)) == ""


class TestReadProgressEvents:
    def test_skips_malformed_tail_line(self, tmp_path: Path) -> None:
        """A torn line from a crash mid-write must not break consumers."""
        path = tmp_path / "test.jsonl"
        ProgressLog(path).emit("good_event")
        with open(path, "a") as f:
            f.write('{"ts": "2026-07-18T10:0')  # torn write

        events = read_progress_events(path)
        assert len(events) == 1
        assert events[0]["event"] == "good_event"


class TestSummarizeEvents:
    """R3.2: fold one run's events into per-component activity."""

    def _two_run_log(self, tmp_path: Path) -> Path:
        path = tmp_path / "progress.jsonl"
        old = ProgressLog(path, run_id="run-old")
        old.factory_started("demo", 1)
        old.component_started("comp-a")
        old.component_failed("comp-a", "old failure")
        new = ProgressLog(path, run_id="run-new")
        new.factory_started("demo", 2)
        new.component_started("comp-a")
        new.component_usage("comp-a", "engineer", {
            "calls": 3, "known_calls": 2, "unreported_calls": 1,
            "total_tokens": 1200, "cost_usd": 0.25,
        })
        new.verification_result("comp-a", passed=True)
        new.review_result("comp-a", passed=True, mode="hard")
        new.component_usage("comp-a", "review", {
            "calls": 1, "known_calls": 1, "unreported_calls": 0,
            "total_tokens": 300, "cost_usd": 0.05,
        })
        new.component_retrying("comp-b", attempt=2, reason="tests failed")
        return path

    def test_filters_to_named_run(self, tmp_path: Path) -> None:
        events = read_progress_events(self._two_run_log(tmp_path))
        activity = summarize_events(events, latest_run_id(events))

        assert activity.run_id == "run-new"
        # The old run's failure must not leak into comp-a's activity.
        comp_a = activity.components["comp-a"]
        assert comp_a.phase != "failed"
        assert comp_a.last_event == "component_usage"

    def test_usage_totals_accumulate_across_phases(
        self, tmp_path: Path,
    ) -> None:
        events = read_progress_events(self._two_run_log(tmp_path))
        comp_a = summarize_events(events, "run-new").components["comp-a"]

        assert comp_a.total_tokens == 1500
        assert comp_a.usage_calls == 4
        assert comp_a.unreported_calls == 1
        assert abs(comp_a.cost_usd - 0.30) < 1e-9

    def test_phase_and_attempt_derivation(self, tmp_path: Path) -> None:
        events = read_progress_events(self._two_run_log(tmp_path))
        activity = summarize_events(events, "run-new")

        # Last phase-bearing event for comp-a is the review usage record.
        assert activity.components["comp-a"].phase == "review"
        assert activity.components["comp-b"].attempt == 2
        assert activity.components["comp-b"].phase == "retrying"

    def test_finished_flag(self, tmp_path: Path) -> None:
        path = tmp_path / "progress.jsonl"
        log = ProgressLog(path, run_id="r1")
        log.factory_started("demo", 0)
        events = read_progress_events(path)
        assert summarize_events(events, "r1").finished is False

        log.factory_completed(completed=1, failed=0, skipped=0)
        events = read_progress_events(path)
        assert summarize_events(events, "r1").finished is True

    def test_empty_run_id_includes_legacy_events(self, tmp_path: Path) -> None:
        path = tmp_path / "progress.jsonl"
        ProgressLog(path).component_started("comp-a")
        activity = summarize_events(read_progress_events(path), "")
        assert "comp-a" in activity.components


class TestTimeHelpers:
    def test_parse_event_ts_roundtrip(self) -> None:
        parsed = parse_event_ts("2026-07-18T10:00:00Z")
        assert parsed is not None
        assert parsed.year == 2026

    def test_parse_event_ts_malformed(self) -> None:
        assert parse_event_ts("not-a-timestamp") is None

    def test_format_age_bands(self) -> None:
        assert format_age(5) == "5s"
        assert format_age(300) == "5m"
        assert format_age(7500) == "2h05m"
        assert format_age(200000) == "2.3d"
