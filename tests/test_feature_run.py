"""The feature flow as an event-stream run."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.config.config import KstrlConfig
from kstrl.events.reducer import load_run_state
from kstrl.factory.commandrun import open_command_run
from kstrl.feature_cmd import run_feature
from kstrl.loop.loop import LoopResult
from kstrl.ui.plain import PlainUI
from tests.test_feature_cmd import ScriptedChannel, StubAgent, _params


def _loop_results_with_agent(*codes: int) -> Callable[..., LoopResult]:
    """run_loop stub: drains the phase agent (so transcripts tee) and
    returns the scripted exit codes in order."""
    remaining = list(codes)

    def fake(config: Any, ui: Any, agent: Any, *args: Any, **kwargs: Any) -> LoopResult:
        for _ in agent.run("prompt"):
            pass
        code = remaining.pop(0)
        return LoopResult(completed=code == 0, iterations=2, exit_code=code)

    return fake


def _run_recorded(
    tmp_path: Path,
    *,
    codes: tuple[int, ...],
    choice: int = 0,
) -> int:
    params = _params(tmp_path, repair_max_runs=2)
    ui = PlainUI(no_color=True, file=io.StringIO())
    command_run = open_command_run(
        ui,
        tmp_path,
        "feature",
        component=params.feature_name,
        enabled=True,
        heartbeat=False,
    )
    try:
        with (
            patch("kstrl.feature_cmd.run_loop", _loop_results_with_agent(*codes)),
            patch("kstrl.feature_cmd.get_agent", return_value=StubAgent()),
        ):
            return run_feature(
                params,
                KstrlConfig(),
                StubAgent(),
                ui,
                tmp_path,
                interaction=ScriptedChannel(choice),
                run=command_run,
            )
    finally:
        command_run.close()


class TestFeatureRunRecording:
    def test_full_repair_arc_folds(self, tmp_path: Path) -> None:
        code = _run_recorded(tmp_path, codes=(0, 1, 0))
        assert code == 0

        state, _ = load_run_state(tmp_path)
        assert state.kind == "feature"
        assert state.finished
        assert state.plan_order == ["demo"]
        comp = state.components["demo"]
        assert comp.status == "completed"
        assert [p["phase"] for p in comp.phase_history] == [
            "understand",
            "implement",
            "repair-1",
        ]
        assert [p["passed"] for p in comp.phase_history] == [
            True,
            False,
            True,
        ]
        assert comp.checkpoint_open == ""  # gate resolved
        assert [a["label"] for a in state.artifacts] == [
            "understand_file",
            "repair_prd",
        ]

    def test_gate_quit_folds_as_skipped(self, tmp_path: Path) -> None:
        code = _run_recorded(tmp_path, codes=(0,), choice=1)
        assert code == 0
        state, _ = load_run_state(tmp_path)
        assert state.finished
        comp = state.components["demo"]
        assert comp.status == "skipped"
        assert [p["phase"] for p in comp.phase_history] == ["understand"]

    def test_incomplete_understand_is_skipped(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        ui = PlainUI(no_color=True, file=io.StringIO())
        command_run = open_command_run(
            ui,
            tmp_path,
            "feature",
            component="demo",
            enabled=True,
            heartbeat=False,
        )

        def incomplete(*args: Any, **kwargs: Any) -> LoopResult:
            return LoopResult(completed=False, iterations=1, exit_code=0)

        try:
            with patch("kstrl.feature_cmd.run_loop", incomplete):
                code = run_feature(
                    params,
                    KstrlConfig(),
                    StubAgent(),
                    ui,
                    tmp_path,
                    interaction=ScriptedChannel(0),
                    run=command_run,
                )
        finally:
            command_run.close()
        assert code == 0
        state, _ = load_run_state(tmp_path)
        comp = state.components["demo"]
        assert state.finished
        assert comp.status == "skipped"
        assert comp.phase_history[-1]["passed"] is False
        assert comp.phase_history[-1]["detail"] == "ended before completion"
        assert state.artifacts == []

    @pytest.mark.parametrize(
        ("phase", "outcomes"),
        [
            ("implement", ((True, 0), (False, 0))),
            ("repair-1", ((True, 0), (False, 1), (False, 0))),
        ],
    )
    def test_incomplete_later_phase_is_skipped(
        self,
        tmp_path: Path,
        phase: str,
        outcomes: tuple[tuple[bool, int], ...],
    ) -> None:
        params = _params(tmp_path, repair_max_runs=1)
        params.implementation_auto_run = True
        ui = PlainUI(no_color=True, file=io.StringIO())
        command_run = open_command_run(
            ui,
            tmp_path,
            "feature",
            component="demo",
            enabled=True,
            heartbeat=False,
        )
        remaining = iter(outcomes)

        def scripted(*args: Any, **kwargs: Any) -> LoopResult:
            completed, exit_code = next(remaining)
            return LoopResult(
                completed=completed,
                iterations=1,
                exit_code=exit_code,
            )

        try:
            with (
                patch("kstrl.feature_cmd.run_loop", scripted),
                patch("kstrl.feature_cmd.get_agent", return_value=StubAgent()),
            ):
                code = run_feature(
                    params,
                    KstrlConfig(),
                    StubAgent(),
                    ui,
                    tmp_path,
                    run=command_run,
                )
        finally:
            command_run.close()
        assert code == 0
        state, _ = load_run_state(tmp_path)
        comp = state.components["demo"]
        assert state.finished
        assert comp.status == "skipped"
        assert comp.phase_history[-1]["phase"] == phase
        assert comp.phase_history[-1]["passed"] is False
        assert comp.phase_history[-1]["detail"] == "ended before completion"

    def test_exception_records_terminal_failure(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        ui = PlainUI(no_color=True, file=io.StringIO())
        command_run = open_command_run(
            ui,
            tmp_path,
            "feature",
            component="demo",
            enabled=True,
            heartbeat=False,
        )

        def explode(*args: Any, **kwargs: Any) -> LoopResult:
            raise RuntimeError("agent exploded")

        try:
            with (
                patch("kstrl.feature_cmd.run_loop", explode),
                pytest.raises(RuntimeError, match="agent exploded"),
            ):
                run_feature(
                    params,
                    KstrlConfig(),
                    StubAgent(),
                    ui,
                    tmp_path,
                    interaction=ScriptedChannel(0),
                    run=command_run,
                )
        finally:
            command_run.close()
        state, _ = load_run_state(tmp_path)
        comp = state.components["demo"]
        assert state.finished
        assert comp.status == "failed"
        assert comp.error == "RuntimeError: agent exploded"
        assert comp.phase_history[-1]["passed"] is False

    def test_transcript_tees_on_top_of_legacy_logs(
        self,
        tmp_path: Path,
    ) -> None:
        _run_recorded(tmp_path, codes=(0, 0))
        runs_root = tmp_path / ".kstrl" / "runs"
        run_dir = next(iter(runs_root.iterdir()))
        transcript = run_dir / "components" / "demo" / "engineer.log"
        assert transcript.read_text().count("line") == 2  # both phases
        legacy = list(
            (tmp_path / ".kstrl" / "logs" / "feature_demo").glob("*.log"),
        )
        assert len(legacy) == 2  # understand_*.log + run_*.log
        for log in legacy:
            assert "line" in log.read_text()

    def test_narration_identical_with_and_without_recording(
        self,
        tmp_path: Path,
    ) -> None:
        def run_once(root: Path, recorded: bool) -> str:
            params = _params(root, repair_max_runs=0)
            stream = io.StringIO()
            ui = PlainUI(no_color=True, file=stream)
            command_run = open_command_run(
                ui, root, "feature", component="demo", enabled=recorded, heartbeat=False
            )
            try:
                with patch(
                    "kstrl.feature_cmd.run_loop",
                    _loop_results_with_agent(0, 0),
                ):
                    run_feature(
                        params,
                        KstrlConfig(),
                        StubAgent(),
                        ui,
                        root,
                        interaction=ScriptedChannel(0),
                        run=command_run,
                    )
            finally:
                command_run.close()
            return stream.getvalue()

        recorded = run_once(tmp_path / "a", True)
        plain = run_once(tmp_path / "b", False)
        assert recorded.replace(str(tmp_path / "a"), "ROOT") == (
            plain.replace(str(tmp_path / "b"), "ROOT")
        )
