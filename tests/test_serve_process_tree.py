"""#209 and #203 item 3: two facts about a live `ks serve` run's processes.

Both were stated in prose and neither was tested, which is how the first
of them came to be stated wrongly for a year.

1. :class:`TestTheRunGroupMembership` counts the processes
   ``subprocess_factory_runner`` puts in a run's process group. The
   docstring it corrects said "on macOS the direct child is the
   ``caffeinate`` wrapper, so the factory itself is a grandchild".
   Measured on macOS 26.6.2 (Darwin 25.6.0) by enumerating the whole
   group rather than reading one pid: ``caffeinate -i`` FORKS. The
   utility keeps the pid ``Popen`` returned, so the daemon's direct child
   IS the factory, and a second ``caffeinate`` runs as a child of it,
   inside the same group, holding ``PreventUserIdleSystemSleep``.

   Pid identity cannot tell exec-in-place from fork-the-helper apart -
   both leave the utility on the pid the daemon was given - which is why
   the seam canary in ``tests/test_serve_seam.py`` could not decide it
   and why no claim is made here about how caffeinate behaved on older
   macOS. Two readings that CAN decide it are here instead: a census of
   the run's process GROUP, and the utility asking whether it inherited
   a child it never created, which needs no ``ps`` and no privileges.

   Why the count is the property worth pinning rather than the
   mechanism: the helper being INSIDE the run's group is what makes the
   timeout path's group kill release the power assertion. A caffeinate
   that forked the helper OUT of the group would leave the machine
   unable to idle-sleep after every timed-out run, and would read as
   one member here.

2. :class:`TestTheReaperRunsOnlyUnderTheDaemonLock` pins the ORDER in
   which ``serve()`` takes the daemon lock and runs the lease reaper.
   ``reap_leases`` requeues a RUNNING item whose lease has lapsed against
   the WALL CLOCK, which advances across a suspend, so a run suspended
   overnight blows its 3600s lease while its pid is alive. What stops a
   second scheduled firing requeueing it is that the second firing takes
   ``serve_lock`` before it reaches the reaper and exits. Measured while
   writing this: a ``serve()`` mutated to run its cycle BEFORE acquiring
   the lock left every existing lock test in the suite green.

The census is macOS-only because ``caffeinate`` is, and is skipped on a
mac that does not have it installed. The lock tests are neither: they
need ``fcntl``, exactly as ``TestServeLock`` next door does.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from kstrl import serve as serve_module
from kstrl.serve import (
    RunOutcome,
    ServeLockedError,
    caffeinate_prefix,
    run_supervised,
    serve,
    serve_lock,
)
from kstrl.workqueue import ItemState, Queue, QueueConfig
from tests.helpers import procs

#: Gates the ``caffeinate=True`` cases and NOTHING else. Said once,
#: because the earlier shape was a platform decorator plus an in-body
#: ``shutil.which`` skip on each test, and the fourth copy of that pair
#: next door in ``tests/test_serve_seam.py`` has already drifted in
#: wording.
#:
#: It is a per-parameter mark rather than a class marker, which was
#: round 1 of #209's S4. CI is ubuntu, and under a class marker the
#: whole census contributed nothing there - including the two cases
#: that need no ``caffeinate`` and no macOS. Measured: the mutation that
#: drops ``start_new_session=True`` from ``run_supervised`` is caught by
#: this census and by nothing else in this file, and
#: ``start_new_session`` has nothing to do with macOS.
_NEEDS_CAFFEINATE = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("caffeinate") is None,
    reason="caffeinate is macOS-only and must be installed",
)

#: Gates every case that takes a census, on either platform. Where ``ps``
#: does not list pid 1 - a ``hidepid`` mount, a container - the read
#: under ``procs.group_member_pids`` refuses, correctly, and raises
#: through ``on_spawn``; the red test then reads as a defect in
#: ``kstrl/procgroup.py`` rather than as an environment that cannot be
#: measured. ``tests/test_shutdown.py`` skips on the same helper for the
#: same reason. Evaluated once at import, which costs one ``ps``.
_NEEDS_A_READABLE_PS = pytest.mark.skipif(
    not procs.ps_is_readable(),
    reason="ps here is absent or filtered to one uid, so a group census would be an undercount",
)

# --------------------------------------------------------------------------
# 1. The run's process group
# --------------------------------------------------------------------------

#: A utility that outlives the run's deadline without producing output.
#: Long enough that the group is stable for the whole census and the
#: timeout path is the only thing that ends it.
_SLEEPER_ARGV = ("-c", "import time; time.sleep(30)")

#: The same sleeper, after spawning one child of its own into the run's
#: group. A real factory run's group is this shape and not the one
#: above: it holds agent subprocesses, git, and the verify commands. The
#: child is spawned WITHOUT ``start_new_session``, which is what makes it
#: a member, and the count moves by one because of it.
_SPAWNER_ARGV = (
    "-c",
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
    "time.sleep(30)",
)

#: The deadline ``run_supervised`` is given. Short because the test wants
#: the timeout path (it is what proves the group kill takes the
#: caffeinate helper with it), not because the census needs it.
_RUN_TIMEOUT_SECONDS = 1.0

#: How long the census waits for the group to reach its expected size.
#: Measured over 12 spawns: the caffeinate helper is in the FIRST sample
#: 12 times out of 12, and that sample lands at a median of 21.6 ms and
#: a maximum of 22.4 ms after ``Popen`` returns. Those are the cost of
#: the instrument, not of the fork: one ``read_group_members`` measured
#: a median of 21.7 ms on the same machine, so the fork is faster than
#: anything this census can time and no latency figure is claimed here.
#: Five seconds is 223x the first sample. It is a bound, so a helper
#: that never appears fails rather than hangs.
_SETTLE_DEADLINE_SECONDS = 5.0

#: How long the census keeps WATCHING after the group reached its
#: expected size, keeping the last of the maximal samples. It is
#: insurance, not a control, and round 1 of #209's review is what
#: settled that: deleting the hold loop leaves the file green, and so
#: does deleting it TOGETHER with the mutation its argument names
#: (``caffeinate_prefix`` ignoring its ``enabled`` flag). On this
#: machine one ``read_group_members`` costs about 22 ms, and the helper
#: is in the first sample every time, so there is no window for the hold
#: to catch. It is kept because that ordering is a property of this
#: machine: where ``ps`` is faster than the fork, an early-exit census
#: would assert "one member" before the helper could appear and would
#: clear a runner that wrapped the run unconditionally.
_HOLD_SECONDS = 0.25

#: Interval between ``ps`` forks. One ``read_group_members`` was
#: measured at a median of 21.7 ms over 40 samples, so a hold iteration
#: costs about 42 ms rather than the interval alone, and a whole census
#: is 7 forks: 1 to settle and 6 across the hold, counted rather than
#: derived.
_POLL_SECONDS = 0.02


#: A utility that reports whether it has a child process it never
#: created. Under ``caffeinate -i`` it does: caffeinate forks, the child
#: keeps running caffeinate and holds the power assertion, and the parent
#: execs the utility - so the utility inherits a child it knows nothing
#: about. ``waitpid(-1, WNOHANG)`` raises ``ChildProcessError`` when
#: there are no children at all and returns ``(0, 0)`` when there are but
#: none has changed state, which is exactly the distinction wanted, and
#: it needs no ``ps``.
_FORK_PROBE = (
    "import os, sys\n"
    "try:\n"
    "    os.waitpid(-1, os.WNOHANG)\n"
    "    sys.stdout.write('INHERITED_A_CHILD')\n"
    "except ChildProcessError:\n"
    "    sys.stdout.write('NO_CHILDREN')\n"
)

#: The deadline the fork probe gets. It exits on its own in milliseconds;
#: this only has to be long enough that a loaded machine does not make
#: the test measure its own timeout path instead.
_PROBE_TIMEOUT_SECONDS = 60.0


def _census(pgid: int, expected: int) -> list[int]:
    """Poll ``pgid`` to its expected size, then hold and keep the largest.

    Returns the pids at the largest sample; its LENGTH is the census, so
    there is one number rather than two that a later edit could
    desynchronise. The two halves answer different questions: waiting for
    the count to REACH ``expected`` catches a group that is too small,
    and holding afterwards catches one that is too large.

    ``>=`` rather than ``>``, so the LAST maximal sample wins rather than
    the first. Which one is reported changes nothing about the count, and
    the later sample is the one whose pids are still live.

    It polls rather than delegating because ``tests/helpers/procs.py``
    has no bounded poll that returns the members: ``wait_for_group_to_die``
    and ``wait_for_pid_to_die`` both wait for a group to EMPTY and answer
    a bool. This one waits for a group to fill and answers the pids.
    """
    deadline = time.monotonic() + _SETTLE_DEADLINE_SECONDS
    pids = procs.group_member_pids(pgid)
    while len(pids) < expected and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        pids = procs.group_member_pids(pgid)

    hold_until = time.monotonic() + _HOLD_SECONDS
    while time.monotonic() < hold_until:
        time.sleep(_POLL_SECONDS)
        sample = procs.group_member_pids(pgid)
        if len(sample) >= len(pids):
            pids = sample
    return pids


class TestTheRunGroupMembership:
    """What `subprocess_factory_runner` actually puts in a run's group.

    WHAT THIS DOES NOT READ, said here rather than discovered later: the
    members' command lines. ``kstrl/procgroup.py`` is the only place in
    ``kstrl/`` or ``tests/`` allowed to shell out to ``ps`` - two copies
    of one parse drift on failure handling - and its listing asks for
    ``pid=,pgid=,stat=`` deliberately, because adding command lines
    measured 23.5 ms per call against 11.6 ms. So the argument that the
    extra member IS caffeinate's is differential rather than nominal: the
    two parametrized runs differ in nothing but ``caffeinate_prefix``,
    and one has a member the other does not.
    :meth:`test_the_factory_inherits_a_child_it_never_created` closes the
    same fact from the other side, from inside the utility, with no
    ``ps`` at all.
    """

    @staticmethod
    def _run_and_census(
        tmp_path: Path,
        *,
        caffeinate: bool,
        argv: tuple[str, ...],
        expected: int,
    ) -> tuple[RunOutcome, int, list[int]]:
        """Spawn through the shipping supervisor and count the group.

        The census runs inside ``on_spawn`` because that is the only
        moment the caller is handed the pid while the child is certainly
        alive - the same reason production adopts the lease there.

        NOTHING IS ASSERTED IN HERE, deliberately. ``run_supervised``
        calls ``on_spawn`` OUTSIDE its ``try``, so an exception raised in
        this callback escapes with the child neither waited on nor
        signalled, and a failing run would leave a 30-second sleeper
        behind for the next test to trip over. Everything is captured and
        re-raised after the supervisor has finished with the child.
        """
        spawned: int | None = None
        members: list[int] | None = None
        failure: Exception | None = None

        def on_spawn(pid: int) -> None:
            nonlocal spawned, members, failure
            try:
                spawned = pid
                members = _census(os.getpgid(pid), expected)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                failure = exc

        outcome = run_supervised(
            [*caffeinate_prefix(caffeinate), sys.executable, *argv],
            cwd=tmp_path,
            timeout_seconds=_RUN_TIMEOUT_SECONDS,
            on_spawn=on_spawn,
        )
        if failure is not None:
            raise failure
        assert spawned is not None, "on_spawn was never called"
        assert members is not None, "the census produced nothing"
        return outcome, spawned, members

    @_NEEDS_A_READABLE_PS
    @pytest.mark.parametrize(
        ("caffeinate", "argv", "expected_members"),
        [
            pytest.param(True, _SLEEPER_ARGV, 2, marks=_NEEDS_CAFFEINATE, id="caffeinate"),
            pytest.param(False, _SLEEPER_ARGV, 1, id="bare"),
            pytest.param(
                True, _SPAWNER_ARGV, 3, marks=_NEEDS_CAFFEINATE, id="caffeinate-with-child"
            ),
            pytest.param(False, _SPAWNER_ARGV, 2, id="bare-with-child"),
        ],
    )
    def test_the_group_holds_the_factory_its_children_and_one_caffeinate(
        self,
        tmp_path: Path,
        caffeinate: bool,
        argv: tuple[str, ...],
        expected_members: int,
    ) -> None:
        """The census, closed by construction rather than by a search.

        The number pinned is the DIFFERENCE the prefix makes, not a
        constant about a factory run: `caffeinate` adds exactly one
        member to whatever the utility's own tree holds. That is why the
        childless sleeper and the sleeper that spawns one child are both
        parametrized. Round 1 of #209's review found "exactly two members
        with caffeinate, one without" written as a claim about a run's
        group, and a real run's group also holds agent subprocesses, git
        and the verify commands - which is the defect class this PR
        exists to fix, one file over.

        Four different regressions all move the number. An exec-in-place
        caffeinate loses one. A caffeinate that forked a second helper
        gains one. A helper that escaped into another process group loses
        one, and that is the dangerous case: the timeout path's group
        kill would not reach it, so every timed-out run would leave the
        machine unable to idle-sleep. Dropping ``start_new_session=True``
        gives whatever pytest's own group holds. Dropping
        ``caffeinate_prefix`` from the runner's command loses one as
        well, which is the mutation ``tests/test_serve_seam.py`` records
        as having passed the entire suite when it was measured.

        THE REAP IS ASSERTED HERE rather than by a fifth spawn. The
        consequence the census exists for is that the power assertion is
        released because the process holding it is in the group the
        timeout kills, and this run already times out and already kills
        that group; a separate test built the identical argv a third
        time to read the same two fields. It is asserted as a group reap
        rather than by parsing ``pmset -g assertions``: the release
        follows from membership, which the count above pins, plus the
        measured fact that the helper's assertion row disappears with
        the helper. Every parametrization asserts it, so the reap is
        measured with caffeinate off and with a descendant to reach too.
        """
        outcome, pid, pids = self._run_and_census(
            tmp_path,
            caffeinate=caffeinate,
            argv=argv,
            expected=expected_members,
        )

        assert len(pids) == expected_members, (
            f"the run's process group held {len(pids)} process(es), not "
            f"{expected_members} (caffeinate={caffeinate}): {pids}. The "
            f"group holds the utility, whatever the utility spawned, and "
            f"with caffeinate on one more: the forked caffeinate that "
            f"holds PreventUserIdleSystemSleep. If caffeinate has stopped "
            f"putting its helper in the run's group, the timeout path's "
            f"group kill no longer releases the power assertion (#209)."
        )
        assert pids.count(pid) == 1, (
            f"the pid on_spawn was handed ({pid}) is not a member of the "
            f"group it leads: {pids}. The lease adopts that pid, so this "
            f"is the daemon adopting a process that is not the factory."
        )
        assert outcome.timed_out is True
        assert outcome.group_reaped is True, (
            f"the run's group was not confirmed reaped after the timeout "
            f"({outcome.group_reap_detail or outcome.group_occupied_detail}), "
            f"so a caffeinate helper may still hold "
            f"PreventUserIdleSystemSleep after a timed-out run"
        )

    @pytest.mark.parametrize(
        ("caffeinate", "expected"),
        [
            pytest.param(True, "INHERITED_A_CHILD", marks=_NEEDS_CAFFEINATE),
            pytest.param(False, "NO_CHILDREN"),
        ],
    )
    def test_the_factory_inherits_a_child_it_never_created(
        self,
        tmp_path: Path,
        caffeinate: bool,
        expected: str,
    ) -> None:
        """The fork, measured from inside the utility and without ``ps``.

        This is the assertion that decides what the seam canary could
        not. That canary reports the utility keeping the pid ``Popen``
        returned, which is true of an exec-in-place caffeinate AND of one
        that forks a helper and execs in the parent, so it cannot tell
        them apart - and #209 exists because a docstring said it could.
        A process that inherited a child it never spawned did not exec in
        place: something forked before the exec.

        The False case is the control, and it is what makes the True case
        mean anything: the same interpreter, the same spawn, no prefix,
        no child.
        """
        outcome = run_supervised(
            [*caffeinate_prefix(caffeinate), sys.executable, "-c", _FORK_PROBE],
            cwd=tmp_path,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        )
        assert outcome.timed_out is False, "the probe should exit on its own"
        assert outcome.returncode == 0, f"the probe failed: {outcome.output_tail!r}"
        assert outcome.output_tail.strip() == expected, (
            f"under caffeinate={caffeinate} the utility reported "
            f"{outcome.output_tail.strip()!r}, expected {expected!r}. "
            f"caffeinate -i forks a helper and execs the utility in the "
            f"parent, so the utility inherits a child it never created; "
            f"a run without the prefix has none (#209)."
        )


# --------------------------------------------------------------------------
# 2. The daemon lock is taken before the lease reaper runs
# --------------------------------------------------------------------------

#: A lease TTL short enough to lapse inside a test, plus the wall-clock
#: wait that makes it lapse. Ten times the TTL, and REAL time rather than
#: a mocked clock, because ``lease_expired`` compares against the wall
#: clock and that is the whole reason a suspended run is at risk.
#:
#: Measured separately, because round 1 of #209's review found the pair
#: claimed as load-bearing when only half of it is. Raising the TTL to
#: 3600s so the lease never lapses IS caught: the control below goes
#: red. Deleting the ``time.sleep`` entirely is NOT - the file stays
#: green, because 0.01s has already elapsed by the time ``serve()``
#: reaches the reaper on this machine. The sleep is insurance for a
#: machine where it has not, and the earlier comment's evidence (0.001s
#: with no sleep failing the control) is about a TTL that is not the one
#: in this file.
_LAPSED_LEASE_TTL_SECONDS = 0.01
_LAPSE_WAIT_SECONDS = 0.1


def _running_item_with_a_lapsed_lease(tmp_path: Path) -> str:
    """A RUNNING item whose lease has lapsed while its owner lives.

    Exactly the shape a suspended run leaves behind: the pid is this
    process, which is alive, and the wall-clock lease has run out
    anyway. ``reap_leases`` requeues on ``lease_expired(moment) OR not
    _pid_alive(...)``, so it is the first half that fires here.
    """
    queue = Queue(
        tmp_path,
        QueueConfig(lease_ttl_seconds=_LAPSED_LEASE_TTL_SECONDS, max_attempts=3),
    )
    item = queue.add("# Spec\n\nDo the thing.\n")
    queue.start(queue.lease(item, pid=os.getpid()))
    time.sleep(_LAPSE_WAIT_SECONDS)
    return item.item_id


def _runner_that_must_not_be_called(
    *,
    root_dir: Path,
    spec_path: Path,
    project_name: str,
    pause_before_pr_merge: bool,
    timeout_seconds: float,
    on_spawn: object = None,
) -> RunOutcome:
    """The `FactoryRunner` these tests inject. Spending money would be a bug.

    A reaped item is requeued with a backoff, so nothing is ready to run
    in the same cycle. If that ever changes, this fails loudly instead of
    launching a real factory out of a unit test.
    """
    raise AssertionError(
        f"serve tried to run {project_name} from {spec_path}; these tests "
        f"are about the reaper and no item should have been ready"
    )


def _spy_on_reap(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every ``reap_leases`` call and let the real one happen.

    ONE spy, wired the same way for the guard and for its control. Two
    copies of these lines is how the control stops controlling the thing
    next door: it exists to show an empty log means the lock stopped the
    reaper rather than that the patch missed its target, and it can only
    show that if it is the SAME patch.

    It delegates rather than stubbing, so a green control is still
    exercising real reaping.
    """
    real_reap = serve_module.reap_leases
    calls: list[object] = []

    def spy(queue: object, **kwargs: object) -> object:
        calls.append(queue)
        return real_reap(queue, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(serve_module, "reap_leases", spy)
    return calls


class TestTheReaperRunsOnlyUnderTheDaemonLock:
    """#203 item 3: the ordering is correct on the head and nothing pinned it.

    The hazard is a second scheduled firing (interval mode fires a NEW
    process on a calendar, so there is no in-process guard) reaching
    ``reap_leases`` while the first run is suspended with a lapsed lease.
    That would requeue an item that is still executing, which is the
    double-run ``factory.lock`` exists to prevent.

    WHAT THIS PINS IS THE PATH, NOT THE PROPERTY, and the class is named
    for the property. Measured in round 1 of #209's review: a ``serve()``
    changed to take ``serve_lock``, RELEASE it, and then run the cycle -
    so the reaper runs outside the lock - leaves all four tests below
    green, because the contended case still raises at acquisition. What
    would close the gap is a call-site census like
    ``TestOnlyOneModuleShellsOutToPs`` next door, over the one call site
    each of ``reap_leases`` and ``serve_cycle`` has today. That is not in
    this change; the number is here so it does not have to be re-derived.
    """

    def test_control_the_item_is_reapable_when_no_one_holds_the_lock(
        self,
        tmp_path: Path,
    ) -> None:
        """Without this, the two guards below pass on an unreapable fixture.

        A fixture whose lease has not actually lapsed makes "the reaper
        did not touch it" true for the wrong reason, which is
        ``assert hits == []`` wearing a different hat. This measures that
        the same fixture IS reaped when the lock is free.
        """
        item_id = _running_item_with_a_lapsed_lease(tmp_path)
        results = serve(tmp_path, once=True, runner=_runner_that_must_not_be_called)
        assert results[0].reaped.failed_for_retry == (item_id,), (
            f"the fixture is not reapable, so the lock tests below would "
            f"pass whatever the ordering is: {results[0].reaped}"
        )

    def test_the_reaper_is_never_reached_while_another_serve_holds_the_lock(
        self,
        tmp_path: Path,
    ) -> None:
        pytest.importorskip("fcntl")
        item_id = _running_item_with_a_lapsed_lease(tmp_path)
        with serve_lock(tmp_path):
            with pytest.raises(ServeLockedError):
                serve(tmp_path, once=True, runner=_runner_that_must_not_be_called)

        item = Queue(tmp_path, QueueConfig()).get(item_id)
        assert item is not None
        assert item.state is ItemState.RUNNING, (
            f"a second firing reaped a run that is still executing: the "
            f"item is {item.state.value}, not running. serve() must take "
            f"serve_lock BEFORE the cycle's reaper (#203)."
        )
        assert item.attempts == 1, (
            f"the item has been charged {item.attempts} attempts; a "
            f"refused second firing must spend nothing"
        )

    def test_the_reap_call_itself_is_not_made(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ORDER directly, rather than through a state it happens to leave.

        The test above reads the item afterwards, which cannot separate
        "the reaper never ran" from "it ran and decided not to act". This
        one records the call.
        """
        pytest.importorskip("fcntl")
        calls = _spy_on_reap(monkeypatch)
        _running_item_with_a_lapsed_lease(tmp_path)
        with serve_lock(tmp_path):
            with pytest.raises(ServeLockedError):
                serve(tmp_path, once=True, runner=_runner_that_must_not_be_called)
        assert calls == [], (
            "reap_leases ran before serve_lock was acquired, so a second "
            "scheduled firing can requeue a suspended run (#203)"
        )

    def test_the_spy_would_see_a_reap_that_did_happen(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The control for the spy. An empty log is two different things.

        ``calls == []`` is true when the lock stopped the reaper and also
        true when the patch missed its target and the spy was never
        wired in. Only running the same spy WITHOUT the lock held tells
        them apart, which is why both go through
        :func:`_spy_on_reap` rather than through two copies of it.
        """
        calls = _spy_on_reap(monkeypatch)
        _running_item_with_a_lapsed_lease(tmp_path)
        serve(tmp_path, once=True, runner=_runner_that_must_not_be_called)
        assert len(calls) == 1, (
            "the spy is not on the path serve() takes, so the assertion next door measures nothing"
        )
