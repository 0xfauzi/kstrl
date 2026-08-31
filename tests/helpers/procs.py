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


def read_pid(pidfile: Path, timeout: float = 5.0) -> int:
    """The pid a fake agent wrote, waiting out the write.

    Tolerates the window where the file exists but is still empty: the
    shell creates it on the redirect and fills it a moment later, so a
    bare ``read_text`` races.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = pidfile.read_text().strip()
            if text:
                return int(text)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"pid file never appeared: {pidfile}")


def group_has_live_member(pgid: int) -> bool:
    """Whether any non-zombie process is still in process group ``pgid``.

    A zombie has already died and is only waiting to be reaped, so it is
    not an orphan and must not count as one. This is NOT
    ``os.killpg(pgid, 0)``: measured while writing #292, with the
    parent alive and not yet reaping, that call reported a SIGKILLed
    group as present for the whole 6s observation window and never
    converged, because a zombie stays signalable. Building a wait loop on
    it would trade a machine-state flake for a reap-timing one.

    Only the two columns the question needs are requested. Asking ``ps``
    for command lines as well measured 23.5ms per call against 11.6ms for
    this on a 895-process machine, and this runs inside a poll loop.

    RAISES rather than reports absence when it cannot see. A helper that
    answers "nothing is there" because ``ps`` failed is the same defect
    #292 exists to remove, one level down: measured with ``ps`` forced to
    return 127, this reported the caller's OWN live process group as dead
    and ``wait_for_group_to_die`` returned True, so the orphan assertion
    would have passed having measured nothing. ``ps`` is absent or
    restricted often enough to matter, on ``hidepid`` mounts and in
    minimal containers.

    So absence is only ever reported by a call that proved it can see
    something: the caller's own process group is alive by construction,
    and if that is missing from the output the output is not evidence.
    """
    out = subprocess.run(
        ["ps", "-A", "-o", "pgid=,stat="],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise AssertionError(
            f"ps failed (rc={out.returncode}): {out.stderr.strip()!r}. "
            f"Process-group liveness cannot be measured here, and "
            f"reporting 'no live member' would be a false negative."
        )

    ours = str(os.getpgrp())
    saw_own_group = False
    found = False
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == ours:
            saw_own_group = True
        if parts[0] == str(pgid) and not parts[1].startswith("Z"):
            found = True

    if not saw_own_group:
        raise AssertionError(
            f"ps did not report this process's own group ({ours}), so it "
            f"cannot be trusted to report the absence of group {pgid}. "
            f"Restricted or filtered ps output."
        )
    return found


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
