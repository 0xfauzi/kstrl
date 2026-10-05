"""A stop ends the agent whoever asks for it, and a hangup is a stop (#642).

The defect, measured on the real ``ks factory --tui`` in a pty: the quit
modal says "In-flight agents will be group-killed", but confirming it only
set a flag. Inline, the scheduler is blocked inside the component, so the
agent ran on (alive 15 s after ``y``, the run never exited and the manifest
stayed ``running``). Only the SIGINT/SIGTERM handler killed agents. And a
hangup was not handled at all: a closed terminal killed ``ks`` outright,
the leash ended the agent, and the manifest was left ``running`` with no
abort recorded.

The fix: ``StopController.request`` starts the kill on a thread of its
own, so every stop (a signal, the TUI's confirm, the home shell's exit)
ends the agents running in this process, and SIGHUP is handled like
SIGINT unless it was already ignored (``nohup``).

Every process these tests signal is the ``ks`` process group they started
or a pid the stand-in agent wrote (#292).
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

from kstrl.agents.proc import DEFAULT_TERM_GRACE_SECONDS
from kstrl.loop import STOP_EXIT_CODE
from kstrl.manifest import Manifest
from tests.helpers import procs
from tests.test_agent_dies_with_kstrl import (
    AGENT_BOUND_SECONDS,
    GRANDCHILD_BOUND_SECONDS,
    START_FUSE_SECONDS,
    _factory_args,
    _ks,
    _pairs,
    _silent_agent,
    _wait_for_agents,
)
from tests.test_agent_processes_outlive_run import COMP, FLAGS, _env, _kill_pid, _repo

#: Seconds from the hangup or the confirmed stop to the run's exit. Measured
#: on 10 cores: 2.5 s after a SIGHUP (load 28), 7.9 to 8.6 s after the
#: TUI's ``y`` with an agent that ignores SIGTERM (n=12, load 27 to 49).
#: A fuse with margin: before the fix the run did not exit at all.
EXIT_BOUND_SECONDS = DEFAULT_TERM_GRACE_SECONDS + 25.0

#: Seconds from ``y`` to the "shutting down" notice on the screen. The
#: notice is drawn after ``request`` returns, so a kill run on the caller's
#: thread delays it by the whole grace when the agent ignores SIGTERM.
#: Must stay below that grace. Measured: 0.03 to 0.25 s, n=12 at load 27
#: to 49; with the kill planted on the caller's thread the test failed here.
RESPONSE_BOUND_SECONDS = 2.0

#: Starts the CLI with the pty on fd 0 as its controlling terminal and
#: SIGHUP at its default disposition, then ``exec``s it, so the pid is the
#: CLI's. Without the controlling terminal the pty was revoked 1.16 s after
#: the start and the TUI never drew a frame (measured, 1 of 1); a real
#: terminal is always the controlling terminal of its session.
_TTY_EXEC = (
    "import fcntl, os, signal, sys, termios; "
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
    "signal.signal(signal.SIGHUP, signal.SIG_DFL); "
    "os.execv(sys.executable, [sys.executable, '-m', 'kstrl', *sys.argv[1:]])"
)

#: As ``_TTY_EXEC`` without a terminal, with SIGHUP ignored: what ``nohup``
#: hands the command it starts.
_NOHUP_EXEC = (
    "import os, signal, sys; signal.signal(signal.SIGHUP, signal.SIG_IGN); "
    "os.execv(sys.executable, [sys.executable, '-m', 'kstrl', *sys.argv[1:]])"
)

_ANSI = re.compile(rb"\x1b\[[0-9;?<>=$]*[A-Za-z~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


class _Terminal:
    """A pty this test owns, its output drained on a thread so the program
    on it never blocks on a full buffer. The drain polls with ``select``
    because closing the master while a thread is blocked reading it hangs
    on macOS (measured: the close had not returned after 70 s)."""

    def __init__(self) -> None:
        self.master, self.slave = os.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        self._out = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._drain = threading.Thread(target=self._read, daemon=True)

    def start(self, argv: list[str], root: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
        )
        os.close(self.slave)
        self._drain.start()
        return proc

    def _read(self) -> None:
        while not self._stop.is_set():
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._out.extend(chunk)

    def text(self) -> str:
        with self._lock:
            return _ANSI.sub(b"", bytes(self._out)).decode("utf-8", "replace")

    def wait_for(self, needle: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while needle not in self.text():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    def type(self, keys: bytes) -> None:
        os.write(self.master, keys)

    def close(self) -> None:
        """Close the master: the kernel hangs up the session on it."""
        if self.master == -1:
            return
        self._stop.set()
        self._drain.join(timeout=5)
        os.close(self.master)
        self.master = -1


def _tui_args(root: Path) -> list[str]:
    plain = {"--no-tui", "--ui", "plain"}
    return [
        "factory",
        "--manifest",
        str(root / "scripts" / "kstrl" / "manifest.json"),
        "--root",
        str(root),
        "--max-parallel",
        "1",
        *[flag for flag in FLAGS if flag not in plain],
        "--no-worktrees",
        "--tui",
    ]


def _deaf_agent(pidfile: Path) -> str:
    """Ignores SIGTERM itself, so ending its group takes the whole grace
    and a kill run on the caller's thread holds that caller as long. It
    writes its pid once the trap is set, and nothing else."""
    return f"trap '' TERM; echo $$ > '{pidfile}'; while :; do sleep 0.2; done"


def _component(root: Path) -> tuple[str, str, str, str]:
    manifest = Manifest.load(root / "scripts" / "kstrl" / "manifest.json")
    comp = manifest.get_component(COMP)
    assert comp is not None
    return manifest.completed_at, comp.status, comp.failed_phase, comp.error or ""


def _end(proc: subprocess.Popen[bytes] | subprocess.Popen[str], *pids: int) -> None:
    for pid in pids:
        _kill_pid(pid)
    procs.kill_group(proc.pid)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def test_confirming_stop_in_the_tui_ends_the_agent_and_the_run(tmp_path: Path) -> None:
    """Test 12. The real TUI in a pty: ``q``, then ``y`` in the quit modal.
    The screen answers inside ``RESPONSE_BOUND_SECONDS``, so the kill did
    not run on the event loop; the agent's group, whose leader ignores
    SIGTERM, is gone within ``GRANDCHILD_BOUND_SECONDS``; the run exits 130
    with no second press; and the component is recorded as aborted. Every
    wait is on a signal the program gives (a pid file, text on the screen,
    the process's exit), never a sleep."""
    root = _repo(tmp_path)
    pidfile = tmp_path / "agent.pid"
    env = _env(_deaf_agent(pidfile))
    env.pop("KSTRL_NO_TUI")
    env["TERM"] = "xterm-256color"
    terminal = _Terminal()
    proc = terminal.start([sys.executable, "-c", _TTY_EXEC, *_tui_args(root)], root, env)
    agent: int | None = None
    try:
        agent = procs.read_pid(pidfile, timeout=START_FUSE_SECONDS)
        group = os.getpgid(agent)
        assert group != os.getpgrp()
        assert terminal.wait_for("Detach", START_FUSE_SECONDS), terminal.text()[-2000:]
        terminal.type(b"q")
        assert terminal.wait_for("stop the run?", START_FUSE_SECONDS), terminal.text()[-2000:]
        confirmed = time.monotonic()
        terminal.type(b"y")
        assert terminal.wait_for("shutting down", RESPONSE_BOUND_SECONDS), (
            f"the screen did not answer within {RESPONSE_BOUND_SECONDS}s of the confirm: "
            f"the stop blocked the TUI's event loop"
        )
        assert procs.wait_for_group_to_die(group, timeout=GRANDCHILD_BOUND_SECONDS), (
            f"the agent's group {group} was alive {GRANDCHILD_BOUND_SECONDS}s after the "
            f"stop was confirmed: the quit modal promises it is group-killed"
        )
        try:
            proc.wait(timeout=max(0.1, confirmed + EXIT_BOUND_SECONDS - time.monotonic()))
        except subprocess.TimeoutExpired:
            pytest.fail(f"ks still running {EXIT_BOUND_SECONDS}s after the stop was confirmed")
        assert proc.returncode == STOP_EXIT_CODE, terminal.text()[-2000:]
        completed_at, status, phase, error = _component(root)
        assert (status, phase) == ("failed", "aborted"), (status, phase, error)
        assert "stopped from TUI" in error
        assert completed_at, "the manifest was left running"
    finally:
        _end(proc, *([agent] if agent is not None else []))
        terminal.close()


@pytest.mark.parametrize("how", ["signal", "terminal-closed"])
def test_a_hangup_stops_the_run_and_records_the_abort(tmp_path: Path, how: str) -> None:
    """SIGHUP to a running ``ks factory``, sent by ``kill`` or by closing
    its terminal. The agent is gone within ``AGENT_BOUND_SECONDS`` and its
    SIGTERM-deaf grandchild within ``GRANDCHILD_BOUND_SECONDS``; ``ks``
    exits on its own rather than dying of the signal; the component is
    recorded as aborted by the hangup and the manifest is not left
    running."""
    root = _repo(tmp_path)
    pairs = tmp_path / "pairs"
    env = _env(_silent_agent(pairs))
    args = _factory_args(root, "1", "--no-worktrees")
    terminal: _Terminal | None = None
    if how == "terminal-closed":
        terminal = _Terminal()
        proc: subprocess.Popen[bytes] | subprocess.Popen[str] = terminal.start(
            [sys.executable, "-c", _TTY_EXEC, *args], root, env
        )
    else:
        proc = _ks(root, env, *args)
    try:
        _wait_for_agents(proc, pairs, 1)
        [(agent, grandchild)] = _pairs(pairs)
        hung_up = time.monotonic()
        if terminal is not None:
            terminal.close()
        else:
            os.kill(proc.pid, signal.SIGHUP)
        assert procs.wait_for_pid_to_die(agent, timeout=AGENT_BOUND_SECONDS), (
            f"agent pid {agent} alive {AGENT_BOUND_SECONDS}s after the hangup"
        )
        remaining = hung_up + GRANDCHILD_BOUND_SECONDS - time.monotonic()
        assert procs.wait_for_pid_to_die(grandchild, timeout=remaining), (
            f"grandchild pid {grandchild} alive {GRANDCHILD_BOUND_SECONDS}s after the hangup"
        )
        try:
            proc.wait(timeout=max(0.1, hung_up + EXIT_BOUND_SECONDS - time.monotonic()))
        except subprocess.TimeoutExpired:
            pytest.fail(f"ks still running {EXIT_BOUND_SECONDS}s after the hangup")
        assert proc.returncode != -signal.SIGHUP, "ks died of the hangup instead of handling it"
        # 120 is CPython's status when the last flush of stdout fails, which
        # a closed terminal makes it do (measured: 8 of 8 closed terminals
        # exited 120, 8 of 8 signals exited 130).
        allowed = {STOP_EXIT_CODE, 120} if terminal is not None else {STOP_EXIT_CODE}
        assert proc.returncode in allowed, proc.returncode
        completed_at, status, phase, error = _component(root)
        assert (status, phase) == ("failed", "aborted"), (status, phase, error)
        assert "received SIGHUP" in error
        assert completed_at, "the manifest was left running"
    finally:
        _end(proc, *[pid for pair in _pairs(pairs) for pid in pair])
        if terminal is not None:
            terminal.close()


def test_a_run_started_under_nohup_outlives_a_hangup(tmp_path: Path) -> None:
    """``nohup`` hands ``ks`` SIGHUP ignored, which says the run is to
    outlive the terminal. A hangup must not stop it: the agent is still
    alive ``AGENT_BOUND_SECONDS`` after it (a handled hangup ended it in
    0.03 s, measured) and ``ks`` is still running."""
    root = _repo(tmp_path)
    pairs = tmp_path / "pairs"
    proc = subprocess.Popen(
        [sys.executable, "-c", _NOHUP_EXEC, *_factory_args(root, "1", "--no-worktrees")],
        cwd=root,
        env=_env(_silent_agent(pairs)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_for_agents(proc, pairs, 1)
        [(agent, _grandchild)] = _pairs(pairs)
        os.kill(proc.pid, signal.SIGHUP)
        assert not procs.wait_for_pid_to_die(agent, timeout=AGENT_BOUND_SECONDS), (
            f"agent pid {agent} died after a hangup to a run started with SIGHUP ignored"
        )
        assert proc.poll() is None, f"ks exited {proc.returncode} on a hangup it was told to ignore"
    finally:
        _end(proc, *[pid for pair in _pairs(pairs) for pid in pair])
