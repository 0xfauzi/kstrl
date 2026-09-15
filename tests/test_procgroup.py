"""#298: what ``kstrl/procgroup.py`` makes of a ``ps`` reading.

Three answers and no fourth: live, gone, or unknown. The reading itself
and the parse of it belong to ``kstrl/procgroup_listing.py`` and are
tested in ``tests/test_procgroup_listing.py``, which #366 split out of
this file for the 800-line ratchet.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from kstrl.procgroup import (
    PS_ARGV,
    PS_TIMEOUT_SECONDS,
    GroupLiveness,
    _kernel_says_group_is_empty,
    read_group_liveness,
    signal_probe_alive,
)
from tests.helpers import procs


class TestAGoneIsOnlyReportedWhenItIsEvidence:
    """The safety argument, and the two controls that could not carry it.

    The first asked whether the caller's own group appeared in the
    listing. Measured: the read does not setpgid, so the `ps` child runs
    in the caller's group and `ps -A` always lists it. It was satisfied
    by construction, and `_read_ps` keeps it that way by declining
    `start_new_session`.

    The second was "every listed row for this group is a zombie, so the
    listing can see this group". Seeing SOME of a group is not seeing all
    of it: `hidepid` hides individual PROCESSES by uid, so a group can
    show a visible zombie while hiding a running descendant that changed
    uid, which is the threat the module docstring names.

    What replaced both: pid 1 in the listing proves the view is not
    filtered to our uid, because pid 1 belongs to root and, if we are
    root, nothing is hidden from us anyway.
    """

    def test_a_complete_listing_showing_only_zombies_is_gone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#298's case, and it still needs the completeness control."""
        procs.fake_ps(monkeypatch, stdout="1 1 Ss\n50 4242 Z\n51 4242 Z+\n")
        assert read_group_liveness(4242) == GroupLiveness(False)

    def test_zombies_in_a_FILTERED_listing_are_not_gone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The round-2 hole. Same rows, no pid 1: a running descendant
        that changed uid would be invisible, so "only zombies here" is an
        inference about a listing that is known to be partial. Reporting
        gone would let serve_cycle requeue onto a live repo (#186 F1)."""
        procs.fake_ps(monkeypatch, stdout="50 4242 Z\n51 4242 Z+\n")
        liveness = read_group_liveness(4242)
        assert liveness.live is None
        assert "did not list pid 1" in liveness.reason
        assert "another uid" in liveness.reason

    def test_a_missing_group_the_kernel_calls_empty_is_gone(self) -> None:
        """The other trustworthy route: the kernel, not the listing."""
        assert read_group_liveness(procs.dead_group()) == GroupLiveness(False)

    def test_a_missing_group_the_kernel_calls_occupied_is_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Our own group is alive by construction, and pid 1 is listed so
        # the view is complete; the group simply is not in it.
        procs.fake_ps(monkeypatch, stdout="1 1 Ss\n")
        liveness = read_group_liveness(os.getpgrp())
        assert liveness.live is None
        assert "kernel reports" in liveness.reason

    def test_a_running_row_still_reads_live(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live needs no control: seeing a runner is positive evidence,
        and a filtered listing can only ever show FEWER processes."""
        procs.fake_ps(monkeypatch, stdout="50 4242 Ss\n")
        assert read_group_liveness(4242) == GroupLiveness(True)


class TestTheKernelControl:
    """ESRCH is the only conclusive emptiness, so pin that it is the only one."""

    def test_a_dead_group_is_empty(self) -> None:
        assert _kernel_says_group_is_empty(procs.dead_group()) is True

    def test_our_own_group_is_not_empty(self) -> None:
        assert _kernel_says_group_is_empty(os.getpgrp()) is False

    def test_a_refused_signal_is_not_emptiness(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EPERM means something is there refusing us. Reading it as
        emptiness is the false negative this control exists to stop."""

        def refuse(pgid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr("kstrl.procgroup.os.killpg", refuse)
        assert _kernel_says_group_is_empty(4242) is False

    def test_an_unanswered_question_is_not_emptiness(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def broken(pgid: int, sig: int) -> None:
            raise OSError(22, "Invalid argument")

        monkeypatch.setattr("kstrl.procgroup.os.killpg", broken)
        assert _kernel_says_group_is_empty(4242) is False


class TestTheTriStateWhenPsGivesNoAnswer:
    """A ``ps`` that RAISED, reported as unknown rather than as absent.

    The two cases below are the ones whose exception type is the subject.
    Its non-zero-exit and missing-binary siblings moved to
    ``tests/test_procgroup_members.py`` in #209 round 3, to sit with the
    count twins that make the same assertion from the other side: those
    four are one matched set, and splitting them across two files is what
    let the count half go a round without being able to fail.
    """

    def test_a_wedged_ps_is_unknown_not_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``TimeoutExpired`` is a SubprocessError, not an OSError, so it
        escapes that branch and needs its own."""
        procs.fake_ps(
            monkeypatch,
            raises=lambda: subprocess.TimeoutExpired(cmd=list(PS_ARGV), timeout=PS_TIMEOUT_SECONDS),
        )
        assert read_group_liveness(os.getpgrp()).live is None

    def test_an_undecodable_listing_is_unknown_rather_than_a_crash(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """UnicodeDecodeError is a ValueError, so a fail-closed
        ``except OSError`` would let it escape. It would then propagate
        through `process_group_alive` and `terminate_process_group` into
        `run_supervised`'s `except TimeoutExpired`, which does not catch
        it, and out of `serve_cycle` uncaught, taking the daemon down over
        a diagnostic that the docstring promises will never do that. The
        read pins encoding='utf-8', errors='replace' so it cannot arise;
        this pins that it is caught even if that changes.
        """
        procs.fake_ps(
            monkeypatch,
            raises=lambda: UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
        )
        liveness = read_group_liveness(os.getpgrp())
        assert liveness.live is None
        assert "failed to run" in liveness.reason


class TestTheSignalProbeIsKeptAsTheDegradedReading:
    """It exists to be wrong in a known direction, so pin that."""

    def test_it_sees_a_live_group(self) -> None:
        assert signal_probe_alive(os.getpgrp()) is True

    def test_it_reports_a_group_that_is_really_gone(self) -> None:
        assert signal_probe_alive(procs.dead_group()) is False

    def test_a_refused_signal_reads_as_alive(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The branch #298 is about. Measured on a real zombie-only group,
        macOS raises EPERM rather than succeeding, so this mapping is what
        made a corpse read as running. ``tests/test_shutdown.py`` proves
        it on a real tree; this pins the mapping itself."""

        def refuse(pgid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr("kstrl.procgroup.os.killpg", refuse)
        assert signal_probe_alive(4242) is True

    @pytest.mark.parametrize(
        "errno_and_text",
        [
            (22, "Invalid argument"),
            # The errno #309 round 1 reproduced it with.
            (5, "Input/output error"),
        ],
    )
    def test_an_unexplained_error_reads_as_alive_not_gone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        errno_and_text: tuple[int, str],
    ) -> None:
        """#309 round 1, F1, and the assertion that was inverted before it.

        This used to assert False, pinning the pre-#298 mapping as
        "endorsed by nobody but carried over unchanged". "Gone" is the
        unsafe direction, for the reason the `kstrl.procgroup` module
        docstring opens with. It survived while this function was nearly
        unreachable; #309 made it the routine fallback for every read
        `ps` cannot answer, so it had to be decided rather than deferred.

        Flipping this assertion is the point of the test: it fails on the
        other choice, which is what the old one could not do for the
        choice it pinned.
        """
        number, text = errno_and_text

        def broken(pgid: int, sig: int) -> None:
            raise OSError(number, text)

        monkeypatch.setattr("kstrl.procgroup.os.killpg", broken)
        assert signal_probe_alive(4242) is True

    def test_esrch_is_still_the_one_thing_that_means_gone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The control for the test above. Without it, that one would
        pass just as well on a function that had been made to return True
        unconditionally, which measures nothing."""

        def absent(pgid: int, sig: int) -> None:
            raise ProcessLookupError(3, "No such process")

        monkeypatch.setattr("kstrl.procgroup.os.killpg", absent)
        assert signal_probe_alive(4242) is False
