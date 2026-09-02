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
from tests.helpers.settle import drained, mounted, settled
from tests.spine_utils import git as spine_git
from tests.test_decompose import VALID_DECOMPOSE_OUTPUT, MockDecomposeAgent


class FakeSession:
    """Injected through the app's start_session seam: streams a fake
    factory run and optionally blocks on one CONFIRM prompt."""

    def __init__(
        self,
        root: Path,
        *,
        ask: bool = False,
        exit_code: int = 0,
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
                while not self.channel.can_prompt() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.channel.request(
                    PromptRequest(
                        kind=PromptKind.CONFIRM,
                        header="Proceed with the fake run?",
                        options=("Proceed", "Stop"),
                        default=0,
                    )
                )
            bus.emit(ev.RunCompleted(completed=1))
            bus.close()
            return exit_code

        self.handle = start_command_thread(_target, stop=stop)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.channel.detach()


def _git_repo_on(root: Path, branch: str) -> Path:
    """One-commit repo at ``root`` whose only branch is ``branch``."""
    spine_git("init", "-q", "-b", branch, cwd=root)
    spine_git("config", "user.email", "launch@test", cwd=root)
    spine_git("config", "user.name", "Launch Test", cwd=root)
    (root / "a.txt").write_text("a\n")
    spine_git("add", "-A", cwd=root)
    spine_git("commit", "-q", "-m", "init", cwd=root)
    return root


def _home_app(tmp_path: Path) -> KstrlTuiApp:
    return KstrlTuiApp(root_dir=tmp_path, mode=Mode.HOME, poll_interval=0.05)


def _notified(app: KstrlTuiApp, fragment: str) -> bool:
    """Has the app raised a notification whose text contains ``fragment``?

    A guard that REFUSES an action leaves no state behind - the nav
    guard and the retry confirmation both just warn - so the warning is
    the only thing a test can wait on that is not the absence it wants
    to assert. Textual keeps a notification on the app until its
    timeout (5s by default), which is far longer than a settle poll, so
    this observes the guard rather than guessing how long it takes.
    """
    return any(fragment in note.message for note in app._notifications)


class TestLaunchSeam:
    async def test_launch_puts_the_board_up_and_finishes_in_place(
        self,
        tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        sessions: list[FakeSession] = []

        def fake_start(spec: object) -> FakeSession:
            session = FakeSession(tmp_path, ask=True)
            sessions.append(session)
            return session

        app.start_session = fake_start
        async with app.run_test(size=(120, 40)) as pilot:
            # No wait before the launch: the app's own on_mount (which
            # pushes home) is dispatched before run_test hands the
            # pilot over, and push_screen puts the board on the stack
            # synchronously, so the union assertion below reads state
            # that launch has already made true.
            app.launch(FactoryLaunch())
            assert isinstance(app.screen, (OverviewScreen, OptionsModal))
            await settled(
                pilot,
                lambda: isinstance(app.screen, OptionsModal),
                what="the fake session's prompt to open its modal",
            )
            await pilot.press("1")
            await settled(
                pilot,
                lambda: sessions[0].handle.done(),
                what="the answered session thread to finish",
            )
            # _check_session runs on a 0.5s interval and sets this flag
            # before it decides anything. Weaker than both assertions
            # below: a check that exited the app still fails on
            # `return_value`, and one that dropped the board still
            # fails on `app.screen`.
            await settled(
                pilot,
                lambda: app._session_notified,
                what="the session check to notice the finished run",
            )
            # The board stays up (owns_app_exit=False); app still runs.
            assert app.return_value is None
            assert isinstance(app.screen, OverviewScreen)
            # Escape pops home and tears the session down.
            await pilot.press("escape")
            await settled(
                pilot,
                lambda: isinstance(app.screen, HomeScreen),
                what="escape to pop the board back to home",
            )
            # The teardown is home's on_screen_resume, a message on the
            # home screen's own queue: draining that queue proves the
            # handler ran without asserting what it did, so the two
            # assertions below still report their own failures.
            await drained(
                pilot,
                app.screen,
                what="home's screen-resume teardown to run",
            )
            assert isinstance(app.screen, HomeScreen)
            assert app.run_context is None
            assert sessions[0].closed

    async def test_in_flight_guards_block_escape_and_second_launch(
        self,
        tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        session = FakeSession(tmp_path, ask=True)  # blocks on the prompt
        app.start_session = lambda spec: session
        async with app.run_test(size=(120, 40)) as pilot:
            app.launch(FactoryLaunch())
            await settled(
                pilot,
                lambda: isinstance(app.screen, OptionsModal),
                what="the blocked session's prompt to open its modal",
            )
            # Dismiss the prompt modal but leave it pending: the run
            # stays in flight. The modal closing is the outcome of the
            # escape and says nothing about the run, which is what the
            # assertion is for.
            await pilot.press("escape")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, OptionsModal),
                what="escape to close the prompt modal",
            )
            assert app.session_in_flight()
            # The guard refuses, so nothing changes and there is no
            # state to wait for: its warning is the only trace. The OR
            # covers the defect too - if the escape pops home, the wait
            # ends there and the assertion below fails with its own
            # message instead of timing out here.
            await pilot.press("escape")  # nav guard refuses
            await settled(
                pilot,
                lambda: _notified(app, "press q to stop") or isinstance(app.screen, HomeScreen),
                what="the nav guard to refuse the escape out loud",
            )
            assert not isinstance(app.screen, HomeScreen)
            before = app.run_context
            app.launch(FactoryLaunch())  # single-session guard
            assert app.run_context is before
            # Answer via c -> reopen so the thread can finish.
            await pilot.press("c")
            await settled(
                pilot,
                lambda: isinstance(app.screen, OptionsModal),
                what="c to reopen the pending prompt",
            )
            await pilot.press("1")
            await settled(
                pilot,
                lambda: session.handle.done(),
                what="the answered session thread to finish",
            )
        session.handle.join(timeout=2)

    async def test_launch_error_notifies_and_stays_home(
        self,
        tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)

        def failing(spec: object) -> object:
            raise LaunchError("no manifest - decompose a spec first")

        app.start_session = failing
        async with app.run_test(size=(120, 40)) as pilot:
            app.launch(FactoryLaunch())
            # The refused launch leaves no state behind, so the wait is
            # on the notification OR on the run context it must not
            # open: on that defect the wait ends and the assertions
            # below fail with their own messages.
            await settled(
                pilot,
                lambda: _notified(app, "no manifest") or app.run_context is not None,
                what="the launch error to be notified",
            )
            # The test's name promises this half and nothing checked it
            # before; the wait above only proves a notification landed.
            assert _notified(app, "no manifest - decompose a spec first")
            assert isinstance(app.screen, HomeScreen)
            assert app.run_context is None


class TestStartRunSession:
    def test_factory_without_manifest_raises_before_any_thread(
        self,
        tmp_path: Path,
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
        self,
        tmp_path: Path,
    ) -> None:
        manifest_dir = tmp_path / "scripts" / "kstrl"
        manifest_dir.mkdir(parents=True)
        Manifest(
            version="1",
            spec_file="s",
            project_name="demo",
            base_branch="main",
            single_pr=False,
            components=[],
        ).save(manifest_dir / "manifest.json")
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ntype = "gemini"\n',
            encoding="utf-8",
        )

        with pytest.raises(LaunchError, match="Unknown agent type"):
            start_run_session(FactoryLaunch(), tmp_path)

        assert not (tmp_path / ".kstrl").exists() or not list(
            (tmp_path / ".kstrl" / "runs").glob("*"),
        )

    def test_decompose_canonicalizes_agent_alias(
        self,
        tmp_path: Path,
    ) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.", encoding="utf-8")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ntype = "claude"\n',
            encoding="utf-8",
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
        self,
        tmp_path: Path,
    ) -> None:
        class ExplodingAgent:
            def run(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                raise RuntimeError("architect exploded")

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.", encoding="utf-8")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ncommand = "fake-agent"\n',
            encoding="utf-8",
        )
        with patch("kstrl.agents.get_agent", return_value=ExplodingAgent()):
            session = start_run_session(
                DecomposeLaunch(spec_path=spec_file, project_name="demo"),
                tmp_path,
            )
        session.handle.join(timeout=15)
        session.close()

        assert session.handle.exit_code == 1
        assert "architect exploded" in (session.run_dir / "orchestrator.log").read_text(
            encoding="utf-8"
        )

    def test_decompose_session_runs_end_to_end(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ncommand = "fake-agent"\n',
            encoding="utf-8",
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

    def test_decompose_session_resolves_an_unset_base_branch(self, tmp_path: Path) -> None:
        """#259: DecomposeLaunch carries no base branch of its own, so
        the session asks the repo instead of writing a manifest against
        a `main` that a `git init` repo on `master` does not have."""
        _git_repo_on(tmp_path, "trunk")
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ncommand = "fake-agent"\n',
            encoding="utf-8",
        )
        assert DecomposeLaunch().base_branch == ""
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
        written = Manifest.load(tmp_path / "scripts" / "kstrl" / "manifest.json")
        assert written.base_branch == "trunk"


class TestLaunchForms:
    async def test_factory_form_validates_then_launches(
        self,
        tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        specs: list[Any] = []

        def capture(spec: Any) -> FakeSession:
            specs.append(spec)
            return FakeSession(tmp_path)

        app.start_session = capture
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(FactoryLaunchForm())
            from textual.widgets import Button, Input

            # Each field sits a few levels down (a FormField inside the
            # dialog panel) and composes in its own task, so no pause
            # count orders them: wait for every widget this test drives.
            for widget_id in ("#factory-parallel", "#factory-start", "#launch-errors"):
                await mounted(pilot, lambda: app.screen, widget_id)
            form = app.screen
            errors_widget = form.query_one("#launch-errors")
            form.query_one("#factory-parallel", Input).value = "zebra"
            form.query_one("#factory-start", Button).press()
            # Weaker than the assertion on purpose: ANY error, not the
            # one about integers. The OR covers the opposite defect - a
            # form that accepts `zebra` launches instead of reporting -
            # so that fails on the assertions below rather than here.
            await settled(
                pilot,
                lambda: str(errors_widget.content) or specs,
                what="the form to report a validation error",
            )
            errors = str(form.query_one("#launch-errors").content)
            assert "must be an integer" in errors
            assert specs == []
            # Fix the field and provide a manifest.
            manifest_dir = tmp_path / "scripts" / "kstrl"
            manifest_dir.mkdir(parents=True)
            Manifest(
                version="1",
                spec_file="s",
                project_name="demo",
                base_branch="main",
                single_pr=False,
                components=[],
            ).save(manifest_dir / "manifest.json")
            form.query_one("#factory-parallel", Input).value = "2"
            form.query_one("#factory-start", Button).press()
            await settled(
                pilot,
                lambda: specs,
                what="the valid form to hand a spec to the launch seam",
            )
            assert len(specs) == 1
            assert specs[0].max_parallel == 2

    async def test_decompose_form_requires_spec_and_project(
        self,
        tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(DecomposeLaunchForm())
            from textual.widgets import Button, Input

            for widget_id in (
                "#decompose-spec",
                "#decompose-project",
                "#decompose-start",
                "#launch-errors",
            ):
                await mounted(pilot, lambda: app.screen, widget_id)
            form = app.screen
            errors_widget = form.query_one("#launch-errors")
            form.query_one("#decompose-start", Button).press()
            # ANY error, not these two, and the OR covers the opposite
            # defect: an empty form that launches fails on the
            # assertions below rather than timing out here.
            await settled(
                pilot,
                lambda: str(errors_widget.content) or specs,
                what="the empty form to report a validation error",
            )
            errors = str(form.query_one("#launch-errors").content)
            assert "spec path is required" in errors
            assert "project name is required" in errors
            (tmp_path / "spec.md").write_text("# spec")
            form.query_one("#decompose-spec", Input).value = "spec.md"
            form.query_one("#decompose-project", Input).value = "demo"
            form.query_one("#decompose-start", Button).press()
            await settled(
                pilot,
                lambda: specs,
                what="the filled form to hand a spec to the launch seam",
            )
            assert len(specs) == 1
            assert specs[0].project_name == "demo"

    async def test_decompose_form_offers_the_detected_base_branch(
        self,
        tmp_path: Path,
    ) -> None:
        """#259 through the TUI: the field used to pre-fill the literal
        `main`, so a plain `git init` repo on `master` launched a run
        whose every worktree cut names a branch that does not exist."""
        _git_repo_on(tmp_path, "master")
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(DecomposeLaunchForm())
            from textual.widgets import Button, Input

            # Waiting for the branch field to mount is weaker than the
            # assertion about what it was pre-filled with.
            for widget_id in (
                "#decompose-spec",
                "#decompose-project",
                "#decompose-branch",
                "#decompose-start",
            ):
                await mounted(pilot, lambda: app.screen, widget_id)
            form = app.screen
            assert form.query_one("#decompose-branch", Input).value == "master"

            (tmp_path / "spec.md").write_text("# spec")
            form.query_one("#decompose-spec", Input).value = "spec.md"
            form.query_one("#decompose-project", Input).value = "demo"
            # Clearing the field must not resurrect the literal either.
            form.query_one("#decompose-branch", Input).value = ""
            form.query_one("#decompose-start", Button).press()
            await settled(
                pilot,
                lambda: specs,
                what="the filled form to hand a spec to the launch seam",
            )
            assert specs[0].base_branch == "master"


class TestRetryScreen:
    def _failed_manifest(self, tmp_path: Path) -> Path:
        manifest_dir = tmp_path / "scripts" / "kstrl"
        manifest_dir.mkdir(parents=True)
        manifest = Manifest(
            version="1",
            spec_file="s",
            project_name="demo",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    id="comp-a",
                    title="A",
                    description="",
                    dependencies=[],
                    prd_path="p.json",
                    branch_name="kstrl/comp-a",
                    status=ComponentStatus.FAILED.value,
                    failed_phase="review",
                    failed_check="criteria",
                    error="review found blocking issues",
                ),
                Component(
                    id="comp-b",
                    title="B",
                    description="",
                    dependencies=[],
                    prd_path="p.json",
                    branch_name="kstrl/comp-b",
                    status=ComponentStatus.COMPLETED.value,
                ),
            ],
        )
        manifest.save(manifest_dir / "manifest.json")
        return manifest_dir / "manifest.json"

    async def test_lists_failed_and_launches_after_confirm(
        self,
        tmp_path: Path,
    ) -> None:
        manifest_file = self._failed_manifest(tmp_path)
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            app.push_screen(RetryScreen())
            table = await mounted(pilot, lambda: app.screen, "#retry-table")
            detail_widget = await mounted(pilot, lambda: app.screen, "#retry-detail")
            # compose and on_mount both run before a screen takes
            # anything off its own queue, so a callback on that queue
            # is proof the manifest has been read into the table. That
            # is weaker than the two assertions, which are about WHAT
            # it read.
            await drained(
                pilot,
                app.screen,
                what="the retry screen's on_mount to run",
            )
            assert table.row_count == 1  # type: ignore[attr-defined]
            detail = str(detail_widget.content)
            assert "review found blocking issues" in detail
            await pilot.press("r")
            # Weaker than the assertion: r handed over to some other
            # screen, not specifically to the confirm modal.
            await settled(
                pilot,
                lambda: not isinstance(app.screen, RetryScreen),
                what="r to open the retry confirmation",
            )
            assert isinstance(app.screen, OptionsModal)
            assert "comp-a" in app.screen.request.header
            await pilot.press("1")  # Start retry
            # Either outcome of the confirmation, so a wrongly refused
            # retry fails on the assertion below and not here.
            await settled(
                pilot,
                lambda: specs or _notified(app, "retry"),
                what="the confirmation to launch the retry or refuse it",
            )
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
            app.push_screen(RetryScreen())
            detail_widget = await mounted(pilot, lambda: app.screen, "#retry-detail")
            await drained(
                pilot,
                app.screen,
                what="the retry screen's on_mount to run",
            )
            detail = str(detail_widget.content)
            assert "nothing to retry" in detail

    async def test_confirmation_does_not_overwrite_changed_manifest(
        self,
        tmp_path: Path,
    ) -> None:
        manifest_file = self._failed_manifest(tmp_path)
        app = _home_app(tmp_path)
        specs: list[Any] = []
        app.start_session = lambda spec: specs.append(spec) or FakeSession(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            app.push_screen(RetryScreen())
            await mounted(pilot, lambda: app.screen, "#retry-table")
            await drained(
                pilot,
                app.screen,
                what="the retry screen's on_mount to run",
            )
            await pilot.press("r")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, RetryScreen),
                what="r to open the retry confirmation",
            )
            assert isinstance(app.screen, OptionsModal)

            changed = Manifest.load(manifest_file)
            comp = changed.get_component("comp-a")
            assert comp is not None
            comp.status = ComponentStatus.COMPLETED.value
            changed.save(manifest_file)

            await pilot.press("1")
            # A refusal writes nothing, so its warning is the only
            # trace; the OR covers the defect, where the confirmation
            # goes through and `specs` grows, so the assertions below
            # fail with their own messages instead of timing out here.
            await settled(
                pilot,
                lambda: _notified(app, "retry plan changed") or specs,
                what="the confirmation to act on the changed manifest",
            )

        persisted = Manifest.load(manifest_file).get_component("comp-a")
        assert persisted is not None
        assert persisted.status == ComponentStatus.COMPLETED.value
        assert specs == []


class TestDecomposeSessionOnBoard:
    async def test_launched_decompose_opens_the_rich_screen(
        self,
        tmp_path: Path,
    ) -> None:
        """The real session + the real board, driven by a fake agent."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
        (tmp_path / "kstrl.toml").write_text(
            '[agent]\ncommand = "fake-agent"\n',
            encoding="utf-8",
        )
        app = _home_app(tmp_path)
        with patch(
            "kstrl.agents.get_agent",
            return_value=MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT),
        ):
            async with app.run_test(size=(130, 45)) as pilot:
                # launch pushes the kind's whole stack synchronously,
                # so the screen assertion needs no wait of its own.
                app.launch(
                    DecomposeLaunch(
                        spec_path=spec_file,
                        project_name="demo",
                    )
                )
                assert isinstance(app.screen, DecomposeScreen)
                run = app.run_context
                assert run is not None and run.handle is not None
                await settled(
                    pilot,
                    lambda: run.handle.done(),
                    what="the decompose run to finish",
                    timeout=15.0,
                )
                assert run.handle.exit_code == 0
                summary_widget = await mounted(
                    pilot,
                    lambda: app.screen,
                    "#decompose-summary",
                )
                # Board reflects the finished run. The summary is
                # written by the poll that folds the completion, so the
                # wait is on the fold - weaker than the assertion,
                # which is about what the summary then says.
                await settled(
                    pilot,
                    lambda: run.store.state.finished,
                    what="the board to fold the finished run",
                )
                summary = str(summary_widget.content)
                assert "2 component(s)" in summary
                await pilot.press("escape")
                await settled(
                    pilot,
                    lambda: not isinstance(app.screen, DecomposeScreen),
                    what="escape to leave the decompose screen",
                )
                await pilot.press("escape")
                await settled(
                    pilot,
                    lambda: not isinstance(app.screen, OverviewScreen),
                    what="escape to pop the board back to home",
                )
                assert isinstance(app.screen, HomeScreen)
