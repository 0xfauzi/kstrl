"""Stage 3 PR C (TUI rewrite): byte-offset tailers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from kstrl import events as ev
from kstrl import reducer
from kstrl.tui.tail import JsonlTailer, RunTailer, TextTailer
from tests.helpers.fake_run import FakeRunSpec, stream_fake_run, write_fake_run


class TestJsonlTailer:
    def test_incremental_polls_see_only_new_events(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = ev.JsonlSink(path)
        bus = ev.EventBus(sink, run_id="r")
        tailer = JsonlTailer(path)

        assert tailer.poll().events == []
        bus.emit(ev.Log(text="one"))
        first = tailer.poll().events
        assert [e.to_dict()["data"]["text"] for e in first] == ["one"]
        bus.emit(ev.Log(text="two"))
        bus.emit(ev.Log(text="three"))
        second = tailer.poll().events
        assert [e.to_dict()["data"]["text"] for e in second] == ["two", "three"]
        assert tailer.poll().events == []

    def test_torn_tail_completed_across_polls(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        line = ev.EventBus(run_id="r").emit(ev.Log(text="torn")).to_json_line()
        cut = len(line) // 2
        tailer = JsonlTailer(path)
        with open(path, "a") as f:
            f.write(line[:cut])
            f.flush()
        assert tailer.poll().events == []  # partial buffered, not parsed
        with open(path, "a") as f:
            f.write(line[cut:] + "\n")
        events = tailer.poll().events
        assert len(events) == 1
        assert events[0].to_dict()["data"]["text"] == "torn"

    def test_truncated_file_resets_and_reports(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        bus = ev.EventBus(ev.JsonlSink(path), run_id="r")
        bus.emit(ev.Log(text="old-1"))
        bus.emit(ev.Log(text="old-2"))
        tailer = JsonlTailer(path)
        assert len(tailer.poll().events) == 2
        path.write_text("")  # replaced/truncated under us
        bus2 = ev.EventBus(ev.JsonlSink(path), run_id="r")
        bus2.emit(ev.Log(text="fresh"))
        chunk = tailer.poll()
        assert chunk.truncated is True
        assert [e.to_dict()["data"]["text"] for e in chunk.events] == ["fresh"]

    def test_replaced_larger_file_resets_and_reports(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        bus = ev.EventBus(ev.JsonlSink(path), run_id="r")
        bus.emit(ev.Log(text="old"))
        tailer = JsonlTailer(path)
        assert len(tailer.poll().events) == 1
        replacement = tmp_path / "replacement.jsonl"
        replacement_bus = ev.EventBus(ev.JsonlSink(replacement), run_id="r")
        replacement_bus.emit(ev.Log(text="fresh-one"))
        replacement_bus.emit(ev.Log(text="fresh-two"))
        os.replace(replacement, path)

        chunk = tailer.poll()

        assert chunk.truncated is True
        assert [e.to_dict()["data"]["text"] for e in chunk.events] == [
            "fresh-one", "fresh-two",
        ]

    def test_invalid_json_line_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        with open(path, "a") as f:
            f.write("{not json}\n")
            f.write(json.dumps({"event": "log", "data": {"text": "ok"}}) + "\n")
        events = JsonlTailer(path).poll().events
        assert len(events) == 1

    def test_missing_file_quiet(self, tmp_path: Path) -> None:
        assert JsonlTailer(tmp_path / "nope.jsonl").poll().events == []


class TestTextTailer:
    def test_incremental_lines_and_partial(self, tmp_path: Path) -> None:
        path = tmp_path / "engineer.log"
        tailer = TextTailer(path)
        with open(path, "a") as f:
            f.write("line one\nline two\npart")
        assert tailer.poll() == ["line one", "line two"]
        with open(path, "a") as f:
            f.write("ial three\n")
        assert tailer.poll() == ["partial three"]

    def test_catch_up_burst_bounded(self, tmp_path: Path) -> None:
        path = tmp_path / "engineer.log"
        with open(path, "a") as f:
            for i in range(500):
                f.write(f"line {i}\n")
        lines = TextTailer(path, max_lines=100).poll()
        assert len(lines) == 100
        assert lines[-1] == "line 499"

    def test_max_lines_must_be_positive(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(ValueError, match="max_lines must be positive"):
            TextTailer(tmp_path / "engineer.log", max_lines=0)


class TestRunTailer:
    def test_streamed_run_arrives_incrementally_and_folds(
        self, tmp_path: Path,
    ) -> None:
        """Step the fake run; every poll's events fold incrementally to
        the same state a one-shot fold produces (fold == tailed apply)."""
        run_id = "factory-20260720-130000.000000-t"
        stepper = stream_fake_run(
            tmp_path, FakeRunSpec(components=2), run_id=run_id,
        )
        tailer = RunTailer(tmp_path / ".kstrl" / "runs" / run_id)
        state = reducer.RunState()
        polls_with_events = 0
        for _ in stepper:
            for event in tailer.poll_events().events:
                reducer.apply(state, event)
            polls_with_events += 1
        for event in tailer.poll_events().events:  # final drain
            reducer.apply(state, event)

        expected, _ = reducer.load_run_state(tmp_path, run_id)
        assert state == expected
        assert polls_with_events > 3  # genuinely incremental
        assert state.components["comp-a"].status == "completed"
        assert state.finished is True

    def test_worker_files_discovered_late(self, tmp_path: Path) -> None:
        run_id = "factory-20260720-140000.000000-t"
        run_dir = tmp_path / ".kstrl" / "runs" / run_id
        bus = ev.EventBus(
            ev.JsonlSink(run_dir / "events.jsonl"), run_id=run_id,
        )
        tailer = RunTailer(run_dir)
        bus.emit(ev.ComponentStarted(component="late"))
        assert len(tailer.poll_events().events) == 1
        assert tailer.known_components() == []
        # Worker dir appears AFTER the tailer started:
        paths = ev.RunPaths.for_run(tmp_path, run_id)
        worker = ev.EventBus(
            ev.JsonlSink(paths.engineer_events("late")),
            run_id=run_id, source="worker", component="late",
        )
        worker.emit(ev.IterationStarted(iteration=1, max_iterations=3))
        events = tailer.poll_events().events
        assert [type(e).type for e in events] == ["iteration_started"]
        assert tailer.known_components() == ["late"]

    def test_merged_order_by_ts(self, tmp_path: Path) -> None:
        write_fake_run(tmp_path, FakeRunSpec(components=2))
        run_dir = tmp_path / ".kstrl" / "runs"
        (run_dir,) = list(run_dir.iterdir())
        events = RunTailer(run_dir).poll_events().events
        timestamps = [e.ts for e in events]
        assert timestamps == sorted(timestamps)
        assert any(e.source == "worker" for e in events)

    def test_worker_replacement_rebuilds_complete_snapshot(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=2))
        tailer = RunTailer(run_dir)
        initial = tailer.poll_events()
        assert initial.events
        worker_path = run_dir / "components" / "comp-a" / "engineer.jsonl"
        replacement = tmp_path / "replacement.jsonl"
        replacement_bus = ev.EventBus(
            ev.JsonlSink(replacement), run_id=run_dir.name,
            source="worker", component="comp-a",
        )
        replacement_bus.emit(ev.Log(text="replacement worker stream"))
        replacement_bus.emit(ev.Log(text="second replacement event"))
        os.replace(replacement, worker_path)

        rebuilt = tailer.poll_events()

        assert rebuilt.truncated is True
        assert any(isinstance(event, ev.RunStarted) for event in rebuilt.events)
        replacement_logs = [
            event for event in rebuilt.events
            if isinstance(event, ev.Log)
            and event.text.startswith("replacement")
        ]
        assert len(replacement_logs) == 1
