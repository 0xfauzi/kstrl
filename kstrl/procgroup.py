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

WHEN A "GONE" IS TRUSTWORTHY, which is the whole safety argument. A
wrong "gone" is the dangerous direction: ``serve`` releases the item and
a second factory starts on a repo the first is still writing to (#186
F1). ``ps`` may not show everything - a ``hidepid`` mount hides
individual PROCESSES owned by other uids, so a group can show one
visible zombie while hiding a running descendant that changed uid. That
means "every row I saw for this group is a zombie" is NOT on its own a
safe conclusion; it is only safe once the listing is known to be
complete. So a "gone" needs both a listing that can be trusted and one
of two positive findings:

* THE LISTING IS COMPLETE. ``ps -A`` reported pid 1. Under ``hidepid``
  the caller sees only its own uid's processes, and pid 1 belongs to
  root; if we are root, nothing is hidden from us in the first place.
  Either way, seeing pid 1 rules out a uid-filtered view. Measured on
  this tree: pid 1 appears as ``['1', '1', 'Ss']`` in every listing while
  running as uid 501. This costs nothing - ``pid=,pgid=,stat=`` measured
  16.11ms per call against 16.10ms for ``pgid=,stat=`` on a 945-process
  machine, inside the noise.
* Then either every row listed for the group is a zombie, or the group
  has no rows at all AND ``killpg(pgid, 0)`` raises ESRCH - the kernel
  saying the group holds no process, which no listing filter can fake.

Anything else is "cannot see".

TWO CONTROLS THAT DID NOT WORK, written down because each was claimed in
this docstring before it was measured. The first checked that the
caller's own process group appeared in the listing; ``subprocess.run``
does not ``setpgid``, so the ``ps`` child runs in the caller's group and
``ps -A`` always lists it - our own pgid appeared four times in every
listing, the last row being ``ps`` itself. Satisfied by construction, it
could never fire. The second was "every listed row is a zombie, so the
listing can see this group": true, but seeing SOME of a group is not
seeing ALL of it, which is exactly the ``hidepid`` case above.

COST, and the row trim that was measured and REJECTED. Only the three
columns the question needs are requested: asking for command lines as
well measured 23.5ms per call against 11.6ms for two columns on a
895-process machine (#292). The kernel control costs 0.00054ms and is
paid only on the "no rows" path. Trimming ROWS is the larger factor and
is deliberately not done: ``ps -g <pgid>,<ours>`` measured 2.07ms against
14.52ms on a 1011-process machine, a 7x cut, but ``-g`` selects by
SESSION, and Linux procps documents it as session or effective group
NAME. A process-group id is not generally a session id, so on Linux that
listing would come back without the target, which is the filtered case
above and would now cost a spurious "cannot see" on every call.

That cost is affordable because the production path is not hot:
``serve.terminate_process_group`` asks once per timed-out run, on the way
out. Measured across the whole suite, every ``wait_for_group_to_die``
call converged on its first poll: 14 real ``ps`` forks totalling 275ms
against a 246.5s suite, 0.11% of wall clock.

POSIX only. ``ps`` group listings and ``killpg`` do not exist on Windows.

WHAT THE ``ps`` TIMEOUT DOES NOT COVER, stated because an earlier version
of this docstring claimed a bound it does not have. ``PS_TIMEOUT_SECONDS``
bounds a ``ps`` that is slow but killable. It does NOT bound one wedged
in an uninterruptible read (a hung NFS mount, a dead container runtime):
CPython's ``subprocess.run`` handles its own timeout by calling
``process.kill()`` and then ``process.wait()`` with NO timeout, and
``Popen.__exit__`` waits again, so a D-state child that cannot be killed
blocks the call indefinitely. Tracked as #309.

This module is the only place in ``kstrl/`` or ``tests/`` that shells out
to ``ps``, and ``tests/test_procgroup.py`` fails on a second one in
either root. A rule with no mechanism is not a plan: the argument for
centralising the parse is that two copies drift, and nothing but a net
stops a third landing.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

#: The three columns the question needs and no more. ``pid`` is there for
#: the completeness control, not for identifying anything. See the
#: docstring above for why each is load-bearing and what it costs.
PS_ARGV = ("ps", "-A", "-o", "pid=,pgid=,stat=")

#: A bound on how long a KILLABLE ``ps`` may stall the caller. 440x the
#: 11.29ms measured for the call, so it cannot fire on a slow machine.
#: It does not bound an unkillable one; see the docstring and #309.
PS_TIMEOUT_SECONDS = 5.0

#: Said once, because several branches report it and reflowed copies of
#: one sentence are how the two answers drift apart.
_UNMEASURABLE = (
    "Process-group liveness cannot be measured here, and reporting "
    "'no live member' would be a false negative."
)


@dataclass(frozen=True)
class GroupLiveness:
    """Whether a group holds a running process, or why that is unknown."""

    #: True: at least one non-zombie member. False: none, and the answer
    #: earned the trust conditions in the module docstring. None: nothing
    #: was measured, and ``reason`` says what went wrong.
    live: bool | None
    reason: str = ""


def _may_signal_group(pgid: int) -> bool:
    """Whether ``killpg(pgid, ...)`` is safe to issue at all.

    ``killpg(1, sig)`` is ``kill(-1, sig)``: every process this user owns.
    ``serve._safe_pgid``, ``verify._signal_process_group`` and
    ``agents.proc`` each carry a copy of this rule (#308); this module
    needs its own because nothing enforces that its callers came through
    one of them, and a docstring saying they do is a convention, not a
    mechanism.

    This module only ever sends signal 0, so unlike those three it does
    NOT exclude the caller's own group: probing it is harmless and is a
    question the suite legitimately asks. The broadcast pgid is the part
    that must be refused whatever the signal.
    """
    return hasattr(os, "killpg") and pgid > 1


def read_group_liveness(pgid: int) -> GroupLiveness:
    """Whether any non-zombie process is in group ``pgid``.

    Absence is only ever reported by a call that earned the right to
    report it. The conditions, and the measurements showing that two
    earlier controls could not, are in the module docstring.
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
    return _interpret(_read_listing(out.stdout, pgid), pgid)


@dataclass(frozen=True)
class _Listing:
    """What one ``ps`` read saw, before any of it is believed."""

    #: pid 1 was present, so the view is not filtered to our own uid.
    complete: bool
    #: Rows carrying this pgid, and how many of them are not zombies.
    rows: int
    running: int


def _interpret(listing: _Listing, pgid: int) -> GroupLiveness:
    if listing.running:
        return GroupLiveness(True)
    if not listing.complete:
        return GroupLiveness(
            None,
            f"ps did not list pid 1, so the view is filtered to this uid "
            f"and a running member of group {pgid} owned by another uid "
            f"would be invisible. {_UNMEASURABLE}",
        )
    if listing.rows:
        # A complete listing that shows this group holding only zombies.
        # #298's case.
        return GroupLiveness(False)
    if _kernel_says_group_is_empty(pgid):
        return GroupLiveness(False)
    return GroupLiveness(
        None,
        f"ps listed no process in group {pgid}, but the kernel reports "
        f"that group is not empty, so the listing did not show every "
        f"process. {_UNMEASURABLE}",
    )


def _read_listing(stdout: str, pgid: int) -> _Listing:
    """Parse ``pid pgid stat`` rows into the three facts that decide it.

    Fields are named on ``_Listing`` rather than returned positionally,
    because all three would type-check in any order.
    """
    want = str(pgid)
    complete = False
    rows = 0
    running = 0
    for line in stdout.splitlines():
        parts = line.split()
        # A row missing a column would IndexError below. Real ps does not
        # emit one; a filtered or truncated listing might.
        if len(parts) < 3:
            continue
        pid, group, state = parts[0], parts[1], parts[2]
        complete = complete or pid == "1"
        if group != want:
            continue
        rows += 1
        # "Z" is the zombie state on both macOS and Linux, and flags may
        # follow it ("Z+", "Zl"), so match the prefix rather than the cell.
        if not state.startswith("Z"):
            running += 1
    return _Listing(complete=complete, rows=rows, running=running)


def _kernel_says_group_is_empty(pgid: int) -> bool:
    """True ONLY when the kernel says no process at all is in the group.

    ESRCH is the one conclusive answer. Success and ``EPERM`` both mean
    something is there, and any other ``OSError`` means the question was
    not answered. Neither is emptiness, and reading them as emptiness is
    the false negative this control exists to prevent. A pgid we must not
    signal is likewise not emptiness.
    """
    if not _may_signal_group(pgid):
        return False
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

    A pgid this module must not signal reads as ALIVE, which is the
    conservative direction: the caller then declines to call a run reaped.

    Prefer ``read_group_liveness``. This answers the question #298 was
    about getting wrong.
    """
    if not _may_signal_group(pgid):
        return True
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
