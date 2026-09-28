"""Deadline-enforced subprocess streaming shared by all agent adapters.

R0.1: every agent subprocess launches with ``start_new_session=True`` so it
owns its process group, and stdout is consumed on a reader thread so a child
that hangs WITHOUT emitting output still trips the wall-clock deadline (a
plain ``for line in proc.stdout`` only notices time passing when a line
arrives). On breach the whole group receives SIGTERM, then SIGKILL when the
direct child exits or the grace period ends, whichever comes first, so a
grandchild dies with the direct child even when it ignores SIGTERM (#641).

POSIX-first like the rest of the codebase: on platforms without
``os.killpg`` the kill degrades to signalling the direct child only.

#642: every one of those cleanups runs inside the kstrl process that owns
the agent, so a SIGKILL, an OOM kill or a closed terminal ran none of them
and a silent agent kept running. The child this class starts is therefore
not the agent but ``kstrl/agents/leash.py``, which leads the group, starts
the agent inside it, and holds the read end of a pipe whose one write end
stays here. However this process ends, the kernel closes that write end,
the leash reads EOF, and it ends the group: SIGTERM, the grace, SIGKILL.
``_settle`` closes the write end on every orderly disposal. The leash needs
``pass_fds``, so an agent can no longer be started where there is none.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Iterator
from pathlib import Path

from kstrl.procdispose import close_quietly, reap_or_abandon
from kstrl.procgroup import safe_pgid, signal_group, signal_process_tree

# Every adapter yields this line when its subprocess is killed on deadline
# breach. loop.py matches on the prefix to count timed-out iterations; it is
# a hint for error reporting, never a control-flow gate (a CustomAgent
# command could print the same string).
TIMEOUT_MESSAGE_PREFIX = "ERROR: agent timed out"

DEFAULT_TERM_GRACE_SECONDS = 5.0
DEFAULT_FINISH_WAIT_SECONDS = 10.0

#: The program every agent is started under (#642). Run by path with
#: ``-I -S``, so it needs nothing but the standard library.
LEASH_PATH = str(Path(__file__).with_name("leash.py"))


def timeout_message(timeout: float | None) -> str:
    """Uniform timeout line yielded by every adapter."""
    return f"{TIMEOUT_MESSAGE_PREFIX} after {timeout}s"


# PR B (TUI rewrite): live streamers register here so a shutdown signal
# can group-kill every in-flight agent subprocess. WeakSet: a streamer
# that was garbage collected is by definition no longer streaming.
_ACTIVE: weakref.WeakSet[DeadlineStreamer] = weakref.WeakSet()


def kill_active_process_groups() -> int:
    """SIGTERM->grace->SIGKILL every live agent process group.

    The shutdown path for both worker SIGTERM forwarding (pool mode)
    and the parent's inline-executor abort. Returns the number of
    streamers signalled; never raises.
    """
    count = 0
    for streamer in list(_ACTIVE):
        try:
            streamer.kill()
            count += 1
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
    return count


def _read_until_closed(fd: int) -> bytes:
    """Everything the leash writes on its status pipe, then close it.

    Returns at EOF, which comes when the leash closes its end after a clean
    start or exits. No deadline, for the reason CPython's own ``Popen``
    reads its exec-failure pipe without one: the other end is a process
    this call just started, and it either writes or exits.
    """
    chunks: list[bytes] = []
    try:
        while chunk := os.read(fd, 256):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _spawn_error(failure: bytes, program: str) -> OSError:
    """The ``OSError`` a direct ``Popen`` of the agent would have raised.

    ``errno <n>`` is rebuilt with ``OSError``'s own errno mapping, so an
    ENOENT is a ``FileNotFoundError``, which ``claude_code`` reports as a
    missing CLI. Anything else is the leash refusing to start.
    """
    text = failure.decode("ascii", "replace")
    number = text.removeprefix("errno ")
    if number != text and number.isdigit():
        return OSError(int(number), os.strerror(int(number)), program)
    return OSError(f"the agent leash did not start {program!r}: {text}")


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
        self._disposed = False
        self._term_grace = term_grace
        self._deadline: float | None = (
            time.monotonic() + timeout if timeout and timeout > 0 else None
        )
        self._queue: queue.Queue[str | None] = queue.Queue()
        argv = [cmd] if isinstance(cmd, str) else list(cmd)
        if shell:
            argv = ["/bin/sh", "-c", *argv]
        lifeline_read, self._lifeline = os.pipe()
        status_read, status_write = os.pipe()
        try:
            self._proc = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    LEASH_PATH,
                    str(lifeline_read),
                    str(status_write),
                    str(term_grace),
                    "--",
                    *argv,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                cwd=cwd,
                start_new_session=True,
                pass_fds=(lifeline_read, status_write),
            )
        except BaseException:
            os.close(self._lifeline)
            os.close(status_read)
            raise
        finally:
            os.close(lifeline_read)
            os.close(status_write)
        failure = _read_until_closed(status_read)
        if failure:
            os.close(self._lifeline)
            reap_or_abandon(self._proc, term_grace)
            close_quietly(self._proc.stdin)
            close_quietly(self._proc.stdout)
            raise _spawn_error(failure, argv[0])
        # The group is read NOW, while the child is certainly alive. Once
        # the child has exited, even as an unreaped zombie, getpgid on its
        # pid fails (ESRCH, measured on macOS), so a group looked up at
        # kill time is lost in exactly the case where members outlive it
        # (#641).
        self._pgid = safe_pgid(self._proc)
        self._writer = threading.Thread(
            target=self._write_stdin,
            args=(stdin_text,),
            daemon=True,
        )
        self._writer.start()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        _ACTIVE.add(self)

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

    def _settle(self) -> None:
        """Join the two pipe threads and leave the registry clean.

        The tail of all three disposals, and the only place ``_disposed``
        is set. The ``if self._disposed: return`` that makes a disposal
        idempotent stays at each CALL SITE rather than moving in here,
        because :meth:`_breach` has one statement that must run on every
        call: a deadline that fires after ``finish`` still has to record
        ``timed_out``, which is what every adapter reads to decide
        whether the run produced a usable answer.

        The joins are bounded because a thread blocked on an unreapable
        grandchild's pipe never returns, and a shutdown that waits for it
        is a hang rather than a shutdown.
        """
        self._disposed = True
        self._reader.join(timeout=1.0)
        self._writer.join(timeout=1.0)
        _ACTIVE.discard(self)
        # The leash's lifeline. Closed on every disposal, so a leash whose
        # agent outlived the kill still ends its group.
        try:
            os.close(self._lifeline)
        except OSError:
            pass

    def finish(self, timeout: float = DEFAULT_FINISH_WAIT_SECONDS) -> None:
        """Bounded wait for exit; escalate to a group kill on expiry.

        Replaces the unbounded ``proc.wait()`` the adapters used to call.
        The ORDERLY disposal: the child is expected to be on its way out,
        so it is given ``timeout`` to leave on its own.
        :meth:`close` is the other one, for a consumer that walked away.
        """
        if self._disposed:
            return
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
        except BaseException:
            # The rule this module's own guard mechanises for `verify`,
            # `serve` and `procgroup`, applied to the fifth disposal. A
            # timeout is not the only way out of a wait: a
            # KeyboardInterrupt in the orderly grace used to leave the
            # child running with nobody left to read it.
            self.kill()
            raise
        finally:
            self._settle()

    def close(self) -> None:
        """Dispose of a child whose consumer WALKED AWAY. Idempotent.

        Every adapter's ``run`` is a generator, and a generator can be
        abandoned mid-yield: ``decompose.collect_agent_output`` raises
        ``AgentOutputTooLarge`` from inside ``for line in agent.run(...)``
        and does exactly that today. Nothing then called ``finish``, so
        the child was never killed, never reaped and never deregistered
        from ``_ACTIVE`` - and it was not even collected, because the
        reader thread's bound method holds a strong reference to the
        streamer that the ``WeakSet`` deliberately does not. The agent
        CLI ran to its own completion, spending tokens, after the caller
        had decided to abort it.

        A ``finally`` in the generator is what reaches this: CPython
        throws ``GeneratorExit`` at the suspended ``yield`` when the
        generator object is closed or collected, and a ``finally`` that
        does not itself yield runs to completion there.

        KILL RATHER THAN WAIT, which is the whole difference from
        :meth:`finish`. Nobody is going to read this child's output
        again, so giving it ten seconds to finish work whose result is
        already discarded is ten seconds of spend for nothing. The
        SIGTERM grace inside :meth:`kill` is still honoured.

        Idempotent because the orderly path calls ``finish`` and then
        unwinds through the same ``finally``: the second call must not
        start a second kill.

        WHAT THIS COSTS, because it is not free and the caller does not
        write the moment it happens. Measured: after an orderly
        ``finish`` this returns in 1.6 microseconds, the ``_disposed``
        flag short-circuiting it. On the abandonment path with a child
        that honours SIGTERM, 1.3ms. With one that traps it and a
        ``term_grace`` of 5s, 5.01s, and up to about 12s worst case - the
        SIGTERM grace, then ``reap_or_abandon``'s, then two bounded
        joins. A generator's ``finally`` can be run by the collector at
        an arbitrary allocation point, so that block can land somewhere
        the caller did not write it. It is still the right trade: what it
        replaces is an agent CLI running to completion and spending
        tokens on an answer nobody will read.
        """
        if self._disposed:
            return
        try:
            self.kill()
        finally:
            self._settle()

    def kill(self) -> None:
        """SIGTERM the process group, wait a grace period, then SIGKILL it.

        The SIGKILL goes to the group whether the direct child left inside
        the grace or not (#641). A shell that honours SIGTERM exits at
        once, and a member of its group that ignores SIGTERM - a test
        server, a watcher, a ``trap '' TERM`` - kept running beside every
        later iteration when the SIGKILL was sent only on a timed-out
        wait. The grace is not waited a second time: the group is signalled
        by the id read at spawn, which stays valid after the direct child
        is reaped.

        The last leg is :func:`kstrl.procdispose.reap_or_abandon` rather
        than a third bare ``wait`` (#326). An unreapable child - stuck in
        uninterruptible IO, or with a grandchild outside the group
        holding the pipes - used to be dropped here with nothing left
        holding its pid but ``Popen.__del__``, which under
        ``PYTHONWARNINGS=error`` raises before it registers anything and
        leaves a zombie for the life of the process.

        ``reap_or_abandon`` and not ``drain_or_abandon``, and that is the
        measured half: this class reads stdout on ``self._reader`` and
        writes stdin on ``self._writer``, and closing either end from
        THIS thread waits on the io lock the blocked thread is holding -
        3.005s and still waiting, in both directions. Those two ends are
        closed by the threads that own them instead.
        """
        self._signal_group(signal.SIGTERM)
        try:
            self._proc.wait(timeout=self._term_grace)
        except subprocess.TimeoutExpired:
            # The direct child outlived the grace; the SIGKILL below takes
            # it with the rest of the group.
            pass
        except BaseException:
            # MEASURED, and this is the escape all three disposals
            # shared. A SIGINT delivered 1.0s into a 5s grace on a child
            # that traps SIGTERM left the group alive, `_disposed` False
            # and the streamer still in `_ACTIVE`, and the
            # KeyboardInterrupt displaced the caller's own exception on
            # the way out. The escalation now runs whatever ended the
            # grace, so the child is gone before the exception continues;
            # the `raise` is bare, so nothing about it is changed.
            #
            # The two lines are written out rather than lifted into a
            # helper, and that is deliberate: `base_disposal_guards`
            # requires the disposal call to be lexically inside the
            # handler, so a helper here would satisfy the rule's INTENT
            # and be invisible to the rule. Two duplicated lines are the
            # price of this module being mechanised like the other three
            # rather than exempted by a comment.
            self._signal_group(signal.SIGKILL)
            reap_or_abandon(self._proc, self._term_grace)
            raise
        self._signal_group(signal.SIGKILL)
        reap_or_abandon(self._proc, self._term_grace)

    def _breach(self) -> None:
        """Deadline hit: kill the group and leave the registry clean.

        The deregistration belongs here rather than only in ``finish``
        because every adapter returns early on ``timed_out`` without
        calling it, so a killed streamer used to sit in ``_ACTIVE``
        until garbage collection and a later
        ``kill_active_process_groups`` would signal a corpse. ``kill``
        has already waited out the SIGTERM/SIGKILL grace, so the child
        is gone and the pipe is closed; the joins are bounded for the
        unreapable-grandchild case above. ``discard`` is idempotent, so
        a caller that does reach ``finish`` stays correct.
        """
        self.timed_out = True
        if self._disposed:
            return
        try:
            self.kill()
        finally:
            self._settle()

    def _signal_group(self, sig: signal.Signals) -> None:
        """Signal the group, degrading to the direct child.

        The group is the one :func:`kstrl.procgroup.safe_pgid` read at
        spawn, sent through :func:`kstrl.procgroup.signal_group`, which
        refuses the same pgids ``safe_pgid`` does. When there is no such
        id, or the group is gone or refused, this forwards to
        :func:`kstrl.procgroup.signal_process_tree`, kept as a method
        because ``tests/test_timeout_enforcement.py`` pins the guard
        through it. #308 lifted the pid/pgid guard into ``procgroup`` and
        left the routine here and in ``verify``, so ``os.killpg`` stayed
        spelled in three modules; #329 is the cost of that, and the whole
        routine now has one home.
        """
        if self._pgid is not None and signal_group(self._pgid, sig).sent:
            return
        signal_process_tree(self._proc, sig)

    def _write_stdin(self, stdin_text: str | None) -> None:
        """Feed the child its stdin, then close OUR end of that pipe.

        The close is in a ``finally`` because this thread is the only
        one that may do it: measured, closing a pipe end from a second
        thread waits on the io lock the owning thread holds through a
        blocking write, and had not returned after 3.005s. So a write
        that raises must still hand the descriptor back here rather than
        leave it to whatever kills the child (#326).
        """
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            if stdin_text:
                stdin.write(stdin_text)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            close_quietly(stdin)

    def _read_stdout(self) -> None:
        """Drain the child's stdout onto the queue, then close our end.

        Same ownership rule as :meth:`_write_stdin`, and the same
        measurement behind it. ``procdispose.reap_or_abandon``, which is
        what disposes of an unreapable child here, deliberately closes
        no pipe for exactly this reason.
        """
        stdout = self._proc.stdout
        try:
            if stdout is not None:
                for raw_line in stdout:
                    self._queue.put(raw_line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            # THE SENTINEL GOES FIRST, and the reasoning here was wrong
            # twice before it went that way. `lines()` waits on
            # `self._queue.get()` with NO deadline when the caller passed
            # no timeout, so a reader thread that ends without posting
            # `None` hangs the harness for good - measured at 600s on a
            # real run, when `close_quietly` was `suppress(OSError,
            # ValueError)` and a MagicMock stdout raised AttributeError
            # on close.
            #
            # Widening the suppression to `Exception` fixed that case and
            # was then argued, in this comment, as making the ORDER safe.
            # It does not: `suppress(Exception)` does not cover
            # BaseException, so a KeyboardInterrupt, a SystemExit, a
            # GeneratorExit or a MemoryError in the close still skips a
            # `put` written after it. Posting first loses the sentinel
            # for none of them, because a `put` on an unbounded Queue
            # does not block and has nothing to raise. Breadth and order
            # are not two arguments for one property: breadth stops the
            # ordinary case, order stops the rest.
            self._queue.put(None)
            close_quietly(stdout)
