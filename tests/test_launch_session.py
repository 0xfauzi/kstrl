"""TUI surface D6: session seam, launch forms, retry surface."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.interaction import (
    PromptKind,
    PromptRequest,
    QueueInteractionChannel,
)
from kstrl.launch import DecomposeLaunch, FactoryLaunch, LoopLaunch
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.shutdown import StopController
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.bridge import start_command_thread
from kstrl.tui.screens.decompose import DecomposeScreen
from kstrl.tui.screens.home import HomeScreen
from kstrl.tui.screens.launch import DecomposeLaunchForm, FactoryLaunchForm
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.screens.overview import OverviewScreen
from kstrl.tui.screens.retry import RetryScreen
from kstrl.tui.session import LaunchError, start_run_session
from tests.test_decompose import VALID_DECOMPOSE_OUTPUT, MockDecomposeAgent


class FakeSession:
    """Injected through the app's start_session seam: streams a fake
    factory run and optionally blocks on one CONFIRM prompt."""

    def __init__(
        self, root: Path, *, ask: bool = False, exit_code: int = 0,
        run_id: str = "factory-20260720-170000.000000-fake",
    ) -> None:
        from kstrl import events as ev

        self.kind = "factory"
        self.channel = QueueInteractionChannel()
        paths = ev.RunPaths.for_run(root, run_id)
        self.run_dir = paths.root
        stop = StopController()

        def _target() -> int:
            bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=run_id)
            bus.emit(ev.RunStarted(project="fake", components=1))
            bus.emit(ev.ComponentStarted(component="comp-a"))
            if ask:
                deadline = time.monotonic() + 5
                while (
                    not self.channel.can_prompt()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.channel.request(PromptRequest(
                    kind=PromptKind.CONFIRM,
                    header="Proceed with the fake run?",
                    options=("Proceed", "Stop"),
                    default=0,
                ))
            bus.emit(ev.RunCompleted(completed=1))
            bus.close()
            return exit_code

        self.handle = start_command_thread(_target, stop=stop)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.channel.detach()


def _home_app(tmp_path: Path) -> KstrlTuiApp:
    return KstrlTuiApp(root_dir=tmp_path, mode=Mode.HOME, poll_interval=0.05)


class TestLaunchSeam:
    async def test_launch_puts_the_board_up_and_finishes_in_place(
        self, tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        sessions: list[FakeSession] = []

        def fake_start(spec: object) -> FakeSession:
            session = FakeSession(tmp_path, ask=True)
            sessions.append(session)
            return session

        app.start_session = fake_start
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.launch(FactoryLaunch())
            await pilot.pause(0.2)
            assert isinstance(app.screen, (OverviewScreen, OptionsModal))
            deadline = time.monotonic() + 5
            while not isinstance(app.screen, OptionsModal):
                await pilot.pause(0.05)
                assert time.monotonic() < deadline, "prompt never arrived"
            await pilot.press("1")
            deadline = time.monotonic() + 5
            while not sessions[0].handle.done():
                await pilot.pause(0.05)
                assert time.monotonic() < deadline, "session stuck"
            await pilot.pause(0.7)  # _check_session interval
            # The board stays up (owns_app_exit=False); app still runs.
            assert app.return_value is None
            assert isinstance(app.screen, OverviewScreen)
            # Escape pops home and tears the session down.
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert isinstance(app.screen, HomeScreen)
            assert app.run_context is None
            assert sessions[0].closed

    async def test_in_flight_guards_block_escape_and_second_launch(
        self, tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        session = FakeSession(tmp_path, ask=True)  # blocks on the prompt
        app.start_session = lambda spec: session
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.launch(FactoryLaunch())
            await pilot.pause(0.3)
            # Dismiss the prompt modal but leave it pending: the run
            # stays in flight.
            deadline = time.monotonic() + 5
            while not isinstance(app.screen, OptionsModal):
                await pilot.pause(0.05)
                assert time.monotonic() < deadline
            await pilot.press("escape")
            await pilot.pause()
            assert app.session_in_flight()
            await pilot.press("escape")  # nav guard refuses
            await pilot.pause()
            assert not isinstance(app.screen, HomeScreen)
            before = app.run_context
            app.launch(FactoryLaunch())  # single-session guard
            assert app.run_context is before
            # Answer via c -> reopen so the thread can finish.
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("1")
            deadline = time.monotonic() + 5
            while not session.handle.done():
                await pilot.pause(0.05)
                assert time.monotonic() < deadline
        session.handle.join(timeout=2)

    async def test_launch_error_notifies_and_stays_home(
        self, tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)

        def failing(spec: object) -> object:
            raise LaunchError("no manifest - decompose a spec first")

        app.start_session = failing
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.launch(FactoryLaunch())
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)
            assert app.run_context is None


class TestStartRunSession:
    def test_factory_without_manifest_raises_before_any_thread(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(LaunchError, match="no manifest"):
            start_run_session(FactoryLaunch(), tmp_path)
        assert not (tmp_path / ".kstrl").exists() or not list(
            (tmp_path / ".kstrl" / "runs").glob("*"),
        )

    def test_unsupported_spec_raises(self, tmp_path: Path) -> None:
        with pytest.raises(LaunchError, match="does not support"):
            start_run_session(LoopLaunch(), tmp_path)

    def test_invalid_agent_config_fails_before_run_state(
        self, tmp_path: Path,
    ) -> None:
        manifest_dir = tmp_path / "scripts" / "kstrl"
        manifest_dir.mkdir(parents=True)
        Manifest(
            version="1", spec_file="s", project_name="demo",
            base_branch="main", single_pr=False, components=[],
        ).save(manifest_dir / "manifest.json")
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ntype = "gemini"\n', encoding="utf-8",
        )

        with pytest.raises(LaunchError, match="Unknown agent type"):
            start_run_session(FactoryLaunch(), tmp_path)

        assert not (tmp_path / ".kstrl").exists() or not list(
            (tmp_path / ".kstrl" / "runs").glob("*"),
        )

    def test_decompose_canonicalizes_agent_alias(
        self, tmp_path: Path,
    ) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.", encoding="utf-8")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ntype = "claude"\n', encoding="utf-8",
        )
        with (
            patch("kstrl.cli.ClaudeCodeAgent.is_available", return_value=True),
            patch(
                "kstrl.agents.get_agent",
                return_value=MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT),
            ) as get_agent,
        ):
            session = start_run_session(
                DecomposeLaunch(spec_path=spec_file, project_name="demo"),
                tmp_path,
            )
        try:
            session.handle.join(timeout=15)
            assert session.handle.exit_code == 0
        finally:
            session.close()
        assert get_agent.call_args.args[3] == "claude-code"

    def test_worker_exception_is_written_to_run_log(
        self, tmp_path: Path,
    ) -> None:
        class ExplodingAgent:
            def run(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                raise RuntimeError("architect exploded")

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.", encoding="utf-8")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ncommand = "fake-agent"\n', encoding="utf-8",
        )
        with patch("kstrl.agents.get_agent", return_value=ExplodingAgent()):
            session = start_run_session(
                DecomposeLaunch(spec_path=spec_file, project_name="demo"),
                tmp_path,
            )
        session.handle.join(timeout=15)
        session.close()

        assert session.handle.exit_code == 1
        assert "architect exploded" in (
            session.run_dir / "orchestrator.log"
        ).read_text(encoding="utf-8")

    def test_decompose_session_runs_end_to_end(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ncommand = "fake-agent"\n', encoding="utf-8",
        )
        with patch(
            "kstrl.agents.get_agent",
            return_value=MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT),
        ):
            session = start_run_session(
                DecomposeLaunch(spec_path=spec_file, project_name="demo"),
                tmp_path,
            )
        try:
            session.handle.join(timeout=15)
            assert session.handle.exit_code == 0
        finally:
            session.close()
        assert session.kind == "decompose"
        assert (session.run_dir / "events.jsonl").exists()
        assert (session.run_dir / "orchestrator.log").exists()
        manifest = json.loads(
            (tmp_path / "scripts" / "kstrl" / "manifest.json").read_text(),
        )
        assert len(manifest["components"]) == 2


class TestLaunchForms:
    async def test_factory_form_validates_then_launches(
        self, tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        specs: list[Any] = []

        def capture(spec: Any) -> FakeSession:
            specs.append(spec)
            return FakeSession(tmp_path)

        app.start_session = capture
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.push_screen(FactoryLaunchForm())
            await pilot.pause(0.2)
            from textual.widgets import Button, Input

            form = app.screen
            form.query_one("#factory-parallel", Input).value = "zebra"
            form.query_one("#factory-start", Button).press()
            await pilot.pause()
            errors = str(form.query_one("#launch-errors").content)
            assert "must be an integer" in errors
            assert specs == []
            # Fix the field and provide a manifest.
            manifest_dir = tmp_path / "scripts" / "kstrl"
            manifest_dir.mkdir(parents=True)
            Manifest(
                version="1", spec_file="s", project_name="demo",
                base_branch="main", single_pr=False,
                components=[],
            ).save(manifest_dir / "manifest.json")
            form.query_one("#factory-parallel", Input).value = "2"
            form.query_one("#factory-start", Button).press()
            await pilot.pause(0.3)
            assert len(specs) == 1
            assert specs[0].max_parallel == 2

    async def test_decompose_form_requires_spec_and_project(
        self, tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: (
            specs.append(spec) or FakeSession(tmp_path)
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.push_screen(DecomposeLaunchForm())
            await pilot.pause(0.2)
            from textual.widgets import Button, Input

            form = app.screen
            form.query_one("#decompose-start", Button).press()
            await pilot.pause()
            errors = str(form.query_one("#launch-errors").content)
            assert "spec path is required" in errors
            assert "project name is required" in errors
            (tmp_path / "spec.md").write_text("# spec")
            form.query_one("#decompose-spec", Input).value = "spec.md"
            form.query_one("#decompose-project", Input).value = "demo"
            form.query_one("#decompose-start", Button).press()
            await pilot.pause(0.3)
            assert len(specs) == 1
            assert specs[0].project_name == "demo"


class TestRetryScreen:
    def _failed_manifest(self, tmp_path: Path) -> Path:
        manifest_dir = tmp_path / "scripts" / "kstrl"
        manifest_dir.mkdir(parents=True)
        manifest = Manifest(
            version="1", spec_file="s", project_name="demo",
            base_branch="main", single_pr=False,
            components=[
                Component(
                    id="comp-a", title="A", description="",
                    dependencies=[], prd_path="p.json",
                    branch_name="kstrl/comp-a",
                    status=ComponentStatus.FAILED.value,
                    failed_phase="review", failed_check="criteria",
                    error="review found blocking issues",
                ),
                Component(
                    id="comp-b", title="B", description="",
                    dependencies=[], prd_path="p.json",
                    branch_name="kstrl/comp-b",
                    status=ComponentStatus.COMPLETED.value,
                ),
            ],
        )
        manifest.save(manifest_dir / "manifest.json")
        return manifest_dir / "manifest.json"

    async def test_lists_failed_and_launches_after_confirm(
        self, tmp_path: Path,
    ) -> None:
        manifest_file = self._failed_manifest(tmp_path)
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: (
            specs.append(spec) or FakeSession(tmp_path)
        )
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause(0.2)
            app.push_screen(RetryScreen())
            await pilot.pause(0.2)
            screen = app.screen
            table = screen.query_one("#retry-table")
            assert table.row_count == 1  # type: ignore[attr-defined]
            detail = str(screen.query_one("#retry-detail").content)
            assert "review found blocking issues" in detail
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, OptionsModal)
            assert "comp-a" in app.screen.request.header
            await pilot.press("1")  # Start retry
            await pilot.pause(0.3)
            assert len(specs) == 1
            assert isinstance(specs[0], FactoryLaunch)
            assert specs[0].manifest_path == manifest_file
            # prepare_retry really ran: the component is pending again.
            reloaded = Manifest.load(manifest_file)
            comp = reloaded.get_component("comp-a")
            assert comp is not None
            assert comp.status == ComponentStatus.PENDING.value

    async def test_empty_state(self, tmp_path: Path) -> None:
        app = _home_app(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause(0.2)
            app.push_screen(RetryScreen())
            await pilot.pause(0.2)
            detail = str(app.screen.query_one("#retry-detail").content)
            assert "nothing to retry" in detail

    async def test_confirmation_does_not_overwrite_changed_manifest(
        self, tmp_path: Path,
    ) -> None:
        manifest_file = self._failed_manifest(tmp_path)
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: (
            specs.append(spec) or FakeSession(tmp_path)
        )
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause(0.2)
            app.push_screen(RetryScreen())
            await pilot.pause(0.2)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, OptionsModal)

            changed = Manifest.load(manifest_file)
            comp = changed.get_component("comp-a")
            assert comp is not None
            comp.status = ComponentStatus.COMPLETED.value
            changed.save(manifest_file)

            await pilot.press("1")
            await pilot.pause(0.2)

        persisted = Manifest.load(manifest_file).get_component("comp-a")
        assert persisted is not None
        assert persisted.status == ComponentStatus.COMPLETED.value
        assert specs == []


class TestDecomposeSessionOnBoard:
    async def test_launched_decompose_opens_the_rich_screen(
        self, tmp_path: Path,
    ) -> None:
        """The real session + the real board, driven by a fake agent."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ncommand = "fake-agent"\n', encoding="utf-8",
        )
        app = _home_app(tmp_path)
        with patch(
            "kstrl.agents.get_agent",
            return_value=MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT),
        ):
            async with app.run_test(size=(130, 45)) as pilot:
                await pilot.pause(0.2)
                app.launch(DecomposeLaunch(
                    spec_path=spec_file, project_name="demo",
                ))
                await pilot.pause(0.2)
                assert isinstance(app.screen, DecomposeScreen)
                run = app.run_context
                assert run is not None and run.handle is not None
                deadline = time.monotonic() + 15
                while not run.handle.done():
                    await pilot.pause(0.1)
                    assert time.monotonic() < deadline, "decompose stuck"
                assert run.handle.exit_code == 0
                await pilot.pause(0.7)
                # Board reflects the finished run.
                summary = str(
                    app.screen.query_one("#decompose-summary").content,
                )
                assert "2 component(s)" in summary
                await pilot.press("escape")
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause(0.2)
                assert isinstance(app.screen, HomeScreen)
