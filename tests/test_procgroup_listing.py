"""#366: the ``ps`` READ and its parse, tested where they live.

Split out of ``tests/test_procgroup.py``, which was one line under the
800-line file-length ratchet. The division is by subject module:
``kstrl/procgroup_listing.py`` owns the spawn, the bound on it and the
parse of its output, and everything here is about one of those three.
What ``kstrl/procgroup.py`` makes of a reading - live, gone, or unknown -
stayed next door.

The state matching is table-driven against synthetic ``ps`` output, and
the real never-reaping-parent tree in ``tests/test_shutdown.py`` stays
where it is as the end-to-end proof that the synthetic rows describe
something that actually happens.
"""

from __future__ import annotations

import ast
import os
import subprocess
import time
from pathlib import Path

import pytest

from kstrl import procdispose
from kstrl.procgroup import (
    PS_KILL_GRACE_SECONDS,
    PS_TIMEOUT_SECONDS,
    GroupLiveness,
    _read_listing,
    read_group_liveness,
)
from tests.helpers import astwalk, procs


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

    # `test_the_three_facts_are_not_transposed` and the ragged-row test
    # moved to tests/test_procgroup_members.py with #209: their expected
    # values are `_Listing`'s two pid tuples now, so they read next to the
    # reading built on them. This file was at 796 of the 800-line ratchet.

    def test_a_group_id_is_matched_whole_not_as_a_prefix(self) -> None:
        """#292 in miniature: 7 must not match 70."""
        assert _read_listing("1 1 Ss\n50 70 Ss\n", pgid=7).rows == 0

    def test_pid_one_is_what_marks_the_listing_complete(self) -> None:
        assert _read_listing("50 7 Ss\n", pgid=7).complete is False
        assert _read_listing("1 1 Ss\n50 7 Ss\n", pgid=7).complete is True


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
        monkeypatch.setattr("kstrl.procgroup_listing.PS_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr("kstrl.procgroup_listing.PS_KILL_GRACE_SECONDS", 0.05)
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
        monkeypatch.setattr("kstrl.procgroup_listing.PS_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr("kstrl.procgroup_listing.PS_KILL_GRACE_SECONDS", 0.02)
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
        assert procdispose._ABANDONED == []

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
        monkeypatch.setattr("kstrl.procgroup_listing.PS_ARGV", ("sleep", "30"))
        monkeypatch.setattr("kstrl.procgroup_listing.PS_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr("kstrl.procgroup_listing.PS_KILL_GRACE_SECONDS", 0.05)
        # Nothing may be killed, so the child is genuinely abandoned alive.
        monkeypatch.setattr("kstrl.procgroup_listing.subprocess.Popen.kill", lambda self: None)
        read_group_liveness(4242)
        registered = list(procdispose._ABANDONED)
        assert len(registered) == 1

        child = registered[0]
        child.terminate()
        child.wait(timeout=10)
        # The next read sweeps it, which is the whole contract.
        monkeypatch.setattr("kstrl.procgroup_listing.PS_ARGV", ("true",))
        read_group_liveness(4242)
        assert procdispose._ABANDONED == [], "a dead child must leave the register"

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
        monkeypatch.setattr("kstrl.procgroup_listing.PS_ARGV", ("sleep", "30"))
        monkeypatch.setattr("kstrl.procgroup_listing.PS_TIMEOUT_SECONDS", 0.05)

        before = len(os.listdir(fd_dir))
        liveness = read_group_liveness(4242)
        after = len(os.listdir(fd_dir))

        assert liveness.live is None, "an unread group must not report as gone"
        assert after == before, f"descriptors leaked: {before} -> {after}"
        assert procdispose._ABANDONED == [], "a killable child must be reaped, not kept"

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


# The centralisation has a mechanism, not just a docstring. Two layers
# since #324, and the class below says what each one is for.

#: Callables whose string arguments are a command line, by LAST
#: IDENTIFIER, which deliberately over-matches. ``popen`` and
#: ``getstatusoutput`` were missing at first, so ``os.popen("ps -A")``
#: passed the net silently.
_SUBPROCESS_CALLS = frozenset(
    "run Popen popen call check_call check_output getoutput getstatusoutput system".split()
)

#: The same callables as DOTTED ORIGINS. Nothing can resolve to the two
#: products that do not exist, so the cross product costs nothing.
_SPAWN_TARGETS = frozenset(
    {f"subprocess.{name}" for name in _SUBPROCESS_CALLS} | {"os.popen", "os.system"}
)

#: The roots this net walks, and so the reach of the claim
#: ``kstrl/procgroup.py`` makes. ``spike/`` is deliberately outside: its
#: throwaway scripts call ``ps`` today, and covering them would mean
#: failing on evidence. ``astwalk``'s two corpora ARE these two roots.
_SCANNED_ROOTS = ("kstrl", "tests")

#: The one file allowed to shell out to ``ps``.
_PS_OWNER = "kstrl/procgroup_listing.py"


def _leading_command(folded: str | None) -> str | None:
    """The basename of a folded argv's first token, so that ``/bin/ps``
    and a ``shell=True`` string both count and ``psql`` does not."""
    tokens = folded.split() if folded else []
    return Path(tokens[0]).name if tokens else None


def _spells_the_ps_command(node: ast.AST) -> bool:
    """Layer 1's predicate: does this expression NAME the ps command?"""
    return _leading_command(astwalk.folded_str(node)) == "ps"


def _command_name(arg: ast.expr) -> str | None:
    """The command one ARGUMENT names. ``assignment_parts`` unwraps
    ``run((argv := [...]))``; the sequence branch is the argv list."""
    _targets, bound = astwalk.assignment_parts(arg)
    node = bound if bound is not None else arg
    command = _leading_command(astwalk.folded_str(node))
    if command is None and isinstance(node, ast.List | ast.Tuple) and node.elts:
        command = _leading_command(astwalk.folded_str(node.elts[0]))
    return command


def _argv_constants(tree: ast.Module) -> dict[str, str]:
    """Names bound to a literal argv, mapped to the command it names.

    A copier writes ``PS_ARGV = ("ps", ...)`` then ``run(PS_ARGV)``, as
    this module's owner does; without it the net passed over the one call
    it exists to protect. Per MODULE, not per scope: over-reporting.
    """
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        targets, value = astwalk.assignment_parts(node)
        command = _command_name(value) if value is not None else None
        if command is None:
            continue
        for target in targets:
            if target is not None:
                found.setdefault(target, command)
    return found


def _names_ps(arg: ast.expr, constants: dict[str, str]) -> bool:
    """Whether this argument is an argv whose command is ``ps``."""
    command = _command_name(arg)
    if command is None:
        command = constants.get(astwalk.dotted(arg) or "")
    return command == "ps"


def _is_spawn(node: ast.Call, table: astwalk.Bindings) -> bool:
    """Whether this call spawns a process, by NAME or by RESOLVED ORIGIN.
    The name reaches ``sp.check_output(...)`` in a snippet that never
    imported ``sp``; the origin reaches ``run as spawn``."""
    return (
        astwalk.leaf_name(node.func) in _SUBPROCESS_CALLS
        or table.resolve(node.func) in _SPAWN_TARGETS
    )


def _ps_call_lines(source: str, module: str = "") -> list[int]:
    """Line numbers of subprocess calls in ``source`` that invoke ``ps``."""
    tree = astwalk.parse(source)
    table = astwalk.bindings(tree, module=module)
    constants = _argv_constants(tree)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_spawn(node, table)
        and any(
            _names_ps(arg, constants) for arg in [*node.args, *(kw.value for kw in node.keywords)]
        )
    ]


#: Layer 1's inventory, per module. This file is excluded because its
#: own fixtures spell the command on purpose; layer 2 still walks it.
EXPECTED_PS_COMMAND_SPELLINGS: dict[str, int] = {
    # RE-DERIVED BY RUNNING THIS CENSUS, not by editing the literal, at
    # every step of #209 round 3. Reading a pin is not running a guard:
    # a refactor that splits a file leaves the pin naming one file while
    # the census counts two halves, and the unchanged literal then reads
    # as evidence that nothing moved. #366 moved this guard and its
    # `exclude=Path(__file__)` here together and re-ran the census: the
    # two excluded nodes moved with it and `tests/test_procgroup.py`
    # contributes no row, so the literal below is unchanged by
    # measurement, not by assumption.
    #
    # `PS_ARGV` left this file when #209 round 3 split the `ps` call and
    # its parse into `kstrl/procgroup_listing.py` for the 800-line
    # ratchet. What stays is the five refusal messages, which are
    # diagnostic sentences beginning with the word - the case this
    # census's own message says to add a row for, not a second place
    # that shells out to `ps`.
    "procgroup.py": 5,
    "procgroup_listing.py": 1,  # PS_ARGV itself, and the one call site
    # The skip reason for a `ps` filtered to one uid. #209 round 3 moved
    # it here from `tests/test_serve_process_tree.py` (a mark) and
    # `tests/test_shutdown.py` (an in-body skip), which is why both of
    # those rows are gone: three spellings of one predicate became one,
    # after round 2 measured the local copy unable to fire.
    "tests/helpers/procs.py": 1,
    # #209's members read reports the same uid-filtered listing the
    # liveness read does, from the one `_FILTERED_VIEW` sentence; these
    # are the tests that assert the refusal reaches its caller. 1 -> 2 in
    # round 3, when the two liveness ps-failure tests moved out of this
    # file to sit with their count twins, taking a "ps failed" assertion
    # with them.
    "tests/test_procgroup_members.py": 2,
    "tests/test_process_scoping.py": 2,  # two assertions on those messages
    "tests/test_serve.py": 6,  # the fake's argv, plus five assertions
}


class TestOnlyOneModuleShellsOutToPs:
    """``kstrl/procgroup.py`` and ``tests/helpers/procs.py`` both argue
    that two copies of one ``ps`` parse with slightly different failure
    handling is how the suite's answer and the daemon's drift apart. That
    argument was true and unenforced: nothing stopped a third copy. The
    repo already answers this class with an AST net (``test_atomicio`` on
    ``mkstemp``, ``test_process_scoping`` on ``pgrep``), and the rule is
    that if there is no mechanism there is no plan.

    LAYER 1 is a census of every expression whose folded value NAMES the
    command; it enumerates no node types and no fields, so no call shape
    can get past it, and the price is that prose opening with the word
    folds in too. LAYER 2 is the walk, which names the line and the
    callee, because "this module's count moved" is the wrong message for
    "you shelled out to ps, call read_group_liveness".

    #324 changed layer 2 twice, both measured. The callee resolves by
    ORIGIN as well as by last identifier, since ``run as spawn`` walked
    past the name match undisclosed; and the argv resolves through
    ``astwalk.assignment_parts``, so an annotated assignment, a walrus, a
    dotted target and a name bound in a function body all resolve now.
    """

    def test_no_second_ps_call_exists(self) -> None:
        offenders: list[str] = []
        for source in astwalk.package_sources() + astwalk.test_sources():
            rel = source.relative_to(astwalk.REPO_ROOT).as_posix()
            if rel == _PS_OWNER:
                continue
            text = source.read_text(encoding="utf-8")
            module = astwalk.module_name(source)
            offenders += [f"{rel}:{n}" for n in _ps_call_lines(text, module)]
        assert offenders == [], (
            f"{offenders} shell out to ps. There must be exactly one parse "
            f"of ps output in this tree, in {_PS_OWNER}, because two copies "
            f"drift on failure handling and the daemon's answer and the "
            f"suite's stop agreeing. Call kstrl.procgroup.read_group_liveness."
        )

    def test_nobody_names_the_ps_command_without_appearing_here(self) -> None:
        """Layer 1, the net. NEW code naming the command has to change
        this dict whatever shape it uses: a count of folded values has no
        shape list to be incomplete."""
        astwalk.assert_census(
            sources=astwalk.package_sources() + astwalk.test_sources(exclude=Path(__file__)),
            sees=_spells_the_ps_command,
            expected=EXPECTED_PS_COMMAND_SPELLINGS,
            control='subprocess.run(["ps", "-A"])\n',
            message=(
                "The set of places naming the ps command changed. There must be one "
                f"parse of ps output in this tree, in {_PS_OWNER}: two copies drift on "
                "failure handling. If this is a diagnostic message, add the row."
            ),
        )

    def test_the_owner_still_calls_ps(self) -> None:
        """Without this the net could be passing because nothing calls ps
        at all, which would mean the module had been gutted."""
        text = (astwalk.REPO_ROOT / _PS_OWNER).read_text(encoding="utf-8")
        assert _ps_call_lines(text), f"{_PS_OWNER} no longer calls ps, so this net measures nothing"

    def test_the_net_walks_a_real_tree(self) -> None:
        assert len(astwalk.package_sources() + astwalk.test_sources()) > 100

    def test_the_claim_names_the_roots_the_net_actually_walks(self) -> None:
        """A mechanism cited for a claim it does not cover is worse than
        none. `spike/` calls ps and is outside the net, so the module's
        docstring must say "kstrl/ or tests/", not "the tree"."""
        text = (astwalk.REPO_ROOT / _PS_OWNER).read_text(encoding="utf-8")
        claim = "only place in ``kstrl/`` or ``tests/``"
        assert claim in text, f"{_PS_OWNER} must scope its uniqueness claim to {_SCANNED_ROOTS}"

    @pytest.mark.parametrize(
        ("body", "line"),
        [
            ('subprocess.run(["ps", "-A"])', 1),
            ('subprocess.run(["/bin/ps", "-eo", "pid="])', 1),
            ('subprocess.Popen("ps -A", shell=True)', 1),
            ('sp.check_output(("ps", "-A"))', 1),
            ('os.popen("ps -A")', 1),
            ('subprocess.getstatusoutput("ps -A")', 1),
            # #324: no last identifier can see this one, and nothing
            # disclosed that. The resolved origin can.
            ('from subprocess import run as spawn\nspawn(["ps", "-A"])\n', 2),
            # Folded, and a walrus, so neither is a way past.
            ('subprocess.run(["p" + "s", "-A"])', 1),
            ('subprocess.run((argv := ["ps", "-A"]))', 1),
            # The shape the owner uses, and the shape a copier writes.
            ('ARGV = ("ps", "-A")\nsubprocess.run(ARGV, timeout=5)\n', 2),
            ('ARGV: tuple = ("ps", "-A")\nsubprocess.run(ARGV)\n', 2),
            # A disclosed miss until #324: a name bound in a function body.
            ('def f():\n    cmd = ["ps", "-A"]\n    subprocess.run(cmd)\n', 3),
        ],
    )
    def test_the_net_catches_a_planted_call(self, body: str, line: int) -> None:
        """Its reach, measured rather than asserted in a docstring."""
        assert _ps_call_lines(body) == [line], body

    @pytest.mark.parametrize(
        "body",
        [
            'x = ["ps", "-A"]',  # not a subprocess call at all
            'subprocess.run(["psql", "-c", "select 1"])',  # a different tool
            '"""Do not call ps here."""',  # prose: the net reads the AST
        ],
    )
    def test_the_net_stays_quiet_on_these(self, body: str) -> None:
        assert _ps_call_lines(body) == [], body

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason="folding, not interpreting")
    def test_a_command_the_interpreter_has_to_build_is_missed(self) -> None:
        """The one disclosed limit, with a test behind it rather than a
        sentence. Both layers fold and neither runs the program, so
        ``"".join(...)`` is missed while ``"p" + "s"`` is caught, and
        ``strict=True`` fails the day that stops being true."""

        def either_layer(source: str) -> int:
            walk = ast.walk(astwalk.parse(source))
            hits = sum(1 for node in walk if _spells_the_ps_command(node))
            return hits + len(_ps_call_lines(source))

        astwalk.blind_spot(either_layer, 'subprocess.run(["".join(("p", "s")), "-A"])')
