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
caller's own process group appeared in the listing; the read does not
``setpgid``, so the ``ps`` child runs in the caller's group and
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

WHY THE READ IS A ``Popen`` AND NOT ``subprocess.run`` (#309), because
an earlier version of this docstring claimed a bound it did not have.
``subprocess.run`` cannot give one. Read against the CPython 3.12.8 this
tree runs: its timeout handler calls ``process.kill()`` and then
``process.wait()`` with NO timeout, and the enclosing ``with Popen(...)``
calls ``__exit__``, which calls ``self.wait()`` again, also unbounded. A
``ps`` wedged in an uninterruptible read (a hung NFS mount, a dead
container runtime) cannot be killed, so both waits block forever - and
the one production caller is the daemon's reap path, which runs while
``serve`` holds its singleton lock for the daemon's whole lifetime. The
timeout named exactly the case it did not cover.

So the read is a ``Popen`` driven by two BOUNDED ``communicate`` calls:
``PS_TIMEOUT_SECONDS`` for the read itself, then a kill, then
``PS_KILL_GRACE_SECONDS`` to collect a child the kill reached.
``communicate(timeout=...)`` ends in ``wait(timeout=remaining)``, so both
legs have a deadline. Nothing here uses ``with Popen(...)``: that is the
second unbounded wait.

WHAT THAT BOUND DOES NOT COVER, stated because the first version of this
section promised a flat ceiling it does not have. The two deadlines cover
the READ and the DISPOSAL, and nothing else. Process STARTUP is outside
them: ``Popen.__init__`` forks and then blocks in ``_execute_child`` on
``os.read(errpipe_read, 50000)`` until the child either execs or reports
why it could not, and that read takes no timeout. A fork that never gets
that far stalls there. This is not new and is not something this module
can fix from the outside - ``subprocess.run`` builds its ``Popen`` through
the identical path, so the residual is exactly what it was before #309.
What changed is the part that WAS in this module's hands: once the child
is running, no wait on it is unbounded any more.

WHAT THAT COSTS, since a bound bought with nothing would be suspicious. A
child the kill did NOT reach is ABANDONED rather than waited on: we close
our two pipe ends, which cannot block on a read end, and report "cannot
see".

It is not a permanent zombie, and #309 round 2 is why that sentence now
rests on code in this module rather than on CPython's. The claim used to
be that ``Popen.__del__`` hands an unreaped child to
``subprocess._active`` for a later ``Popen`` to poll. Read the order it
does it in: it calls ``_warn(..., ResourceWarning)`` FIRST and appends to
``_active`` after. Under warnings-as-errors that warn RAISES, ``__del__``
aborts, the interpreter prints "Exception ignored in" and swallows it,
and the registration never happens - so the object is freed with nobody
left holding the pid, and the child stays a zombie for the life of the
process. Measured on this tree: default warnings gives ``_active`` length
1 and the child reaped; ``PYTHONWARNINGS=error`` gives length 0 and the
child in state Z. That setting is not hypothetical here, and this file's
own caller says so - ``serve._group_liveness_for_reap`` records that an
earlier ``warnings.warn`` in the reap check took the daemon down under
it, calling it "a common CI setting". A daemon that holds its lock for
its whole life, abandoning up to five children per terminated run, is the
worst place in the tree to rest on a ``__del__`` that may not finish.

So ``_kill_or_abandon`` registers the child itself, on our own
``_ABANDONED`` and on CPython's ``_active`` both; ``_register_abandoned``
says why one of them is not enough. Holding the reference also means
``__del__`` never runs, so the ResourceWarning that started this is not
raised at all.

THIS FIXES ONE SITE, NOT THE CLASS. ``verify.run_scrubbed``,
``serve._wait_out_grace`` and ``agents.proc.DeadlineStreamer.kill`` each
kill a child and give up on it the same way, and none of them registers
what it drops. ``run_scrubbed`` is the wider window of the three by a
long way - it needs no D-state child at all, only something outside the
group holding the pipe write end, and it runs once per verification
command rather than once per timed-out run. Tracked separately; said
here so this section is not read as the class being handled.

The caller's fallback for "cannot see" is the signal
probe, whose only error is over-reporting alive. That claim was written
here before it was true: #309 round 1 found the probe reporting GONE for
any error it could not explain, which is the dangerous direction this
docstring opens with. This change is what made it routine rather than
exotic, so ``signal_probe_alive`` was fixed with it and now reports alive
for everything but ESRCH.

THE COST IS PER READ, NOT PER RUN, and ``serve`` reads more than once.
``_wait_out_grace`` waits out the direct child and then re-reads the
group until its grace expires, and it checks the clock only at the top of
that loop, so each read can overshoot by a whole 6.0s. Simulated over the
two shapes that loop takes with a permanently wedged ``ps``: if the
direct child outlives both graces, 2 reads and 37.00s; if it dies at once
and a descendant holds on, 5 reads and 30.75s. So up to five abandoned
children per terminated run, and a ``terminate_process_group`` worst case
of 37s against the 25s an operator reads off ``GROUP_TERM_GRACE_SECONDS``
plus the SIGKILL leg. Both numbers are finite, which is the change; the
overshoot is worth knowing before anyone tunes either constant.

This module is the only place in ``kstrl/`` or ``tests/`` that shells out
to ``ps``, and ``tests/test_procgroup.py`` fails on a second one in
either root. A rule with no mechanism is not a plan: the argument for
centralising the parse is that two copies drift, and nothing but a net
stops a third landing.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import suppress
from dataclasses import dataclass

#: The three columns the question needs and no more. ``pid`` is there for
#: the completeness control, not for identifying anything. See the
#: docstring above for why each is load-bearing and what it costs.
PS_ARGV = ("ps", "-A", "-o", "pid=,pgid=,stat=")

#: How long the ``ps`` read itself may take. 440x the 11.29ms measured
#: for the call, so it cannot fire on a slow machine. Re-measured for
#: #309 on a 914-process machine: median 11.47ms, max 13.35ms over 60
#: samples.
PS_TIMEOUT_SECONDS = 5.0

#: How long a KILLED ``ps`` is given to be collected before it is
#: abandoned. This buys the kill a scheduler round trip, not work, and
#: measuring it says so: over 60 samples on the same machine, kill to
#: reaped was max 0.236ms for ``ps`` and max 1.122ms for a ``sleep``
#: child killed mid-run. 1.0s is ~890x the worse of the two. Raising it
#: cannot rescue a D-state child, which is the only case that reaches
#: the end of it; it would only lengthen the hang this bound exists to
#: stop. The two together bound every WAIT on the child at 6.0s, which is
#: not the same as bounding the call; see the docstring on ``_read_ps``.
PS_KILL_GRACE_SECONDS = 1.0

#: Children the kill did not reach, kept REFERENCED so they can still be
#: reaped. The module docstring's #309 round 2 section is why this exists
#: rather than ``Popen.__del__``. On a healthy machine it stays empty.
#: Each entry can retain more than the object: measured at 18,833 bytes
#: for a read that completed before the child was abandoned, almost all
#: of it CPython's ``_fileobj2output`` holding the listing it had already
#: buffered, against ~300 bytes for one abandoned before writing.
_ABANDONED: list[subprocess.Popen[str]] = []

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
    ``serve``, ``verify`` and ``agents.proc`` each used to carry a copy of
    this rule; #308 moved it here and they now reach it through
    :func:`safe_pgid`. This stays a function of its own because the pgid
    entry points below take an id from anywhere, so nothing enforces that
    THEIR callers came through ``safe_pgid``.

    The own-group exclusion is deliberately NOT here. The callers inside
    this module only ever send signal 0, where probing our own group is
    harmless and is a question the suite legitimately asks;
    :func:`safe_pgid`, whose callers send real signals, adds that
    exclusion on top. The broadcast pgid is the part that must be refused
    whatever the signal.
    """
    return hasattr(os, "killpg") and pgid > 1


def safe_pgid(process: subprocess.Popen[str]) -> int | None:
    """A child's process-group id, or None when group-signalling is unsafe.

    The single copy of the guard ``serve._safe_pgid``,
    ``verify._signal_process_group`` and ``agents.proc._signal_group``
    used to write out three times (#308). Driven over one matrix of pid,
    ``getpgid`` and ``killpg`` outcomes before the move, the three agreed
    on which pgid was safe for every input; they differed only in the
    error channel, ``serve`` letting ``getpgid`` raise where the other two
    swallowed. This takes the swallowing form, so a caller no longer wraps
    the call in ``except OSError`` to get a None.

    Three things make a pgid unsafe. A pid that is not an ``int``, because
    a mocked ``Popen``'s pid coerces to 1 through ``MagicMock.__index__``
    rather than raising - measured on this machine,
    ``os.getpgid(MagicMock())`` returns 1. A pgid of 1 or below, because
    ``killpg(1, sig)`` is ``kill(-1, sig)``, every process this user owns,
    which is how a CI runner was once taken down. And our OWN group,
    because ``start_new_session=True`` gives the child a group of its own,
    so seeing ours back means the child never got one and the signal would
    land on the harness doing the signalling.

    This is the entry point for a ``Popen``. :func:`_may_signal_group`
    remains the entry point for a bare pgid.
    """
    pid = process.pid
    # ``killpg`` gates ``getpgid`` as much as itself: both are POSIX-only,
    # and an absent one raises AttributeError, which no caller catches.
    if not hasattr(os, "killpg") or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        pgid = os.getpgid(pid)
    except OSError:
        # ESRCH (already reaped) or EPERM. Either way there is no group id
        # here that we are entitled to signal.
        return None
    if not _may_signal_group(pgid) or pgid == os.getpgrp():
        return None
    return pgid


def read_group_liveness(pgid: int) -> GroupLiveness:
    """Whether any non-zombie process is in group ``pgid``.

    Absence is only ever reported by a call that earned the right to
    report it. The conditions, and the measurements showing that two
    earlier controls could not, are in the module docstring.
    """
    try:
        out = _read_ps()
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return GroupLiveness(None, f"ps failed to run ({exc!r}). {_UNMEASURABLE}")
    if out.returncode != 0:
        return GroupLiveness(
            None,
            f"ps failed (rc={out.returncode}): {out.stderr.strip()!r}. {_UNMEASURABLE}",
        )
    return _interpret(_read_listing(out.stdout, pgid), pgid)


def _read_ps() -> subprocess.CompletedProcess[str]:
    """One ``ps`` read, with every wait on the child bounded.

    ``PS_TIMEOUT_SECONDS`` for the read and ``PS_KILL_GRACE_SECONDS`` for
    the disposal. NOT a flat ceiling on the call: process startup is
    outside both, because ``Popen.__init__`` blocks on an ``os.read`` of
    the exec error pipe that takes no timeout. Measured with a 3.0s stall
    injected there and both constants at 0.05: 3.011s. The module
    docstring's "WHAT THAT BOUND DOES NOT COVER" section has the rest,
    including why that residual is not new.

    Why this is not ``subprocess.run``, and why no ``with`` block, is the
    #309 section of the module docstring: both of those wait on the child
    without a deadline, which is the hang.

    No ``start_new_session``, matching what ``subprocess.run`` did: the
    ``ps`` child stays in the caller's process group, which is what makes
    the rejected "our own pgid is listed" control satisfied by
    construction rather than merely usually true.
    """
    _reap_abandoned()
    process = subprocess.Popen(
        PS_ARGV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Pinned rather than left to the locale, and non-decodable bytes
        # are replaced rather than raised. A decode error here is a
        # ValueError, which would escape a fail-closed ``except OSError``
        # and take the daemon down over a diagnostic (the repo's #291
        # lesson). Caught by the caller as well, so a future edit cannot
        # reintroduce it.
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=PS_TIMEOUT_SECONDS)
    except BaseException:
        # Every exit that is not a completed read leaves a child behind,
        # so every one of them goes through the same disposal.
        # ``BaseException`` because a KeyboardInterrupt out of the daemon
        # must not be the one path that leaks the child.
        _kill_or_abandon(process)
        raise
    return subprocess.CompletedProcess(PS_ARGV, process.returncode, stdout, stderr)


def _kill_or_abandon(process: subprocess.Popen[str]) -> None:
    """Kill the child, give the kill a bounded grace, then LET GO.

    The second ``communicate`` is the wait ``subprocess.run`` does
    without a deadline.

    WHAT REACHING ITS TIMEOUT ACTUALLY MEANS, corrected in #309 round 2
    because the first version stated a stronger thing as a POSIX fact.
    ``communicate`` waits for EOF on both pipes and then for the process,
    so the grace can expire with the direct child already dead and
    reaped: anything that inherited the write ends still holds them open.
    Demonstrated with a ``ps`` that forks - ``poll()`` returned 0 at kill
    time and the full grace expired anyway, because a grandchild held the
    pipes. It does not arise for ``PS_ARGV``, which forks nothing, but it
    is why this says "let go" rather than "the kill did not land": what
    is abandoned may be a descendant we never had a handle on, and no
    amount of further waiting here is owed to it.
    """
    try:
        process.kill()
        process.communicate(timeout=PS_KILL_GRACE_SECONDS)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # One handler for both calls, because a kill that could not be
        # sent leads to the same place as one that did not work: the
        # child is not ours to collect. SWALLOWED rather than raised,
        # because the caller is already carrying the original failure and
        # is about to re-raise it; letting a grace timeout out of here
        # would displace the exception that says what actually went
        # wrong. The pipes are released either way, below.
        pass
    finally:
        # In a FINALLY rather than in the handler above, because the
        # exceptions this names are not the only ones that get here. A
        # KeyboardInterrupt landing in the grace escapes both calls with
        # the pipe pair still held, which is the leak this whole path
        # exists to avoid (#309 round 1, F3).
        _release_pipes(process)
        if process.returncode is None:
            _register_abandoned(process)


def _release_pipes(process: subprocess.Popen[str]) -> None:
    """Close our ends of the pipes to a child we are done with.

    Closing a pipe READ end cannot block, which is what makes this safe
    on a path whose contract is not to block. A second close is a no-op,
    so the branch where ``communicate`` already closed them costs
    nothing.
    """
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            with suppress(OSError):
                pipe.close()


def _register_abandoned(process: subprocess.Popen[str]) -> None:
    """Keep an uncollected child reachable until something can reap it.

    TWO registers, because they sweep at different rates and neither
    rate alone is good enough (#309 round 2).

    ``_ABANDONED`` is ours and is swept by the next read here. That is
    the one this module can promise, and the one a test can see.

    ``subprocess._active`` is CPython's, and every ``Popen`` constructed
    anywhere in the process sweeps it in ``_cleanup``. This module's
    reads are rare by design - the production caller asks once per
    timed-out run - so ours alone would leave a corpse until the next
    timeout, which may be days away. There are 70 spawn sites in
    ``kstrl``, so ``_active`` collects it at the next git call or verify
    command instead. That is what ``Popen.__del__`` would have done; we
    do it up front precisely because ``__del__`` may not get there.
    Guarded with ``getattr`` because it is CPython-private and is None on
    Windows, which this module does not support anyway.

    Registering also means ``__del__`` never runs, so the ResourceWarning
    that made ``__del__`` unreliable is not raised at all.

    ONLY SOMETHING ``_cleanup`` CAN POLL may go on ``_active``, which is
    what the ``_internal_poll`` check is. ``_cleanup`` calls that method
    on every entry, so anything else corrupts interpreter state for the
    whole process: measured, a test double in there crashed the next real
    spawn anywhere in the suite with ``AttributeError: '_FakePs' object
    has no attribute '_internal_poll'``. That is a constraint of the
    private list, not an accommodation for tests, and in production the
    branch is always taken. Checked by attribute rather than by
    ``isinstance``, because the name ``subprocess.Popen`` is itself
    replaceable - the suite's own seam replaces it with a function, and
    an ``isinstance`` against it then raises ``TypeError``.
    """
    _ABANDONED.append(process)
    active = getattr(subprocess, "_active", None)
    if active is not None and hasattr(process, "_internal_poll"):
        active.append(process)


def _reap_abandoned() -> None:
    """Collect any abandoned child the kernel has since let die.

    ``poll`` is ``waitpid(WNOHANG)``, so this cannot block - the only
    reason it is safe to call on the path whose whole contract is not to
    block. Swept before each fork because that is the moment the cost is
    about to be paid again.

    Rebuilt rather than removed from. ``list.remove`` is what CPython's
    ``_cleanup`` does, and it has to guard the ``ValueError`` for an
    entry another thread already dropped; a rebuild cannot raise that at
    all, and walks the list once instead of once per corpse.
    """
    _ABANDONED[:] = [process for process in _ABANDONED if process.poll() is None]


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

    The signal reading, kept because it is the only thing left when
    ``ps`` gives no answer at all, and because a test needs it as the
    control that proves a zombie window was real rather than a group that
    quietly went away. ``PermissionError`` means something is there
    refusing us, which is the closest to "alive" a signal can report, and
    on a zombie-only group that is measurably the branch taken.

    A pgid this module must not signal reads as ALIVE, which is the
    conservative direction: the caller then declines to call a run reaped.

    ONLY ESRCH MEANS GONE (#309 round 1, F1), and that is why this is one
    line rather than its own branch table. It used to have one, and its
    generic ``OSError`` branch returned False - an error nobody could
    explain read as "nothing is there", which ``serve`` turns into
    "reaped" (the #186 F1 hazard the module docstring opens with). That
    was the pre-#298 mapping carried forward, survivable only while this
    function was nearly unreachable. #309 made "ps cannot see" a routine
    outcome and this the routine fallback, so the fail-open moved onto the
    normal path and had to go.

    What replaced it is not a matching branch table but the SAME one.
    ``_kernel_says_group_is_empty`` has always read ESRCH as the one
    conclusive answer and every other ``OSError`` as "the question was not
    answered", so once this function agrees with it on every branch,
    writing the branches out twice is how they drift apart again. Two
    readings of one ``killpg`` disagreeing about an unexplained error was
    the defect underneath the defect; delegating is the only version of
    the fix that cannot come undone.

    This does NOT recreate the "every timed-out run is poisoned" hazard
    ``serve._group_liveness_for_reap`` warns about. That one is about a
    machine with no ``ps``, where this probe still answers ESRCH or EPERM
    correctly and runs stay reapable. Only signal 0 failing for a reason
    POSIX does not define reaches the changed branch, and refusing to
    conclude from it is the whole point.

    Prefer ``read_group_liveness``. This answers the question #298 was
    about getting wrong.
    """
    return not _kernel_says_group_is_empty(pgid)
