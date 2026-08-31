"""#298: the ``ps`` reading itself, tested where it lives.

Before this file the parse was pinned only through two consumer suites,
each of which was really testing its own POLICY (raise vs degrade). That
left the load-bearing part - which rows count as running - reachable only
via ``tests/test_shutdown.py``'s real never-reaping-parent tree, an
expensive and timing-sensitive fixture that can only ever produce the one
zombie spelling the local kernel happens to print.

So the state matching is table-driven here against synthetic ``ps``
output, and the real tree stays where it is as the end-to-end proof that
the synthetic rows describe something that actually happens.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from kstrl.procgroup import (
    PS_ARGV,
    PS_TIMEOUT_SECONDS,
    GroupLiveness,
    _scan,
    read_group_liveness,
    signal_probe_alive,
)
from tests.helpers import procs


class TestTheScanReadsStatesNotJustGroups:
    """``_scan`` returns two bools positionally, so both are pinned."""

    @pytest.mark.parametrize(
        ("state", "counts_as_running"),
        [
            ("Ss", True),
            ("R+", True),
            ("S", True),
            ("D", True),
            ("T", True),
            # Z is the zombie state on macOS and Linux, and flags follow
            # it. Only the prefix is guaranteed, which is why the
            # production check is startswith and not equality.
            ("Z", False),
            ("Z+", False),
            ("Zl", False),
            ("Zs", False),
        ],
    )
    def test_only_a_zombie_state_is_excluded(
        self,
        state: str,
        counts_as_running: bool,
    ) -> None:
        saw_own_group, found = _scan(f"7 {state}\n9 Ss\n", pgid=7, ours=9)
        assert saw_own_group is True, "the fixture must contain our own group"
        assert found is counts_as_running

    def test_the_two_flags_are_not_transposed(self) -> None:
        """Both fields are bool, so a swap type-checks. Only a test with
        the two answers DIFFERENT can catch it."""
        saw_own_group, found = _scan("9 Ss\n", pgid=7, ours=9)
        assert (saw_own_group, found) == (True, False)

        saw_own_group, found = _scan("7 Ss\n", pgid=7, ours=9)
        assert (saw_own_group, found) == (False, True)

    def test_a_ragged_row_is_skipped_without_dropping_its_neighbours(self) -> None:
        """A row with no state column would IndexError. The rows either
        side of it must still be read, or the skip is a silent
        truncation."""
        saw_own_group, found = _scan("\n  7\n9 Ss\n7 Ss\n", pgid=7, ours=9)
        assert (saw_own_group, found) == (True, True)

    def test_a_group_id_is_matched_whole_not_as_a_prefix(self) -> None:
        """#292 in miniature: 7 must not match 70."""
        saw_own_group, found = _scan("70 Ss\n9 Ss\n", pgid=7, ours=9)
        assert (saw_own_group, found) == (True, False)


class TestTheTriState:
    """Which of the three answers each condition produces."""

    def test_a_live_group_reads_live(self) -> None:
        assert read_group_liveness(os.getpgrp()) == GroupLiveness(True)

    def test_a_nonzero_exit_is_unknown_not_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, returncode=127, stderr="ps: command not found")
        liveness = read_group_liveness(os.getpgrp())
        assert liveness.live is None
        assert "ps failed" in liveness.reason
        assert "false negative" in liveness.reason

    def test_a_missing_binary_is_unknown_not_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ps`` absent raises OSError rather than exiting non-zero."""
        procs.fake_ps(monkeypatch, raises=FileNotFoundError(2, "no ps"))
        liveness = read_group_liveness(os.getpgrp())
        assert liveness.live is None
        assert "failed to run" in liveness.reason

    def test_a_wedged_ps_is_unknown_not_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``TimeoutExpired`` is a SubprocessError, not an OSError, so it
        escapes the branch above and needs its own."""
        procs.fake_ps(
            monkeypatch,
            raises=subprocess.TimeoutExpired(cmd=list(PS_ARGV), timeout=PS_TIMEOUT_SECONDS),
        )
        assert read_group_liveness(os.getpgrp()).live is None

    def test_output_missing_our_own_group_is_unknown_not_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rc=0 with our own processes hidden, which is what a ``hidepid``
        mount looks like. Exit status alone is not enough to trust the
        silence: our group is alive by construction, so a listing without
        it is filtered and its silence about anything else means nothing."""
        procs.fake_ps(monkeypatch, stdout="    1 Ss\n  517 Ss\n")
        liveness = read_group_liveness(os.getpgrp())
        assert liveness.live is None
        assert "own group" in liveness.reason

    def test_a_trustworthy_ps_can_still_report_absence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guard must not make absence UNREPORTABLE, which would be
        the opposite failure and would poison every timed-out run."""
        ours = os.getpgrp()
        procs.fake_ps(monkeypatch, stdout=f"{ours} Ss\n")
        assert read_group_liveness(4242) == GroupLiveness(False)


class TestTheSignalProbeIsKeptAsTheDegradedReading:
    """It exists to be wrong in a known direction, so pin that."""

    def test_it_sees_a_live_group(self) -> None:
        assert signal_probe_alive(os.getpgrp()) is True

    def test_it_reports_a_group_that_is_really_gone(self) -> None:
        child = subprocess.Popen(
            ["sleep", "30"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(child.pid)
        procs.kill_group(pgid)
        child.wait(timeout=10)
        assert signal_probe_alive(pgid) is False

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

    def test_any_other_oserror_reads_as_gone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pinned rather than endorsed. This is the pre-#298 mapping,
        carried over unchanged: "gone" is the UNSAFE direction here,
        because `terminate_process_group` turns it into "reaped" and the
        item is released for another attempt. #298 shrank its reach from
        every call to only the calls where `ps` also gave no answer, and
        changing the mapping is a separate decision with its own
        reasoning."""

        def broken(pgid: int, sig: int) -> None:
            raise OSError(22, "Invalid argument")

        monkeypatch.setattr("kstrl.procgroup.os.killpg", broken)
        assert signal_probe_alive(4242) is False
