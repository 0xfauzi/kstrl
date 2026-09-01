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

import ast
import os
import subprocess
import time
from pathlib import Path

import pytest

from kstrl import procgroup
from kstrl.procgroup import (
    PS_ARGV,
    PS_KILL_GRACE_SECONDS,
    PS_TIMEOUT_SECONDS,
    GroupLiveness,
    _kernel_says_group_is_empty,
    _Listing,
    _may_signal_group,
    _read_listing,
    read_group_liveness,
    signal_probe_alive,
)
from tests.helpers import procs

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestTheListingReadsStatesNotJustGroups:
    """``_read_listing`` returns three facts, and all three are pinned.

    Rows are ``pid pgid stat``. The pid column exists only for the
    completeness control below; nothing identifies a process by it.
    """

    @pytest.mark.parametrize(
        ("state", "counts_as_running"),
        [
            ("Ss", True),
            ("R+", True),
            ("S", True),
            ("D", True),
            ("T", True),
            # Z is the zombie state on macOS and Linux, and flags follow
            # it. Only the prefix is guaranteed, which is why the check is
            # startswith and not equality. Z+, Zl and Zs are asserted from
            # synthetic rows; the local kernel only ever printed plain Z.
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
        listing = _read_listing(f"1 1 Ss\n50 7 {state}\n", pgid=7)
        assert listing.rows == 1, "the row must be counted whatever its state"
        assert bool(listing.running) is counts_as_running

    def test_the_three_facts_are_not_transposed(self) -> None:
        """All three would type-check in any order, so only cases where
        they DIFFER can catch a swap."""
        assert _read_listing("1 1 Ss\n50 7 Z\n", pgid=7) == _Listing(
            complete=True, rows=1, running=0
        )
        assert _read_listing("50 7 Ss\n51 7 Z\n", pgid=7) == _Listing(
            complete=False, rows=2, running=1
        )

    def test_a_ragged_row_is_skipped_without_dropping_its_neighbours(self) -> None:
        """A row missing a column would IndexError. The rows either side
        of it must still be read, or the skip is a silent truncation."""
        listing = _read_listing("\n  7\n1 1 Ss\n50 7 Ss\n", pgid=7)
        assert listing == _Listing(complete=True, rows=1, running=1)

    def test_a_group_id_is_matched_whole_not_as_a_prefix(self) -> None:
        """#292 in miniature: 7 must not match 70."""
        assert _read_listing("1 1 Ss\n50 70 Ss\n", pgid=7).rows == 0

    def test_pid_one_is_what_marks_the_listing_complete(self) -> None:
        assert _read_listing("50 7 Ss\n", pgid=7).complete is False
        assert _read_listing("1 1 Ss\n50 7 Ss\n", pgid=7).complete is True


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


class TestTheGroupIdIsGuardedBeforeAnySignal:
    """#298 round 2: this module signals, so it carries the guard.

    `killpg(1, sig)` is `kill(-1, sig)`, every process this user owns.
    `serve._safe_pgid` has this rule and the module docstring used to
    appeal to it, which is a convention, not a mechanism: nothing
    enforces that callers came through it. The triplication across
    serve / verify / agents.proc is #308; this is procgroup's own half.
    """

    @pytest.mark.parametrize("pgid", [-1, 0, 1])
    def test_a_broadcast_pgid_is_never_signalled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pgid: int,
    ) -> None:
        calls: list[tuple[int, int]] = []

        def recording(target: int, sig: int) -> None:
            calls.append((target, sig))

        monkeypatch.setattr("kstrl.procgroup.os.killpg", recording)
        assert _kernel_says_group_is_empty(pgid) is False
        assert signal_probe_alive(pgid) is True
        assert calls == [], "kill(-1, sig) must never be issued"

    def test_the_refusals_fail_in_the_conservative_direction(self) -> None:
        """Not empty, and alive: both keep a caller from calling a run
        reaped on a question that was never asked."""
        assert _may_signal_group(1) is False
        assert _kernel_says_group_is_empty(1) is False
        assert signal_probe_alive(1) is True

    def test_a_real_group_is_still_signalled(self) -> None:
        """The positive control, or the guard could be rejecting all."""
        assert _may_signal_group(os.getpgrp()) is True


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
        procs.fake_ps(monkeypatch, raises=lambda: FileNotFoundError(2, "no ps"))
        liveness = read_group_liveness(os.getpgrp())
        assert liveness.live is None
        assert "failed to run" in liveness.reason

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


class TestThePsCallIsBounded:
    """The bound is the only thing stopping a wedged ps hanging the daemon.

    Deleting ``timeout=PS_TIMEOUT_SECONDS`` from ``read_group_liveness``
    left the whole suite green before this class existed: the wedged-ps
    case raises a pre-built TimeoutExpired from the fake whether or not
    the kwarg was ever passed, so it could not detect the loss.

    Then #309 showed that pinning the kwarg does not establish that the
    kwarg BOUNDS anything: it was passed, and a ``ps`` that could not be
    killed hung the call anyway, for the reasons the ``kstrl.procgroup``
    module docstring sets out. So the class now measures the clock as
    well as the kwargs, against a child that refuses to die.
    """

    def test_the_read_passes_its_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = procs.fake_ps(monkeypatch, stdout="50 4242 Ss\n")
        read_group_liveness(4242)
        assert calls, "the fake must have intercepted the ps call"
        assert calls[0].timeouts == [PS_TIMEOUT_SECONDS]

    def test_an_unkillable_ps_returns_within_the_bound(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The #309 case, measured on a clock rather than asserted.

        Both constants are shrunk so the test costs its own bound rather
        than six seconds; the fake burns whatever deadline it is handed,
        so the reading is real at either size. The margin is 10x the
        configured bound, which is loose enough for a loaded CI runner and
        still 60x tighter than the counterfactual below.

        MEASURED against the pre-#309 body restored under this same fake:
        the read took 60.06s and this assertion failed with exactly this
        message (two runs, 60.065s and 60.060s). That is the two unbounded
        waits, 30s each - the one ``subprocess.run``'s timeout handler
        does after ``kill()``, and the one ``Popen.__exit__`` does after
        it. Both are gone; that is what the clock here is measuring.
        """
        monkeypatch.setattr("kstrl.procgroup.PS_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr("kstrl.procgroup.PS_KILL_GRACE_SECONDS", 0.05)
        calls = procs.unkillable_ps(monkeypatch)

        started = time.monotonic()
        liveness = read_group_liveness(4242)
        elapsed = time.monotonic() - started

        # A real ps answers in ~11ms, so without this the assertion below
        # would pass just as well on a fake that never intercepted.
        assert len(calls) == 1, "the wedged fake must have answered the read"
        assert elapsed < 1.0, f"the read took {elapsed:.3f}s, so nothing bounded it"
        assert liveness.live is None, "an unmeasurable group must not read as gone"
        assert "failed to run" in liveness.reason

    def test_an_unkillable_ps_is_killed_and_let_go_of(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bound above is only honest if the child was dealt with.

        Two deadlines are spent, not one: the read, then the grace the
        kill is given. The pipe pair is released rather than left to the
        garbage collector, which is the leak half of #309. What happens
        to the child itself is `_register_abandoned`'s job and is tested
        below; this used to claim `subprocess._active` collects it, which
        round 2 measured false under warnings-as-errors.
        """
        monkeypatch.setattr("kstrl.procgroup.PS_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr("kstrl.procgroup.PS_KILL_GRACE_SECONDS", 0.02)
        calls = procs.unkillable_ps(monkeypatch)

        read_group_liveness(4242)

        assert len(calls) == 1, "one read, not a retry loop"
        assert calls[0].timeouts == [0.01, 0.02]
        assert calls[0].kills == 1, "the child must be killed, not waited on"
        assert calls[0].closed == ["stdout", "stderr"]

    def test_an_interrupted_disposal_still_releases_the_pipes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#309 round 1, F3.

        The pipe close used to sit in the handler for
        ``(OSError, ValueError, TimeoutExpired)``. A ``KeyboardInterrupt``
        is none of those, so an operator stopping the daemon while it was
        disposing of a wedged ``ps`` escaped with both fds still held -
        the exact leak the disposal path exists to prevent, reached
        through the one door it did not watch. The release is in a
        ``finally`` now.

        The interrupt itself must still propagate: the caller asked to
        stop, and swallowing that would be a worse bug than the leak.
        ``raises`` is the CLASS, so each raise is a fresh instance and the
        interrupt arrives twice; the two deadlines in the log are the
        proof it reached the disposal rather than stopping at the read.
        """
        calls = procs.fake_ps(monkeypatch, raises=KeyboardInterrupt)

        with pytest.raises(KeyboardInterrupt):
            read_group_liveness(4242)

        assert calls[0].timeouts == [PS_TIMEOUT_SECONDS, PS_KILL_GRACE_SECONDS], (
            "the interrupt must land in the disposal, not only in the read"
        )
        assert calls[0].kills == 1, "the child is still killed before we let go"
        assert calls[0].closed == ["stdout", "stderr"]

    def test_a_collected_child_is_not_registered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The control. Without it the register could be growing on every
        read, which is a leak wearing the costume of a fix."""
        procs.fake_ps(monkeypatch, stdout="50 4242 Ss\n")
        read_group_liveness(4242)
        assert procgroup._ABANDONED == []

    def test_the_register_drains_once_the_child_dies(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The register is only not-a-leak if something empties it.

        A REAL child, because the sweep is ``poll()`` and a fake's poll is
        whatever the fake says. This one is abandoned while alive, so it
        lands on the register, and is then reaped by the next read.

        Both halves of C1 are here: that an uncollected child is kept at
        all, and that keeping it is not itself a leak. Why
        ``Popen.__del__`` could not be trusted to do the keeping, with the
        measurement, is in the ``kstrl.procgroup`` module docstring.
        """
        monkeypatch.setattr("kstrl.procgroup.PS_ARGV", ("sleep", "30"))
        monkeypatch.setattr("kstrl.procgroup.PS_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr("kstrl.procgroup.PS_KILL_GRACE_SECONDS", 0.05)
        # Nothing may be killed, so the child is genuinely abandoned alive.
        monkeypatch.setattr("kstrl.procgroup.subprocess.Popen.kill", lambda self: None)
        read_group_liveness(4242)
        registered = list(procgroup._ABANDONED)
        assert len(registered) == 1

        child = registered[0]
        child.terminate()
        child.wait(timeout=10)
        # The next read sweeps it, which is the whole contract.
        monkeypatch.setattr("kstrl.procgroup.PS_ARGV", ("true",))
        read_group_liveness(4242)
        assert procgroup._ABANDONED == [], "a dead child must leave the register"

    def test_a_real_child_is_disposed_of_without_leaking_a_descriptor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The killable-but-slow case, against a REAL child.

        Nothing else in the suite exercises the disposal against a real
        ``Popen``: ``_FakePs`` stubs ``kill`` and ``communicate``, and its
        ``close`` only appends to a list, so an fd leak or an unreaped
        child would be invisible.
        """
        fd_dir = Path("/dev/fd")
        if not fd_dir.is_dir():
            pytest.skip("no /dev/fd on this platform")
        monkeypatch.setattr("kstrl.procgroup.PS_ARGV", ("sleep", "30"))
        monkeypatch.setattr("kstrl.procgroup.PS_TIMEOUT_SECONDS", 0.05)

        before = len(os.listdir(fd_dir))
        liveness = read_group_liveness(4242)
        after = len(os.listdir(fd_dir))

        assert liveness.live is None, "an unread group must not report as gone"
        assert after == before, f"descriptors leaked: {before} -> {after}"
        assert procgroup._ABANDONED == [], "a killable child must be reaped, not kept"

    def test_the_read_pins_its_encoding(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Left to the locale, a stray byte under LC_ALL=C raises a
        ValueError out of a function whose contract is not to raise."""
        calls = procs.fake_ps(monkeypatch, stdout="50 4242 Ss\n")
        read_group_liveness(4242)
        assert calls[0].kwargs["encoding"] == "utf-8"
        assert calls[0].kwargs["errors"] == "replace"


class TestTheFakeDoesNotAnswerForEveryCommand:
    """`fake_ps` replaces the STDLIB `subprocess.Popen`, not procgroup's.

    `procgroup.subprocess` is the stdlib module object, so a setattr on
    it is process-wide. Measured before the delegation guard existed,
    when the seam was still on `run`: a plain
    `subprocess.run(["git", "rev-parse", "HEAD"])` under
    `fake_ps(stdout="1 Ss\\n")` returned that stdout and
    `args=['ps','-A','-o','pgid=,stat=']`. A test that combined the
    helper with any other subprocess call would have measured nothing and
    passed, which is the #292 class the helper exists to prevent.

    #309 moved the seam from `run` to `Popen`, which makes the guard
    carry MORE: `subprocess.run` is itself built on `Popen`, so an
    undelegated fake would now answer every `run` in the suite as well.
    The test below calling `subprocess.run` is the proof it still does
    not.
    """

    def test_another_command_reaches_the_real_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, stdout="50 4242 Ss\n")
        out = subprocess.run(
            ["echo", "not-the-fake"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert out.stdout.strip() == "not-the-fake"
        assert out.args == ["echo", "not-the-fake"]

    def test_the_ps_call_is_still_intercepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The positive control: without it the delegation above could be
        passing because nothing is faked at all."""
        procs.fake_ps(monkeypatch, stdout="50 4242 Ss\n")
        assert read_group_liveness(4242) == GroupLiveness(True)


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


# ---------------------------------------------------------------------------
# The centralisation has a mechanism, not just a docstring.
# ---------------------------------------------------------------------------

#: Callables whose string arguments are a command line. ``popen`` and
#: ``getstatusoutput`` were missing from the first version, so
#: ``os.popen("ps -A")`` in ``kstrl/`` passed the net silently.
_SUBPROCESS_CALLS = frozenset(
    {
        "run",
        "Popen",
        "popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "system",
    }
)

#: The roots this net walks, and therefore the exact reach of the claim
#: ``kstrl/procgroup.py`` makes. ``spike/`` is deliberately outside: it
#: holds throwaway measurement scripts (``spike/tui0/measure.py`` calls
#: ``ps`` today) that are not part of the package or its suite, and
#: widening the net to cover them would mean either failing on evidence
#: or carrying an allowlist that rots. The docstring in ``procgroup``
#: names these two roots rather than "the tree" for the same reason: a
#: mechanism cited for a claim it does not cover is worse than none.
_SCANNED_ROOTS = ("kstrl", "tests")

#: The one file allowed to shell out to ``ps``: the module whose whole
#: reason for existing is that there is exactly one parse of its output.
_PS_OWNER = "kstrl/procgroup.py"


def _first_string(arg: ast.expr) -> str | None:
    """The first string constant of a literal argv, or the string itself."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.List | ast.Tuple) and arg.elts:
        head = arg.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _module_argv_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level names bound to a literal argv, mapped to its first token.

    One level of resolution, and it is the level that matters: a copier
    writes ``PS_ARGV = ("ps", ...)`` and then ``run(PS_ARGV)``, exactly as
    this module's own owner does. Without this the net would have been
    passing over the one call it is supposed to protect - which is what
    ``test_the_owner_still_calls_ps`` caught on the first run.
    """
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        first = _first_string(node.value)
        if first is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = first
    return found


def _callee_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _call_arguments(node: ast.Call) -> list[ast.expr]:
    return [*node.args, *(kw.value for kw in node.keywords)]


def _names_ps(arg: ast.expr, constants: dict[str, str]) -> bool:
    """Whether this argument is an argv whose command is ``ps``.

    Matches on the BASENAME of the first whitespace-separated token, so
    ``/bin/ps`` and a ``shell=True`` string both count, and ``psql`` does
    not.
    """
    head = _first_string(arg)
    if head is None and isinstance(arg, ast.Name):
        head = constants.get(arg.id)
    tokens = head.split() if head else []
    return bool(tokens) and Path(tokens[0]).name == "ps"


def _ps_call_lines(source: str) -> list[int]:
    """Line numbers of subprocess calls in ``source`` that invoke ``ps``.

    Resolves a bare name against module-level argv constants; a name
    bound inside a function body is a known miss, pinned below.
    """
    tree = ast.parse(source)
    constants = _module_argv_constants(tree)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _callee_name(node) in _SUBPROCESS_CALLS
        and any(_names_ps(arg, constants) for arg in _call_arguments(node))
    ]


def _scannable_sources() -> list[Path]:
    return [p for root in _SCANNED_ROOTS for p in sorted((REPO_ROOT / root).rglob("*.py"))]


class TestOnlyOneModuleShellsOutToPs:
    """``kstrl/procgroup.py`` and ``tests/helpers/procs.py`` both argue
    that two copies of one ``ps`` parse with slightly different failure
    handling is how the suite's answer and the daemon's drift apart. That
    argument was true and unenforced: nothing stopped a third copy. The
    repo already answers this class with an AST net (``test_atomicio`` on
    ``mkstemp``, ``test_process_scoping`` on ``pgrep``), and the rule is
    that if there is no mechanism there is no plan.
    """

    def test_no_second_ps_call_exists(self) -> None:
        offenders: list[str] = []
        for source in _scannable_sources():
            rel = source.relative_to(REPO_ROOT).as_posix()
            if rel == _PS_OWNER:
                continue
            text = source.read_text(encoding="utf-8")
            offenders += [f"{rel}:{line}" for line in _ps_call_lines(text)]
        assert offenders == [], (
            f"{offenders} shell out to ps. There must be exactly one parse "
            f"of ps output in this tree, in {_PS_OWNER}, because two copies "
            f"drift on failure handling and the daemon's answer and the "
            f"suite's stop agreeing. Call kstrl.procgroup.read_group_liveness."
        )

    def test_the_owner_still_calls_ps(self) -> None:
        """Without this the net could be passing because nothing calls ps
        at all, which would mean the module had been gutted."""
        text = (REPO_ROOT / _PS_OWNER).read_text(encoding="utf-8")
        assert _ps_call_lines(text), f"{_PS_OWNER} no longer calls ps, so this net measures nothing"

    def test_the_net_walks_a_real_tree(self) -> None:
        assert len(_scannable_sources()) > 100

    def test_the_claim_names_the_roots_the_net_actually_walks(self) -> None:
        """A mechanism cited for a claim it does not cover is worse than
        none. `spike/` calls ps and is outside the net, so the module's
        docstring must say "kstrl/ or tests/", not "the tree"."""
        text = (REPO_ROOT / _PS_OWNER).read_text(encoding="utf-8")
        claim = "only place in ``kstrl/`` or ``tests/``"
        assert claim in text, f"{_PS_OWNER} must scope its uniqueness claim to {_SCANNED_ROOTS}"

    @pytest.mark.parametrize(
        "body",
        [
            'subprocess.run(["ps", "-A"])',
            'subprocess.run(["/bin/ps", "-eo", "pid="])',
            'subprocess.Popen("ps -A", shell=True)',
            'sp.check_output(("ps", "-A"))',
            'os.popen("ps -A")',
            'subprocess.getstatusoutput("ps -A")',
        ],
    )
    def test_the_net_catches_a_planted_call(self, body: str) -> None:
        """Its reach, measured rather than asserted in a docstring."""
        assert _ps_call_lines(body) == [1], body

    def test_it_resolves_a_module_level_argv_constant(self) -> None:
        """The shape the owner itself uses, and the shape a copier would
        write. The net missed it until this case was added."""
        body = 'ARGV = ("ps", "-A")\nsubprocess.run(ARGV, timeout=5)\n'
        assert _ps_call_lines(body) == [2]

    @pytest.mark.parametrize(
        "body",
        [
            # Not a subprocess call at all.
            'x = ["ps", "-A"]',
            # A different tool whose name merely starts with the letters.
            'subprocess.run(["psql", "-c", "select 1"])',
            # Prose. The net reads the AST, so a docstring cannot trip it.
            '"""Do not call ps here."""',
        ],
    )
    def test_the_net_stays_quiet_on_these(self, body: str) -> None:
        assert _ps_call_lines(body) == [], body

    def test_a_command_built_inside_a_function_is_a_known_miss(self) -> None:
        """Stated so the net is not trusted past its reach. Module-level
        constants resolve; a name bound in a function body does not, and
        following that needs dataflow the AST does not give."""
        body = 'def f():\n    cmd = ["ps", "-A"]\n    subprocess.run(cmd)\n'
        assert _ps_call_lines(body) == []
