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
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from kstrl.procdispose import drain_or_abandon, reap_abandoned

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


def _refuse_group(pgid: int) -> str:
    """Why this module will not send a REAL signal to ``pgid``, or "".

    The two-part rule :func:`safe_pgid` and :func:`signal_group` each
    used to spell out for themselves, in the module whose whole subject
    is that a rule with two spellings has two behaviours eventually.
    Returns the EVIDENCE rather than a bool because ``signal_group``
    reports its refusal to a caller that writes it into a
    ``GroupTermination`` record, and a bool there would have needed the
    text reconstructed from the condition that produced it.

    :func:`_may_signal_group` stays separate underneath, and the split is
    load-bearing rather than tidy: the signal-0 probes in this module
    legitimately ask about our OWN group, and only a real signal has to
    be refused there.

    Reached twice on the ``signal_process_tree`` path, once through
    ``safe_pgid`` and once through ``signal_group``. That is deliberate.
    ``signal_group`` takes a bare integer from anywhere, so its own check
    is the only thing standing between a caller that never went through
    ``safe_pgid`` and ``os.killpg`` - which is #329 exactly. The repeat
    costs one ``os.getpgrp()``, measured at 111ns.
    """
    if not _may_signal_group(pgid):
        return f"group {pgid} must never be signalled"
    if pgid == os.getpgrp():
        return f"group {pgid} is our own, so the signal would land on us"
    return ""


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
    # Each ``hasattr`` sits next to the call it guards: this one over
    # ``getpgid`` below, and ``_may_signal_group``'s over ``killpg``. Both
    # are POSIX-only, and an absent one raises AttributeError, which no
    # caller catches.
    if not hasattr(os, "getpgid") or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        pgid = os.getpgid(pid)
    except OSError:
        # ESRCH (already reaped) or EPERM. Either way there is no group id
        # here that we are entitled to signal.
        return None
    if _refuse_group(pgid):
        return None
    return pgid


@dataclass(frozen=True)
class GroupSignal:
    """What one attempt to signal a process group did.

    FOUR outcomes a caller has to tell apart, and ``serve`` is why the
    last two are separate fields rather than one string. ``sent`` drives
    the escalation. ``vanished`` (ESRCH) is a CONFIRMED empty group and
    lets a caller stop early.

    ``denied`` and ``refused`` both mean "not signalled and not known
    gone", and a caller must never read either as success - but they are
    not the same news and ``GroupTermination`` next door records what
    conflating them costs: EPERM is the KERNEL saying it found processes
    in that group, which is positive evidence a factory is alive and the
    operator has to act on, while a refusal from this module is evidence
    of nothing at all except that the pgid was not one we may signal.
    Reporting the second as the first would tell the operator the
    opposite of the truth in the one case where they must act.
    """

    sent: bool
    vanished: bool = False
    #: The kernel would not deliver it. Evidence about the group.
    denied: str = ""
    #: This module would not send it. Evidence about the pgid.
    refused: str = ""


def signal_group(pgid: int, sig: int) -> GroupSignal:
    """Send ``sig`` to process group ``pgid``, or refuse and say so.

    THE ONE PLACE A REAL SIGNAL REACHES A GROUP (#329). ``safe_pgid``
    answers the question about a ``Popen``; this answers it about the
    bare integer that ends up in ``killpg``, and the two are not the same
    question. ``serve.terminate_process_group`` took a caller-supplied
    ``pgid`` straight to ``os.killpg`` with no check of any kind, which
    #328 declined to fix because it is a behaviour change rather than a
    cleanup. It was safe by PROVENANCE - every caller happened to source
    the value from ``safe_pgid`` - and provenance is not a mechanism.

    What the mechanism costs: nothing on any path a caller takes today.
    The guard can only fire for a pgid that never came from
    ``safe_pgid``, which no current caller produces, so this closes a
    latent hazard rather than changing a live behaviour. What it prevents
    is measured and not hypothetical: #328's mutation sweep found that
    dropping the own-group check does not fail a test, it kills the test
    RUNNER with signal 15, because a serve test sets ``fake.pid =
    os.getpid()``. ``ks serve`` holds the daemon singleton lock for its
    whole process lifetime, so the same mistake from a real caller is
    the daemon.

    The refusal is REPORTED rather than raised. Raising would reach three
    callers that today cannot fail here, and a caller that ignores a
    ``GroupSignal`` gets ``sent=False``, which every one of them already
    has to handle for the ESRCH and EPERM branches.
    """
    refused = _refuse_group(pgid)
    if refused:
        return GroupSignal(False, refused=refused)
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return GroupSignal(False, vanished=True)
    except OSError as exc:
        return GroupSignal(False, denied=f"the kernel refused signal {sig} to group {pgid}: {exc}")
    return GroupSignal(True)


def signal_process_tree(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal a child's whole group, falling back to the direct child.

    The single copy of a routine ``verify._signal_process_group`` and
    ``agents.proc.DeadlineStreamer._signal_group`` wrote out twice,
    identically, after #308 had already lifted the guard half of it here.
    Lifting the guard and leaving the routine behind is what let the
    ``os.killpg`` call itself stay in three modules, which is the thing
    #329 is about.

    A group that cannot be signalled is not an error: a mocked ``Popen``,
    a non-POSIX platform, an unsafe pgid, a child already reaped and a
    group that went between the lookup and the signal all land on the
    direct child instead. That degradation is the whole reason
    :func:`safe_pgid` can afford to be strict.
    """
    pgid = safe_pgid(process)
    if pgid is not None and signal_group(pgid, sig).sent:
        return
    try:
        if sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
    except OSError:
        pass


def pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` names a process this machine still holds.

    The bare-pid twin of :func:`signal_probe_alive`, and here for the
    same reason ``signal_group`` is: ``serve._pid_alive`` sent signal 0
    to a lease holder's pid with its own inline guard, a fourth copy of
    a rule this module owns. A ZOMBIE reads as alive, which is correct
    for the lease question - the pid is still allocated, so it has not
    been handed to anything else - and is the opposite of what
    :func:`read_group_liveness` answers about a GROUP.

    ``pid <= 0`` is refused rather than probed, and that is the part
    that has to live next to the syscall: ``os.kill(0, sig)`` is the
    caller's whole process group and ``os.kill(-1, sig)`` is every
    process this user owns. Signal 0 makes both harmless TODAY; the
    guard is here so that stays true if the signal ever stops being 0.

    Alive is the fail direction for everything that is not a definite
    ESRCH, because the caller reaps a lease on False and a wrong reap
    puts a second factory on a repo the first is still writing to
    (#186 F1).
    """
    if pid <= 0 or not hasattr(os, "kill"):
        return False
    return not _probe_says_gone(lambda: os.kill(pid, 0))


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
    reap_abandoned()
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
        drain_or_abandon(process, PS_KILL_GRACE_SECONDS)
        raise
    return subprocess.CompletedProcess(PS_ARGV, process.returncode, stdout, stderr)


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


def _probe_says_gone(send: Callable[[], None]) -> bool:
    """The one branch table for a signal-0 probe. ESRCH, and nothing else.

    ONLY ESRCH MEANS GONE. Success and ``EPERM`` both mean something is
    there, and any other ``OSError`` means the question was not answered,
    which is not the same as an answer of "nothing". Reading an
    unexplained errno as emptiness is the pre-#298 mapping that #309
    round 1 removed, and it fails towards calling a live run reaped.

    A function rather than the two copies it replaces, for the reason
    :func:`signal_probe_alive` states at length about its own delegation:
    two readings of one ``killpg`` disagreeing about an unexplained error
    was the defect underneath the defect, and this file had grown a THIRD
    copy of the table in :func:`pid_is_alive` while saying so.

    The caller supplies the syscall and keeps its own eligibility guard,
    because they differ: a group must be one we may signal at all, and a
    pid must be positive, ``os.kill(0, sig)`` being our own group and
    ``os.kill(-1, sig)`` every process this user owns.
    """
    try:
        send()
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


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
    return _probe_says_gone(lambda: os.killpg(pgid, 0))


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
