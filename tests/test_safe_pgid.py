"""#308: the signal guard, tested once in the module that owns it.

`procgroup.safe_pgid` is the one copy of a rule `serve._safe_pgid`,
`verify._signal_process_group` and `agents.proc._signal_group` each wrote
out for themselves. Its rationale, and what was measured about the three
copies before they were merged, is in its own docstring; this file pins
the decision table rather than restating the argument.

`_may_signal_group` is here too, because `safe_pgid` calls it and tests
for one rule split across two files drift. It came out of
`tests/test_procgroup.py`, which is about the `ps` READ: that file was at
798 lines against a `file-length-ratchet` hook that fails a file crossing
800, so the two halves needed separating anyway and this is the seam.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kstrl import procgroup
from kstrl.procgroup import (
    _kernel_says_group_is_empty,
    _may_signal_group,
    signal_probe_alive,
)
from tests.helpers import procs

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestTheGroupIdIsGuardedBeforeAnySignal:
    """#298 round 2: this module signals, so it carries the guard.

    `killpg(1, sig)` is `kill(-1, sig)`, every process this user owns.
    `_may_signal_group` stays a function of its own, rather than folding
    into `safe_pgid`, because `read_group_liveness` and
    `signal_probe_alive` take a bare pgid from anywhere and nothing
    enforces that THEIR callers came through `safe_pgid`.
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


class TestSafePgidIsTheOneCopyOfThePopenGuard:
    """Every branch of the decision, the negatives and the control.

    This is the table. Each of the three call sites keeps its own test
    that it ROUTES through here, which is the half a shared unit test
    cannot prove, and does not re-pin the table on top of it.
    """

    def test_a_real_child_still_gets_its_group(self) -> None:
        """The positive control. Every other test here asserts a None, so
        a guard that rejected everything would pass all of them while
        silently turning every group kill in the factory into a
        direct-child kill that leaks the grandchildren it exists to
        collect."""
        child = subprocess.Popen(
            ["sleep", "30"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Read before the assertions, so a failure cannot leave the
        # cleanup asking `getpgid` about a pid that has since gone.
        pgid = os.getpgid(child.pid)
        try:
            assert pgid != os.getpgrp(), "start_new_session must give it its own"
            assert procgroup.safe_pgid(child) == pgid
        finally:
            procs.kill_group(pgid)
            child.kill()
            child.wait(timeout=10.0)

    def test_a_mocked_popen_is_refused(self) -> None:
        """The CI-killer shape. A MagicMock pid does NOT raise TypeError
        out of `getpgid`: it coerces to 1 through `MagicMock.__index__`
        (measured on this machine, `os.getpgid(MagicMock())` returns 1),
        and `killpg(1, sig)` is `kill(-1, sig)`."""
        fake = procs.fake_popen(MagicMock())
        # A plausible group, so the pgid checks cannot be what rejects it
        # and only the isinstance check can.
        with patch.object(os, "getpgid", lambda pid: 99999):
            assert procgroup.safe_pgid(fake) is None

    @pytest.mark.parametrize("bad_pid", [None, -1, 0, 1, True, "1234"])
    def test_a_pid_that_cannot_own_a_group_is_refused(self, bad_pid: object) -> None:
        """`True` is in here on purpose: `isinstance(True, int)` is True,
        so `pid <= 1` is the only thing that rejects it."""
        fake = procs.fake_popen(bad_pid)
        with patch.object(os, "getpgid", lambda pid: 99999):
            assert procgroup.safe_pgid(fake) is None

    @pytest.mark.parametrize("broadcast", [-1, 0, 1])
    def test_a_broadcast_pgid_is_refused(self, broadcast: int) -> None:
        fake = procs.fake_popen(4242)
        with patch.object(os, "getpgid", lambda pid: broadcast):
            assert procgroup.safe_pgid(fake) is None

    def test_our_own_group_is_refused(self) -> None:
        """Signalling our own group kills the process doing the
        signalling. Seeing ours back means `start_new_session` never took."""
        fake = procs.fake_popen(os.getpid())
        assert procgroup.safe_pgid(fake) is None

    @pytest.mark.parametrize(
        "exc",
        [
            ProcessLookupError(3, "no such process"),
            PermissionError(1, "operation not permitted"),
            OSError(5, "input/output error"),
        ],
    )
    def test_a_failed_lookup_is_a_none_and_not_a_raise(self, exc: OSError) -> None:
        """The one place the three copies differed, so the one that had to
        be decided rather than moved."""
        fake = procs.fake_popen(4242)

        def raiser(pid: int) -> int:
            raise exc

        with patch.object(os, "getpgid", raiser):
            assert procgroup.safe_pgid(fake) is None

    def test_a_platform_without_the_syscalls_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POSIX gating. An absent `getpgid` raises AttributeError, which
        no caller catches, so its hasattr has to come BEFORE the lookup."""
        monkeypatch.delattr(os, "killpg")
        monkeypatch.delattr(os, "getpgid")
        fake = procs.fake_popen(4242)
        assert procgroup.safe_pgid(fake) is None


class TestNoCallerCarriesItsOwnCopy:
    """The point of #308: a fourth site would be invisible to the above.

    WHAT THIS NET SEES is an `os.getpgid(...)` call written as an
    attribute on a name called `os`. That is how all three copies were
    spelled and how a fourth would most likely be spelled. It does NOT
    see `from os import getpgid` or a rebound module, which is a known
    miss rather than a claim: the test below plants that spelling and
    records that it goes through.
    """

    def _offenders(self, source: str) -> list[int]:
        found: list[int] = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getpgid"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                found.append(node.lineno)
        return found

    def test_no_module_outside_procgroup_derives_a_pgid(self) -> None:
        owner = REPO_ROOT / "kstrl" / "procgroup.py"
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "kstrl").rglob("*.py")):
            if path == owner:
                continue
            for lineno in self._offenders(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        assert offenders == [], (
            "os.getpgid outside procgroup means a fourth copy of the guard; "
            "call procgroup.safe_pgid instead"
        )

    def test_the_net_still_catches_the_spelling_all_three_copies_used(self) -> None:
        """The control. Without this the test above passes on an empty
        walk, a broken parse or a matcher that matches nothing."""
        assert self._offenders("import os\npgid = os.getpgid(pid)\n") == [2]

    def test_the_owner_is_the_only_file_excluded(self) -> None:
        owner = REPO_ROOT / "kstrl" / "procgroup.py"
        assert self._offenders(owner.read_text(encoding="utf-8")) != []

    def test_a_bare_import_is_a_known_miss(self) -> None:
        """Written down rather than fixed, so the net's reach is a
        measured fact and not something a reader has to assume."""
        assert self._offenders("from os import getpgid\npgid = getpgid(pid)\n") == []
