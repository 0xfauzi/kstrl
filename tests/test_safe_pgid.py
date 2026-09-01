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
from dataclasses import dataclass
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

    def test_a_platform_with_getpgid_but_no_killpg_cannot_signal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The `killpg` half of the POSIX gate, on its own.

        Round-1 review found that deleting `hasattr(os, "killpg")` from
        `_may_signal_group` left all 25 safe-pgid tests green, because
        the only test that removed a syscall removed BOTH and stopped at
        the `getpgid` gate in `safe_pgid`. The two gates guard two
        different calls, so they need separating to be tested: this one
        leaves `getpgid` in place, so every earlier check passes and the
        `killpg` gate is the only thing left that can refuse.

        POSIX always has both, so the case cannot arise here naturally.
        `monkeypatch.delattr` is what makes it exercisable, and it is
        exact: an absent `killpg` is an AttributeError at the call, which
        no caller catches, so the wrong answer here is a crash in the
        timeout path rather than a wrong pgid.
        """
        monkeypatch.delattr(os, "killpg")

        assert _may_signal_group(99999) is False

        fake = procs.fake_popen(4242)
        with patch.object(os, "getpgid", lambda pid: 99999):
            assert procgroup.safe_pgid(fake) is None, (
                "a pgid handed back on a platform that cannot signal it is a "
                "crash in the caller, not a kill"
            )


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


#: The module and callable the net looks for.
_TARGET_MODULE = "os"
_TARGET_FUNC = "getpgid"


@dataclass(frozen=True)
class _Scan:
    """Everything the resolver needs, collected in ONE tree walk.

    The first version of this took four walks per file: imports, class
    bodies, a fixed-point pass that re-walked on every iteration, and the
    calls. Measured over the 126 files this net sweeps, that was 942,444
    node visits against 235,611, and it put the sweep at 0.41s against
    0.19s for the literal matcher it replaced. Bucketing gets the same
    answer on all 146 inputs checked, in 0.22s.
    """

    imports: list[ast.Import]
    from_imports: list[ast.ImportFrom]
    #: (targets, value) for every assignment, including class bodies.
    assigns: list[tuple[list[ast.expr], ast.expr]]
    #: Names bound in a class body, so reachable as an attribute too.
    class_bound: set[str]
    calls: list[ast.Call]


def _assign_targets(node: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    return [node.target]


def _class_body_names(node: ast.ClassDef) -> set[str]:
    """Names a class body binds, so reachable as an attribute of the class."""
    names: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, ast.Assign | ast.AnnAssign):
            names.update(t.id for t in _assign_targets(stmt) if isinstance(t, ast.Name))
    return names


def _scan(tree: ast.Module) -> _Scan:
    scan = _Scan([], [], [], set(), [])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            scan.imports.append(node)
        elif isinstance(node, ast.ImportFrom):
            scan.from_imports.append(node)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            if node.value is not None:
                scan.assigns.append((_assign_targets(node), node.value))
        elif isinstance(node, ast.ClassDef):
            scan.class_bound.update(_class_body_names(node))
        elif isinstance(node, ast.Call):
            scan.calls.append(node)
    return scan


def _reaches_target(
    node: ast.expr,
    aliases: set[str],
    direct: set[str],
    attrs: set[str],
) -> bool:
    """Whether this expression evaluates to ``os.getpgid``."""
    if isinstance(node, ast.Attribute):
        if (
            node.attr == _TARGET_FUNC
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            return True
        if node.attr in attrs:
            return True
    return isinstance(node, ast.Name) and node.id in direct


def _module_aliases(scan: _Scan) -> set[str]:
    """Every name bound to the ``os`` module itself, aliases included."""
    aliases = {_TARGET_MODULE}
    for node in scan.imports:
        for alias in node.names:
            # `import os.path` binds `os`, which is already in the set.
            if alias.asname is not None and alias.name == _TARGET_MODULE:
                aliases.add(alias.asname)
    return aliases


def _imported_callables(scan: _Scan) -> set[str]:
    """Every name `from os import getpgid` binds to the callable."""
    direct: set[str] = set()
    for node in scan.from_imports:
        if node.module != _TARGET_MODULE or node.level != 0:
            continue
        for alias in node.names:
            if alias.name == _TARGET_FUNC:
                direct.add(alias.asname or alias.name)
    return direct


def _absorb(
    targets: list[ast.expr],
    class_bound: set[str],
    direct: set[str],
    attrs: set[str],
) -> bool:
    """Bind the targets of one resolving assignment. True if a set grew."""
    grew = False
    for target in targets:
        if isinstance(target, ast.Name):
            if target.id not in direct:
                direct.add(target.id)
                grew = True
            # A class body's `lookup = os.getpgid` is reachable as
            # `Cls.lookup` as well, so it belongs in BOTH buckets.
            if target.id in class_bound and target.id not in attrs:
                attrs.add(target.id)
                grew = True
        elif isinstance(target, ast.Attribute) and target.attr not in attrs:
            attrs.add(target.attr)
            grew = True
    return grew


def _bindings(scan: _Scan) -> tuple[set[str], set[str], set[str]]:
    """(names for the os module, names for the callable, attribute names).

    Resolving IMPORTS AND REBINDS is the whole difference between this
    net and the one #308 first shipped, which matched the literal shape
    ``os.getpgid(...)`` and nothing else. Round-1 review defeated that
    version by planting a complete working fourth guard behind
    ``import os as operating_system``, and three further spellings went
    through with it.

    THE TWO NAME BUCKETS ARE NOT ONE. ``direct`` is names that ARE the
    callable, ``attrs`` is attribute names that resolve to it. Merging
    them costs precision that ``_MUST_IGNORE`` pins: after
    ``lookup = os.getpgid`` at module level, an unrelated
    ``other.lookup(1)`` is not this callable, and one merged set cannot
    say so.
    """
    aliases = _module_aliases(scan)
    direct = _imported_callables(scan)
    attrs: set[str] = set()

    # To a fixed point, so `a = os.getpgid; b = a; b(pid)` resolves. No
    # iteration bound: both sets only grow, over the finite set of names
    # in one file, so this terminates. An earlier version capped it at 16
    # and called the cap cycle protection, which was two errors - a cycle
    # cannot spin a monotonically growing set, and the cap silently
    # stopped resolving a rebind chain longer than itself.
    while True:
        grew = False
        for targets, value in scan.assigns:
            if _reaches_target(value, aliases, direct, attrs):
                grew |= _absorb(targets, scan.class_bound, direct, attrs)
        if not grew:
            return aliases, direct, attrs


def _getpgid_calls(source: str) -> list[int]:
    """Line numbers of every call in ``source`` that reaches ``os.getpgid``."""
    scan = _scan(ast.parse(source))
    aliases, direct, attrs = _bindings(scan)
    return sorted(
        node.lineno for node in scan.calls if _reaches_target(node.func, aliases, direct, attrs)
    )


#: Every spelling the net must flag. `module alias` is the one round-1
#: review planted a working fourth copy of the guard behind; the three
#: below it are the rest of what went through with it. The others are
#: forms the sibling guards in this repo were each separately holed by,
#: kept as regression cover against a matcher that starts reading
#: arguments or scope again.
_MUST_CATCH = {
    "direct": "import os\npgid = os.getpgid(pid)\n",
    "keyword argument": "import os\npgid = os.getpgid(pid=pid)\n",
    "inside a helper": "import os\n\n\ndef helper(pid):\n    return os.getpgid(pid)\n",
    "nested function": (
        "import os\n\n\ndef outer():\n    def inner():\n"
        "        return os.getpgid(1)\n\n    return inner\n"
    ),
    "module alias": "import os as operating_system\npgid = operating_system.getpgid(pid)\n",
    "from import": "from os import getpgid\npgid = getpgid(pid)\n",
    "from import aliased": "from os import getpgid as gp\npgid = gp(pid)\n",
    "callable rebind": "import os\nlookup = os.getpgid\npgid = lookup(pid)\n",
    "rebind of a rebind": "import os\na = os.getpgid\nb = a\npgid = b(pid)\n",
    "annotated assignment": (
        "import os\nfrom collections.abc import Callable\n"
        "lookup: Callable[[int], int] = os.getpgid\npgid = lookup(pid)\n"
    ),
    "class attribute": (
        "import os\n\n\nclass G:\n    lookup = os.getpgid\n\n\npgid = G.lookup(pid)\n"
    ),
    "instance attribute": (
        "import os\n\n\nclass G:\n    lookup = os.getpgid\n\n\npgid = G().lookup(pid)\n"
    ),
}

#: Spellings that must NOT be flagged, or the net fails closed so hard
#: that nobody keeps it. A name is not a binding.
_MUST_IGNORE = {
    "unrelated method of the same name": (
        "class C:\n    def getpgid(self):\n        return 1\n\n\nC().getpgid()\n"
    ),
    "unrelated free function of the same name": (
        "def getpgid(pid):\n    return 1\n\n\ngetpgid(2)\n"
    ),
    "the attribute without the call": "import os\nlookup = os.getpgid\n",
    "a different os call": "import os\npid = os.getpid()\n",
    # Pins the precision the two name buckets buy: `lookup` is the
    # callable, `other.lookup` is somebody else's attribute of that name.
    "the same name as an attribute of something else": (
        "import os\nlookup = os.getpgid\npgid = other.lookup(1)\n"
    ),
}


class TestNoCallerCarriesItsOwnCopy:
    """The point of #308: a fourth site would be invisible to the above.

    WHAT THIS NET SEES is a call that RESOLVES to `os.getpgid` - through
    module aliases, from-imports, rebinds and class attributes - and not
    just the literal shape `os.getpgid(...)` that #308 first shipped.
    Round-1 review defeated that version by planting a complete working
    guard behind `import os as operating_system`.

    WHAT IT DOES NOT SEE, so the assert message does not overclaim it: a
    fourth site that gets a pgid from somewhere OTHER than this lookup -
    a run record, a pidfile, an int off the wire - and signals it. That
    is the bare-pgid hazard, it is real, and it is #329, not this.

    THIS IS A LOCAL FIX TO A REPO-WIDE DEFECT, and saying so is the
    point. About eleven AST guards in this tree each re-implement this
    resolution, and they have been holed one at a time: the timeout audit
    missed `wait(timeout=None)` and `with Popen(...)`, then still missed
    `from subprocess import Popen as Spawn` after being repaired once;
    the toml guard fell to `import tomllib as _tl`; the journal guard to
    `open(path, mode="a")`; and this one to a module alias. That is five
    instances of one defect, of which this is the fifth. #324 tracks
    factoring the resolution onto a shared helper, and two round-2
    reviewers named `tests/test_toml_readers.py` as the closest existing
    copy. This class does not solve that, and a reader should not take it
    as though the problem were local.

    WHAT IT STILL MISSES is recorded by `test_the_remaining_misses`, so
    the reach is a measured fact rather than a claim.
    """

    @pytest.mark.parametrize("spelling", sorted(_MUST_CATCH))
    def test_every_spelling_of_the_call_is_caught(self, spelling: str) -> None:
        assert _getpgid_calls(_MUST_CATCH[spelling]) != [], (
            f"a fourth copy written as {spelling!r} would go through the net"
        )

    @pytest.mark.parametrize("spelling", sorted(_MUST_IGNORE))
    def test_a_name_is_not_a_binding(self, spelling: str) -> None:
        assert _getpgid_calls(_MUST_IGNORE[spelling]) == []

    def test_no_module_outside_procgroup_derives_a_pgid(self) -> None:
        owner = REPO_ROOT / "kstrl" / "procgroup.py"
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "kstrl").rglob("*.py")):
            if path == owner:
                continue
            for lineno in _getpgid_calls(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        assert offenders == [], (
            "deriving a process-group id outside procgroup is how a fourth "
            "copy of the safe-pgid guard starts; call procgroup.safe_pgid"
        )

    def test_the_owner_is_the_only_file_excluded(self) -> None:
        """The control on the sweep. Without it the test above passes on
        an empty walk, a broken parse or a matcher that matches nothing."""
        owner = REPO_ROOT / "kstrl" / "procgroup.py"
        assert _getpgid_calls(owner.read_text(encoding="utf-8")) != []

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "known misses, recorded rather than fixed. Marked xfail and "
            "written the way a PASS should read, so strengthening the "
            "resolver under #324 reports XPASS instead of breaking a test "
            "that asserted the hole stays open."
        ),
    )
    @pytest.mark.parametrize(
        "spelling",
        [
            'import os\npgid = getattr(os, "getpgid")(pid)\n',
            'import importlib\npgid = importlib.import_module("os").getpgid(pid)\n',
            'import os\nTABLE = {"f": os.getpgid}\npgid = TABLE["f"](pid)\n',
        ],
    )
    def test_the_remaining_misses(self, spelling: str) -> None:
        """Each needs value tracking through a string or a container,
        which is where a per-file AST net stops being the right tool.
        They are also spellings nobody reaches for by accident, unlike
        the module alias that got through round 1."""
        assert _getpgid_calls(spelling) != []
