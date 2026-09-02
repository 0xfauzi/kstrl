"""The five names #326 and #329 gave one home, tested on behaviour.

``tests/test_process_lifecycle.py`` is a STATIC guard: it asserts that
nothing outside ``kstrl/procgroup.py`` spells a process primitive. That
says nothing at all about whether the primitives inside it work, and a
guard that only pins placement would let the home itself be gutted with
CI green. This file is the other half.

Five names, one class each below: :func:`signal_group` and
:func:`pid_is_alive` decide whether a signal may be sent at all,
:func:`signal_process_tree` sends it and degrades, and
:func:`drain_or_abandon` / :func:`reap_or_abandon` let go of what
survives. The sixth class is the end-to-end for the shape #326's sweep
found still open: an agent adapter's ``run`` is a generator, and a
consumer that walks away from it used to leave the CLI running.

WHAT EACH TEST NAMES. Every assertion here is about a process this file
started, identified by a pid it read back from that process's own
pidfile, which is #292's rule and the reason nothing below searches the
machine.

POSIX. Process groups do not exist on Windows.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kstrl import procdispose, procgroup
from kstrl.agents import proc as proc_module
from kstrl.agents.custom import CustomAgent
from kstrl.agents.proc import DeadlineStreamer
from kstrl.procdispose import drain_or_abandon, reap_or_abandon
from kstrl.procgroup import (
    GroupSignal,
    pid_is_alive,
    signal_group,
    signal_process_tree,
)
from tests.helpers import procs

posix_only = pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="process groups are POSIX-only",
)


def _spawn_group(tmp_path: Path, name: str = "child") -> tuple[subprocess.Popen[str], int, int]:
    """A sleeper in its own session. Returns (process, pid, pgid)."""
    pidfile = tmp_path / f"{name}.pid"
    process = subprocess.Popen(
        ["sh", "-c", procs.SLEEPER.format(pidfile=pidfile)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pid = procs.read_pid(pidfile)
    return process, pid, os.getpgid(process.pid)


@posix_only
class TestSignalGroupRefusesRatherThanBroadcasting:
    """#329: the bare-integer half of the guard ``safe_pgid`` holds.

    ``serve.terminate_process_group`` took a caller-supplied pgid to
    ``os.killpg`` with no check of any kind, and was safe by provenance
    alone. The refusals below are the mechanism that replaced it, and
    each asserts that the syscall was NOT REACHED rather than merely
    that the return value said so - a guard that returns the right
    dataclass after signalling anyway is the defect, not the fix.
    """

    @staticmethod
    def _record_killpg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
        return sent

    def test_group_zero_is_refused_and_never_reaches_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``killpg(0, sig)`` is the caller's WHOLE process group."""
        sent = self._record_killpg(monkeypatch)
        outcome = signal_group(0, signal.SIGTERM)
        assert outcome.sent is False
        assert outcome.refused
        assert sent == []

    def test_our_own_group_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The mutation #328 measured: dropping this check does not fail
        a test, it kills the test RUNNER with signal 15, because a serve
        test sets ``fake.pid = os.getpid()``. ``ks serve`` holds the
        daemon singleton lock for its whole process lifetime, so the same
        mistake from a real caller is the daemon."""
        sent = self._record_killpg(monkeypatch)
        outcome = signal_group(os.getpgrp(), signal.SIGTERM)
        assert outcome.sent is False
        assert "our own" in outcome.refused
        assert sent == []

    def test_a_refusal_is_never_reported_as_occupancy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The distinction ``GroupTermination`` next door is built on.
        EPERM is the KERNEL saying it found processes in that group, so
        it is positive evidence a factory is alive; a refusal from this
        module is evidence of nothing. Reporting the second as the first
        tells the operator the opposite of the truth."""
        self._record_killpg(monkeypatch)
        refusal = signal_group(0, signal.SIGTERM)
        assert refusal.denied == ""
        assert refusal.refused != ""

    def test_eperm_is_reported_as_denied_and_not_as_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def deny(pgid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "killpg", deny)
        outcome = signal_group(os.getpgrp() + 1, signal.SIGTERM)
        assert outcome.sent is False
        assert outcome.vanished is False
        assert outcome.denied != ""
        assert outcome.refused == ""

    def test_esrch_is_a_confirmed_empty_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def gone(pgid: int, sig: int) -> None:
            raise ProcessLookupError(3, "No such process")

        monkeypatch.setattr(os, "killpg", gone)
        outcome = signal_group(os.getpgrp() + 1, signal.SIGTERM)
        assert outcome == GroupSignal(False, vanished=True)

    def test_a_real_group_is_actually_signalled(self, tmp_path: Path) -> None:
        """The positive control. Without it every refusal above is
        satisfied by a function that refuses everything.

        The assertion is the RETURN CODE and not ``pid_is_alive``,
        because those two answer different questions and this file
        asserts both: the child is a zombie the moment the signal lands
        and stays one until this process reaps it, so the pid probe
        would still read alive here and be right to (#298).
        """
        process, _pid, pgid = _spawn_group(tmp_path)
        try:
            assert signal_group(pgid, signal.SIGKILL).sent is True
            assert process.wait(timeout=5.0) == -signal.SIGKILL
            assert procs.group_has_live_member(pgid) is False
        finally:
            drain_or_abandon(process, 5.0)


@posix_only
class TestPidIsAlive:
    """The bare-pid twin, lifted out of ``serve._pid_alive`` (#329)."""

    def test_a_non_positive_pid_never_reaches_the_syscall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``os.kill(0, sig)`` is the caller's whole process group and
        ``os.kill(-1, sig)`` is every process this user owns. Signal 0
        makes both harmless today; the guard is here so that stays true
        if the signal ever stops being 0."""
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
        assert pid_is_alive(0) is False
        assert pid_is_alive(-1) is False
        assert sent == []

    def test_a_live_child_reads_alive_and_a_reaped_one_does_not(self, tmp_path: Path) -> None:
        process, pid, _pgid = _spawn_group(tmp_path)
        assert pid_is_alive(pid) is True
        drain_or_abandon(process, 5.0)
        assert procs.wait_for_pid_to_die(pid)

    def test_a_zombie_reads_alive_and_that_is_the_right_answer(self, tmp_path: Path) -> None:
        """The half of #298 that goes the OTHER way, asserted so the two
        readings cannot be conflated later.

        ``read_group_liveness`` answers "is anything RUNNING", and a
        zombie is not. This answers "is that pid still allocated", and a
        zombie is - so it has not been handed to anything else, which is
        exactly what the lease question needs. A killed, unreaped child
        makes the two disagree, and both are correct.
        """
        process, pid, pgid = _spawn_group(tmp_path)
        try:
            assert signal_group(pgid, signal.SIGKILL).sent is True
            deadline = time.monotonic() + 5.0
            while procs.group_has_live_member(pgid) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert procs.group_has_live_member(pgid) is False
            assert pid_is_alive(pid) is True
        finally:
            drain_or_abandon(process, 5.0)
        assert procs.wait_for_pid_to_die(pid)

    def test_eperm_reads_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Alive is the fail direction: the caller reaps a lease on
        False, and a wrong reap puts a second factory on a repo the first
        is still writing to (#186 F1)."""

        def deny(pid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "kill", deny)
        assert pid_is_alive(os.getpid()) is True


@posix_only
class TestSignalProcessTreeDegrades:
    """The routine ``verify`` and ``agents.proc`` each wrote out (#329)."""

    def test_a_real_tree_dies_by_its_group(self, tmp_path: Path) -> None:
        process, _pid, pgid = _spawn_group(tmp_path)
        try:
            signal_process_tree(process, signal.SIGKILL)
            assert process.wait(timeout=5.0) == -signal.SIGKILL
            assert procs.group_has_live_member(pgid) is False
        finally:
            drain_or_abandon(process, 5.0)

    def test_an_unusable_pgid_falls_back_to_the_direct_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mocked ``Popen``, a non-POSIX platform, an unsafe pgid and a
        child already reaped all land here. That degradation is the whole
        reason ``safe_pgid`` can afford to be strict."""
        monkeypatch.setattr(procgroup, "safe_pgid", lambda process: None)
        killed: list[str] = []

        class _Fake:
            def kill(self) -> None:
                killed.append("kill")

            def terminate(self) -> None:
                killed.append("terminate")

        fake: Any = _Fake()
        signal_process_tree(fake, signal.SIGTERM)
        signal_process_tree(fake, signal.SIGKILL)
        assert killed == ["terminate", "kill"]


@posix_only
class TestTheTwoDisposals:
    """``drain_or_abandon`` closes the pipes, ``reap_or_abandon`` does not.

    The difference is measured rather than stylistic and the measurement
    is on ``reap_or_abandon``'s docstring: with a reader thread blocked
    on stdout, a ``close`` from a second thread had not returned after
    3.005s. ``agents.proc.DeadlineStreamer`` runs exactly that way, so a
    disposal that closed for it would be a hang and not a tidier drain.
    """

    def test_drain_returns_what_the_child_wrote(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "talker.pid"
        process = subprocess.Popen(
            ["sh", "-c", f"echo hello; {procs.SLEEPER.format(pidfile=pidfile)}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        pid = procs.read_pid(pidfile)
        stdout, _stderr = drain_or_abandon(process, 5.0)
        assert "hello" in stdout
        assert procs.wait_for_pid_to_die(pid)

    def test_drain_closes_every_pipe_end(self, tmp_path: Path) -> None:
        process, pid, _pgid = _spawn_group(tmp_path)
        drain_or_abandon(process, 5.0)
        for pipe in (process.stdin, process.stdout, process.stderr):
            assert pipe is None or pipe.closed
        assert procs.wait_for_pid_to_die(pid)

    def test_reap_closes_no_pipe(self, tmp_path: Path) -> None:
        """The half that has to stay true, because the streamer's reader
        and writer threads own these two ends and close them themselves."""
        process, pid, _pgid = _spawn_group(tmp_path)
        stdout = process.stdout
        stdin = process.stdin
        assert stdout is not None and stdin is not None
        reap_or_abandon(process, 5.0)
        assert stdout.closed is False
        assert stdin.closed is False
        assert procs.wait_for_pid_to_die(pid)
        stdout.close()
        stdin.close()

    @pytest.mark.parametrize("dispose", [drain_or_abandon, reap_or_abandon])
    def test_an_unreapable_child_is_registered_rather_than_dropped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dispose: Any,
    ) -> None:
        """#326's core. ``Popen.__del__`` calls ``_warn`` BEFORE
        ``_active.append`` (CPython 3.12.8 subprocess.py:1139 and :1145),
        so under ``PYTHONWARNINGS=error`` the warn raises, ``__del__``
        aborts, and an abandoned child is a zombie for the life of the
        process. Registering here is what makes that not the fallback."""
        registered: list[object] = []
        monkeypatch.setattr(procdispose, "_register_abandoned", registered.append)

        class _Unreapable:
            returncode: int | None = None
            stdin = None
            stdout = None
            stderr = None

            def kill(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 0.0)

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 0.0)

        process: Any = _Unreapable()
        dispose(process, 0.01)
        assert registered == [process]

    def test_a_reaped_child_is_not_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The negative control: without it, "register everything"
        passes the test above."""
        registered: list[object] = []
        monkeypatch.setattr(procdispose, "_register_abandoned", registered.append)
        process = subprocess.Popen(
            ["sh", "-c", "exit 0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        drain_or_abandon(process, 5.0)
        assert registered == []


@posix_only
class TestAnAbandonedGeneratorLetsGoOfItsChild:
    """#326's second shape, end to end through a real adapter.

    Every agent adapter's ``run`` is a GENERATOR.
    ``decompose.collect_agent_output`` raises ``AgentOutputTooLarge`` from
    inside ``for line in agent.run(...)`` and abandons one today. Nothing
    then called ``finish``, so the child was never killed, never reaped
    and never deregistered - and it was not even collected, because the
    reader thread's bound method holds a strong reference that the
    ``WeakSet`` deliberately does not. The agent CLI ran to its own
    completion, spending tokens, after the caller had given up on it.
    """

    def test_a_consumer_that_walks_away_kills_the_command(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "agent.pid"
        agent = CustomAgent(f"echo started; {procs.SLEEPER.format(pidfile=pidfile)}")
        stream = agent.run("prompt", cwd=tmp_path, timeout=60.0)
        # Read until the marker rather than taking the first line: a
        # login shell may print its own before the command runs.
        for line in stream:
            if line.strip() == "started":
                break
        else:  # pragma: no cover - the command always prints it
            raise AssertionError("the agent command never started")
        pid = procs.read_pid(pidfile)
        assert pid_is_alive(pid) is True

        # What decompose does: give up on the generator mid-yield.
        stream.close()

        assert procs.wait_for_pid_to_die(pid), (
            "the agent command outlived the consumer that abandoned it, "
            "which is the leak the `finally: streamer.close()` exists to close"
        )

    def test_close_after_finish_does_not_start_a_second_kill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The orderly path calls ``finish`` and then unwinds through the
        same ``finally``, so the second disposal must be a no-op.

        Asserted on a child that does NOT exit, because that is the only
        case where it costs anything: ``kill`` spends a SIGTERM grace and
        then a reap grace, so a second one would add ten seconds to every
        timed-out agent run. A child that exits on its own makes both
        calls free and the test vacuous.
        """
        pidfile = tmp_path / "stubborn.pid"
        streamer = DeadlineStreamer(
            ["sh", "-c", procs.SLEEPER.format(pidfile=pidfile)],
            timeout=60.0,
            term_grace=1.0,
        )
        pid = procs.read_pid(pidfile)
        kills: list[int] = []
        real_kill = DeadlineStreamer.kill

        def counted(self: DeadlineStreamer) -> None:
            kills.append(1)
            real_kill(self)

        monkeypatch.setattr(DeadlineStreamer, "kill", counted)
        streamer.finish(timeout=0.02)
        assert kills == [1]
        streamer.close()
        streamer.close()
        assert kills == [1]
        assert procs.wait_for_pid_to_die(pid)

    def test_a_close_that_raises_still_posts_the_sentinel(self) -> None:
        """The disposal must never be able to hang the thing it disposes for.

        ``lines()`` ends on a ``None`` sentinel the reader thread posts,
        and with no caller deadline it waits on ``queue.get()`` with no
        bound at all. So a close in the reader's ``finally`` that raises
        anything the suppression does not name skips the ``put`` and
        hangs the harness for good.

        MEASURED, and this is a regression test rather than a
        hypothetical: the first version of that close named
        ``(OSError, ValueError)``, and a ``stdout`` whose ``close``
        raises ``AttributeError`` - which is what a ``MagicMock`` proc
        with an iterator for stdout produces, and what an adapter test in
        this suite hands it - hung a real test run for 600 seconds.

        Asserted through a BOUNDED ``get`` rather than by calling
        ``lines()``, because a test for a hang must fail rather than
        reproduce it.
        """
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = iter(["hello\n"])
        with patch("subprocess.Popen", return_value=proc):
            streamer = DeadlineStreamer(["irrelevant"])
        try:
            streamer._reader.join(timeout=5.0)
            assert streamer._reader.is_alive() is False
            assert streamer._queue.get(timeout=1.0) == "hello"
            assert streamer._queue.get(timeout=1.0) is None
        finally:
            streamer.close()

    def test_a_read_that_raises_still_posts_the_sentinel(self) -> None:
        """The other half of the same rule: the READ can fail too.

        The sentinel is in a ``finally`` rather than after the loop
        precisely so that an exception on the way out still posts it.
        MEASURED as a gap rather than assumed: with only the
        close-raises test above, changing that ``finally`` to an ``else``
        left every gate in this suite green, because nothing made the
        read itself fail. An ``else`` runs only when the ``try`` body
        completed cleanly, so a child whose pipe dies mid-stream - an
        ordinary ``OSError`` on a descriptor the kernel tore down -
        would end the reader thread with no sentinel and leave
        ``lines()`` on an unbounded ``queue.get()``.

        The exception is raised from ``__next__`` rather than from
        ``close`` so it lands in the loop, which is the path the other
        test cannot reach.
        """

        def failing_lines() -> Iterator[str]:
            yield "first\n"
            raise OSError("the pipe went away")

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = failing_lines()
        with patch("subprocess.Popen", return_value=proc):
            streamer = DeadlineStreamer(["irrelevant"])
        try:
            streamer._reader.join(timeout=5.0)
            assert streamer._reader.is_alive() is False
            assert streamer._queue.get(timeout=1.0) == "first"
            assert streamer._queue.get(timeout=1.0) is None
        finally:
            streamer.close()

    def test_close_deregisters_from_the_active_set(self, tmp_path: Path) -> None:
        """A killed streamer left in ``_ACTIVE`` means the next
        ``kill_active_process_groups`` signals a corpse."""
        pidfile = tmp_path / "registered.pid"
        streamer = DeadlineStreamer(
            ["sh", "-c", procs.SLEEPER.format(pidfile=pidfile)],
            timeout=60.0,
        )
        pid = procs.read_pid(pidfile)
        assert streamer in set(proc_module._ACTIVE)
        streamer.close()
        assert streamer not in set(proc_module._ACTIVE)
        assert procs.wait_for_pid_to_die(pid)
