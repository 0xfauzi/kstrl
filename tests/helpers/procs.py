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
    raises: BaseException | None = None,
) -> None:
    """Make ``kstrl.procgroup``'s ``ps`` return this, or raise this.

    One patch target in one place. Before #298 the fake was hand-rolled
    at six call sites across two files, and rewiring them when the
    reading moved is what showed the cost: a site missed keeps passing
    while measuring nothing, which is exactly what #292 exists to stop.
    """

    def fake(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=list(procgroup.PS_ARGV),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(procgroup.subprocess, "run", fake)


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
    an orphan behind that then matched the next run's ``pgrep``.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
