"""Letting go of a child the kill could not collect.

Split out of ``kstrl/procgroup.py``, whose subject is whether a group
still holds anything RUNNING. Reading a group and letting go of a child
are two jobs, and the file-length ratchet is what said so out loud: the
sweep for #326 and #329 took that module from 596 lines to 871.

THE CLASS THIS MODULE IS THE HOME OF (#326, #329). ``procgroup`` used to
end its abandonment section with "THIS FIXES ONE SITE, NOT THE CLASS" and
name the other three, and naming them did not stop them being found one
at a time four PRs later. ``verify.run_scrubbed``,
``serve.run_supervised`` and ``agents.proc.DeadlineStreamer.kill`` each
killed a child and gave up on it with no register and no close.
``tests/test_process_lifecycle.py`` is what stops a fourth landing
outside these two functions.

WHY A REGISTER AT ALL, rather than ``Popen.__del__``.

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

So ``drain_or_abandon`` registers the child itself, on our own
``_ABANDONED`` and on CPython's ``_active`` both; ``_register_abandoned``
says why one of them is not enough. Holding the reference also means
``__del__`` never runs, so the ResourceWarning that started this is not
raised at all.

So :func:`drain_or_abandon` and :func:`reap_or_abandon` register the
child themselves, on our own ``_ABANDONED`` and on CPython's ``_active``
both; :func:`_register_abandoned` says why one of them is not enough.
Holding the reference also means ``__del__`` never runs, so the
ResourceWarning that started this is not raised at all.

TWO DISPOSALS AND NOT ONE, because the difference is measured. A pipe end
can only be closed by code that is not racing another thread on the same
object: with a reader thread blocked on stdout, a ``close`` from a second
thread had not returned after 3.005s, and the same holds for a stdin
whose writer is blocked on a full pipe. ``agents.proc.DeadlineStreamer``
runs exactly that way, so its disposal is :func:`reap_or_abandon`, which
waits and registers and closes nothing, and its threads close their own
ends in a ``finally``. The three callers that own their pipes outright
use :func:`drain_or_abandon`, which drains and closes them here.

THE GRACE IS A REQUIRED ARGUMENT, not a default read from a constant.
``grace: float = PS_KILL_GRACE_SECONDS`` is the obvious signature and is
wrong twice over: a default argument is evaluated ONCE at import, so the
constant stops being the live answer the moment anything rebinds it
(measured - two of ``tests/test_procgroup.py``'s bound tests set it to
0.02 and got 1.0), and that constant is named for the ``ps`` read, which
is one caller out of four. Every caller says how long it is willing to
wait.

WHERE A DISPOSAL HAS TO BE ATTACHED, which is the half of the class these
two functions do not decide. Two shapes, and the sweep for #326 found one
instance of each still open after the three named sites were fixed.

* A ``communicate`` guarded by ``except TimeoutExpired`` ALONE.
  ``procgroup._read_ps`` has caught ``BaseException`` since #309 and says
  why; ``verify.run_scrubbed`` and ``serve.run_supervised`` did not, so a
  KeyboardInterrupt or a MemoryError out of either left a child with no
  signal, no reap, no register and both pipe ends held. Both now carry
  the same clause. RESIDUAL, shared with ``_read_ps`` and stated rather
  than implied: the clause guards the ``communicate``, not the handler
  above it, so an exception raised INSIDE the timeout handler - between
  the SIGTERM and the drain - still escapes with the child registered
  nowhere. Every statement in those handlers swallows its own errors, so
  the only way in is an asynchronous signal in a window of a few seconds,
  and closing it costs a nested handler at all three sites.
* A child owned by a GENERATOR the caller can walk away from. Every agent
  adapter's ``run`` is one, and ``decompose.collect_agent_output``
  abandons it mid-yield today. ``agents.proc.DeadlineStreamer.close`` is
  the disposal, reached from a ``finally`` in the generator, and its
  docstring carries why kill and not wait.

POSIX-first, like ``procgroup``: nothing here signals a group, but the
children it collects were put in one.
"""

from __future__ import annotations

import subprocess
from contextlib import suppress
from typing import IO

#: Children the kill did not reach, kept REFERENCED so they can still be
#: reaped. The module docstring's #309 round 2 section is why this exists
#: rather than ``Popen.__del__``. On a healthy machine it stays empty.
#: Each entry can retain more than the object: measured at 18,833 bytes
#: for a read that completed before the child was abandoned, almost all
#: of it CPython's ``_fileobj2output`` holding the listing it had already
#: buffered, against ~300 bytes for one abandoned before writing.
_ABANDONED: list[subprocess.Popen[str]] = []


def drain_or_abandon(
    process: subprocess.Popen[str],
    grace: float,
) -> tuple[str, str]:
    """Kill the child, drain its pipes under a deadline, then LET GO.

    The disposal for a caller that OWNS the pipes: nothing else is
    reading or writing them, so this may both drain and close them. The
    sibling for a caller whose threads own the pipes is
    :func:`reap_or_abandon`, and the difference between the two is
    measured rather than stylistic - see that docstring.

    Returns whatever was drained, or ``("", "")`` when the grace expired
    with the pipes still held. Three callers wanted exactly that pair
    and each wrote it out for itself, which is #326: ``verify`` and
    ``serve`` set it in an ``except`` branch and then dropped the child
    with no register and no close.

    The ``communicate`` here is the wait ``subprocess.run`` does without
    a deadline.

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
    amount of further waiting here is owed to it. It is also the routine
    case at the other three sites, which run agent-authored commands
    that fork freely.
    """
    reap_abandoned()
    try:
        process.kill()
        return process.communicate(timeout=grace)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # One handler for both calls, because a kill that could not be
        # sent leads to the same place as one that did not work: the
        # child is not ours to collect. SWALLOWED rather than raised,
        # because a caller on this path is already carrying the failure
        # that brought it here and is about to re-raise it; letting a
        # grace timeout out of here would displace the exception that
        # says what actually went wrong. The pipes are released either
        # way, below.
        return "", ""
    finally:
        # In a FINALLY rather than in the handler above, because the
        # exceptions this names are not the only ones that get here. A
        # KeyboardInterrupt landing in the grace escapes both calls with
        # the pipe pair still held, which is the leak this whole path
        # exists to avoid (#309 round 1, F3).
        _release_pipes(process)
        _register_if_unreaped(process)


def reap_or_abandon(
    process: subprocess.Popen[str],
    grace: float,
) -> None:
    """Kill the child, wait under a deadline, then LET GO. CLOSES NO PIPE.

    The disposal for a caller whose pipes belong to OTHER THREADS, which
    is ``agents.proc.DeadlineStreamer``: a reader thread iterates stdout
    and a writer thread owns stdin. Closing a pipe from here would not be
    a tidier version of :func:`drain_or_abandon`, it would be a hang.

    MEASURED, because the difference is the whole reason there are two
    functions. A ``BufferedReader`` holds its lock for the duration of a
    blocking raw read, and ``close`` from another thread waits on that
    lock. With a grandchild outside the group holding the write end - the
    exact case this disposal exists for - a reader thread never returns,
    and ``stdout.close()`` from a second thread had not returned after
    3.005s. The same measurement on the write end: with a thread blocked
    writing to a full pipe, ``stdin.close()`` had not returned after
    3.005s either. Against that, closing a stdin with pending buffered
    data and a DEAD child returned in 0.0001s with ``BrokenPipeError``,
    which is why the owning thread can always do it safely and this
    function never can.

    So the pipes are closed by the threads that own them, in their own
    ``finally``. What is left here is the part no thread can do for
    itself: the bounded wait, and the register that stops an uncollected
    child becoming a permanent zombie.

    THE RESIDUAL, stated rather than implied: a reader thread that never
    returns never closes its end, and this function pins the process
    object, so those descriptors are held for the life of the harness.
    That is worse than a leak that GC would eventually clear and better
    than a disposal path that blocks forever, and there is no third
    option that does not close a descriptor another thread is reading.
    """
    reap_abandoned()
    try:
        process.kill()
        process.wait(timeout=grace)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Same reasoning as the sibling above: an unsendable kill and an
        # expired grace both mean the child is not ours to collect.
        pass
    finally:
        _register_if_unreaped(process)


def _register_if_unreaped(process: subprocess.Popen[str]) -> None:
    """The half of a disposal that runs however the wait ended.

    The guard stays OUTSIDE :func:`_register_abandoned` rather than
    folding into it, and that is a test seam rather than a style
    preference: ``tests/test_procgroup_disposal.py`` replaces
    ``_register_abandoned`` with a recorder and asserts it is not called
    for a child that WAS reaped, which only works while the condition is
    somewhere the recorder can see.
    """
    if process.returncode is None:
        _register_abandoned(process)


def _release_pipes(process: subprocess.Popen[str]) -> None:
    """Close our ends of the pipes to a child we are done with.

    A second close is a no-op, so the branch where ``communicate``
    already closed them costs nothing.

    CALLED ONLY WHERE NO OTHER THREAD TOUCHES THESE OBJECTS. Closing a
    pipe end is not unconditionally non-blocking, which an earlier
    version of this docstring claimed of the read end and is why the
    claim now names its condition: the blocking is not about the
    DIRECTION of the pipe, it is about the io lock, and either end
    blocks a second thread's ``close`` while the owning thread sits in a
    read or a flush. :func:`reap_or_abandon` carries the measurements
    and is the entry point for callers that cannot meet the condition.

    ``stdin`` is closed as well as the read ends. It is a WRITE end and
    so can flush on close, which is why it is named here rather than
    left to fall out of the loop: with a dead child that flush measured
    0.0001s and raised ``BrokenPipeError``.
    """
    for pipe in (process.stdout, process.stderr, process.stdin):
        close_quietly(pipe)


def close_quietly(pipe: IO[str] | None) -> None:
    """Close our end of a pipe, and never let the close be a failure.

    ``suppress(Exception)`` rather than a named tuple of error types,
    which is the opposite of this repo's usual rule and has a measured
    reason: the object is not always one we made. A caller under test
    hands a streamer a ``MagicMock`` whose ``stdout`` is a plain iterator
    with no ``close`` at all, so the close raises ``AttributeError``;
    production hands it a ``BufferedReader`` that raises
    ``BrokenPipeError`` on the flush, and a detached or already-closed
    wrapper raises ``ValueError``. What the close raises is not
    actionable in any of those - there is nothing to be done about a
    descriptor we have finished with - and the cost of getting the
    enumeration wrong is measured at 600 seconds: the narrow form let an
    ``AttributeError`` out of a reader thread's ``finally``, the thread
    died before posting its queue sentinel, and the consumer waited on
    an unbounded ``queue.get()`` for the rest of the run.

    ONE helper rather than the two this class of change first grew, one
    here with the narrow enumeration and one in ``agents.proc`` with the
    wide one. The narrow copy was on the disposal path, inside the
    ``finally`` whose entire job is to run when something unexpected
    happened.
    """
    if pipe is None:
        return
    with suppress(Exception):
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


def reap_abandoned() -> None:
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
