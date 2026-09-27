"""#298: what ``kstrl/procgroup.py`` makes of a real process group.

Three answers and no fourth: live, gone, or unknown. The reading itself
and the parse of it belong to ``kstrl/procgroup_listing.py`` and are
tested in ``tests/test_procgroup_listing.py``, which #366 split out of
this file for the 800-line ratchet. What remains here runs against the
caller's own group and against a group the kernel has already reaped:
the kernel's ESRCH is the one conclusive emptiness, a real ``ps`` read
of a reaped group is gone, and the signal probe, kept as the degraded
reading, sees a live group and reports a gone one.
"""

from __future__ import annotations

import os

from kstrl.procgroup import (
    GroupLiveness,
    _kernel_says_group_is_empty,
    read_group_liveness,
    signal_probe_alive,
)
from tests.helpers import procs


class TestAGoneIsOnlyReportedWhenItIsEvidence:
    def test_a_missing_group_the_kernel_calls_empty_is_gone(self) -> None:
        """The trustworthy route: the kernel, not the listing."""
        assert read_group_liveness(procs.dead_group()) == GroupLiveness(False)


class TestTheKernelControl:
    """ESRCH is the only conclusive emptiness, so pin that it is the only one."""

    def test_a_dead_group_is_empty(self) -> None:
        assert _kernel_says_group_is_empty(procs.dead_group()) is True

    def test_our_own_group_is_not_empty(self) -> None:
        assert _kernel_says_group_is_empty(os.getpgrp()) is False


class TestTheSignalProbeIsKeptAsTheDegradedReading:
    """It exists to be wrong in a known direction, so pin that."""

    def test_it_sees_a_live_group(self) -> None:
        assert signal_probe_alive(os.getpgrp()) is True

    def test_it_reports_a_group_that_is_really_gone(self) -> None:
        assert signal_probe_alive(procs.dead_group()) is False
