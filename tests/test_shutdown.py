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
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from kstrl.agents.proc import DeadlineStreamer, kill_active_process_groups
from kstrl.factory import (
    ComponentResult,
    _abort_inflight,
    _wait_interruptible,
    run_factory,
)
from kstrl.shutdown import StopController, install_signal_handlers
from kstrl.ui.plain import PlainUI
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
            stop,
            on_second=lambda: seconds.append(True),
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
            {future},
            30.0,
            stop,
            slice_seconds=0.2,
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
            {future},
            0.2,
            stop,
            slice_seconds=0.1,
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
            executor,
            {},
            Mock(),
            Mock(),
            stop,  # type: ignore[arg-type]
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
            executor,
            {},
            Mock(),
            Mock(),
            stop,  # type: ignore[arg-type]
            term_grace=30.0,
        )

        assert worker.terminated is True
        assert worker.killed is False


class TestAgentGroupKill:
    def test_kill_active_process_groups_kills_real_subprocess(self) -> None:
        """A live DeadlineStreamer child (own session) dies on the
        shutdown group-kill; nothing is orphaned."""
        streamer = DeadlineStreamer(
            ["sh", "-c", "sleep 60"],
            timeout=60.0,
            term_grace=1.0,
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

        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=fake_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                _factory_config(root),
                _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()),
                root,
                stop=stop,
            )
        assert launched == []  # nothing started after the stop
        assert result.exit_code == 130
        assert manifest.completed_at  # terminal state stamped

    def test_stop_mid_run_aborts_inflight_and_records(
        self,
        tmp_path: Path,
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
        with (
            patch(
                "kstrl.factory._run_component",
                side_effect=slow_component,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(
                manifest,
                _factory_config(root),
                _make_base_config(root),
                PlainUI(no_color=True, file=buf),
                root,
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
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=result,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(
                manifest,
                _factory_config(root),
                _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()),
                root,
                run_id="factory-20260720-999999.000000-fixed",
            )
        assert (
            root / ".kstrl" / "runs" / "factory-20260720-999999.000000-fixed" / "events.jsonl"
        ).exists()


class TestLoopStopCheck:
    def test_stop_between_iterations(self, tmp_path: Path) -> None:
        from kstrl.config import KstrlConfig
        from kstrl.loop import run_loop

        class CountingAgent:
            def __init__(self) -> None:
                self.runs = 0

            @property
            def name(self) -> str:
                return "counting"

            def run(
                self, prompt: str, cwd: Path | None = None, timeout: float | None = None
            ) -> Any:
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
        config = KstrlConfig(
            max_iterations=5,
            sleep_seconds=0,
            prompt_file=tmp_path / "prompt.md",
            prd_file=tmp_path / "prd.json",
            kstrl_branch="",
            kstrl_branch_explicit=True,
            ui_mode="plain",
            no_color=True,
        )
        (tmp_path / "prompt.md").write_text("p")
        result = run_loop(
            config,
            PlainUI(no_color=True, file=io.StringIO()),
            agent,
            tmp_path,
            stop_check=stop_after_first,
        )
        assert agent.runs == 1
        assert result.exit_code == 130
        assert result.completed is False


# ---------------------------------------------------------------------------
# #292: the orphan check is about ONE process group, not about the machine
#
# This used to assert that `pgrep -f "sleep 60"` found nothing, with no
# scoping at all. `pgrep -f` matches a substring of the full command line
# machine-wide, so the assertion failed whenever ANY process anywhere had
# "sleep 60" in its command line - a `sleep 600` monitor loop in another
# session matches, because "sleep 60" is a prefix of it. The confirmed
# instance was exactly that. It read as "timing-sensitive under load" to
# roughly six agents in a row, several of whom ran controlled A/B
# comparisons against a clean ref before concluding, correctly and
# expensively, that it had nothing to do with their diff. It is not
# timing-sensitive, it is machine-state-sensitive; load only correlates
# because a busy machine is likelier to be running another sleep. CI
# stayed green because a runner is a clean machine.
#
# The claim the test actually wants to make is "the worker killed ITS
# agent's process group". That is a statement about one specific group,
# so the assertion below names that group and never searches a command
# line. Command-line matching survives only in DISCOVERY, where it is
# scoped to this process's own descendants, which is a set no other
# session can add to.
#
# LIVENESS IS NOT killpg(pgid, 0). Measured while writing this: with the
# agent's parent alive and not yet reaping, `os.killpg(pgid, 0)` reported
# a SIGKILLed group as still present for the entire 6s observation window
# and never converged, because a zombie stays signalable. Taking that as
# the primitive would have replaced a machine-state flake with a
# reap-timing flake. Group membership from `ps`, with zombies excluded,
# was correct immediately in the same measurement.


def _ps_rows() -> list[tuple[int, int, int, str, str]]:
    """(pid, ppid, pgid, state, args) for every process on the machine.

    ``-w -w`` asks for untruncated command lines on both GNU and BSD ps;
    the trailing ``=`` on each key suppresses the header.
    """
    out = subprocess.run(
        ["ps", "-A", "-w", "-w", "-o", "pid=,ppid=,pgid=,stat=,args="],
        capture_output=True,
        text=True,
    )
    rows: list[tuple[int, int, int, str, str]] = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        rows.append((pid, ppid, pgid, parts[3], parts[4]))
    return rows


def _descendant_pids(rows: list[tuple[int, int, int, str, str]], root_pid: int) -> set[int]:
    """Every pid below ``root_pid`` in the process tree ``rows`` describes."""
    children: dict[int, list[int]] = {}
    for pid, ppid, _pgid, _state, _args in rows:
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _find_descendant_group(needle: str) -> int | None:
    """The process group of a descendant of THIS process matching ``needle``.

    Our own group is excluded and so are pgids 0 and 1. That guard is
    load-bearing rather than defensive: a pool worker is a plain fork, so
    it shares our process group, and returning that pgid to a caller that
    goes on to signal it would be ``killpg`` on the harness's own group -
    the same footgun ``agents/proc.py::_signal_group`` documents. An
    early draft of this helper did exactly that and killed its own test
    run.
    """
    rows = _ps_rows()
    descendants = _descendant_pids(rows, os.getpid())
    ours = os.getpgrp()
    for pid, _ppid, pgid, _state, args in rows:
        if pid in descendants and needle in args and pgid not in (ours, 0, 1):
            return pgid
    return None


def _group_has_live_member(pgid: int) -> bool:
    """Whether any non-zombie process is still in process group ``pgid``.

    A zombie is a process that has already died and is only waiting to be
    reaped, so it is not an orphaned agent and must not count as one.
    """
    return any(pgid == row[2] and not row[3].startswith("Z") for row in _ps_rows())


def _kill_group(pgid: int) -> None:
    """Best-effort SIGKILL of a process group, for test cleanup.

    A test that fails without this leaves the agent alive, and a stray
    `sleep 60` on a shared machine is precisely what #292 is about: the
    old test's own failures poisoned every later run of it.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


@pytest.mark.spine
class TestWorkerSigterm:
    def test_pool_worker_sigterm_kills_agent_group(
        self,
        tmp_path: Path,
    ) -> None:
        """A REAL pool worker running a sleeping agent: SIGTERM to the
        worker must kill the agent's process group (no orphans) and the
        component must be recorded aborted."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        base = _make_base_config(root)
        base.agent_cmd = "sleep 60"
        config = _factory_config(root, max_parallel=2)
        stop = StopController()
        # Written by the watcher thread, read by the assertions below.
        agent_pgid: list[int] = []

        def stop_soon() -> None:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                pgid = _find_descendant_group("sleep 60")
                if pgid is not None:
                    agent_pgid.append(pgid)
                    break
                time.sleep(0.2)
            # Requested even when discovery failed, so a miss surfaces as
            # the explicit assertion below rather than as run_factory
            # blocking until its own timeout.
            stop.request("sigterm spine test")

        watcher = threading.Thread(target=stop_soon)
        watcher.start()
        try:
            with patch("kstrl.git.get_diff_content", return_value=""):
                result = run_factory(
                    manifest,
                    config,
                    base,
                    PlainUI(no_color=True, file=io.StringIO()),
                    root,
                    stop=stop,
                )
            assert result.exit_code == 130
            # Without this the test could pass having observed nothing:
            # an agent that never started cannot leave an orphan either.
            assert agent_pgid, "never observed the agent subprocess start"

            pgid = agent_pgid[0]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _group_has_live_member(pgid):
                time.sleep(0.2)
            assert not _group_has_live_member(pgid), (
                f"agent subprocess orphaned: process group {pgid} still has a "
                f"live member after the worker was told to stop"
            )
        finally:
            watcher.join(timeout=20)
            for pgid in agent_pgid:
                _kill_group(pgid)


class TestTheOrphanCheckIsScopedToItsOwnGroup:
    """#292 regression: an unrelated `sleep 600` must not fail the check.

    The old assertion and the new one are run against the same machine
    state, so this pins the difference rather than asserting the new one
    in isolation: with a decoy running, `pgrep -f "sleep 60"` finds
    something (the old test would have failed) while the group check
    correctly reports the sentinel's own group as gone.
    """

    def test_a_stranger_sleeping_600_does_not_look_like_an_orphan(self) -> None:
        # A process from "another session", as far as this test is
        # concerned: its own session, never a descendant of ours.
        decoy = subprocess.Popen(
            ["sleep", "600"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sentinel = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            sentinel_pgid = os.getpgid(sentinel.pid)
            assert sentinel_pgid != os.getpgrp()

            # The sentinel dies and is reaped, exactly as a worker kills
            # and reaps its agent. Nothing else about the machine changes.
            os.killpg(sentinel_pgid, signal.SIGKILL)
            sentinel.wait(timeout=10)

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _group_has_live_member(sentinel_pgid):
                time.sleep(0.1)

            # The new check: the group the test owned is gone.
            assert not _group_has_live_member(sentinel_pgid)

            # The old check, on that same machine state: still matches,
            # because "sleep 60" is a prefix of the decoy's "sleep 600".
            # This is the assertion that used to fail runs, and it is
            # here so the fix cannot be quietly reverted to it.
            stale = subprocess.run(["pgrep", "-f", "sleep 60"], capture_output=True)
            assert stale.returncode == 0, (
                "expected the decoy to still match the old machine-wide "
                "pattern; without that this test proves nothing"
            )
            assert str(decoy.pid).encode() in stale.stdout
        finally:
            decoy.kill()
            decoy.wait(timeout=10)
            if sentinel.poll() is None:
                sentinel.kill()
                sentinel.wait(timeout=10)

    def test_discovery_never_returns_our_own_process_group(self) -> None:
        """The guard that stops a group kill from reaching the harness.

        A pool worker is a plain fork and shares our process group, so a
        descendant matching on command line is NOT enough on its own.
        """
        # This test's own interpreter is a descendant-free match for its
        # own argv, and every direct child of it shares our group.
        child = subprocess.Popen(
            ["sleep", "60"],  # no start_new_session: stays in OUR group
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert os.getpgid(child.pid) == os.getpgrp()
            # Present as a descendant, matching the needle, and still
            # refused because signalling that group would kill the run.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(row[0] == child.pid for row in _ps_rows()):
                    break
                time.sleep(0.1)
            assert _find_descendant_group("sleep 60") is None
        finally:
            child.kill()
            child.wait(timeout=10)
