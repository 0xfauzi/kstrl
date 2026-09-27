"""#209: the membership read against a real process group.

Split from ``tests/test_procgroup.py`` because that file was at 796 of
the 800-line ratchet, not because the subject is separate: this is
``kstrl/procgroup.py``, and everything the module's docstring says about
there being ONE ``ps`` parse in this tree applies here. The parse table
itself lives with ``tests/test_procgroup_listing.py``.

The reading exists because #209 asks a question liveness cannot answer.
Whether ``caffeinate -i`` puts its forked assertion holder INSIDE the
run's process group is a question about MEMBERSHIP: a helper that escaped
the group would survive the timeout path's ``killpg`` still holding
``PreventUserIdleSystemSleep``, and a bool cannot see that. What remains
here uses a REAL group as the control: an empty answer is only given for
a group the kernel agrees is empty, the read finds pytest in its own
group, and the suite's skip predicate (``procs.ps_is_readable``) is shown
to fire on a real listing with pid 1 removed and to clear with it put
back (#209 round 2, B1).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from kstrl import procgroup
from kstrl.procgroup import (
    GroupMembers,
    read_group_liveness,
    read_group_members,
)
from tests.helpers import procs


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
