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
from tests.helpers.astwalk import (
    KSTRL_PACKAGE,
    Sites,
    assert_census,
    assert_sites,
    blind_spot,
    calls_to,
    label,
    module_name,
    package_sources,
    parse,
    parsed,
    spells,
)


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


#: The callable the net looks for, as the DOTTED ORIGIN the resolver
#: reports rather than as a bare name. A bare ``getpgid`` is this lookup
#: only when the resolver says it came from ``os``.
_TARGET = "os.getpgid"

#: The one file allowed to derive a group id: the module that owns the
#: guard every other caller has to route through.
_OWNER = "procgroup.py"


def _sites(source: str) -> Sites:
    """Every call in one snippet that resolves to ``os.getpgid``, and
    every call that could be one and could not be decided.

    The resolution is ``tests/helpers/astwalk``'s. This file used to
    carry 155 lines of its own, and #324 is the record of what having
    eleven such copies cost: two review rounds each defeated the version
    before it by planting a complete working fourth guard, round 1 behind
    ``import os as operating_system`` and round 2 behind ``_o = os``.
    """
    return calls_to(parse(source), {_TARGET})


def _expression(row: str) -> str:
    """The expression half of an ``astwalk`` site row, without its line.

    A pinned list carrying line numbers fails whenever an unrelated edit
    to an unrelated file moves one, which is how a guard becomes
    something to be silenced. The module and the expression are what a
    reader needs and neither moves.
    """
    return row.split(" ", 1)[1]


def _module_sites(source_file: Path) -> Sites:
    """One package module's answer, keyed by module and expression."""
    found = calls_to(parsed(source_file), {_TARGET}, module=module_name(source_file))
    where = label(source_file)
    return Sites(
        tuple(f"{where}: {_expression(row)}" for row in found.seen),
        tuple(f"{where}: {_expression(row)}" for row in found.undecided),
    )


def _surfaced(source: str) -> int:
    """How many sites layer 2 reports about one snippet, in EITHER half.

    Both halves, because the ratchet on a recorded miss has to fail the
    day the walk starts merely NOTICING the shape, not only the day it
    resolves it.
    """
    found = _sites(source)
    return len(found.seen) + len(found.undecided)


def _spellings(source: str) -> int:
    """How many nodes in one snippet spell ``getpgid``. Layer 1, on text."""
    sees = spells(_TARGET.split(".")[-1])
    return sum(1 for node in ast.walk(parse(source)) if sees(node))


#: Every spelling the net must flag.
#:
#: Four of these are spellings that a review round planted a COMPLETE,
#: working fourth copy of the guard behind and watched go through:
#: `module alias`, `from import`, `callable rebind` and `class attribute`
#: in round 1, and `module rebind` in round 2. The rest are forms the
#: sibling guards in this repo were each separately holed by, kept as
#: regression cover against a matcher that starts reading arguments or
#: scope again. Order here carries no meaning: `parametrize` sorts.
#:
#: Two rows exist to pin resolver machinery that nothing else reaches,
#: which is why they read as contrived. `global installed later` is the
#: only input where the fixed point changes the answer (every one of the
#: 127 real files converges in a single pass), and `attribute assigned
#: on self` is the only input that reaches the attribute-target branch.
#:
#: `getattr` was a row in `test_the_remaining_misses` until #324. The
#: shared resolver folds the name and resolves the receiver, so it is
#: caught now and belongs here: a recorded miss that quietly stops being
#: one is the exact failure the strict xfail below exists to prevent.
_MUST_CATCH = {
    "direct": "import os\npgid = os.getpgid(pid)\n",
    "keyword argument": "import os\npgid = os.getpgid(pid=pid)\n",
    "inside a helper": "import os\n\n\ndef helper(pid):\n    return os.getpgid(pid)\n",
    "nested function": (
        "import os\n\n\ndef outer():\n    def inner():\n"
        "        return os.getpgid(1)\n\n    return inner\n"
    ),
    "module alias": "import os as operating_system\npgid = operating_system.getpgid(pid)\n",
    "module rebind": "import os\nx = os\npgid = x.getpgid(pid)\n",
    "module rebind chain": "import os\na = os\nb = a\npgid = b.getpgid(pid)\n",
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
    # A module that installs its hook from one function and uses it from
    # another defined above it. `ast.walk` hands the inner assignment over
    # before the outer one, so a single resolution pass never sees it.
    "global installed later": (
        "import os\n\n_resolve = None\n\n\ndef derive(pid):\n"
        "    hop = _resolve\n    return hop(pid)\n\n\ndef install():\n"
        "    global _resolve\n\n    _resolve = os.getpgid\n"
    ),
    # An `ast.Attribute` on the LEFT of the assignment, which no other row
    # here produces.
    "attribute assigned on self": (
        "import os\n\n\nclass Holder:\n    def __init__(self) -> None:\n"
        "        self.lookup = os.getpgid\n\n"
        "    def derive(self, pid):\n        return self.lookup(pid)\n"
    ),
    # Promoted from `test_the_remaining_misses` by #324's shared resolver.
    "getattr with a foldable name": 'import os\npgid = getattr(os, "getpgid")(pid)\n',
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
    # Pins the precision resolving to a dotted ORIGIN buys: `lookup` is
    # the callable, `other.lookup` is somebody else's attribute of that
    # name. This holds only while no attribute of that name is bound
    # anywhere in the same file; `astwalk.Bindings.attributes` carries no
    # owner and says so.
    "the same name as an attribute of something else": (
        "import os\nlookup = os.getpgid\npgid = other.lookup(1)\n"
    ),
}

#: What the walk could not DECIDE about the rows above, per row.
#:
#: Two of the five are not dismissed, they are undecided: a call on the
#: result of a call has no name to read, and a bare name spelled like the
#: target and bound nowhere could be either. Neither is an offender and
#: neither is silence, which is the whole point of the partition. Written
#: as a claim, so a walk that stopped noticing them fails here.
_UNDECIDED_IGNORES: dict[str, tuple[str, ...]] = {
    "unrelated method of the same name": ("6 C().getpgid",),
    "unrelated free function of the same name": ("5 getpgid",),
}

#: Spellings the walk SURFACES but cannot decide, with the exact row it
#: reports. Both were rows in `test_the_remaining_misses` until #324,
#: where they read as silent misses; the shared resolver reports them as
#: undecided instead, which is a different fact and belongs in a
#: different table. A guard that reports "I cannot tell" about a call is
#: not a guard that passed.
_MUST_BE_UNDECIDED = {
    "a module fetched by name": (
        'import importlib\npgid = importlib.import_module("os").getpgid(pid)\n',
        ("2 importlib.import_module('os').getpgid",),
    ),
    "a callable fetched from a table": (
        'import os\nTABLE = {"f": os.getpgid}\npgid = TABLE["f"](pid)\n',
        ("3 TABLE['f']",),
    ),
}

#: What layer 2 still cannot see AT ALL, in either half. Two rows, both
#: measured on this tree rather than reasoned about.
#:
#: `attribute to module` needs a map from an attribute name to a MODULE,
#: which `astwalk.Bindings` deliberately does not keep: `attributes` maps
#: an attribute to a callable origin, and `G.mod.getpgid` asks it to
#: resolve the RECEIVER of an attribute rather than the attribute itself.
#: `tuple destructuring` needs a target the AST cannot spell as a path,
#: which `astwalk.assignment_parts` answers `None` for rather than
#: guessing at.
#:
#: Layer 1 catches both, which is why layer 1 exists;
#: `test_layer_one_catches_what_layer_two_cannot` is that measurement.
_LAYER_TWO_MISSES = {
    "attribute to module": "import os\n\n\nclass G:\n    mod = os\n\n\npgid = G.mod.getpgid(pid)\n",
    "tuple destructuring": (
        "import os\n\nhop, other = _resolve, None\n_resolve = os.getpgid\npgid = hop(1)\n"
    ),
}

#: Every node in ``kstrl/`` that spells ``getpgid``. Layer 1, and it
#: resolves nothing: a module cannot ask the kernel for a process group
#: without naming the call, so a fourth copy of the guard has to change
#: this dict whatever shape it takes. Two rows in one file: the call
#: itself, and the `hasattr` gate above it.
EXPECTED_GETPGID_SPELLINGS: dict[str, int] = {_OWNER: 2}

#: What layer 2 cannot decide about ``kstrl/``, and it is the same four
#: sites whatever the target is: a call through a subscript and a call on
#: the result of a call have no last identifier for the walk to read.
#: `astwalk.calls_to` documents these four as the hard undecidable, and
#: pinning them here is the difference between a guard that says "I did
#: not look at these" and one that does not mention them.
EXPECTED_UNDECIDED_SITES: tuple[str, ...] = (
    "gateparse.py: TOOL_PARSERS[chosen]",
    "gateparse.py: TOOL_PARSERS[name]",
    "tui/app.py: initial_screens_for_kind(kind, observe_only=False)",
    "tui/app.py: initial_screens_for_kind(kind, observe_only=True)",
)


class TestNoCallerCarriesItsOwnCopy:
    """The point of #308: a fourth site would be invisible to the above.

    TWO LAYERS, since #324.

    LAYER 1 is a census of every expression in ``kstrl/`` that SPELLS
    ``getpgid``, per module. It enumerates no node types and no fields: a
    module cannot ask the kernel for a process group without naming the
    call, so a fourth copy in any shape has to change that dict first,
    whatever it does with the answer afterwards. It reaches both shapes
    layer 2 records as misses.

    LAYER 2 is the walk, which RESOLVES a call to `os.getpgid` - through
    module aliases, module rebinds, from-imports, callable rebinds, class
    attributes and `getattr` - and names the offending line. It is not
    redundant: layer 1 can only say "procgroup.py's count moved", which
    is the wrong message when the answer is "this file derives a pgid of
    its own, call procgroup.safe_pgid". Two review rounds each defeated
    the version before it by planting a complete working guard: round 1
    behind `import os as operating_system`, round 2 behind `_o = os`.

    WHAT NEITHER LAYER SEES, so the assert message does not overclaim it:
    a fourth site that gets a pgid from somewhere OTHER than this lookup
    - a run record, a pidfile, an int off the wire - and signals it. That
    is the bare-pgid hazard, it is real, and it is #329, not this. It is
    pinned by `test_a_pgid_that_was_never_looked_up_is_invisible` rather
    than left as a sentence.

    THIS WAS A LOCAL FIX TO A REPO-WIDE DEFECT until #324. The resolution
    lives in `tests/helpers/astwalk` now, along with the ten other copies
    #324 logged, and this file's share of it - 168 lines - is deleted
    rather than maintained.
    """

    @pytest.mark.parametrize("spelling", sorted(_MUST_CATCH))
    def test_every_spelling_of_the_call_is_caught(self, spelling: str) -> None:
        found = _sites(_MUST_CATCH[spelling])

        assert found.seen != (), f"a fourth copy written as {spelling!r} would go through the net"
        assert found.undecided == (), f"{spelling!r} resolved AND left a loose end"

    @pytest.mark.parametrize("spelling", sorted(_MUST_IGNORE))
    def test_a_name_is_not_a_binding(self, spelling: str) -> None:
        """Both halves, so "not an offender" cannot quietly mean "not
        looked at". Two of these five are undecided rather than
        dismissed, and `_UNDECIDED_IGNORES` says which."""
        assert_sites(
            _sites(_MUST_IGNORE[spelling]),
            seen=(),
            undecided=_UNDECIDED_IGNORES.get(spelling, ()),
            message=f"{spelling!r} is a name, not a binding, and must not be flagged.",
        )

    @pytest.mark.parametrize("spelling", sorted(_MUST_BE_UNDECIDED))
    def test_a_call_the_walk_cannot_follow_is_undecided_not_absent(self, spelling: str) -> None:
        """The #324 change, on the two rows it moved.

        A call through a string or a container is not resolvable, and it
        is not clean either. Reported as neither, it is a fourth copy of
        the guard shipping unnoticed; reported as undecided, it is a row
        somebody has to account for."""
        source, expected = _MUST_BE_UNDECIDED[spelling]

        assert_sites(
            _sites(source),
            seen=(),
            undecided=expected,
            message=f"{spelling!r} must be reported as undecided.",
        )

    def test_no_module_outside_procgroup_derives_a_pgid(self) -> None:
        """Layer 2, over the package, with both halves pinned.

        ``undecided`` is not an inconvenience here, it is the assertion:
        writing it out is what stops "no offenders" also meaning "four
        calls I never looked at"."""
        found = Sites()
        for path in package_sources():
            if label(path) != _OWNER:
                found += _module_sites(path)

        assert_sites(
            found.sorted(),
            seen=(),
            undecided=EXPECTED_UNDECIDED_SITES,
            message=(
                "deriving a process-group id outside procgroup is how a fourth "
                "copy of the safe-pgid guard starts; call procgroup.safe_pgid."
            ),
        )

    def test_the_owner_is_the_only_file_excluded(self) -> None:
        """The control on the sweep. Without it the test above passes on
        an empty walk, a broken parse or a matcher that matches nothing."""
        assert _module_sites(KSTRL_PACKAGE / _OWNER).seen == (f"{_OWNER}: {_TARGET}",)

    def test_no_module_gets_hold_of_getpgid_without_appearing_here(self) -> None:
        """Layer 1, the net: pin every spelling of the call itself.

        A module cannot ask the kernel for a process group without naming
        the call, so NEW code that reaches for it has to change this
        dict, whatever shape the derivation takes afterwards. That is why
        this layer resolves nothing and enumerates no node types: an
        exact count of spellings has no shape list to be incomplete.
        """
        assert_census(
            sources=package_sources(),
            sees=spells(_TARGET.split(".")[-1]),
            expected=EXPECTED_GETPGID_SPELLINGS,
            control="import os\npgid = os.getpgid(1)\n",
            message=(
                "The set of places that name getpgid changed. Deriving a "
                "process-group id outside procgroup is how a fourth copy of the "
                "safe-pgid guard starts: killpg(1, sig) is kill(-1, sig), every "
                "process this user owns. Call procgroup.safe_pgid."
            ),
        )

    @pytest.mark.parametrize("spelling", sorted(_LAYER_TWO_MISSES))
    def test_layer_one_catches_what_layer_two_cannot(self, spelling: str) -> None:
        """The reason there are two layers, measured on the two rows the
        strict xfail below records as layer 2's misses. Neither is
        invisible to the guard as a whole; both are invisible to the
        walk, and the disclosure has to say which."""
        assert _spellings(_LAYER_TWO_MISSES[spelling]) > 0, (
            f"{spelling!r} is disclosed as a layer 2 miss, so layer 1 is the only "
            f"thing covering it, and it does not"
        )


class TestTheDisclosedLimits:
    """Every "this cannot see X" above, with a test behind it.

    Under ``xfail(strict=True, raises=AssertionError)``, which is what
    makes these a ratchet rather than a note. Round-2 review of #308
    measured the first version: with ``strict=False`` and no ``raises``,
    XFAIL, XPASS and a resolver that raises on entry were all green, so
    the record could go stale in silence. Now closing a hole fails here,
    and gutting the resolver fails here.
    """

    @pytest.mark.parametrize("spelling", sorted(_LAYER_TWO_MISSES))
    @pytest.mark.xfail(strict=True, raises=AssertionError, reason="recorded layer 2 misses")
    def test_the_remaining_misses(self, spelling: str) -> None:
        """Each needs something the shared resolver does not do: a map
        from an attribute name to a module, or destructuring a tuple
        assignment. Written the way a PASS should read, so strengthening
        `astwalk` fails this test and forces the row into `_MUST_CATCH`
        rather than leaving a stale note behind."""
        blind_spot(_surfaced, _LAYER_TWO_MISSES[spelling])

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason="layer 1 folds, it does not run")
    def test_a_name_the_interpreter_has_to_build_is_missed(self) -> None:
        """Layer 1 folds ``"get" + "pgid"`` and every f-string it can
        decide. What it cannot decide is a value that needs the
        interpreter: ``"".join(...)``, ``%``-formatting, a name looked up
        at run time."""
        blind_spot(_spellings, 'import os\npgid = getattr(os, "".join(("get", "pgid")))(pid)\n')

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason="#329, not this guard")
    def test_a_pgid_that_was_never_looked_up_is_invisible(self) -> None:
        """The scope claim in the class docstring, pinned rather than
        asserted in prose. A pgid read off a pidfile and signalled
        reaches neither layer, because neither layer is about signalling
        - they are about the lookup. That hazard is #329."""
        source = "pgid = int(open('run.pid').read())\nos.killpg(pgid, 9)\n"

        blind_spot(lambda text: _surfaced(text) + _spellings(text), source)
