"""The agent's leash: the agent's process group ends when kstrl ends (#642).

Every agent runs in a session of its own, so killing kstrl's process
group does not reach it, and every cleanup kstrl has runs inside the
kstrl process. A SIGKILL, an OOM kill or a closed terminal runs none of
them, and a silent agent kept running and spending (measured: alive 15 s
after a SIGKILL of ``ks factory``). The leash is the one process in the
agent's group that is not the agent: it starts the agent, then blocks on
the read end of a pipe whose only write end kstrl holds. The kernel
closes that write end however kstrl ends, so the read returns EOF, and
the leash sends SIGTERM to its own group, waits until nothing but itself
is left in the group or the grace runs out, and sends SIGKILL, which is
the order ``DeadlineStreamer.kill`` uses (#641). The wait ends early only
on a reading that shows the group empty (#708); a member that is alive,
or a reading that cannot be made, keeps it waiting the whole grace.

Run by path and never imported:
``python -I -S leash.py <lifeline fd> <status fd> <grace> <nonce> -- <argv...>``.
The leash does nothing with the nonce. It is there to be READ: kstrl
records it beside the group id at spawn, and a later command names a
group as kstrl's only while that group's leader, this process, shows the
nonce in its command line, so a group id the kernel has since given to
another process is never named (#642).
``-I -S`` means nothing outside the standard library is on the path, so
a broken site or a missing kstrl dependency cannot stop it starting.
The one kstrl import, the group reading, is taken only once the owner
has gone, and an import that fails is a reading that cannot be made.

``killpg(0, ...)`` signals the caller's own group, so the leash refuses
to start (exit 70) unless it leads its own group. That is the one check
that stops it signalling kstrl's group or the operator's shell.

The leash leaves with the agent's exit status, so a caller that reads
the returncode (``verify.run_scrubbed``, #642 slice 5) reads the agent's.

The status fd carries one message back to the spawner, and only on
failure: ``errno <n>`` when the agent could not be started, ``refused``
when the leash does not lead its group. A clean start closes it with
nothing written, so the spawner raises the same ``OSError`` a direct
``Popen`` of the agent would have raised. This is CPython's own
``subprocess`` protocol for an ``exec`` failure, one level up.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

#: Exit status when the leash does not lead its own process group.
NOT_A_GROUP_LEADER = 70

#: Exit status when the agent could not be started.
SPAWN_FAILED = 71

#: Seconds between two readings of the group while the grace runs (#708).
POLL_SECONDS = 0.1


def _ignore(signum: int, frame: object) -> None:
    """Take SIGTERM and SIGINT without dying.

    A function and not ``signal.SIG_IGN``: an ignored disposition survives
    ``exec``, so the agent would start with the signal ignored. A handler
    is reset to the default by ``exec``.
    """
    del signum, frame


def _follow(agent: subprocess.Popen[bytes], firing: threading.Event) -> None:
    """Leave when the agent leaves, with its exit status, unless the leash
    is already firing.

    The wait has no deadline, and the timeout gate enrols it with this
    argument: the agent's life is bounded by kstrl's own deadline while
    kstrl is alive, and by this leash's SIGKILL of its own group, which
    ends this thread too, once kstrl is gone.

    The status is copied because ``verify.run_scrubbed`` reads it: a
    check passes on exit 0 and fails on anything else (#642 slice 5). A
    death by signal N leaves as 128 + N, the status a shell reports for
    it, because a thread other than the main one cannot reset the
    SIGTERM and SIGINT handlers it would need to die of the same signal.
    """
    status = agent.wait()
    if not firing.is_set():
        os._exit(status if status >= 0 else 128 - status)


def _wait_for_owner(lifeline: int) -> None:
    """Return once every write end of the lifeline is closed.

    An error other than EINTR (which ``os.read`` retries itself) means
    the leash cannot tell whether its owner is alive, and that is read
    as the owner being gone.
    """
    while True:
        try:
            if not os.read(lifeline, 1):
                return
        except OSError:
            return


def _others_in_group() -> bool:
    """Whether a process other than the leash may still be in its group.

    False only when one ``ps`` reading lists no running member but the
    leash and pids that are gone by the time it is checked. The ``ps``
    that made the reading is in the group while it runs, so it is listed,
    and it is gone once the reading returns. The reading is kstrl's own,
    ``read_group_members``, so the tree keeps one ``ps`` parse. Anything
    that stops the reading, an import that fails included, is True: the
    leash then waits the whole grace, as it did before #708.
    """
    try:
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.append(root)
        from kstrl.procgroup import pid_is_alive, read_group_members

        pids = read_group_members(os.getpid()).pids
        return pids is None or any(pid != os.getpid() and pid_is_alive(pid) for pid in pids)
    except Exception:  # noqa: BLE001 - a reading not made may hide a member
        return True


def _wait_out(grace: float) -> None:
    """Return once the group holds nothing but the leash, or after ``grace``.

    The SIGKILL after this is sent either way: a member that started after
    the last reading is still killed.
    """
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and _others_in_group():
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def main(argv: list[str]) -> int:
    if len(argv) < 7 or argv[5] != "--":
        raise ValueError(
            f"usage: leash.py <lifeline fd> <status fd> <grace> <nonce> -- <argv...>: {argv}"
        )
    lifeline, status, grace = int(argv[1]), int(argv[2]), float(argv[3])
    command = argv[6:]
    if os.getpgrp() != os.getpid():
        os.write(status, b"refused")
        return NOT_A_GROUP_LEADER
    signal.signal(signal.SIGTERM, _ignore)
    signal.signal(signal.SIGINT, _ignore)
    try:
        agent = subprocess.Popen(command)
    except OSError as exc:
        os.write(status, f"errno {exc.errno or 0}".encode())
        return SPAWN_FAILED
    os.close(status)
    firing = threading.Event()
    threading.Thread(target=_follow, args=(agent, firing), daemon=True).start()
    _wait_for_owner(lifeline)
    firing.set()
    os.killpg(0, signal.SIGTERM)
    _wait_out(grace)
    os.killpg(0, signal.SIGKILL)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
