"""The ``ps`` read and its parse: bytes in, :class:`_Listing` out.

Split out of ``kstrl/procgroup.py`` by #209 round 3. That file sat at
exactly 800 lines, which is the repo's per-file ratchet, and round 2 of
the review asked for three more paragraphs of recorded reasoning. The
alternative was deleting reasoning to fit, so the cut was taken instead.

WHERE THE CUT IS. This module turns a listing into the four facts
:class:`_Listing` carries and knows nothing about what they mean. What
they mean about a process group - the refusal table, the kernel
cross-check, the two public reads and their consequences - stays in
``kstrl.procgroup``, which imports every name below, so the module that
owned them still exports them and no caller moved.

WHAT IS NOT HERE, deliberately, and why this file is not the place to
look for it. Why there is exactly ONE ``ps`` parse in this tree, why
absence needs a control, what each of ``PS_ARGV``'s three columns is
load-bearing for, and what the two deadlines below do and do not bound
are all argued at length in ``kstrl.procgroup``'s module docstring.
Every reader of this file arrives from there. Repeating any of it here
is how two statements of one rule start to disagree, which is the defect
class the module next door exists to write down.

THE UNIQUENESS CLAIM MOVED WITH THE CALL. This module is the
only place in ``kstrl/`` or ``tests/`` that shells out to ``ps``,
and ``tests/test_procgroup.py`` fails on a second one in either root. The
argument for centralising the parse - that two copies drift on failure
handling until the daemon's answer and the suite's stop agreeing - is
next door with the reading it protects; the net that enforces it points
here, because here is where the call is.

ONE THING MOVED THAT A TEST CAN SEE: a test patching the ``ps`` argv or
either deadline must patch ``kstrl.procgroup_listing``, because that is
where :func:`_read_ps` reads them from. Measured when this file was cut:
the four bounded-call tests patched ``kstrl.procgroup.PS_ARGV``, the
patch reached a re-exported binding nothing reads, and all four went RED
rather than quietly green. Red is not guaranteed for the next such test,
which is why this paragraph is here.
"""

from __future__ import annotations

import subprocess
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
    #: Every non-blank row parsed. False means at least one row could
    #: not be attributed to a group, so this is not a full view of any
    #: group. No default: a default is how a later constructor comes to
    #: claim readability it never established.
    readable: bool
    #: Pids carrying this pgid, zombies included.
    listed: tuple[int, ...]
    #: Of those, the ones that are not zombies. Kept as pids rather
    #: than a count because :func:`read_group_members` needs them; the
    #: counts below are derived from them.
    running_pids: tuple[int, ...]

    @property
    def rows(self) -> int:
        return len(self.listed)

    @property
    def running(self) -> int:
        return len(self.running_pids)


def _read_listing(stdout: str, pgid: int) -> _Listing:
    """Parse ``pid pgid stat`` rows into the four facts that decide it.

    Fields are named on ``_Listing`` rather than returned positionally
    because all four would type-check in any order.

    A ROW THIS CANNOT READ MAKES THE WHOLE LISTING UNREADABLE, and both
    public reads then refuse it. Dropping the row instead is an
    UNDERCOUNT, which the reading next door refuses everywhere else
    (#209 round 1). Measured on all three trees: ``"1 1 Ss\\nbad 7 Ss\\n50 7 Z\\n"``
    for group 7 gave ``live=True`` before #209 and ``live=False`` after
    it, which ``serve`` reads as "the group is gone" for a group whose
    only running member is the row that could not be read.

    THE REFUSAL IS WHOLE-LISTING ON PURPOSE, and #209's round-2 review
    asked for both of its directions rather than one. A mangled row is
    evidence about the STREAM, not about the row: a listing carrying one
    row this parse could not read is a listing whose other rows cannot
    be trusted either, so refusing every group read taken from it is the
    reading and not an over-reach. What that costs, measured by the
    reviewer on three groups they created with the refusal forced on:

    =================  ========================  ==================
    group              honest read               under a refusal
    =================  ========================  ==================
    one live member    live=True, reap alive     alive, degraded
    zombie only        live=False, reap reaps    alive, degraded
    genuinely empty    live=False, reap reaps    reaps, unchanged
    =================  ========================  ==================

    So a refusal a real ``ps`` could earn would leave every zombie-only
    group unreapable, because the fallback signal probe counts an
    unreaped zombie as alive; a genuinely empty group still reaps,
    because the kernel answers ESRCH without consulting the listing at
    all. That failure direction is over-reporting alive, which
    ``kstrl.procgroup``'s module docstring already declares as the
    fallback's ONLY error, and it is VISIBLE: the item is poisoned and set ``needs_human``. The
    other direction, a listing that answered anyway and called a running
    group gone, is silent, and silence is #186 F1 itself. Round 1 of
    #209 stated only the first half of this, and the half it left out is
    the one that costs an item.

    THE COST IS ZERO ON REAL OUTPUT: 20 reads of ``PS_ARGV`` gave 16320
    rows on one load, and the reviewer measured 16331 on another, with 0
    non-conforming rows both times - every row exactly three columns
    with a numeric pid and pgid, because the columns come from the
    kernel and the format asks for no free text. So the paragraph above
    is a decision about the RECORD rather than about a reachable case.
    The shape is reachable on a truncated or mangled stream, which is
    what the refusal is for.

    THE PID IS CHECKED BEFORE THE GROUP FILTER. A pid this parse cannot
    read makes the listing unreadable whatever its pgid column says,
    because ``readable`` is a property of the (listing, pgid) pair and a
    row that cannot be attributed to a group cannot be ruled OUT of this
    one. Until #209's round-2 review the check sat after the filter, so
    a garbage pid marked the listing unreadable only when its neighbour
    column happened to name the group being asked about: a clearing
    guard clearing on the strength of a column whose neighbour it had
    just admitted it could not read, in the one case the whole refusal
    is reachable for. The reviewer measured the reorder at +0.082 ms per
    parse of a real 813-row listing, 0.199 ms to 0.281 ms as the median
    of 200, against a ``ps`` read costing about 22 ms here.

    THE PGID IS COMPARED AS AN INT, not against ``str(pgid)``. The cell
    is already converted to establish that it reads as a number, and
    discarding that conversion to compare text attributed any spelling
    ``int()`` accepts and ``str`` does not produce to no group at all,
    with ``readable`` still true and no refusal raised. The round-2
    review measured four: ``007``, ``+7``, ``0_7`` and an Arabic-Indic
    seven, each giving a confident ``(51,)`` for a group holding
    ``(50, 51)``, which is the undercount ``GroupMembers`` says it
    cannot produce. ``pid == "1"`` one line up was the same split and
    gets the same treatment.

    Three shapes are unreadable: a non-blank row with fewer than three
    columns, a pid column that is not a number, and a PGID column that
    is not a number, because a group we cannot read cannot be ruled out.
    A blank line is not, because it claims nothing.
    """
    complete = False
    readable = True
    listed: list[int] = []
    running: list[int] = []
    for line in stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 3:
            readable = False
            continue
        pid, group, state = parts[0], parts[1], parts[2]
        if not _reads_as_int(pid):
            readable = False
            continue
        complete = complete or int(pid) == 1
        if not _reads_as_int(group):
            readable = False
            continue
        if int(group) != pgid:
            continue
        member = int(pid)
        listed.append(member)
        # "Z" is the zombie state on both macOS and Linux, and flags may
        # follow it ("Z+", "Zl"), so match the prefix rather than the cell.
        if not state.startswith("Z"):
            running.append(member)
    return _Listing(
        complete=complete, readable=readable, listed=tuple(listed), running_pids=tuple(running)
    )


def _reads_as_int(cell: str) -> bool:
    """Whether a ``ps`` column is a number this parse can use.

    ``int()`` rather than ``str.isdigit``: measured, ``"-5"`` is not
    ``isdigit`` and converts while ``"\N{SUPERSCRIPT TWO}"`` is
    ``isdigit`` and raises. Asking the conversion that will be done is
    the only test that cannot drift from it.
    """
    try:
        int(cell)
    except ValueError:
        return False
    return True
