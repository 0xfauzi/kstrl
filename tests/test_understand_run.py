"""TUI surface C1: `ks understand` as an event-stream run."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from kstrl import events as ev
from kstrl.cli import cli
from kstrl.loop import LoopResult
from kstrl.reducer import load_run_state
from kstrl.runid import run_kind
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.overview import OverviewScreen
from tests.helpers.fake_run import write_fake_understand_run


class StubAgent:
    name = "stub"
    final_message: str | None = None
    usage_records: list[Any] = []

    def run(
        self, prompt: str, cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        yield "understanding the codebase"
        yield "editing codebase_map.md"


def _fake_run_loop(
    record: dict[str, Any], exit_code: int = 0, *,
    completed: bool | None = None,
) -> Any:
    def fake(
        config: Any, ui: Any, agent: Any, cwd: Any = None,
        context_prefix: Any = None, timeouts: Any = None,
        breaker_config: Any = None, *, bus: Any = None,
        interaction: Any = None, stop_check: Any = None,
    ) -> LoopResult:
        record.update(bus=bus, interaction=interaction, agent=agent)
        for _ in agent.run("prompt", cwd):
            pass
        if bus is not None:
            bus.emit(ev.IterationStarted(iteration=1, max_iterations=3))
            bus.emit(ev.IterationCompleted(
                iteration=1, duration_seconds=5.0, completed=exit_code == 0,
            ))
        return LoopResult(
            completed=exit_code == 0 if completed is None else completed,
            iterations=1,
            exit_code=exit_code,
        )

    return fake


def _invoke_understand(tmp_path: Path, record: dict[str, Any]) -> Any:
    runner = CliRunner()
    with (
        patch("kstrl.cli.get_agent", return_value=StubAgent()),
        patch("kstrl.cli._check_agent_preflight"),
        patch("kstrl.cli.run_loop", _fake_run_loop(record)),
    ):
        return runner.invoke(
            cli, ["understand", "--root", str(tmp_path)],
            catch_exceptions=False,
        )


class TestUnderstandRun:
    def test_writes_a_foldable_run_dir(self, tmp_path: Path) -> None:
        record: dict[str, Any] = {}
        result = _invoke_understand(tmp_path, record)
        assert result.exit_code == 0

        runs_root = tmp_path / ".kstrl" / "runs"
        run_dirs = list(runs_root.iterdir())
        assert len(run_dirs) == 1
        assert run_kind(run_dirs[0].name) == "understand"

        state, source = load_run_state(tmp_path)
        assert source is not None
        assert state.kind == "understand"
        assert state.finished
        assert state.plan_order == ["understand"]
        comp = state.components["understand"]
        assert comp.status == "completed"
        assert comp.iteration == 1
        assert [p["phase"] for p in comp.phase_history] == ["understand"]
        assert comp.phase_history[0]["passed"] is True
        assert [a["label"] for a in state.artifacts] == ["codebase_map"]
        # run_loop received the run's bus (Iteration* came through it).
        assert record["bus"] is not None

    def test_transcript_lands_in_engineer_log(self, tmp_path: Path) -> None:
        record: dict[str, Any] = {}
        _invoke_understand(tmp_path, record)
        runs_root = tmp_path / ".kstrl" / "runs"
        run_dir = next(iter(runs_root.iterdir()))
        transcript = run_dir / "components" / "understand" / "engineer.log"
        content = transcript.read_text()
        assert "understanding the codebase" in content
        assert "editing codebase_map.md" in content

    def test_failed_loop_folds_as_failure(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with (
            patch("kstrl.cli.get_agent", return_value=StubAgent()),
            patch("kstrl.cli._check_agent_preflight"),
            patch("kstrl.cli.run_loop", _fake_run_loop({}, exit_code=1)),
        ):
            result = runner.invoke(
                cli, ["understand", "--root", str(tmp_path)],
                catch_exceptions=False,
            )
        assert result.exit_code == 1
        state, _ = load_run_state(tmp_path)
        assert state.finished
        comp = state.components["understand"]
        assert comp.status == "failed"
        assert state.artifacts == []

    def test_incomplete_zero_exit_folds_as_failure(self, tmp_path: Path) -> None:
        """Choosing interactive Quit is successful at the shell but did
        not complete the understand work."""
        runner = CliRunner()
        with (
            patch("kstrl.cli.get_agent", return_value=StubAgent()),
            patch("kstrl.cli._check_agent_preflight"),
            patch(
                "kstrl.cli.run_loop",
                _fake_run_loop({}, exit_code=0, completed=False),
            ),
        ):
            result = runner.invoke(
                cli, ["understand", "--root", str(tmp_path)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        state, _ = load_run_state(tmp_path)
        assert state.finished
        comp = state.components["understand"]
        assert comp.status == "failed"
        assert comp.error == "understand loop ended before completion"
        assert state.artifacts == []

    def test_exception_records_terminal_failure(self, tmp_path: Path) -> None:
        def explode(*args: Any, **kwargs: Any) -> LoopResult:
            raise RuntimeError("agent exploded")

        runner = CliRunner()
        with (
            patch("kstrl.cli.get_agent", return_value=StubAgent()),
            patch("kstrl.cli._check_agent_preflight"),
            patch("kstrl.cli.run_loop", explode),
        ):
            result = runner.invoke(
                cli, ["understand", "--root", str(tmp_path)],
            )
        assert result.exit_code == 1
        assert isinstance(result.exception, RuntimeError)
        state, _ = load_run_state(tmp_path)
        assert state.finished
        comp = state.components["understand"]
        assert comp.status == "failed"
        assert comp.error == "RuntimeError: agent exploded"
        assert comp.phase_history[-1]["passed"] is False

    def test_disabled_gating_leaves_no_run_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_FACTORY_PROGRESS_LOG_ENABLED", "0")
        record: dict[str, Any] = {}
        result = _invoke_understand(tmp_path, record)
        assert result.exit_code == 0
        assert not (tmp_path / ".kstrl" / "runs").exists()

    def test_terminal_output_identical_with_recording_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recording is silent: the plain terminal bytes must not
        change when the run dir is being written."""
        on = _invoke_understand(tmp_path, {})
        monkeypatch.setenv("KSTRL_FACTORY_PROGRESS_LOG_ENABLED", "0")
        off = _invoke_understand(tmp_path, {})
        assert on.output == off.output


class TestUnderstandDashboard:
    async def test_component_screen_renders_understand_run(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_understand_run(tmp_path)
        app = KstrlTuiApp(
            run_dir=run_dir, root_dir=tmp_path, mode=Mode.DASH,
            poll_interval=0.05,
            screen_factory=lambda: [
                OverviewScreen(observe_only=True),
                ComponentScreen("understand"),
            ],
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, ComponentScreen)
            comp = app.store.state.components["understand"]
            assert comp.status == "completed"
            assert comp.phase_history
            transcript = app.screen.query_one("#transcript")
            assert transcript is not None
