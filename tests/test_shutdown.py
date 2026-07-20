"""Stage 3 PR B (TUI rewrite): graceful shutdown.

Before PR B, Ctrl-C relied on Click's default abort: cleanup was
skipped, executor shutdown could block on live workers, and agent
subprocesses were orphaned. These tests pin the new contract:
stop-request honored within the wait slice, in-flight components
recorded as aborted, agents group-killed (real subprocess test),
worktree cleanup running, exit code 130.
"""

from __future__ import annotations

import io
import signal
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from ralph_py.agents.proc import DeadlineStreamer, kill_active_process_groups
from ralph_py.factory import (
    ComponentResult,
    _abort_inflight,
    _wait_interruptible,
    run_factory,
)
from ralph_py.shutdown import StopController, install_signal_handlers
from ralph_py.ui.plain import PlainUI
from tests.test_event_stream import (
    _component,
    _factory_config,
    _make_base_config,
    _make_manifest,
    _setup_project,
)


class TestStopController:
    def test_request_and_escalation(self) -> None:
        stop = StopController()
        assert stop.is_set() is False
        stop.request("first")
        assert stop.is_set() is True
        assert stop.reason == "first"
        assert stop.force is False
        stop.request("second")
        assert stop.force is True
        assert stop.reason == "first"  # original reason preserved

    def test_signal_handlers_route_and_restore(self) -> None:
        stop = StopController()
        seconds: list[bool] = []
        before = signal.getsignal(signal.SIGTERM)
        uninstall = install_signal_handlers(
            stop, on_second=lambda: seconds.append(True),
        )
        try:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
            assert stop.is_set() is True
            assert "SIGTERM" in stop.reason
            handler(signal.SIGINT, None)
            assert stop.force is True
            assert seconds == [True]
        finally:
            uninstall()
        assert signal.getsignal(signal.SIGTERM) is before


class TestWaitInterruptible:
    def test_no_stop_behaves_like_wait(self) -> None:
        from concurrent.futures import Future

        future: Future[Any] = Future()
        future.set_result(1)
        done, stopped = _wait_interruptible({future}, 1.0, None)
        assert done == {future}
        assert stopped is False

    def test_stop_returns_within_slice(self) -> None:
        from concurrent.futures import Future

        future: Future[Any] = Future()  # never completes
        stop = StopController()

        def _later() -> None:
            time.sleep(0.1)
            stop.request("test")

        threading.Thread(target=_later).start()
        started = time.monotonic()
        done, stopped = _wait_interruptible(
            {future}, 30.0, stop, slice_seconds=0.2,
        )
        elapsed = time.monotonic() - started
        assert stopped is True
        assert done == set()
        assert elapsed < 2.0  # honored well before the 30s backstop

    def test_timeout_expiry_without_stop(self) -> None:
        from concurrent.futures import Future

        future: Future[Any] = Future()
        stop = StopController()
        done, stopped = _wait_interruptible(
            {future}, 0.2, stop, slice_seconds=0.1,
        )
        assert stopped is False
        assert done == set()


class TestAbortInflight:
    class Worker:
        pid = 4242

        def __init__(self, *, exits_on_term: bool) -> None:
            self.alive = True
            self.exits_on_term = exits_on_term
            self.terminated = False
            self.killed = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            if self.exits_on_term:
                self.alive = False

        def kill(self) -> None:
            self.killed = True
            self.alive = False

    class Executor:
        def __init__(self, worker: TestAbortInflight.Worker) -> None:
            self._processes = {worker.pid: worker}
            self.shutdown_called = False

        def shutdown(self, **kwargs: Any) -> None:
            self.shutdown_called = True

    def test_second_request_skips_grace_and_kills_live_worker(self) -> None:
        worker = self.Worker(exits_on_term=False)
        executor = self.Executor(worker)
        stop = StopController()
        stop.request("first")
        stop.request("second")

        _abort_inflight(
            executor, {}, Mock(), Mock(), stop,  # type: ignore[arg-type]
            term_grace=30.0,
        )

        assert worker.terminated is True
        assert worker.killed is True
        assert executor.shutdown_called is True

    def test_exited_worker_is_not_killed(self) -> None:
        worker = self.Worker(exits_on_term=True)
        executor = self.Executor(worker)
        stop = StopController()
        stop.request("first")

        _abort_inflight(
            executor, {}, Mock(), Mock(), stop,  # type: ignore[arg-type]
            term_grace=30.0,
        )

        assert worker.terminated is True
        assert worker.killed is False


class TestAgentGroupKill:
    def test_kill_active_process_groups_kills_real_subprocess(self) -> None:
        """A live DeadlineStreamer child (own session) dies on the
        shutdown group-kill; nothing is orphaned."""
        streamer = DeadlineStreamer(
            ["sh", "-c", "sleep 60"], timeout=60.0, term_grace=1.0,
        )
        pid = streamer._proc.pid
        time.sleep(0.1)
        killed = kill_active_process_groups()
        assert killed >= 1
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if streamer._proc.poll() is not None:
                break
            time.sleep(0.05)
        assert streamer._proc.poll() is not None, f"pid {pid} survived"
        streamer.finish(timeout=2.0)


class TestFactoryShutdown:
    def test_pre_set_stop_aborts_before_launch(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        stop = StopController()
        stop.request("pre-set")
        launched: list[str] = []

        def fake_component(comp_id: str, *a: Any, **k: Any) -> ComponentResult:
            launched.append(comp_id)
            return ComponentResult(comp_id, success=True, iterations=1)

        with patch(
            "ralph_py.factory._run_component", side_effect=fake_component,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, _factory_config(root), _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()), root,
                stop=stop,
            )
        assert launched == []  # nothing started after the stop
        assert result.exit_code == 130
        assert manifest.completed_at  # terminal state stamped

    def test_stop_mid_run_aborts_inflight_and_records(
        self, tmp_path: Path,
    ) -> None:
        """Two components; the stop fires while comp-a's worker runs.
        comp-a is recorded aborted, comp-b never launches, cleanup and
        the manifest flush still happen, exit code 130."""
        root = _setup_project(tmp_path, ["comp-a", "comp-b"])
        comps = [_component("comp-a"), _component("comp-b", deps=["comp-a"])]
        manifest = _make_manifest(comps)
        stop = StopController()

        def slow_component(comp_id: str, *a: Any, **k: Any) -> ComponentResult:
            stop.request("mid-run test stop")
            time.sleep(1.0)  # keep the future in flight past the stop
            return ComponentResult(comp_id, success=True, iterations=1)

        buf = io.StringIO()
        with patch(
            "ralph_py.factory._run_component", side_effect=slow_component,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, _factory_config(root), _make_base_config(root),
                PlainUI(no_color=True, file=buf), root,
                stop=stop,
            )

        assert result.exit_code == 130
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.status == "failed"
        assert comp_a.failed_phase == "aborted"
        assert "aborted" in (comp_a.error or "")
        comp_b = manifest.get_component("comp-b")
        assert comp_b is not None
        assert comp_b.status in ("pending", "skipped")  # never launched
        assert "Aborted in-flight work" in buf.getvalue()

    def test_run_id_override_used(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        result = ComponentResult("comp-a", success=True, iterations=1)
        with patch(
            "ralph_py.factory._run_component", return_value=result,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            run_factory(
                manifest, _factory_config(root), _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()), root,
                run_id="factory-20260720-999999.000000-fixed",
            )
        assert (
            root / ".ralph" / "runs" / "factory-20260720-999999.000000-fixed"
            / "events.jsonl"
        ).exists()


class TestLoopStopCheck:
    def test_stop_between_iterations(self, tmp_path: Path) -> None:
        from ralph_py.config import RalphConfig
        from ralph_py.loop import run_loop

        class CountingAgent:
            def __init__(self) -> None:
                self.runs = 0

            @property
            def name(self) -> str:
                return "counting"

            def run(self, prompt: str, cwd: Path | None = None,
                    timeout: float | None = None) -> Any:
                self.runs += 1
                yield "line"

            @property
            def final_message(self) -> str | None:
                return None

            @property
            def usage_records(self) -> list[Any]:
                return []

        calls = {"n": 0}

        def stop_after_first() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # allow iteration 1, stop iteration 2

        agent = CountingAgent()
        config = RalphConfig(
            max_iterations=5, sleep_seconds=0,
            prompt_file=tmp_path / "prompt.md",
            prd_file=tmp_path / "prd.json",
            ralph_branch="", ralph_branch_explicit=True,
            ui_mode="plain", no_color=True,
        )
        (tmp_path / "prompt.md").write_text("p")
        result = run_loop(
            config, PlainUI(no_color=True, file=io.StringIO()), agent,
            tmp_path, stop_check=stop_after_first,
        )
        assert agent.runs == 1
        assert result.exit_code == 130
        assert result.completed is False


@pytest.mark.spine
class TestWorkerSigterm:
    def test_pool_worker_sigterm_kills_agent_group(
        self, tmp_path: Path,
    ) -> None:
        """A REAL pool worker running a sleeping agent: SIGTERM to the
        worker must kill the agent's process group (no orphans) and the
        component must be recorded aborted."""
        import subprocess

        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        base = _make_base_config(root)
        base.agent_cmd = "sleep 60"
        config = _factory_config(root, max_parallel=2)
        stop = StopController()

        def stop_soon() -> None:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                out = subprocess.run(
                    ["pgrep", "-f", "sleep 60"], capture_output=True,
                )
                if out.returncode == 0:
                    break
                time.sleep(0.2)
            stop.request("sigterm spine test")

        threading.Thread(target=stop_soon).start()
        with patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, base,
                PlainUI(no_color=True, file=io.StringIO()), root,
                stop=stop,
            )
        assert result.exit_code == 130
        # No orphaned agent: the sleep 60 process group must be gone.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            out = subprocess.run(
                ["pgrep", "-f", "sleep 60"], capture_output=True,
            )
            if out.returncode != 0:
                break
            time.sleep(0.2)
        assert out.returncode != 0, "agent subprocess orphaned"
