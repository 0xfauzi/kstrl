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
import subprocess

import pytest

from kstrl import procgroup
from kstrl.procgroup import (
    _UNCOUNTABLE,
    _UNMEASURABLE,
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
            # #209 round 2, S2. The pid check used to sit BELOW the group
            # filter, so this row - unreadable pid, pgid naming some
            # other group - was skipped before anything looked at its
            # first column, and the listing cleared. `readable` is a
            # property of the (listing, pgid) pair: a row that cannot be
            # attributed to a group cannot be ruled OUT of this one.
            ("1 1 Ss\nbad 999 Ss\n50 7 S\n", "a pid that is not a number, in another group"),
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


class TestAPsThatGaveNoAnswerIsRefusedByBOTHReads:
    """A refusal is a HEAD plus ONE consequence, and each read appends its
    own. All four cases live here because they are one matched set.

    THE ABSENT HALF MATTERS AS MUCH AS THE PRESENT ONE. Each test below
    asserts its read's own consequence is there AND that the other read's
    is not. Round 2 of #209 measured why: with only the first assertion,
    putting ``_UNMEASURABLE`` back into each of ``_listing_for``'s two
    heads - so a count refusal carried BOTH consequences - left the count
    tests green, twice, while their own failure message said they existed
    to catch exactly that. An assertion that cannot fail on the defect it
    names is not a control.

    The liveness two moved here from ``tests/test_procgroup.py`` in round
    3. Keeping the pairs in separate files is what let the count half go a
    round without being able to fail, and it also put the file at 825 of
    the 800-line ratchet.
    """

    def test_a_ps_that_did_not_run_is_refused_by_the_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, raises=lambda: FileNotFoundError(2, "no ps"))
        members = read_group_members(4242)
        assert members.pids is None
        assert "ps failed to run" in members.reason
        assert _UNCOUNTABLE in members.reason, (
            "a ps failure is refused for the reason the CALLER was asking "
            "about; before #209's round-1 review this read reported the "
            "liveness consequence, which is an answer to another question"
        )
        assert _UNMEASURABLE not in members.reason, (
            "the count reported the liveness consequence as well as its "
            "own, which is the shape round 2 measured this assertion "
            "unable to see"
        )

    def test_a_nonzero_ps_is_refused_by_the_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, returncode=127, stderr="ps: command not found")
        members = read_group_members(4242)
        assert members.pids is None
        assert "rc=127" in members.reason
        assert _UNCOUNTABLE in members.reason
        assert _UNMEASURABLE not in members.reason, "the foreign consequence, as above"

    def test_a_ps_that_did_not_run_is_refused_by_liveness(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ps`` absent raises OSError rather than exiting non-zero."""
        procs.fake_ps(monkeypatch, raises=lambda: FileNotFoundError(2, "no ps"))
        liveness = read_group_liveness(os.getpgrp())
        assert liveness.live is None
        assert "failed to run" in liveness.reason
        assert _UNMEASURABLE in liveness.reason
        assert _UNCOUNTABLE not in liveness.reason, "the foreign consequence, as above"

    def test_a_nonzero_ps_is_refused_by_liveness(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, returncode=127, stderr="ps: command not found")
        liveness = read_group_liveness(os.getpgrp())
        assert liveness.live is None
        assert "ps failed" in liveness.reason
        assert _UNMEASURABLE in liveness.reason
        assert _UNCOUNTABLE not in liveness.reason, (
            "the liveness read reported the COUNT's consequence as well as "
            "its own; the two reads differ in exactly this and nothing else"
        )


class TestAColumnIsMatchedAsANumberNotAsText:
    """#209 round 2, S1. ``_reads_as_int`` converts a cell to establish
    that it reads as a number; comparing the cell to ``str(pgid)``, or to
    ``"1"``, afterwards throws that conversion away.

    Any spelling ``int()`` accepts and ``str`` does not produce was then
    attributed to no group at all, with ``readable`` still true and no
    refusal raised, so the count came back CONFIDENT and short. The
    review measured all four rows below returning ``(51,)`` for a group
    holding ``(50, 51)``, which is the undercount ``GroupMembers``
    documents itself as unable to produce.

    Unreachable on real ``ps`` output, whose columns come from the kernel
    - 0 non-conforming rows in 16320 on one load and 16331 on another -
    and reachable on the mangled stream the refusal beside it exists for.
    """

    @pytest.mark.parametrize(
        "spelling",
        [
            "007",
            "+7",
            "0_7",
            "\N{ARABIC-INDIC DIGIT SEVEN}",
        ],
    )
    def test_a_pgid_int_accepts_is_the_group_it_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
        spelling: str,
    ) -> None:
        procs.fake_ps(monkeypatch, stdout=f"1 1 Ss\n50 {spelling} Ss\n51 7 Ss\n")
        members = read_group_members(7)
        assert members.pids == (50, 51), (
            f"the pgid spelled {spelling!r} was attributed to no group, so a "
            f"group holding two processes was counted as {members.pids!r}"
        )

    @pytest.mark.parametrize("spelling", ["01", "+1", "0_1", "\N{ARABIC-INDIC DIGIT ONE}"])
    def test_pid_1_is_pid_1_however_it_is_spelled(self, spelling: str) -> None:
        """The completeness control is the same split one line up, and
        it is the one whose failure direction is a REFUSAL rather than an
        undercount: a pid-1 row the comparison does not recognise leaves
        ``complete`` False, and the listing is then refused as filtered
        to one uid when it is not. Measured while writing this: the
        text comparison survives the four pgid rows above, so nothing but
        this pins it."""
        assert _read_listing(f"{spelling} 1 Ss\n50 7 Ss\n", pgid=7).complete is True, (
            f"pid 1 spelled {spelling!r} did not mark the listing complete, "
            f"so a full listing reads as filtered to this uid and is refused"
        )


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

    @procs.NEEDS_A_READABLE_PS
    def test_it_reads_the_real_group_this_process_is_in(self) -> None:
        """The control for all the fakes above: the fake could be
        modelling a `ps` format that does not exist. This one asks about
        pytest's own group, which certainly holds pytest. The group is
        not one this test created, which is why it asserts only its own
        membership and nothing about the size.

        It is the ONE test here that reads the real listing, so it is the
        one that needs the environment guard: under a `hidepid` mount or
        a container `ps` that omits pid 1 the read refuses, correctly,
        and this would go red pointing at `kstrl/procgroup.py` for an
        environment that cannot be measured (#209 round 2, S5)."""
        members = read_group_members(os.getpgrp())
        assert members.pids is not None, members.reason
        assert os.getpid() in members.pids


def _serving(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    template: subprocess.CompletedProcess[str],
) -> None:
    """Make ``procgroup._read_ps`` hand back ``stdout`` and nothing else."""
    replacement = subprocess.CompletedProcess(template.args, template.returncode, stdout, "")
    monkeypatch.setattr(procgroup, "_read_ps", lambda: replacement)


class TestTheSuiteSkipPredicateCanActuallyFire:
    """#209 round 2, B1. ``tests/helpers/procs.ps_is_readable`` gates
    every test that takes a group census, and in the shape it shipped in
    it returned True on exactly the ``ps`` it exists to skip.

    It was ``read_group_liveness(os.getpgrp()).live is True``.
    ``_interpret`` answers True off a visible runner BEFORE it consults
    the refusal table, correctly, because a partial listing can only show
    FEWER processes; the caller's own group always holds the caller; and
    under a ``ps`` filtered to one uid our own processes are precisely
    the ones still visible. So it was True by construction everywhere,
    while ``read_group_members`` refused the same listing and raised
    through ``on_spawn``. A red test in ``kstrl/procgroup.py``'s name for
    an environment that cannot be measured is what the guard exists to
    prevent, and it was pointing the other way.

    THE PLANT IS A REAL LISTING WITH ONE ROW REMOVED, which is the only
    thing a ``hidepid`` mount or a container ``ps`` changes about the
    answer. Both directions come off ONE real read, so the difference
    between them is that row and nothing about load or timing. The PR
    that shipped the broken predicate recorded it as the one guard it
    could not mutate; every other test in this file fakes ``_read_ps``
    for exactly this.
    """

    @staticmethod
    def _real_read() -> subprocess.CompletedProcess[str]:
        return procgroup._read_ps()

    @staticmethod
    def _without_pid_1(stdout: str) -> str:
        return "".join(row for row in stdout.splitlines(keepends=True) if row.split()[:1] != ["1"])

    def test_a_uid_filtered_listing_makes_the_predicate_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real = self._real_read()
        _serving(monkeypatch, self._without_pid_1(real.stdout), real)

        assert read_group_liveness(os.getpgrp()).live is True, (
            "the old spelling of this predicate, unchanged, on the very "
            "listing it must refuse; without this the pair below could be "
            "measuring a listing that broke everything rather than one "
            "that is filtered"
        )
        assert procs.ps_is_readable() is False, (
            "the skip predicate cleared a ps filtered to one uid, so every "
            "census case runs there and reads as a defect in kstrl"
        )

    def test_the_skip_mark_built_from_it_would_fire(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The predicate is only half the guard. This asserts the other
        half: the mark's CONDITION, built the way ``procs`` builds it, is
        True under the filtered listing, so the skip fires rather than
        merely being present."""
        real = self._real_read()
        _serving(monkeypatch, self._without_pid_1(real.stdout), real)
        mark = pytest.mark.skipif(not procs.ps_is_readable(), reason="filtered ps")
        assert mark.mark.args[0] is True, (
            "the mark is attached to every census case and its condition "
            "is False, so it can never skip anything"
        )

    def test_the_same_listing_with_pid_1_put_back_is_readable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The control, and the honest half of the 2x2 the review
        measured. Same real rows, pid 1 restored at the head, so the only
        difference from the test above is the row whose absence means
        "filtered". Restoring rather than trusting the machine's own
        listing keeps this true on a host whose ``ps`` really is
        filtered, where the assertion would otherwise be about the
        environment instead of about the predicate."""
        real = self._real_read()
        _serving(monkeypatch, "1 1 Ss\n" + self._without_pid_1(real.stdout), real)
        assert procs.ps_is_readable() is True
        mark = pytest.mark.skipif(not procs.ps_is_readable(), reason="filtered ps")
        assert mark.mark.args[0] is False
