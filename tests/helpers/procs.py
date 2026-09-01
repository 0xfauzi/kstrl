"""Ask about a process the test itself started, never about the machine.

#292 is the lesson this module exists to write down. A shutdown test
asserted that ``pgrep -f "sleep 60"`` found nothing, which is a claim
about every process on the machine rather than about the agent the test
launched. ``pgrep -f`` matches a substring of the full command line, so
an unrelated ``sleep 600`` in another session failed it, since "sleep 60"
is a prefix of "sleep 600". Roughly six agents diagnosed that separately,
several with controlled A/B runs against a clean ref, because the failure
mode points nowhere near its cause; CI never saw it because a runner is a
clean machine.

The rule that follows, and the reason these helpers take a pid or a pgid
and never a pattern:

    A test may assert on a process it can name. It may not assert on
    what else the machine happens to be running.

``tests/test_process_scoping.py`` mechanises the negative half by AST-walking
this suite for ``pgrep``/``pkill``/``killall``/``pidof``, so the class
cannot come back quietly.

``read_pid`` is the positive half and predates #292: a fake agent writes
its own pid (``echo $$ > pidfile; exec sleep 60``) and the test reads it
back, which is exact, needs no search, and works across the process-pool
boundary where the agent is a grandchild.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, cast
from unittest.mock import MagicMock

import pytest

from kstrl import procgroup
from kstrl.procgroup import read_group_liveness


def read_pid(pidfile: Path, timeout: float = 5.0) -> int:
    """The pid a fake agent wrote, waiting out the write.

    Tolerates the window where the file exists but is still empty: the
    shell creates it on the redirect and fills it a moment later, so a
    bare ``read_text`` races.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = pidfile.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"pid file never appeared: {pidfile}")


def group_has_live_member(pgid: int) -> bool:
    """Whether any non-zombie process is still in process group ``pgid``.

    The reading, and the evidence for why it is not ``os.killpg(pgid, 0)``
    and why absence needs a control, lives in ``kstrl.procgroup``. #298
    centralised it: two copies of one ``ps`` parse with slightly
    different failure handling is how the suite's answer and the daemon's
    drift apart.

    What stays HERE is the POLICY, because it is the opposite of the
    daemon's. This RAISES when it cannot see, where
    ``serve.process_group_alive`` degrades. A test that answers "nothing
    is there" because ``ps`` failed has measured nothing and passed,
    which is the #292 defect one level down; a daemon that raises over a
    diagnostic stops doing its job. A test has nothing to protect.

    THE COMMON-MODE RISK, stated because sharing a reading with the code
    under test is a real cost and not an obviously free one. The suite's
    orphan assertions and the daemon's reap check now run the same parse,
    so one parse bug could blind both at once. Two arguments answer it,
    and neither is "it will not happen". First, a second hand-written
    parse would not have been independent of the failure it is supposed
    to catch: both copies read the same ``ps -A -o pgid=,stat=`` and both
    would break together on a column shift, which is the named risk.
    Second, ``read_group_liveness`` no longer rests on the parse alone
    for the dangerous direction. A "gone" now requires either a zombie
    row it demonstrably saw, or ``killpg`` ESRCH from the kernel - so a
    parse that DROPS a running row yields "cannot see" rather than a
    confident "gone", and this helper then raises rather than passing.
    """
    liveness = read_group_liveness(pgid)
    if liveness.live is None:
        raise AssertionError(liveness.reason)
    return liveness.live


@dataclass
class PsCall:
    """One intercepted ``ps`` spawn, and everything a test asserts on it."""

    #: What the ``Popen`` CONSTRUCTOR received. The deadlines are not here,
    #: because ``Popen`` does not take one.
    argv: list[str]
    kwargs: dict[str, object]
    #: One entry per ``communicate``, which IS where the bound lives.
    timeouts: list[float | None] = field(default_factory=list)
    #: How a test sees the abandoned-child path: the child was signalled,
    #: and its pipe pair was released rather than left to the collector.
    kills: int = 0
    closed: list[str] = field(default_factory=list)


#: What one ``communicate`` does with its deadline: answer with
#: ``(returncode, stdout, stderr)``, or raise.
Respond = Callable[[float | None], tuple[int, str, str]]


def fake_ps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    raises: Callable[[], BaseException] | None = None,
) -> list[PsCall]:
    """Answer ``kstrl.procgroup``'s ``ps`` with this. Returns the call log.

    ``raises`` is a FACTORY rather than an instance. A module-level
    exception object re-raised by every parametrized case accumulates a
    traceback frame and an ``__context__`` per raise, so it retains every
    test frame and its locals until interpreter exit.

    WHERE ``raises`` FIRES depends on what it is, because the two real
    failures do not happen in the same place. A missing binary raises out
    of the SPAWN; a timeout or a decode error raises out of the READ. The
    handling under test is ``read_group_liveness``'s disposal of the
    child, and it only has a child to dispose of in the second case, so
    faking a spawn failure at the read would exercise a path that cannot
    occur. ``OSError`` marks the first case; everything else is the
    second.
    """

    def respond_for() -> Respond:
        if raises is None:
            return lambda _timeout: (returncode, stdout, stderr)
        probe = raises()
        if isinstance(probe, OSError):
            raise probe

        def fail(_timeout: float | None) -> tuple[int, str, str]:
            # A FRESH instance per raise, which is the docstring's rule
            # applied one level down. The read path raises TWICE - once
            # from the read, once from the grace the kill is given - and
            # capturing one instance here instead of the factory made the
            # closure, the fake and the traceback a reference cycle:
            # measured over 200 reads with gc off, 27 objects retained
            # per read against 0 with this line.
            raise raises()

        return fail

    return _patch_ps_popen(monkeypatch, respond_for)


def unkillable_ps(monkeypatch: pytest.MonkeyPatch) -> list[PsCall]:
    """Answer ``ps`` with a child that never dies. Returns the call log.

    #309's fixture. A process in an uninterruptible sleep cannot be
    produced on demand in CI, so this fakes the only property that
    matters: every ``communicate`` burns its whole deadline and then
    raises ``TimeoutExpired``, and ``kill`` does nothing. The deadline is
    honoured rather than skipped so the test measures a real clock.
    """
    return _patch_ps_popen(monkeypatch, lambda: _wedge)


#: What a wedged fake sleeps when handed no deadline. Long enough that a
#: test asserting on a bound fails rather than hangs the suite, since
#: pytest has no per-test timeout here.
UNBOUNDED_WAIT_SECONDS = 30.0


def _wedge(timeout: float | None) -> NoReturn:
    """Spend the whole deadline, then report the call as never finishing.

    Both halves of the wedged fake go through here: the ``communicate``
    that has a deadline to burn, and the ``wait`` that has none and so
    sleeps far past any bound a test allows.
    """
    time.sleep(UNBOUNDED_WAIT_SECONDS if timeout is None else timeout)
    raise subprocess.TimeoutExpired(cmd=list(procgroup.PS_ARGV), timeout=timeout or 0.0)


@dataclass
class _FakePipe:
    """A pipe end that records only whether it was closed."""

    call: PsCall
    name: str

    def close(self) -> None:
        self.call.closed.append(self.name)


class _FakePs:
    """A ``ps`` child that never really ran."""

    def __init__(self, call: PsCall, respond: Respond) -> None:
        self._call = call
        self._respond = respond
        self.returncode: int | None = None
        self.stdout = _FakePipe(call, "stdout")
        self.stderr = _FakePipe(call, "stderr")

    def communicate(
        self,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        self._call.timeouts.append(timeout)
        self.returncode, out, err = self._respond(timeout)
        return out, err

    def kill(self) -> None:
        self._call.kills += 1

    def poll(self) -> int | None:
        """Non-blocking status, which is how ``_reap_abandoned`` sweeps.

        A fake that was abandoned reports None forever, so it stays on
        the register for the life of the test - which is what lets a test
        assert it got there at all.
        """
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """The unbounded wait #309 exists to keep production out of.

        Sleeping rather than raising is deliberate: an exception here
        could be swallowed by a fail-closed ``except``, and the defect
        being tested is a hang, so the fake reproduces a hang.
        """
        _wedge(timeout)

    def __enter__(self) -> _FakePs:
        return self

    def __exit__(self, *_exc: object) -> None:
        """Faithful to ``Popen.__exit__``, which is half of the #309 hang.

        It closes the pipes and then calls ``self.wait()`` with no
        deadline. Reproducing that is what makes the bound test a
        REGRESSION test: reinstating ``subprocess.run``, or wrapping the
        read in ``with Popen(...)``, then costs the same unbounded sleep
        here that it would cost the daemon, and the test fails on the
        clock rather than on a missing method. Verified by running the
        bound test against a reverted ``read_group_liveness``.
        """
        self.stdout.close()
        self.stderr.close()
        self.wait()


def _patch_ps_popen(
    monkeypatch: pytest.MonkeyPatch,
    respond_for: Callable[[], Respond],
) -> list[PsCall]:
    """Route ``PS_ARGV`` to a fake child, everything else to the real ``Popen``.

    The delegation is load-bearing, not politeness: ``procgroup.subprocess``
    IS the stdlib module, so ``setattr`` on it replaces ``Popen`` for the
    whole process, not for ``procgroup`` - and ``subprocess.run`` is built
    on ``Popen``, so an undelegated fake would answer every ``run`` in the
    suite too. Measured before this guard existed, when the seam was on
    ``run``: under ``fake_ps(stdout="1 Ss\n")`` a plain
    ``subprocess.run(["git", "rev-parse", "HEAD"])`` returned
    ``stdout='1 Ss\n'`` and ``args=['ps','-A','-o','pgid=,stat=']``. A test
    that combined the helper with a git or fixture subprocess call would
    have measured nothing and passed, which is the #292 class this module
    exists to prevent.

    The seam is ``Popen`` rather than ``run`` because #309 moved the read
    off ``run``; the module docstring of ``kstrl.procgroup`` says why.
    """
    real_popen = subprocess.Popen
    calls: list[PsCall] = []

    def fake(*args: object, **kwargs: object) -> object:
        argv = list(args[0]) if args and isinstance(args[0], (list, tuple)) else []
        if argv != list(procgroup.PS_ARGV):
            return real_popen(*args, **kwargs)  # type: ignore[arg-type]
        call = PsCall(argv=argv, kwargs=dict(kwargs))
        calls.append(call)
        # Appended BEFORE the responder is built, so a fake that fails at
        # the spawn is still a call the test can see.
        return _FakePs(call, respond_for())

    monkeypatch.setattr(procgroup.subprocess, "Popen", fake)
    return calls


def ps_is_readable() -> bool:
    """Whether `kstrl.procgroup` can actually measure on this machine.

    A test that asserts on `process_group_alive` needs this: where `ps`
    is absent or filtered the production call degrades to the signal
    probe, which counts a zombie as alive by design, so a #298 assertion
    would fail with a message pointing at kstrl rather than at the
    missing binary. Uses the caller's own group, which is alive by
    construction, so a False here is about `ps` and never about timing.
    """
    return read_group_liveness(os.getpgrp()).live is True


def dead_group(timeout: float = 10.0) -> int:
    """A process group that is spawned, killed and reaped. Returns its pgid.

    The pgid of a group that provably held a process and provably holds
    none now, which is what a test needs to assert that absence is still
    reportable. Lives here rather than being copied into each suite
    because a copied fixture is one that stops matching the helper it
    feeds.
    """
    child = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        pgid = os.getpgid(child.pid)
        kill_group(pgid)
        child.wait(timeout=timeout)
        return pgid
    finally:
        # kill_group swallows every OSError, so a SIGKILL that did not
        # land leaves `child.wait` to time out and the Popen dropped
        # unreaped with a real `sleep 30` still on the machine. That is
        # the orphan class #292 exists to stop, planted by the helper
        # written to stop it.
        if child.poll() is None:
            child.kill()
            child.wait(timeout=timeout)


def wait_for_group_to_die(pgid: int, timeout: float = 10.0) -> bool:
    """Poll until nothing live remains in ``pgid``; True if it went away."""
    deadline = time.monotonic() + timeout
    live = group_has_live_member(pgid)
    while live and time.monotonic() < deadline:
        time.sleep(0.1)
        live = group_has_live_member(pgid)
    return not live


def kill_group(pgid: int) -> None:
    """Best-effort SIGKILL of a process group, for test cleanup.

    A test that fails without this leaves the agent alive, and a stray
    ``sleep 60`` on a shared machine is precisely what #292 is about: the
    old test's own failures poisoned every later run of it. Observed
    while verifying that change, a failing run of the pre-fix file left
    an orphan behind that then matched the next run's machine-wide search.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def fake_popen(pid: object) -> subprocess.Popen[str]:
    """A mock ``Popen`` carrying ``pid`` and nothing else.

    The pgid guard's whole subject is a pid that is not a real one, so
    every suite testing it needs this shape and four of them had built it
    inline (#308). ``pid`` is deliberately ``object``: the case the guard
    exists for is a ``MagicMock`` pid, which coerces to 1 through
    ``MagicMock.__index__`` rather than raising, so a signature that only
    accepted ``int`` could not express the test that matters.
    """
    fake = MagicMock(spec=subprocess.Popen)
    fake.pid = pid
    return cast("subprocess.Popen[str]", fake)
