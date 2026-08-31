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

WHEN A "GONE" IS TRUSTWORTHY, which is the whole safety argument.
``ps`` may not show everything: a descendant that changed uid, a
restricted listing, a truncated read. Absence from the listing is
therefore not on its own evidence of absence from the machine, and a
wrong "gone" is the dangerous direction - ``serve`` releases the item and
a second factory starts on a repo the first is still writing to (#186
F1). So a "gone" is only ever returned with a reason it can be trusted:

* ``ps`` listed at least one row for the group and every one is a
  zombie. The listing demonstrably CAN see this group, so its verdict on
  that group's members is evidence. This is the #298 case.
* ``ps`` listed no row for the group, and ``killpg(pgid, 0)`` raises
  ESRCH. That is the kernel saying the group holds no process at all,
  which is a stronger fact than a listing's silence and is immune to
  filtering.

Anything else, meaning no row and a kernel that says the group is
occupied, is reported as "cannot see" rather than as absence.

The control this REPLACED did not work, and it is written down because
the docstring claimed it did. It checked that the caller's own process
group appeared in the listing. ``subprocess.run`` does not ``setpgid``,
so the ``ps`` child itself runs in the caller's group and ``ps -A``
always lists it: measured on this tree, our own pgid appeared four times
in every listing, the last row being ``ps``. The check was therefore
satisfied by construction on every successful call and could never fire.
``hidepid`` does not hide a caller's own uid either, so it did not even
cover the case it named. Measured against a listing with one live group
filtered out, the old rule returned a CONFIDENT "gone" for a group that
was still running; the rule above returns "cannot see".

COST, and the row trim that was measured and REJECTED. Only the two
columns the question needs are requested: asking for command lines as
well measured 23.5ms per call against 11.6ms for this on a 895-process
machine (#292), re-measured for #298 at 11.29ms over 50 calls on a
913-process machine against 0.00089ms for the signal probe. The kernel
control above costs 0.00054ms and is paid only on the "gone" path.
Trimming ROWS is the larger factor and is deliberately not done: ``ps -g
<pgid>,<ours>`` measured 2.07ms against 14.52ms on a 1011-process
machine, a 7x cut, but ``-g`` selects by SESSION, and Linux procps
documents it as session or effective group NAME. A process-group id is
not generally a session id, so on Linux that listing would come back
without the target, which is exactly the filtered case above and would
now cost a spurious "cannot see" on every call.

That cost is affordable because the production path is not hot:
``serve.terminate_process_group`` asks once per timed-out run, on the way
out. Measured across the whole suite, every ``wait_for_group_to_die``
call converged on its first poll: 14 real ``ps`` forks totalling 275ms
against a 246.5s suite, 0.11% of wall clock.

POSIX only. ``ps`` group listings and ``killpg`` do not exist on Windows.
The production caller reaches this only behind ``serve._safe_pgid``,
which returns None without ``os.killpg``; the test helper has no such
gate and its suite skips on Windows.

This module is the ONLY place in the tree that shells out to ``ps``, and
``tests/test_procgroup.py`` fails on a second one. A rule with no
mechanism is not a plan: the argument for centralising the parse is that
two copies drift, and nothing but a net stops a third landing.
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

    #: True: at least one non-zombie member. False: none, and the answer
    #: earned one of the two trust conditions in the module docstring.
    #: None: nothing was measured, and ``reason`` says what went wrong.
    live: bool | None
    reason: str = ""


def read_group_liveness(pgid: int) -> GroupLiveness:
    """Whether any non-zombie process is in group ``pgid``.

    Absence is only ever reported by a call that earned the right to
    report it. Which two conditions those are, and the measurement
    showing the control this replaced was satisfied by construction, are
    in the module docstring.
    """
    try:
        out = subprocess.run(
            PS_ARGV,
            capture_output=True,
            # Pinned rather than left to the locale, and non-decodable
            # bytes are replaced rather than raised. A decode error here
            # is a ValueError, which would escape a fail-closed
            # ``except OSError`` and take the daemon down over a
            # diagnostic (the repo's #291 lesson). Caught below as well,
            # so a future edit cannot reintroduce it.
            encoding="utf-8",
            errors="replace",
            timeout=PS_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return GroupLiveness(None, f"ps failed to run ({exc!r}). {_UNMEASURABLE}")
    if out.returncode != 0:
        return GroupLiveness(
            None,
            f"ps failed (rc={out.returncode}): {out.stderr.strip()!r}. {_UNMEASURABLE}",
        )

    rows, running = _count_group_rows(out.stdout, pgid)
    if running:
        return GroupLiveness(True)
    if rows:
        # ps can demonstrably see this group, and everything it holds is
        # a zombie. #298.
        return GroupLiveness(False)
    if _kernel_says_group_is_empty(pgid):
        return GroupLiveness(False)
    return GroupLiveness(
        None,
        f"ps listed no process in group {pgid}, but the kernel reports "
        f"that group is not empty, so the listing does not show every "
        f"process (a descendant that changed uid, or a restricted ps). "
        f"{_UNMEASURABLE}",
    )


def _count_group_rows(stdout: str, pgid: int) -> tuple[int, int]:
    """(rows ``ps`` listed for ``pgid``, how many are not zombies).

    Returned positionally, so the order is pinned by direct tests in
    ``tests/test_procgroup.py`` rather than by the type checker: both
    fields are ``int`` and a swap would type-check cleanly.
    """
    want = str(pgid)
    rows = 0
    running = 0
    for line in stdout.splitlines():
        parts = line.split()
        # A row with no state column would IndexError below. Real ps does
        # not emit one; a filtered or truncated listing might.
        if len(parts) < 2:
            continue
        if parts[0] != want:
            continue
        rows += 1
        # "Z" is the zombie state on both macOS and Linux, and flags may
        # follow it ("Z+", "Zl"), so match the prefix rather than the cell.
        if not parts[1].startswith("Z"):
            running += 1
    return rows, running


def _kernel_says_group_is_empty(pgid: int) -> bool:
    """True ONLY when the kernel says no process at all is in the group.

    ESRCH is the one conclusive answer. Success and ``EPERM`` both mean
    something is there, and any other ``OSError`` means the question was
    not answered. Neither is emptiness, and reading them as emptiness is
    the false negative this control exists to prevent.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def signal_probe_alive(pgid: int) -> bool:
    """Whether a signal to ``pgid`` would find a target. Zombies COUNT.

    The pre-#298 reading, kept because it is the only thing left when
    ``ps`` gives no answer at all, and because a test needs it as the
    control that proves a zombie window was real rather than a group that
    quietly went away. ``PermissionError`` means something is there
    refusing us, which is the closest to "alive" a signal can report, and
    on a zombie-only group that is measurably the branch taken.

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
