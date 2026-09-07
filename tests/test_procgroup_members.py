"""#209: `_Listing` keeps pids, and `read_group_members` reads them.

Split from ``tests/test_procgroup.py`` because that file was at 796 of
the 800-line ratchet, not because the subject is separate: this is
``kstrl/procgroup.py``, and everything the module's docstring says about
there being ONE ``ps`` parse in this tree applies here.

The reading exists because #209 asks a question liveness cannot answer.
Whether ``caffeinate -i`` puts its forked assertion holder INSIDE the
run's process group is a question about MEMBERSHIP: a helper that escaped
the group would survive the timeout path's ``killpg`` still holding
``PreventUserIdleSystemSleep``, and a bool cannot see that.

So ``_Listing`` carries the pids and derives its two counts, and the
tests whose expected values are those tuples live here with it.
"""

from __future__ import annotations

import os

import pytest

from kstrl.procgroup import (
    GroupMembers,
    _Listing,
    _read_listing,
    read_group_liveness,
    read_group_members,
)
from tests.helpers import procs


class TestTheParseReturnsPidsNotCounts:
    """The three facts ``_read_listing`` returns, two of them now tuples."""

    def test_the_three_facts_are_not_transposed(self) -> None:
        """All three would type-check in any order, so only cases where
        they DIFFER can catch a swap."""
        assert _read_listing("1 1 Ss\n50 7 Z\n", pgid=7) == _Listing(
            complete=True, listed=(50,), running_pids=()
        )
        assert _read_listing("50 7 Ss\n51 7 Z\n", pgid=7) == _Listing(
            complete=False, listed=(50, 51), running_pids=(50,)
        )

    def test_a_ragged_row_is_skipped_without_dropping_its_neighbours(self) -> None:
        """A row missing a column would IndexError. The rows either side
        of it must still be read, or the skip is a silent truncation."""
        listing = _read_listing("\n  7\n1 1 Ss\n50 7 Ss\n", pgid=7)
        assert listing == _Listing(complete=True, listed=(50,), running_pids=(50,))

    def test_a_pid_column_that_is_not_a_number_is_skipped(self) -> None:
        """The parse now returns pids, so a row it cannot read must be
        dropped from BOTH tuples rather than crashing the read or being
        counted as an anonymous member."""
        listing = _read_listing("1 1 Ss\nbad 7 Ss\n50 7 Ss\n", pgid=7)
        assert listing == _Listing(complete=True, listed=(50,), running_pids=(50,))


class TestTheMembershipReadRefusesWhatTheLivenessReadCanInterpret:
    """`read_group_members` (#209), and why its fail direction is stricter.

    The liveness read has a second positive finding to fall back on when
    the listing is partial, so it can interpret one. A COUNT has none: a
    caller asserting "this group holds one process" is asserting there is
    no second one, and a view filtered to this uid answers that with a
    confident undercount. So an incomplete listing is refused here even
    where the same listing would have been interpreted next door - which
    is the one behaviour difference between the two reads, and the reason
    they are two functions over one parse rather than one function.
    """

    def test_a_complete_listing_returns_the_running_pids(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, stdout="1 1 Ss\n50 4242 Ss\n51 4242 S\n9 7 Ss\n")
        assert read_group_members(4242) == GroupMembers((50, 51))

    def test_a_zombie_is_not_a_member(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same reading as liveness: a corpse is not running, so a
        census that counted it would report a group as occupied by a
        process that has already died (#298)."""
        procs.fake_ps(monkeypatch, stdout="1 1 Ss\n50 4242 Ss\n51 4242 Z\n")
        assert read_group_members(4242) == GroupMembers((50,))

    def test_a_filtered_listing_is_refused_not_returned_short(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole reason this is not a thin wrapper over the liveness
        read. Same rows, no pid 1: liveness would read `live=True` off
        the visible row and be right, and a count would read "one member"
        and be wrong about the one thing it is asked."""
        procs.fake_ps(monkeypatch, stdout="50 4242 Ss\n")
        assert read_group_liveness(4242).live is True
        members = read_group_members(4242)
        assert members.pids is None
        assert "did not list pid 1" in members.reason
        assert "undercount" in members.reason

    def test_a_ps_that_did_not_run_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, raises=lambda: FileNotFoundError(2, "no ps"))
        members = read_group_members(4242)
        assert members.pids is None
        assert "ps failed to run" in members.reason

    def test_a_nonzero_ps_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, returncode=127, stderr="ps: command not found")
        members = read_group_members(4242)
        assert members.pids is None
        assert "rc=127" in members.reason

    def test_an_empty_group_in_a_complete_listing_is_an_empty_answer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not a refusal. A complete listing that shows no member IS the
        measurement, and a caller waiting for a group to empty needs to
        be able to tell that apart from "could not see"."""
        procs.fake_ps(monkeypatch, stdout="1 1 Ss\n")
        assert read_group_members(4242) == GroupMembers(())

    def test_it_reads_a_real_group_this_test_created(self) -> None:
        """The control for all the fakes above: the fake could be
        modelling a `ps` format that does not exist. This one asks about
        the pytest process's own group, which certainly holds it."""
        members = read_group_members(os.getpgrp())
        assert members.pids is not None, members.reason
        assert os.getpid() in members.pids
