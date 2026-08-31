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
import sys
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
    _resolve_max_parallel,
    _wait_interruptible,
    run_factory,
)
from kstrl.shutdown import StopController, install_signal_handlers
from kstrl.tui.bridge import start_command_thread
from kstrl.ui.plain import PlainUI
from tests import spine_utils
from tests.helpers.procs import (
    kill_group,
    ps_is_readable,
    read_pid,
    wait_for_group_to_die,
)
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
# The full argument, and the rule it produced, live in
# tests/helpers/procs.py. In short: this used to assert that
# `pgrep -f "sleep 60"` found nothing MACHINE-WIDE, so an unrelated
# `sleep 600` in another session failed it. The test now names the group
# its own agent reported and nothing here searches a command line.


#: The agent must OUTLIVE the whole test, so that finding its process
#: group dead can only mean the shutdown killed it.
#:
#: The previous version used `sleep 60` and proved nothing. Measured: the
#: test's wall time tracked the agent's sleep exactly, 60.19s for
#: `sleep 60` and 20.65s for `sleep 20`. Instrumented, the stop was
#: requested at t=0.08s and `_abort_inflight` was not reached until
#: t=60.03s, so the agent ran its sleep to the end and died of old age.
#: Planting `return 0` at the top of `kill_active_process_groups` left
#: the test GREEN. With `sleep 600` and a real pool the same sabotage
#: leaves the group alive and the assertion red, which is the whole
#: difference between a test and a decoration.
AGENT_SLEEP_SECONDS = 600

#: `run_factory` must return once the stop is set rather than when the
#: agent finishes. Measured at 0.79s against a real pool. The bound is
#: two orders of margin, and it exists so a regression of that promptness
#: FAILS instead of hanging CI for ten minutes on `sleep 600`.
RUN_FACTORY_BOUND_SECONDS = 120


@pytest.mark.spine
class TestWorkerSigterm:
    def test_pool_worker_sigterm_kills_agent_group(
        self,
        tmp_path: Path,
    ) -> None:
        """A REAL pool worker running a sleeping agent: SIGTERM to the
        worker must kill the agent's process group (no orphans).

        "REAL pool worker" is asserted, not assumed. The version this
        replaces asked for `max_parallel=2` and silently got
        `_InlineExecutor`, because `test_event_stream._factory_config`
        defaults to `use_worktrees=False` and `run_factory` forces
        `max_parallel=1` when worktrees are off. So there was no worker,
        nothing was SIGTERMed, and the class name and this docstring were
        both wrong. The inline executor also runs the component in the
        CALLING thread, which is why the stop could not be observed until
        the agent had already exited on its own.

        The spine harness is used instead, because a real pool needs a
        real git repository to make worktrees in, and `use_worktrees=True`
        is what stops `max_parallel` being forced back to 1. It is
        reached through the `spine_utils` module rather than by importing
        its builders, because this file also imports the same five
        builders from `test_event_stream` under a leading underscore, and
        the pair that differ only by that underscore differ exactly in
        `use_worktrees`.
        """
        root = tmp_path / "repo"
        spine_utils.init_kstrl_repo(root, ("comp-a",))
        manifest = spine_utils.make_manifest([spine_utils.component("comp-a")])

        # The agent reports two pids and then becomes the sleep. `$$` is
        # its own, kept across `exec`, and DeadlineStreamer starts it with
        # start_new_session=True so that pid also leads its own group.
        # `$PPID` is the process that spawned it, which is the evidence
        # that a separate worker ran it.
        agent_pidfile = tmp_path / "agent.pid"
        worker_pidfile = tmp_path / "worker.pid"
        base = spine_utils.base_config(
            root,
            agent_cmd=(
                f"echo $PPID > {worker_pidfile}; "
                f"echo $$ > {agent_pidfile}; "
                f"exec sleep {AGENT_SLEEP_SECONDS}"
            ),
        )
        config = spine_utils.factory_config(max_parallel=2)
        stop = StopController()

        # `start_command_thread` is how the TUI runs a command core off
        # the main thread, and it boxes an exception instead of losing it
        # to the threading excepthook. Reused rather than hand-rolled so
        # a `run_factory` that RAISES is reported as itself.
        handle = start_command_thread(
            lambda: (
                run_factory(
                    manifest,
                    config,
                    base,
                    PlainUI(no_color=True, file=io.StringIO()),
                    root,
                    stop=stop,
                ).exit_code
            ),
            stop=stop,
            name="sigterm-spine-test",
        )

        agent_pgid: int | None = None
        worker_pid: int | None = None
        try:
            try:
                # Read on THIS thread while the agent is alive: after the
                # shutdown there is no process left to ask.
                agent_pgid = os.getpgid(read_pid(agent_pidfile, timeout=60.0))
                worker_pid = read_pid(worker_pidfile, timeout=10.0)
            finally:
                # Requested even when the agent never appeared, so a miss
                # surfaces as the explicit assertion below rather than as
                # run_factory blocking for the full sleep.
                stop.request("sigterm spine test")

            handle.join(RUN_FACTORY_BOUND_SECONDS)
            assert handle.done(), (
                f"run_factory did not return within {RUN_FACTORY_BOUND_SECONDS}s "
                f"of the stop being requested; it is waiting for the agent to "
                f"finish rather than aborting it"
            )
            assert not handle.error_box, f"run_factory raised: {handle.error_box[0]!r}"
            assert handle.exit_code == 130

            # An agent that never started cannot leave an orphan either,
            # so without this the test could pass having observed nothing.
            assert agent_pgid is not None, "never observed the agent subprocess start"

            # The precondition the old version silently lost. If the agent
            # was spawned by THIS process there was no worker to SIGTERM,
            # and everything below would be measuring the inline path.
            assert worker_pid != os.getpid(), (
                "the agent was spawned by the test process itself, so no "
                "pool worker exists and this test is not exercising the "
                "SIGTERM forwarding it claims to; check that use_worktrees "
                "is on and max_parallel survived to the executor choice"
            )

            # The invariant that makes a group-scoped assertion safe to
            # make at all: killing our own group would kill the harness,
            # the footgun agents/proc.py::_signal_group documents. The
            # worker shares our group, being a plain fork, which is also
            # why the cleanup below never touches the WORKER's group.
            assert agent_pgid != os.getpgrp()

            assert wait_for_group_to_die(agent_pgid), (
                f"agent subprocess orphaned: process group {agent_pgid} still "
                f"has a live member after the worker was told to stop"
            )
        finally:
            # `sleep 600` outliving a failed run would poison every later
            # run on this machine, which is the #292 failure mode itself.
            # Rediscovered here rather than assumed, because the paths
            # that skip the read above are exactly the ones that leave it
            # running.
            if agent_pgid is None:
                try:
                    agent_pgid = os.getpgid(read_pid(agent_pidfile, timeout=1.0))
                except (AssertionError, ProcessLookupError):
                    agent_pgid = None
            if agent_pgid is not None:
                kill_group(agent_pgid)


class TestTheOrphanCheckIsScopedToItsOwnGroup:
    """#292 regression: an unrelated `sleep 600` must not fail the check.

    The old assertion and the new one are run against the same machine
    state, so this pins the difference rather than asserting the new one
    in isolation: with a decoy running, `pgrep -f "sleep 60"` finds
    something (the old test would have failed, and does - verified by
    running the pre-fix file with a decoy alive) while the group check
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

            # The new check: the group the test owned is gone.
            assert wait_for_group_to_die(sentinel_pgid)

            # The old check, on that same machine state: still matches,
            # because "sleep 60" is a prefix of the decoy's "sleep 600".
            # This is the assertion that used to fail runs, and it is
            # here so the fix cannot be quietly reverted to it. The AST
            # net in tests/test_process_scoping.py allows this file by name.
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

    def test_a_zombie_does_not_count_as_a_live_member(self) -> None:
        """Why the check reads `ps` state instead of `killpg(pgid, 0)`.

        The parent here never reaps, so the killed sentinel stays a
        zombie. Measured against the real production spelling of the
        killpg check: it reports that group as present for as long as
        nothing reaps, so a test built on it would hang to its deadline
        on every run where the pool worker is slow.

        The assertion on `process_group_alive` USED to pin a flaw: #292
        left the production check on the killpg spelling and this test
        asserted True so that whoever fixed it would see the coupling.
        #298 fixed it, so it now asserts the contract. What keeps that
        assertion honest is `signal_probe_alive` on the line above it:
        the old spelling, on the same group at the same moment, still
        reports the group as present. Without that control, a False here
        could mean the zombie had simply been reaped and the test would
        have measured nothing.
        """
        from kstrl.procgroup import signal_probe_alive
        from kstrl.serve import process_group_alive

        if not ps_is_readable():
            pytest.skip(
                "ps is absent or filtered here, so process_group_alive "
                "degrades to the signal probe, which counts a zombie as "
                "alive by design. The assertion below would fail pointing "
                "at kstrl rather than at the environment."
            )

        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess,sys,time;"
                "p=subprocess.Popen(['sleep','60'],start_new_session=True);"
                "print(p.pid,flush=True);"
                "time.sleep(30)",  # deliberately never reaps
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        pgid = -1
        try:
            assert parent.stdout is not None
            pgid = os.getpgid(int(parent.stdout.readline().strip()))
            os.killpg(pgid, signal.SIGKILL)

            assert wait_for_group_to_die(pgid), (
                "a reaped-pending zombie was counted as a live member"
            )

            # The control: this really is the zombie window, not a group
            # that quietly went away. The primitive production used
            # before #298 still reports it as present.
            assert signal_probe_alive(pgid) is True, (
                "the zombie was already reaped, so the assertion below would prove nothing"
            )
            assert process_group_alive(pgid) is False, (
                "a reaped-pending zombie must not count as a live member"
            )
        finally:
            parent.kill()
            parent.wait(timeout=10)
            if pgid > 1:
                kill_group(pgid)


class TestTheParallelismDecisionIsAnnounced:
    """#292's root cause, as a unit.

    `_run_factory_locked` silently rewrote a configured `max_parallel`
    whenever worktrees were off, and said only "running sequentially" at
    info level without naming the knob. That is how the spine test above
    could ask for 2, get 1 and therefore `_InlineExecutor`, and stay
    green for months while measuring nothing: no operator and no test
    author was ever told the number had been discarded.

    So the decision now has a name, and it reports every setting it
    throws away.
    """

    def _ui(self) -> tuple[PlainUI, io.StringIO]:
        buffer = io.StringIO()
        return PlainUI(no_color=True, file=buffer), buffer

    def test_parallelism_survives_when_worktrees_are_on(self) -> None:
        ui, buffer = self._ui()
        config = spine_utils.factory_config(max_parallel=4)
        assert _resolve_max_parallel(config, ui) == 4
        assert buffer.getvalue() == ""

    def test_disabling_worktrees_forces_one_and_says_what_it_discarded(self) -> None:
        ui, buffer = self._ui()
        config = spine_utils.factory_config(max_parallel=8, use_worktrees=False)
        assert _resolve_max_parallel(config, ui) == 1
        out = buffer.getvalue()
        assert "max_parallel" in out, "the discarded knob must be named"
        assert "8" in out, "the operator's configured value must appear"

    def test_it_stays_quiet_when_it_discards_nothing(self) -> None:
        """The old line fired unconditionally, so it was noise at
        max_parallel=1 and therefore easy to stop reading."""
        ui, buffer = self._ui()
        config = spine_utils.factory_config(max_parallel=1, use_worktrees=False)
        assert _resolve_max_parallel(config, ui) == 1
        assert buffer.getvalue() == ""

    def test_single_pr_forces_one_and_says_so(self) -> None:
        ui, buffer = self._ui()
        config = spine_utils.factory_config(max_parallel=4, single_pr=True)
        assert _resolve_max_parallel(config, ui) == 1
        assert "4" in buffer.getvalue()

    def test_single_pr_at_one_is_not_a_discard(self) -> None:
        ui, buffer = self._ui()
        config = spine_utils.factory_config(max_parallel=1, single_pr=True)
        assert _resolve_max_parallel(config, ui) == 1
        assert buffer.getvalue() == ""
