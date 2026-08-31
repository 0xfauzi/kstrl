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
from pathlib import Path

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


def fake_ps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    raises: Callable[[], BaseException] | None = None,
) -> list[dict[str, object]]:
    """Answer ``kstrl.procgroup``'s ``ps`` with this. Returns the call log.

    DELEGATES every other command to the real ``subprocess.run``. That is
    load-bearing, not politeness: ``procgroup.subprocess`` IS the stdlib
    module, so ``setattr`` on it replaces ``run`` for the whole process,
    not for ``procgroup``. Measured before this guard existed: under
    ``fake_ps(stdout="1 Ss\n")`` a plain
    ``subprocess.run(["git", "rev-parse", "HEAD"])`` returned
    ``stdout='1 Ss\n'`` and ``args=['ps','-A','-o','pgid=,stat=']``. Any
    test that combined this helper with a git or fixture subprocess call
    would have measured nothing and passed, which is the #292 class this
    module exists to prevent.

    ``raises`` is a FACTORY rather than an instance. A module-level
    exception object re-raised by every parametrized case accumulates a
    traceback frame and an ``__context__`` per raise, so it retains every
    test frame and its locals until interpreter exit.

    The returned list records the args and kwargs each intercepted ``ps``
    call received, so a test can assert on what was passed - the
    ``timeout=`` in particular, which is the only bound stopping a wedged
    ``ps`` from hanging the daemon and which no test could otherwise see.
    """
    real_run = subprocess.run
    calls: list[dict[str, object]] = []

    def fake(*args: object, **kwargs: object) -> object:
        argv = list(args[0]) if args and isinstance(args[0], (list, tuple)) else []
        if argv != list(procgroup.PS_ARGV):
            return real_run(*args, **kwargs)  # type: ignore[arg-type]
        calls.append({"argv": argv, "kwargs": dict(kwargs)})
        if raises is not None:
            raise raises()
        return subprocess.CompletedProcess(
            args=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(procgroup.subprocess, "run", fake)
    return calls


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
    pgid = os.getpgid(child.pid)
    kill_group(pgid)
    child.wait(timeout=timeout)
    return pgid


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
