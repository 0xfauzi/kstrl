"""Deadline-enforced subprocess streaming shared by all agent adapters.

R0.1: every agent subprocess launches with ``start_new_session=True`` so it
owns its process group, and stdout is consumed on a reader thread so a child
that hangs WITHOUT emitting output still trips the wall-clock deadline (a
plain ``for line in proc.stdout`` only notices time passing when a line
arrives). On breach the whole group receives SIGTERM, then SIGKILL after a
grace period, so grandchildren (e.g. ``sh -c 'sleep 1000 & wait'``) die with
the direct child.

POSIX-first like the rest of the codebase: on platforms without
``os.killpg`` the kill degrades to signalling the direct child only.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

# Every adapter yields this line when its subprocess is killed on deadline
# breach. loop.py matches on the prefix to count timed-out iterations; it is
# a hint for error reporting, never a control-flow gate (a CustomAgent
# command could print the same string).
TIMEOUT_MESSAGE_PREFIX = "ERROR: agent timed out"

DEFAULT_TERM_GRACE_SECONDS = 5.0
DEFAULT_FINISH_WAIT_SECONDS = 10.0


def timeout_message(timeout: float | None) -> str:
    """Uniform timeout line yielded by every adapter."""
    return f"{TIMEOUT_MESSAGE_PREFIX} after {timeout}s"


class DeadlineStreamer:
    """Stream stdout lines from a subprocess under a wall-clock deadline.

    stdin is written on its own thread for the same reason stdout is read on
    one: a child that never reads stdin must not block the harness once the
    pipe buffer fills.

    The deadline is absolute: a child that keeps emitting output past it is
    still killed. ``timed_out`` records whether the deadline fired.
    """

    def __init__(
        self,
        cmd: list[str] | str,
        *,
        cwd: Path | None = None,
        shell: bool = False,
        stdin_text: str | None = None,
        timeout: float | None = None,
        term_grace: float = DEFAULT_TERM_GRACE_SECONDS,
    ) -> None:
        self.timed_out = False
        self._term_grace = term_grace
        self._deadline: float | None = (
            time.monotonic() + timeout if timeout and timeout > 0 else None
        )
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._proc = subprocess.Popen(
            cmd,
            shell=shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
        self._writer = threading.Thread(
            target=self._write_stdin, args=(stdin_text,), daemon=True,
        )
        self._writer.start()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def lines(self) -> Iterator[str]:
        """Yield stdout lines (newline-stripped) until EOF or breach.

        On deadline breach the process group is killed, ``timed_out`` is set,
        and iteration stops.
        """
        while True:
            if self._deadline is None:
                item = self._queue.get()
            else:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    self._breach()
                    return
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    self._breach()
                    return
            if item is None:
                return
            yield item

    def finish(self, timeout: float = DEFAULT_FINISH_WAIT_SECONDS) -> None:
        """Bounded wait for exit; escalate to a group kill on expiry.

        Replaces the unbounded ``proc.wait()`` the adapters used to call.
        """
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
        self._reader.join(timeout=1.0)
        self._writer.join(timeout=1.0)

    def kill(self) -> None:
        """SIGTERM the process group, wait a grace period, then SIGKILL."""
        self._signal_group(signal.SIGTERM)
        try:
            self._proc.wait(timeout=self._term_grace)
        except subprocess.TimeoutExpired:
            self._signal_group(signal.SIGKILL)
            try:
                self._proc.wait(timeout=self._term_grace)
            except subprocess.TimeoutExpired:
                # Unreapable child (e.g. stuck in uninterruptible IO).
                # Do not hang the harness on it; the leak is reported by
                # the caller via the timeout error path.
                pass

    def _breach(self) -> None:
        self.timed_out = True
        self.kill()

    def _signal_group(self, sig: signal.Signals) -> None:
        pid = self._proc.pid
        try:
            # `isinstance(pid, int)` is load-bearing: a mocked Popen's pid
            # coerces to 1 via MagicMock.__index__, and killpg(1, sig) is
            # kill(-1, sig) - "signal every process this user can" - which
            # kills the harness and its whole session. Same reason for the
            # pgid > 1 and not-our-own-group guards: start_new_session=True
            # makes the child its own group leader (pgid == child pid), so
            # any pgid at or below 1, or equal to ours, means something is
            # wrong and group-kill must not proceed.
            if (
                hasattr(os, "killpg")
                and isinstance(pid, int)
                and pid > 1
            ):
                pgid = os.getpgid(pid)
                if pgid > 1 and pgid != os.getpgrp():
                    os.killpg(pgid, sig)
                    return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        # Group already gone, non-POSIX platform, unsafe pgid, or a mocked
        # proc in tests: fall back to signalling the direct child only.
        try:
            if sig == signal.SIGKILL:
                self._proc.kill()
            else:
                self._proc.terminate()
        except (ProcessLookupError, OSError):
            pass

    def _write_stdin(self, stdin_text: str | None) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            if stdin_text:
                stdin.write(stdin_text)
            stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _read_stdout(self) -> None:
        stdout = self._proc.stdout
        try:
            if stdout is not None:
                for raw_line in stdout:
                    self._queue.put(raw_line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(None)
