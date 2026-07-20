"""TUI surface A3: the universal run substrate (open_command_run)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kstrl import events as ev
from kstrl.commandrun import CommandRun, open_command_run
from kstrl.runid import run_kind
from kstrl.ui.bridge import EventBridgeUI, NullPrompter
from kstrl.ui.plain import PlainUI


def _events_on_disk(run: CommandRun) -> list[dict[str, Any]]:
    assert run.paths is not None
    lines = run.paths.events_file.read_text().splitlines()
    return [json.loads(line) for line in lines]


class TestOpenCommandRun:
    def test_enabled_records_events_with_component_stamp(
        self, tmp_path: Path,
    ) -> None:
        ui = PlainUI(no_color=True)
        run = open_command_run(
            ui, tmp_path, "understand", component="understand",
            enabled=True, heartbeat=False,
        )
        assert run.recording
        assert run_kind(run.run_id) == "understand"
        run.bus.emit(ev.RunStarted(project="p", components=1))
        run.close()
        events = _events_on_disk(run)
        assert [e["event"] for e in events] == ["factory_started"]
        assert events[0]["component"] == "understand"
        assert events[0]["run_id"] == run.run_id

    def test_disabled_leaves_no_run_dir(self, tmp_path: Path) -> None:
        run = open_command_run(
            PlainUI(no_color=True), tmp_path, "understand",
            enabled=False,
        )
        assert not run.recording
        run.bus.emit(ev.RunStarted(project="p"))
        assert run.transcript_path("understand") is None
        assert run.transcript_writer("understand") is None
        run.close()  # safe when disabled
        assert not (tmp_path / ".kstrl" / "runs").exists()

    def test_enabled_none_reads_factory_gating(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[factory]\nprogress_log_enabled = false\n",
        )
        run = open_command_run(
            PlainUI(no_color=True), tmp_path, "decompose",
        )
        assert not run.recording
        run.close()

    def test_bridge_ui_bus_is_reused_and_restored(
        self, tmp_path: Path,
    ) -> None:
        """Console narration lands in the run stream (chunk-7 wiring),
        and close() un-stamps the shared bus for post-run lines."""
        rendered: list[ev.Event] = []
        bus = ev.EventBus(
            ev.CallbackSink(rendered.append),
            run_id="outer-run", component="outer-component",
        )
        ui = EventBridgeUI(bus, prompter=NullPrompter())
        run = open_command_run(
            ui, tmp_path, "decompose", component="architect",
            enabled=True, heartbeat=False,
        )
        assert run.bus is bus
        assert bus.run_id == run.run_id
        assert bus.component == "architect"
        ui.info("resolved spec")
        run.close()
        assert bus.run_id == "outer-run"
        assert bus.component == "outer-component"
        ui.info("post-run narration")
        events = _events_on_disk(run)
        assert [e["event"] for e in events] == ["log"]
        assert events[0]["component"] == "architect"
        # The post-close line rendered to the console but not the file.
        assert len(rendered) == 2

    def test_bridge_run_level_session_clears_inherited_component(
        self, tmp_path: Path,
    ) -> None:
        bus = ev.EventBus(run_id="outer-run", component="outer-component")
        ui = EventBridgeUI(bus, prompter=NullPrompter())
        run = open_command_run(
            ui, tmp_path, "decompose", enabled=True, heartbeat=False,
        )
        assert bus.component == ""
        run.bus.emit(ev.RunStarted(project="p"))
        run.close()

        assert bus.run_id == "outer-run"
        assert bus.component == "outer-component"
        assert _events_on_disk(run)[0]["component"] == ""

    def test_heartbeat_started_when_enabled(self, tmp_path: Path) -> None:
        run = open_command_run(
            PlainUI(no_color=True), tmp_path, "feature",
            component="my-feature", enabled=True, heartbeat=True,
        )
        assert run._stop_heartbeat is not None
        run.close()
        assert run._stop_heartbeat is None


class TestStreamFilter:
    def test_agent_stream_stays_out_of_events_jsonl(
        self, tmp_path: Path,
    ) -> None:
        bus = ev.EventBus(ev.CallbackSink(lambda e: None))
        ui = EventBridgeUI(bus, prompter=NullPrompter())
        run = open_command_run(
            ui, tmp_path, "understand", component="understand",
            enabled=True, heartbeat=False,
        )
        ui.stream_line("AI", "full agent output line")
        ui.stream_line("verify", "pytest output line")
        ui.info("narration")
        run.close()
        events = _events_on_disk(run)
        kinds = [(e["event"], e["data"].get("key", "")) for e in events]
        assert ("log", "AI") not in kinds
        assert ("log", "verify") in kinds
        assert ("log", "") in kinds


class TestTranscripts:
    def test_writer_appends_lines_to_engineer_log(
        self, tmp_path: Path,
    ) -> None:
        run = open_command_run(
            PlainUI(no_color=True), tmp_path, "understand",
            component="understand", enabled=True, heartbeat=False,
        )
        write = run.transcript_writer("understand")
        assert write is not None
        write("first line")
        write("second line\n")
        path = run.transcript_path("understand")
        assert path is not None
        assert path == (
            run.paths.root / "components" / "understand" / "engineer.log"  # type: ignore[union-attr]
        )
        assert path.read_text() == "first line\nsecond line\n"
        run.close()
