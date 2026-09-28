"""#298: what ``kstrl/procgroup.py`` makes of a real process group.

Three answers and no fourth: live, gone, or unknown. The reading itself
and the parse of it belong to ``kstrl/procgroup_listing.py`` and are
tested in ``tests/test_procgroup_listing.py``, which #366 split out of
this file for the 800-line ratchet. What remains here runs against the
caller's own group and against a pgid no group holds
(``procs.no_such_group``, #686): the kernel's ESRCH is the one
conclusive emptiness, a real ``ps`` read of such an id is gone, and the
signal probe, kept as the degraded reading, sees a live group and
reports a gone one.
"""

from __future__ import annotations

import os
import threading
import time

from kstrl.procgroup import (
    GroupLiveness,
    _kernel_says_group_is_empty,
    pid_is_alive,
    read_group_liveness,
    signal_probe_alive,
)
from tests.helpers import procs


class TestAGoneIsOnlyReportedWhenItIsEvidence:
    def test_a_missing_group_the_kernel_calls_empty_is_gone(self) -> None:
        """The trustworthy route: the kernel, not the listing."""
        with procs.no_such_group() as pgid:
            assert read_group_liveness(pgid) == GroupLiveness(False)


class TestTheKernelControl:
    """ESRCH is the only conclusive emptiness, so pin that it is the only one."""

    def test_a_dead_group_is_empty(self) -> None:
        with procs.no_such_group() as pgid:
            assert _kernel_says_group_is_empty(pgid) is True

    def test_our_own_group_is_not_empty(self) -> None:
        assert _kernel_says_group_is_empty(os.getpgrp()) is False


class TestTheSignalProbeIsKeptAsTheDegradedReading:
    """It exists to be wrong in a known direction, so pin that."""

    def test_it_sees_a_live_group(self) -> None:
        assert signal_probe_alive(os.getpgrp()) is True

    def test_it_reports_a_group_that_is_really_gone(self) -> None:
        with procs.no_such_group() as pgid:
            assert signal_probe_alive(pgid) is False


class TestAnEmptyGroupStaysEmptyWhileTheSuiteSpawnsInParallel:
    """#686: the pgid the liveness tests assert on cannot read as occupied.

    Those tests took a pgid from a helper that killed a group, reaped its
    leader and returned the id. On macOS 26.6.2 the kernel went on
    answering EPERM for that group after ``wait`` returned, in 0.014 to
    0.42 percent of calls while several threads spawned and reaped, so a
    test asserting emptiness failed with the message that means the group
    is occupied. Here eight threads spawn and release at once, each doing
    what every caller of ``procs.no_such_group`` does.

    Two assertions per probe, and the second is what makes the first
    hold. The kernel reports no such group, asked through
    ``signal_probe_alive`` because it reads the kernel and nothing else: a
    ``ps`` read forks first and outlasts the window (0 refusals in 6888
    measured). And the pid that is the id is still allocated, which is
    what stops any process group taking that id while the test holds it.
    The old helper fails the second on every probe and the first only at
    the rates above.
    """

    THREADS = 8
    PROBES_PER_THREAD = 50
    #: A mutation that wedges a spawn fails here as hung, not as a hang.
    FUSE_SECONDS = 120.0

    def test_every_probe_of_a_held_pgid_reads_empty(self) -> None:
        occupied: list[int] = []
        released: list[int] = []
        lock = threading.Lock()

        def probe() -> None:
            for _ in range(self.PROBES_PER_THREAD):
                with procs.no_such_group() as pgid:
                    empty = not signal_probe_alive(pgid)
                    held = pid_is_alive(pgid)
                with lock:
                    if not empty:
                        occupied.append(pgid)
                    if not held:
                        released.append(pgid)

        threads = [threading.Thread(target=probe, daemon=True) for _ in range(self.THREADS)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.FUSE_SECONDS
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        assert not any(thread.is_alive() for thread in threads), (
            f"the probes outlived their {self.FUSE_SECONDS}s fuse (hung, not failed)"
        )
        probes = self.THREADS * self.PROBES_PER_THREAD
        assert (occupied, released) == ([], []), (
            f"{len(occupied)} of {probes} probes read an empty group as occupied "
            f"{occupied[:10]}, and {len(released)} of {probes} ids were not held "
            f"for the block, so a group could take them mid-test {released[:10]}"
        )
