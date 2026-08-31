"""Whether a process group still holds anything RUNNING.

#298. ``os.killpg(pgid, 0)`` answers a different question from the one
every caller here is asking. It asks "would a signal be delivered", and a
ZOMBIE - a process that has already died and is only waiting for its
parent to call ``wait`` - is still a signal target. So a group holding
nothing but a corpse reports as live.

Measured on this machine, on a real tree whose parent deliberately never
reaps: after ``killpg(pgid, SIGKILL)`` the signal probe reported the
group as present for all 45 samples across a 6 second window and never
converged, while a ``ps`` read with zombies excluded reported it gone on
the first sample. The branch taken was ``PermissionError``, which
``serve.process_group_alive`` mapped to True on the reasoning that a
refused signal proves something is there to refuse it. That reasoning is
sound about signals and wrong about running processes.

The question actually being asked, by both the daemon's reap check and
the suite's orphan assertions, is "is anything in this group still
executing". A zombie is not. So this module asks ``ps`` for group
membership and excludes state ``Z``.

WHY A TRI-STATE. ``ps`` can fail, and it can be filtered so that its
silence about a group is not evidence of absence. The two callers want
OPPOSITE things when that happens, so neither answer can be baked in
here: a test must fail loudly rather than pass having measured nothing
(#292), while the daemon must keep running and fall back to the degraded
probe. This returns "cannot see" and lets each caller choose.

COST, and the row trim that was measured and REJECTED. Only the two
columns the question needs are requested: asking for command lines as
well measured 23.5ms per call against 11.6ms for this on a 895-process
machine (#292), re-measured for #298 at 11.29ms over 50 calls on a
913-process machine against 0.00089ms for the signal probe. Trimming
ROWS is the larger factor and is deliberately not done: ``ps -g
<pgid>,<ours>`` measured 2.07ms against 14.52ms on a 1011-process
machine, a 7x cut, but ``-g`` selects by SESSION, and Linux procps
documents it as session or effective group NAME. A process-group id is
not generally a session id, so on Linux that listing would come back
holding our own group and not the target, which this module reads as
"the target is gone". That is a false negative in the one direction it
must never fail, and a 7x measured on macOS alone is not a reason to
ship one into the daemon's reap check.

That cost is affordable because the production path is not hot:
``serve.terminate_process_group`` asks once per timed-out run, on the way
out. Measured across the whole suite, every ``wait_for_group_to_die``
call converged on its first poll: 14 real ``ps`` forks totalling 275ms
against a 246.5s suite, 0.11% of wall clock.

POSIX only. ``ps`` group listings and ``os.getpgrp`` do not exist on
Windows. The production caller reaches this only behind
``serve._safe_pgid``, which returns None without ``os.killpg``; the test
helper has no such gate and its suite skips on Windows.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

#: The two columns the question needs and no more. See COST above.
PS_ARGV = ("ps", "-A", "-o", "pgid=,stat=")

#: A bound on how long a diagnostic may stall the caller. 440x the
#: 11.29ms measured above, so it cannot fire on a slow machine; a ``ps``
#: that takes longer than this is wedged, which is exactly the case where
#: the caller wants "cannot see" rather than a hang.
PS_TIMEOUT_SECONDS = 5.0

#: Said once, because two branches report it and reflowed copies of one
#: sentence are how the two answers drift apart.
_UNMEASURABLE = (
    "Process-group liveness cannot be measured here, and reporting "
    "'no live member' would be a false negative."
)


@dataclass(frozen=True)
class GroupLiveness:
    """Whether a group holds a running process, or why that is unknown."""

    #: True: at least one non-zombie member. False: none, and ``ps`` was
    #: trustworthy when it said so. None: ``ps`` could not be trusted, so
    #: nothing was measured and ``reason`` says what went wrong.
    live: bool | None
    reason: str = ""


def read_group_liveness(pgid: int) -> GroupLiveness:
    """Ask ``ps`` whether any non-zombie process is in group ``pgid``.

    Absence is only ever reported by a call that proved it can see
    something. The caller's own process group is alive by construction,
    so if that group is missing from the output then the output is
    filtered and its silence about ``pgid`` means nothing. This is the
    ``hidepid`` case, and #292 measured the alternative: with ``ps``
    forced to return 127, a version without this guard reported the
    caller's OWN live group as dead.
    """
    try:
        out = subprocess.run(
            PS_ARGV,
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GroupLiveness(None, f"ps failed to run ({exc!r}). {_UNMEASURABLE}")
    if out.returncode != 0:
        return GroupLiveness(
            None,
            f"ps failed (rc={out.returncode}): {out.stderr.strip()!r}. {_UNMEASURABLE}",
        )

    ours = os.getpgrp()
    saw_own_group, found = _scan(out.stdout, pgid, ours)
    if not saw_own_group:
        return GroupLiveness(
            None,
            f"ps did not report this process's own group ({ours}), so it "
            f"cannot be trusted to report the absence of group {pgid}. "
            f"Restricted or filtered ps output.",
        )
    return GroupLiveness(found)


def _scan(stdout: str, pgid: int, ours: int) -> tuple[bool, bool]:
    """(was our own group present, is a non-zombie member of ``pgid`` present).

    Returned positionally, so the order is pinned by direct tests in
    ``tests/test_procgroup.py`` rather than by the type checker: both
    fields are ``bool`` and a swap here would type-check cleanly.
    """
    want = str(pgid)
    mine = str(ours)
    saw_own_group = False
    found = False
    for line in stdout.splitlines():
        parts = line.split()
        # A row with no state column would IndexError below. Real ps does
        # not emit one; a filtered or truncated listing might.
        if len(parts) < 2:
            continue
        group, state = parts[0], parts[1]
        saw_own_group = saw_own_group or group == mine
        # "Z" is the zombie state on both macOS and Linux, and flags may
        # follow it ("Z+", "Zl"), so match the prefix rather than the cell.
        found = found or (group == want and not state.startswith("Z"))
    return saw_own_group, found


def signal_probe_alive(pgid: int) -> bool:
    """Whether a signal to ``pgid`` would find a target. Zombies COUNT.

    The pre-#298 reading, kept because it is the only thing left when
    ``ps`` gives no answer at all, and because a test needs it as the
    control that proves a zombie window was real rather than a group that
    quietly went away. ``PermissionError`` means something is there
    refusing us, which is the closest to "alive" a signal can report -
    and on a zombie-only group that is measurably the branch taken.

    Prefer ``read_group_liveness``. This answers the question #298 was
    about getting wrong.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
