"""Stage 3 PR F (TUI rewrite): embedded mode - bridge, modal answering,
quit flow, notify capture."""

from __future__ import annotations

import io
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ralph_py import events as ev
from ralph_py.factory import FactoryResult
from ralph_py.interaction import (
    CheckpointContext,
    PromptKind,
    PromptRequest,
    QueueInteractionChannel,
)
from ralph_py.observability import NotifyConfig, NotifyHooks
from ralph_py.shutdown import StopController
from ralph_py.tui.app import Mode, RalphTuiApp
from ralph_py.tui.bridge import OrchestratorHandle, start_orchestrator
from ralph_py.tui.embed import (
    _install_exclusive_root_handler,
    _plain_fallback,
    _restore_root_handlers,
)
from ralph_py.tui.screens.checkpoint import CheckpointModal
from ralph_py.tui.screens.quit import QuitModal


def _write_minimal_run(root: Path, run_id: str) -> Path:
    paths = ev.RunPaths.for_run(root, run_id)
    bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=run_id)
    bus.emit(ev.RunStarted(project="embed-test", components=1))
    bus.emit(ev.ComponentStarted(component="comp-a"))
    bus.close()
    return paths.root


class TestBridge:
    def test_start_orchestrator_runs_and_reports(self, tmp_path: Path) -> None:
        stop = StopController()
        channel = QueueInteractionChannel()
        result = FactoryResult()
        result.exit_code = 0

        with patch(
            "ralph_py.tui.bridge.run_factory", return_value=result,
        ) as fake:
            handle = start_orchestrator(
                object(), object(), object(), object(),  # type: ignore[arg-type]
                tmp_path, None,
                run_id="run-x", stop=stop, channel=channel,
            )
            handle.join(timeout=5)
        assert handle.done()
        assert handle.exit_code == 0
        kwargs = fake.call_args.kwargs
        assert kwargs["run_id"] == "run-x"
        assert kwargs["interaction"] is channel
        assert kwargs["stop"] is stop
        assert kwargs["notify_capture_output"] is True

    def test_orchestrator_exception_lands_in_error_box(
        self, tmp_path: Path,
    ) -> None:
        with patch(
            "ralph_py.tui.bridge.run_factory",
            side_effect=RuntimeError("boom"),
        ):
            handle = start_orchestrator(
                object(), object(), object(), object(),  # type: ignore[arg-type]
                tmp_path, None,
                run_id="run-x", stop=StopController(),
                channel=QueueInteractionChannel(),
            )
            handle.join(timeout=5)
        assert handle.done()
        assert handle.error_box
        assert handle.exit_code == 1


def _fake_orchestrator(
    channel: QueueInteractionChannel,
    stop: StopController,
    decisions: list[int],
    *,
    wait_for_stop: bool = False,
) -> OrchestratorHandle:
    """A thread standing in for run_factory: optionally asks one
    checkpoint question, then finishes (or waits for stop)."""
    result_box: list[FactoryResult] = []
    error_box: list[BaseException] = []

    def _target() -> None:
        if decisions is not None and not wait_for_stop:
            # Real-world semantics: the app attaches on mount; a request
            # fired before that degrades NOT_PROMPTED. Wait for attach.
            deadline = time.monotonic() + 5
            while not channel.can_prompt() and time.monotonic() < deadline:
                time.sleep(0.01)
            response = channel.request(PromptRequest(
                kind=PromptKind.CHECKPOINT,
                header="Approve PR creation and merge for comp-a?",
                options=("Approve", "Reject", "Retry"),
                default=0,
                component_id="comp-a",
                checkpoint=CheckpointContext(
                    component_id="comp-a", diff_excerpt="+x\n",
                ),
            ))
            decisions.append(response.choice if response.answered else -1)
        if wait_for_stop:
            stop.wait(timeout=30)
        result = FactoryResult()
        result.exit_code = 130 if stop.is_set() else 0
        result_box.append(result)

    thread = threading.Thread(target=_target, daemon=False)
    handle = OrchestratorHandle(
        thread=thread, stop=stop,
        result_box=result_box, error_box=error_box,
    )
    thread.start()
    return handle


class TestEmbeddedApp:
    async def test_checkpoint_modal_answers_the_channel(
        self, tmp_path: Path,
    ) -> None:
        """The full bridge loop: orchestrator thread blocks on the
        channel -> call_from_thread opens the modal -> keypress
        resolves -> orchestrator unblocks with the choice -> app exits
        with the run's code."""
        run_dir = _write_minimal_run(tmp_path, "factory-20260720-embed1")
        channel = QueueInteractionChannel()
        stop = StopController()
        decisions: list[int] = []
        handle = _fake_orchestrator(channel, stop, decisions)
        app = RalphTuiApp(
            run_dir=run_dir, root_dir=tmp_path, mode=Mode.EMBEDDED,
            poll_interval=0.05, channel=channel, orchestrator=handle,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            deadline = time.monotonic() + 5
            while not isinstance(app.screen, CheckpointModal):
                await pilot.pause(0.05)
                assert time.monotonic() < deadline, "modal never opened"
            await pilot.press("a")
            deadline = time.monotonic() + 5
            while not handle.done():
                await pilot.pause(0.05)
                assert time.monotonic() < deadline, "orchestrator stuck"
            await pilot.pause(0.6)  # _check_orchestrator interval
        assert decisions == [0]
        assert app.return_value == 0

    async def test_quit_flow_requests_graceful_stop(
        self, tmp_path: Path,
    ) -> None:
        run_dir = _write_minimal_run(tmp_path, "factory-20260720-embed2")
        channel = QueueInteractionChannel()
        stop = StopController()
        handle = _fake_orchestrator(channel, stop, [], wait_for_stop=True)
        app = RalphTuiApp(
            run_dir=run_dir, root_dir=tmp_path, mode=Mode.EMBEDDED,
            poll_interval=0.05, channel=channel, orchestrator=handle,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, QuitModal)
            await pilot.press("y")
            deadline = time.monotonic() + 5
            while not handle.done():
                await pilot.pause(0.05)
                assert time.monotonic() < deadline
            await pilot.pause(0.6)
        assert stop.is_set()
        assert "TUI" in stop.reason
        assert app.return_value == 130
        handle.join(timeout=2)

    async def test_quit_declined_keeps_running(self, tmp_path: Path) -> None:
        run_dir = _write_minimal_run(tmp_path, "factory-20260720-embed3")
        channel = QueueInteractionChannel()
        stop = StopController()
        handle = _fake_orchestrator(channel, stop, [], wait_for_stop=True)
        app = RalphTuiApp(
            run_dir=run_dir, root_dir=tmp_path, mode=Mode.EMBEDDED,
            poll_interval=0.05, channel=channel, orchestrator=handle,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert not stop.is_set()
            assert not handle.done()
            # Now actually stop so the thread does not leak.
            stop.request("test cleanup")
        handle.join(timeout=5)

    async def test_pending_checkpoint_reopens_with_c(
        self, tmp_path: Path,
    ) -> None:
        run_dir = _write_minimal_run(tmp_path, "factory-20260720-embed4")
        channel = QueueInteractionChannel()
        stop = StopController()
        decisions: list[int] = []
        handle = _fake_orchestrator(channel, stop, decisions)
        app = RalphTuiApp(
            run_dir=run_dir, root_dir=tmp_path, mode=Mode.EMBEDDED,
            poll_interval=0.05, channel=channel, orchestrator=handle,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            deadline = time.monotonic() + 5
            while not isinstance(app.screen, CheckpointModal):
                await pilot.pause(0.05)
                assert time.monotonic() < deadline
            await pilot.press("escape")  # leave pending
            await pilot.pause()
            assert not handle.done()  # orchestrator still blocked
            await pilot.press("c")  # reopen
            await pilot.pause()
            assert isinstance(app.screen, CheckpointModal)
            await pilot.press("t")  # retry
            deadline = time.monotonic() + 5
            while not handle.done():
                await pilot.pause(0.05)
                assert time.monotonic() < deadline
        assert decisions == [2]

    async def test_generic_prompt_uses_request_labels_and_valid_choices(
        self, tmp_path: Path,
    ) -> None:
        run_dir = _write_minimal_run(tmp_path, "factory-20260720-generic")
        app = RalphTuiApp(
            run_dir=run_dir, root_dir=tmp_path, mode=Mode.DASH,
            poll_interval=0.05,
        )
        request = PromptRequest(
            kind=PromptKind.ITERATION,
            header="Iteration complete. What next?",
            options=("Continue", "Quit"),
        )
        results: list[int | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(CheckpointModal(request), results.append)
            await pilot.pause()
            labels = [button.label.plain for button in app.screen.query("Button")]
            assert labels == ["Continue (1)", "Quit (2)"]
            await pilot.press("t")
            await pilot.pause()
            assert isinstance(app.screen, CheckpointModal)
            await pilot.press("2")
            await pilot.pause()

        assert results == [1]


class TestFallbackAndLogging:
    def test_plain_fallback_accepts_tailer_chunks(self, tmp_path: Path) -> None:
        run_dir = _write_minimal_run(tmp_path, "factory-20260720-fallback")
        result = FactoryResult()
        result.exit_code = 7
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        handle = OrchestratorHandle(
            thread=thread, stop=StopController(), result_box=[result],
        )

        assert _plain_fallback(handle, run_dir) == 7

    def test_root_logging_is_exclusive_and_restored(self) -> None:
        logger = logging.Logger("embed-test")
        old_stream = io.StringIO()
        tui_stream = io.StringIO()
        old_handler = logging.StreamHandler(old_stream)
        tui_handler = logging.StreamHandler(tui_stream)
        logger.addHandler(old_handler)

        previous = _install_exclusive_root_handler(logger, tui_handler)
        logger.warning("during tui")
        _restore_root_handlers(logger, tui_handler, previous)
        logger.warning("after tui")

        assert "during tui" not in old_stream.getvalue()
        assert "after tui" in old_stream.getvalue()
        assert "during tui" in tui_stream.getvalue()
        assert "after tui" not in tui_stream.getvalue()


class TestNotifyCapture:
    def test_captured_hook_writes_nothing_to_terminal(
        self, capfd: Any,
    ) -> None:
        hooks = NotifyHooks(
            NotifyConfig(on_complete="echo HOOK-NOISE"),
            run_id="r", project="p", capture_output=True,
        )
        hooks.fire_complete("done")
        out, err = capfd.readouterr()
        assert "HOOK-NOISE" not in out
        assert "HOOK-NOISE" not in err

    def test_default_keeps_terminal_bell_path(self, capfd: Any) -> None:
        hooks = NotifyHooks(
            NotifyConfig(on_complete="echo HOOK-RINGS"),
            run_id="r", project="p",
        )
        hooks.fire_complete("done")
        out, _ = capfd.readouterr()
        assert "HOOK-RINGS" in out

    def test_subprocess_module_is_what_fire_uses(self) -> None:
        # Guard against the sink accidentally applying to DEVNULL stdin
        # only; the spike measured fd-level leakage, so stdout/stderr
        # must be the captured pair.
        import inspect

        src = inspect.getsource(NotifyHooks._fire)
        assert "stdout=sink" in src and "stderr=sink" in src
        assert subprocess.DEVNULL  # imported, used above
