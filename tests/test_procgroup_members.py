"""#209: `_Listing` keeps pids, and both public reads share one refusal table.

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
tests whose expected values are those tuples live here with it. The
round-1 review of #209 added the second subject: the two reads consult
one refusal table, so the classes below assert on BOTH reads wherever a
refusal is at stake. Two of the three refusals a parsed listing can earn
were reachable in only one of them before that.
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
    """The four facts ``_read_listing`` returns, two of them tuples."""

    def test_the_four_facts_are_not_transposed(self) -> None:
        """All four would type-check in any order, so only cases where
        they DIFFER can catch a swap."""
        assert _read_listing("1 1 Ss\n50 7 Z\n", pgid=7) == _Listing(
            complete=True, readable=True, listed=(50,), running_pids=()
        )
        assert _read_listing("50 7 Ss\n51 7 Z\n", pgid=7) == _Listing(
            complete=False, readable=True, listed=(50, 51), running_pids=(50,)
        )

    def test_a_blank_line_claims_nothing_so_it_hides_nothing(self) -> None:
        """The one skipped shape that does NOT cost readability, and the
        control for the three below: without it, "unreadable" could be
        true of every listing and the refusal would mean nothing."""
        assert _read_listing("\n1 1 Ss\n\n50 7 Ss\n\n", pgid=7) == _Listing(
            complete=True, readable=True, listed=(50,), running_pids=(50,)
        )

    @pytest.mark.parametrize(
        ("stdout", "shape"),
        [
            ("1 1 Ss\n  7\n50 7 Ss\n", "a row with fewer than three columns"),
            ("1 1 Ss\nbad 7 Ss\n50 7 Ss\n", "a pid column that is not a number"),
            ("1 1 Ss\n60 bad Ss\n50 7 Ss\n", "a pgid column that is not a number"),
            # The case that makes `_reads_as_int` ask `int()` rather than
            # `str.isdigit`: this cell IS isdigit and `int()` rejects it,
            # so an isdigit test would fall through to a conversion that
            # raises out of a read with no handler for it.
            ("1 1 Ss\n60 \N{SUPERSCRIPT TWO} Ss\n50 7 Ss\n", "a pgid of unicode digits"),
        ],
    )
    def test_a_row_the_parse_cannot_read_makes_the_listing_unreadable(
        self,
        stdout: str,
        shape: str,
    ) -> None:
        """A row that cannot be attributed to a group cannot be ruled OUT
        of this one, so the listing is not a full view of it.

        The rows either side must still be read: the point is a listing
        marked untrustworthy, not a listing silently truncated. #209
        dropped such a row instead, which is an undercount, and the
        module refuses undercounts everywhere else."""
        assert _read_listing(stdout, pgid=7) == _Listing(
            complete=True, readable=False, listed=(50,), running_pids=(50,)
        ), shape


class TestARowTheParseCannotReadIsRefusedByBOTHReads:
    """The S1 finding of #209's round-1 review, from both sides.

    Dropping the row moved the LIVENESS answer, which is the one that
    reaches the daemon's kill path. Measured on all three trees with the
    same input, ``"1 1 Ss\\nbad 7 Ss\\n50 7 Z\\n"`` for group 7: before
    #209 the parse counted rows and reported ``live=True``; on the PR
    head it dropped the row and reported ``live=False``, which
    ``serve.terminate_process_group`` reads as "the group is gone" for a
    group whose only running member is the row it could not read. That
    releases the item while a factory may still be writing, which is
    #186 F1's own failure.

    Unreachable on real ``ps`` output - 20 reads on this machine gave
    16320 rows, every one three columns with a numeric pid and pgid -
    and reachable on a truncated or mangled stream.
    """

    _MANGLED = "1 1 Ss\nbad 7 Ss\n50 7 Z\n"

    def test_liveness_refuses_rather_than_reporting_the_group_gone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, stdout=self._MANGLED)
        liveness = read_group_liveness(7)
        assert liveness.live is None, (
            f"a listing carrying a row the parse could not read reported "
            f"live={liveness.live}; the daemon reads False as 'the group "
            f"is gone' and releases the item (#186 F1)"
        )
        assert "could not read" in liveness.reason
        assert "false negative" in liveness.reason

    def test_the_count_refuses_it_too(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, stdout=self._MANGLED)
        members = read_group_members(7)
        assert members.pids is None
        assert "could not read" in members.reason
        assert "undercount" in members.reason

    def test_a_visible_runner_still_outranks_the_refusal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The other direction, and the reason ``_interpret`` checks for
        a runner FIRST. Every refusal says the listing may have shown too
        few processes, and showing too few cannot stop a running process
        running. Without this the new refusal would turn a seen runner
        into "cannot see", which is a worse answer than the one it
        replaces."""
        procs.fake_ps(monkeypatch, stdout="1 1 Ss\nbad 7 Ss\n50 7 Ss\n")
        assert read_group_liveness(7).live is True


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
        assert "running member" in members.reason, (
            "the message names the member whose absence moves either "
            "answer; #209's dedup dropped the word and nothing pinned it"
        )
        assert "undercount" in members.reason

    def test_a_ps_that_did_not_run_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, raises=lambda: FileNotFoundError(2, "no ps"))
        members = read_group_members(4242)
        assert members.pids is None
        assert "ps failed to run" in members.reason
        assert "undercount" in members.reason, (
            "a ps failure is refused for the reason the CALLER was asking "
            "about; before #209's round-1 review this read reported the "
            "liveness consequence, which is an answer to another question"
        )

    def test_a_nonzero_ps_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, returncode=127, stderr="ps: command not found")
        members = read_group_members(4242)
        assert members.pids is None
        assert "rc=127" in members.reason
        assert "undercount" in members.reason


class TestAnEmptyAnswerIsOnlyGivenForAGroupTheKernelAgreesIsEmpty:
    """The B1 finding of #209's round-1 review.

    ``_interpret`` asked the kernel whether a group an empty listing did
    not mention was really empty, and ``read_group_members`` did not, so
    the same listing was a refusal for liveness and a confident ``()``
    for a count. A guard that CLEARS must refuse what it cannot prove,
    and an empty tuple is a clearing.

    The pair below is the whole argument, and neither half means anything
    alone: the first shows an empty answer is still reachable, the second
    shows it is not reachable off a listing the kernel contradicts. Both
    use a REAL group, because the kernel is the control here and faking
    it would be testing the fake.
    """

    def test_a_group_the_kernel_also_calls_empty_is_an_empty_answer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not a refusal. A complete listing that shows no member, for a
        group the kernel confirms holds nothing, IS the measurement, and
        a caller waiting for a group to empty needs to be able to tell
        that apart from "could not see"."""
        pgid = procs.dead_group()
        procs.fake_ps(monkeypatch, stdout="1 1 Ss\n")
        assert read_group_members(pgid) == GroupMembers(())

    def test_a_group_the_kernel_calls_occupied_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Our own group is alive by construction and pid 1 is listed, so
        the view is complete; the group simply is not in it. That means
        the listing did not show every process, and a count taken from it
        would be an undercount. Liveness has refused this since #298; the
        count returned ``GroupMembers(())`` until #209's round-1 review."""
        procs.fake_ps(monkeypatch, stdout="1 1 Ss\n")
        members = read_group_members(os.getpgrp())
        assert members.pids is None, (
            f"a group the kernel reports as occupied was answered with "
            f"{members.pids!r}, which a caller reads as a measurement"
        )
        assert "kernel reports" in members.reason
        assert "undercount" in members.reason

    def test_it_reads_the_real_group_this_process_is_in(self) -> None:
        """The control for all the fakes above: the fake could be
        modelling a `ps` format that does not exist. This one asks about
        pytest's own group, which certainly holds pytest. The group is
        not one this test created, which is why it asserts only its own
        membership and nothing about the size."""
        members = read_group_members(os.getpgrp())
        assert members.pids is not None, members.reason
        assert os.getpid() in members.pids
