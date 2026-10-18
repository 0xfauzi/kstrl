"""The daemon's child processes: one supervised factory run, its process group, and dead
leases.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from kstrl.proc import (
    drain_or_abandon,
    pid_is_alive,
    read_group_liveness,
    safe_pgid,
    signal_group,
    signal_probe_alive,
)
from kstrl.queue import ItemState, Queue
from kstrl.serve.serve_config import ServeObserver
from kstrl.serve.serve_outcome import RunOutcome
from kstrl.serve.serve_spend import _iso, _utc_now

#: Retry backoff: ``base * 2 ** (attempts - 1)``, capped. Not tunable by
#: config on purpose - the cap is a safety floor on how fast an item can
#: consume its attempts, not a preference.
BACKOFF_BASE_SECONDS = 60.0


BACKOFF_CAP_SECONDS = 1800.0


def backoff_seconds(attempts: int) -> float:
    """Delay before an item's next attempt."""
    if attempts <= 0:
        return 0.0
    grown = BACKOFF_BASE_SECONDS * float(2 ** (attempts - 1))
    return min(grown, BACKOFF_CAP_SECONDS)


def _pid_alive(pid: int, host: str) -> bool:
    """Whether a lease holder is still running.

    A lease from ANOTHER host is treated as alive: we cannot probe a
    foreign pid, and two-machine operation is an explicit R8.6 non-goal,
    so the TTL remains the only signal there rather than us reaping work
    that may be in flight elsewhere.

    The probe itself is :func:`kstrl.proc.procgroup.pid_is_alive`. It was a
    fourth hand-rolled copy of the same shape - a pid guard written
    inline, next to a raw signal - which is the arrangement #308 removed
    for groups and #329 found still standing for bare ids. What stays
    here is the only part that is about leases: the host check.
    """
    if host and host != socket.gethostname():
        return True
    return pid_is_alive(pid)


@dataclass(frozen=True)
class ReapResult:
    """What one reaper pass did, for logging and tests."""

    requeued: tuple[str, ...] = ()
    poisoned: tuple[str, ...] = ()
    failed_for_retry: tuple[str, ...] = ()


def reap_leases(
    queue: Queue,
    *,
    now: datetime | None = None,
    actor: str = "reaper",
) -> ReapResult:
    """Recover items whose owner died or slept through its lease.

    The LEASED and RUNNING cases are genuinely different and that
    difference is the reason PR 1 kept two states:

    - A dead ``leased`` item spent nothing (the attempt is charged on the
      transition INTO running), so it goes back to ``queued`` untouched.
    - A dead ``running`` item spent real money. Its attempt is already
      charged, so it is treated as an infrastructure casualty - which is
      what a suspend or an OOM kill actually is - and retried only if it
      has attempts left. Otherwise it poisons.
    """
    moment = now or _utc_now()
    requeued: list[str] = []
    poisoned: list[str] = []
    failed: list[str] = []

    for item in queue.items((ItemState.LEASED,)):
        if item.lease_expired(moment) or not _pid_alive(
            item.lease_pid,
            item.lease_host,
        ):
            queue.requeue(
                item,
                reason="reaped: lease holder gone before any spend",
                actor=actor,
                not_before="",
            )
            requeued.append(item.item_id)

    for item in queue.items((ItemState.RUNNING,)):
        if not (item.lease_expired(moment) or not _pid_alive(item.lease_pid, item.lease_host)):
            continue
        detail = (
            f"run interrupted (lease holder pid {item.lease_pid} on "
            f"{item.lease_host or 'unknown host'} is gone or its lease "
            "lapsed); classified as infrastructure"
        )
        queue.finish_failed(item, error=detail, actor=actor)
        reread = queue.get(item.item_id)
        if reread is None:
            continue
        if reread.attempts_remaining > 0:
            queue.requeue(
                reread,
                reason="reaped: retrying an interrupted run",
                actor=actor,
                not_before=_iso(moment + timedelta(seconds=backoff_seconds(reread.attempts))),
            )
            failed.append(item.item_id)
        else:
            queue.poison(
                reread,
                reason=(f"{detail}; no attempts left ({reread.attempts}/{reread.max_attempts})"),
                actor=actor,
            )
            poisoned.append(item.item_id)

    return ReapResult(
        requeued=tuple(requeued),
        poisoned=tuple(poisoned),
        failed_for_retry=tuple(failed),
    )


def caffeinate_prefix(enabled: bool) -> list[str]:
    """``caffeinate -i`` when it is available and wanted.

    ``-i`` prevents idle SLEEP without keeping the display awake. The
    assertion lasts only as long as one run, which is what lets the
    laptop sleep between items instead of being pinned awake by the
    daemon itself.

    HOW IT IS HELD (measured for #209 on macOS 26.6.2, Darwin 25.6.0;
    the earlier text here said "it wraps the child process", which is
    not the shape). ``caffeinate -i <utility>`` does not exec the
    utility in place: it FORKS. The process the daemon spawned keeps the
    pid ``Popen`` returned and becomes the utility, and a second
    ``caffeinate`` process appears as a CHILD of it, in the same process
    group and session. That child is the assertion holder, so the
    assertion is not inherited across an exec by the factory. ``pmset -g
    assertions`` prints two lines for it: a row header keyed on the
    HELPER's pid whose ``named:`` field is ``"caffeinate command-line
    tool"``, and a ``Details:`` line under it reading ``caffeinate
    asserting on behalf of '<utility>' (pid <utility pid>)``, which
    names the pid the daemon holds. Measured lifetime: the helper is
    gone at the first sample after the utility exits, and gone after a
    ``killpg`` on the spawn-time group, with the ``pmset`` row gone in
    both cases. It leaks neither a process nor a power assertion.

    THE HELPER ALSO HOLDS THE RUN'S PIPE. It inherits fds 1 and 2, so
    ``lsof`` shows the same pipe against the utility and the helper.
    ``run_supervised``'s ``communicate()`` therefore cannot see EOF
    until the HELPER exits, not the factory, and
    ``ServeConfig.factory_timeout_seconds`` defaults to ``0.0``, which
    that call turns into ``timeout=None``. Latent rather than active:
    measured over 8 spawns, the group is empty at the first sample after
    the utility exits, so the wait is bounded by a helper that leaves
    within one ``ps`` read.

    Because the helper is INSIDE the spawned group, the group kill on
    the timeout path releases the power assertion as well as reaping the
    factory. ``tests/test_serve_process_tree.py`` pins that membership as
    the DIFFERENCE the prefix makes rather than as a constant: the group
    holds the factory, whatever the factory spawns, and with caffeinate
    one more. Its census parametrizes a childless utility (2 members with
    caffeinate, 1 without) and one that spawns a child (3 and 2).
    """
    if not enabled or sys.platform != "darwin":
        return []
    binary = shutil.which("caffeinate")
    return [binary, "-i"] if binary else []


def subprocess_factory_runner(
    *,
    root_dir: Path,
    spec_path: Path,
    project_name: str,
    pause_before_pr_merge: bool,
    design_acceptance: bool,
    timeout_seconds: float,
    on_spawn: Callable[[int], None] | None = None,
    caffeinate: bool = True,
) -> RunOutcome:
    """Run ``ks factory`` as a supervised child process GROUP.

    A subprocess rather than an in-process ``run_factory`` call for three
    reasons: a crash or OOM in a run cannot take the daemon with it, the
    ``.kstrl/factory.lock`` flock is released by process death whatever
    happens, and a timeout is enforceable. The cost of that choice is
    that classification reads artifacts from disk instead of holding a
    ``FactoryResult`` - which is also what makes classification work
    after a crash.

    **Why a process group and not ``subprocess.run(timeout=...)``.**
    That helper signals only its DIRECT child, and a factory is not one
    process: it spawns agent subprocesses, git and the verify commands,
    and those outlive a signal to the direct child. That is what #186 F1
    reproduced - descendants still running after ``TimeoutExpired``,
    while this code recorded an infrastructure failure and requeued the
    item, two factories on one repo, which is exactly what
    ``factory.lock`` exists to prevent. So: ``start_new_session=True``
    puts the child in its own process group, and the timeout path
    signals the GROUP and waits for it. The shape is still measured by
    ``tests/test_serve.py::TestProcessGroupSupervision``.

    THE GROUP IS NOT A CAFFEINATE WORKAROUND, and this docstring used to
    say it was: "on macOS the direct child is the ``caffeinate``
    wrapper, so the factory itself is a grandchild". Both halves are
    false. The daemon's direct child IS the factory (see
    :func:`caffeinate_prefix`: caffeinate forks a helper into the run's
    group rather than exec'ing the utility), and a descendant was
    measured surviving a direct-child kill with ``caffeinate`` on and
    off alike (#209, macOS 26.6.2). So the group path must NOT be made
    conditional on ``caffeinate``: turning the flag off removes the
    helper, not the factory's own descendants.

    ``on_spawn`` receives the child's pid so the caller can make it the
    lease owner. Without that the lease records the DAEMON's pid, and a
    successor daemon would see the daemon gone, judge the lease dead, and
    requeue a run that is still executing.
    """
    command = [
        *caffeinate_prefix(caffeinate),
        sys.executable,
        "-m",
        "kstrl",
        "factory",
        "--spec",
        str(spec_path),
        "--project-name",
        project_name,
        "--root",
        str(root_dir),
        "--yes",
        "--ui",
        "plain",
        "--no-color",
    ]
    command.append(
        "--pause-before-pr-merge" if pause_before_pr_merge else "--no-pause-before-pr-merge"
    )
    if design_acceptance:
        # Only a bug report sets the queue item's flag (#654), and a bug
        # report must be reproduced on the base (#700).
        command += ["--design-acceptance", "--bug-report"]
    env = dict(os.environ)

    return run_supervised(
        command,
        cwd=root_dir,
        env=env,
        timeout_seconds=timeout_seconds,
        on_spawn=on_spawn,
    )


#: How long a killed factory child is given to hand back its output
#: before it is abandoned to ``procdispose``. Was a bare ``10`` inline; it
#: is named because it is the last bound on a timed-out run, and it sits
#: ABOVE its two uses because a reader who meets the name first has to
#: scroll to find out what it is worth.
ABANDON_GRACE_SECONDS = 10.0


def run_supervised(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 0.0,
    on_spawn: Callable[[int], None] | None = None,
) -> RunOutcome:
    """Run ``command`` in its own process group, enforcing a deadline.

    Split out of :func:`subprocess_factory_runner` so the supervision -
    the money-critical half - is testable without spawning a factory. The
    runner above only builds the argv.
    """
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            # The whole point: an own session/group so descendants are
            # signallable together.
            start_new_session=True,
        )
    except OSError as exc:
        return RunOutcome(returncode=-1, launch_error=str(exc))

    # Captured now, while the child is certainly alive: after it is
    # reaped the pgid is unrecoverable (#186 F1).
    pgid: int | None = safe_pgid(process)

    if on_spawn is not None:
        on_spawn(process.pid)

    try:
        output, _ = process.communicate(
            timeout=timeout_seconds if timeout_seconds > 0 else None,
        )
        return RunOutcome(
            returncode=process.returncode,
            output_tail=_tail(output),
        )
    except subprocess.TimeoutExpired:
        termination = terminate_process_group(process, pgid)
        # #326: this used to drain the pipes itself and, when the drain
        # expired, set `output = ""` and drop the child with no close and
        # no register. The daemon holds its singleton lock for its whole
        # life, so a child left to `Popen.__del__` here is the worst
        # placed one in the tree: under `PYTHONWARNINGS=error` that
        # `__del__` raises before it registers anything and the zombie
        # outlives the daemon.
        output, _ = drain_or_abandon(process, ABANDON_GRACE_SECONDS)
        return RunOutcome(
            returncode=(process.returncode if process.returncode is not None else -9),
            timed_out=True,
            output_tail=_tail(output),
            group_reaped=termination.reaped,
            group_reap_detail=termination.degraded,
            group_occupied_detail=termination.occupied,
        )
    except BaseException:
        # The same rule as `verify.run_scrubbed` and
        # `procgroup._read_ps`: a timeout is not the only way out of a
        # `communicate`. This one matters most of the three, because the
        # daemon holds its singleton lock for its whole process
        # lifetime, so a child dropped here outlives every run after it.
        drain_or_abandon(process, ABANDON_GRACE_SECONDS)
        raise


#: How much of a child's output to keep. Enough for the exit-2 markers
#: and a human-readable tail, not enough to hold a whole factory log.
OUTPUT_TAIL_CHARS = 8000


def _tail(output: str | None) -> str:
    if not output:
        return ""
    return output[-OUTPUT_TAIL_CHARS:]


#: Grace period between SIGTERM and SIGKILL for a run's process group.
GROUP_TERM_GRACE_SECONDS = 15.0


#: How long the escalation waits after SIGKILL before reporting the group
#: as still occupied. A DIFFERENT quantity from
#: ``ABANDON_GRACE_SECONDS`` despite both reading 10.0 - that one bounds
#: a drain of a child's output, this one bounds a wait for a group to
#: empty - so they are two names rather than one shared constant.
GROUP_KILL_GRACE_SECONDS = 10.0


#: How often to re-check the group while waiting out a signal's grace
#: period. Each check forks ``ps`` at ~11ms, so 0.25s costs ~4% of a core
#: on a path that runs once per timed-out run.
GROUP_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class GroupTermination:
    """Whether the group was confirmed reaped, and what is known if not.

    A bare bool was not enough: `serve_cycle` turns `reaped=False` into a
    poison reason telling the operator a factory may still be running,
    and the three ways of not being sure need different words. The two
    strings are kept apart because conflating them told the operator the
    OPPOSITE of the truth in the one case where they must act.
    """

    reaped: bool
    #: Why the check could not MEASURE. Empty when it did. This is the
    #: "unknown" case: an unreadable `ps`, a filtered listing.
    degraded: str = ""
    #: Positive evidence the group IS occupied. EPERM from `killpg` means
    #: the kernel found processes in that group and refused us, so the
    #: group is definitely not empty. Reporting that as "unknown" would
    #: downgrade the strongest signal a factory is alive.
    occupied: str = ""


def terminate_process_group(
    process: subprocess.Popen[str],
    pgid: int | None = None,
) -> GroupTermination:
    """SIGTERM then SIGKILL a child's whole process group.

    ``reaped`` is False when the group could not be confirmed gone, so a
    caller never reports a timed-out run as finished while a factory may
    still be writing to the repo (#186 F1).

    ESCALATION IS DRIVEN BY THE GROUP, NOT BY THE DIRECT CHILD. An
    earlier version returned as soon as ``process.wait()`` returned, which
    made the SIGKILL leg reachable only when the DIRECT CHILD outlived the
    grace period. Measured: a leader that dies on SIGTERM with a
    descendant holding ``SIG_IGN`` for it returned
    ``GroupTermination(reaped=False)`` in 0.07s with the descendant still
    running and SIGKILL never sent, so nothing ever killed it and a
    factory kept writing to the repo. That is the exact hazard this
    function exists to prevent, so the grace period is now spent watching
    the GROUP and the next signal is sent whenever anything is left.

    ``pgid`` should be captured at SPAWN time. Looking it up here fails
    once the direct child has been reaped - and that is precisely the
    case where descendants may still be alive, so deriving it lazily
    would report "reaped" for the one situation this function exists to
    detect. Caught by the test that kills only the direct child.
    """
    if pgid is None:
        pgid = safe_pgid(process)
    if pgid is None:
        # No group to signal and no id to check. Whether the direct child
        # is gone is the most that can honestly be said, and saying so is
        # not the same as having measured the group.
        return GroupTermination(
            process.poll() is not None,
            degraded=(
                "no process-group id was available, so only the direct "
                "child was inspected and any descendant it left is "
                "unaccounted for"
            ),
        )

    outcome = GroupTermination(False, degraded="the group was never signalled")
    for sig, grace in (
        (signal.SIGTERM, GROUP_TERM_GRACE_SECONDS),
        (signal.SIGKILL, GROUP_KILL_GRACE_SECONDS),
    ):
        attempt = signal_group(pgid, sig)
        if attempt.vanished:
            return GroupTermination(True)
        if not attempt.sent:
            # EPERM is not "could not measure": the kernel FOUND processes
            # in this group and refused us. That is evidence of a live
            # group, and the operator has to act on it. A pgid `procgroup`
            # itself refuses is the OTHER column - evidence of nothing,
            # only that this integer must not be signalled - which is
            # #329: this used to take the caller's pgid straight to
            # `os.killpg` with no check of any kind.
            return GroupTermination(False, occupied=attempt.denied, degraded=attempt.refused)
        outcome = _wait_out_grace(process, pgid, grace)
        if outcome.reaped:
            return outcome
    return outcome


def _wait_out_grace(
    process: subprocess.Popen[str],
    pgid: int,
    grace: float,
) -> GroupTermination:
    """Reap the direct child, then watch the GROUP until it goes or time runs out."""
    deadline = time.monotonic() + grace
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    outcome = _confirm_group_gone(pgid)
    while not outcome.reaped and time.monotonic() < deadline:
        time.sleep(GROUP_POLL_SECONDS)
        outcome = _confirm_group_gone(pgid)
    return outcome


def _confirm_group_gone(pgid: int) -> GroupTermination:
    alive, degraded = _group_liveness_for_reap(pgid)
    return GroupTermination(not alive, degraded=degraded)


def _group_liveness_for_reap(pgid: int) -> tuple[bool, str]:
    """(is anything RUNNING in ``pgid``, why that answer is degraded).

    A zombie does not count. It has already died and is only waiting to
    be reaped, so a caller asking "may a factory still be writing to this
    repo" is answered No by it, and a signal probe answers Yes (#298; the
    reasoning and the measurements are in ``kstrl.proc.procgroup``).

    FAIL DIRECTION when ``ps`` cannot be trusted: fall back to the signal
    probe. That is nearly the pre-#298 behaviour: #309 round 1 fixed the
    one branch of it that reported an unexplained error as GONE, so the
    claim below about its error direction is now true rather than
    aspirational. That is a deliberate choice
    between three options and not a default. Raising would take the
    daemon down over a diagnostic. Reporting "alive" unconditionally is
    the conservative direction for the one caller, but on a machine with
    no ``ps`` it makes EVERY timed-out run unreapable and therefore
    poisoned, which trades a rare wrong answer for a permanent one. The
    probe is right whenever the group is genuinely gone, and its only
    error is over-reporting alive - the safe direction for
    ``terminate_process_group``, which then declines to call the run
    reaped. So the degraded path is no worse than what this function did
    everywhere before, and the ``ps`` path is exact.

    The degrade is RETURNED, not warned. An earlier version called
    ``warnings.warn`` here, which under ``PYTHONWARNINGS=error`` (a common
    CI setting) RAISES out of the reap check: measured, it escaped
    ``run_supervised``'s ``except subprocess.TimeoutExpired``, which does
    not catch ``UserWarning``, and out of ``serve_cycle``, which wraps
    nothing - so on a machine without ``ps`` one timed-out run crashed the
    daemon. That is the same crash the undecodable-listing test exists to
    prevent, through a different door. The message also interpolated the
    pgid and an exception repr, so every such run added a distinct
    permanent key to ``__warningregistry__``.

    So the reason travels as a value and ``serve_cycle`` reports it
    through the ``ServeObserver`` the daemon already threads everywhere
    else, which is bounded, is the operator's real log channel, and
    cannot raise.
    """
    liveness = read_group_liveness(pgid)
    if liveness.live is not None:
        return liveness.live, ""
    return signal_probe_alive(pgid), (
        f"{liveness.reason} Falling back to a signal probe, which counts "
        f"an unreaped zombie as alive, so group {pgid} may be reported as "
        f"running when nothing in it is."
    )


def _report_reap_degrade(obs: ServeObserver, outcome: RunOutcome) -> None:
    """Say out loud when the reap check could not measure.

    Reported on BOTH paths, and the reaped one is why this exists. When
    ``ps`` is blind the fallback signal probe can answer "gone", the run
    is called reaped and the item is released for another attempt - the
    unsafe direction, reached on a doubly-unverified answer, and until
    this was added it was recorded nowhere at all. The poison reason
    covers the not-reaped path; nothing covered this one.
    """
    if not outcome.timed_out or not outcome.group_reap_detail:
        return
    if outcome.group_reaped:
        obs.warn(
            f"  the timed-out run was released as reaped on an UNMEASURED "
            f"check: {outcome.group_reap_detail}"
        )
        return
    obs.warn(f"  the reap check could not measure: {outcome.group_reap_detail}")


def _unreaped_timeout_detail(outcome: RunOutcome) -> str:
    """The poison reason for a timed-out run whose group was not confirmed gone.

    Three different things are worth three different sentences, and
    collapsing them is how an operator acts on the wrong one. Positive
    evidence the group is occupied must NOT be softened into "unknown";
    an unmeasurable check must not be reported as a live factory.
    """
    detail = (
        "the run timed out and its process group could not be confirmed "
        "reaped, so a factory may still be executing against this repo"
    )
    if outcome.group_occupied_detail:
        return (
            f"{detail}. The group is CONFIRMED occupied, not merely "
            f"unconfirmed: {outcome.group_occupied_detail}"
        )
    if outcome.group_reap_detail:
        return (
            f"{detail}. The check could not measure, so this is 'unknown' "
            f"rather than 'still running': {outcome.group_reap_detail}"
        )
    return detail
