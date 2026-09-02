"""#292 as a class: no test may search the whole machine for a process.

The issue's own framing is "a machine-wide ``pgrep`` in a test suite is a
class, not an instance". Fixing the one test that had it leaves nothing
stopping the next one, and this repo's rule is that if there is no
mechanism there is no plan. So the rule stated in
``tests/helpers/procs.py`` is enforced here rather than only written
down:

    A test may assert on a process it can name. It may not assert on
    what else the machine happens to be running.

What that rule bought, concretely: ``pgrep -f "sleep 60"`` matches an
unrelated ``sleep 600`` in any other session, because the pattern is a
substring of the full command line. Roughly six agents diagnosed the
resulting failure separately, several with controlled A/B runs against a
clean ref, and every one correctly concluded it was unrelated to their
diff. The cost was not the bug, it was that the failure mode pointed
nowhere near its cause.

Swept when this landed: ``test_shutdown.py`` was the only instance in the
suite. Every other process test already names a pid it spawned
(``test_timeout_enforcement`` via a pidfile, ``test_serve`` via a printed
pid, ``test_spine_crash_recovery`` via ``killpg`` on its own child).

TWO LAYERS, because one of them is a net and the other is a message.

LAYER 1, :func:`_searches_the_machine` under a census, counts every
expression in ``tests/`` that folds to a command line naming one of the
four tools, per file. It is the net: a process search cannot be run by a
file that never spells the tool, so a new one has to appear here first,
whatever runs it. It resolves nothing and enumerates no node types, and
that is what makes it see the shape the round-1 disclosure gave up on:
``cmd = ["pgrep", ...]`` on one line and ``subprocess.run(cmd)`` on the
next.

LAYER 2, :func:`_machine_wide_hits`, reads the arguments of a subprocess
call and names the offending line. It is not redundant: layer 1 can only
say "this file's count moved", which is the wrong message when the
answer is "line 589 searches the whole machine, assert on a pid you
created". It matches on each whitespace-separated token's BASENAME, so
``/usr/bin/pgrep`` and ``timeout 5 pgrep -f x`` are both caught. The
first version matched only the first token of a literal regardless of
context; measured, it missed both of those and it also exempted any
``test_shutdown.py`` added under a subdirectory, because it keyed the
allowlist on the bare filename.

A SPAWN RENAMED ON IMPORT used to be a disclosed miss here, on the
grounds that it was theoretical. It was not: ``tests/helpers/procs.py``
binds ``real_popen`` to ``subprocess.Popen`` and calls it, which is one
site of 162 that the callee's spelling cannot decide.
``tests/helpers/astwalk.py`` resolves it now, along with every other
import, alias and rebind shape, and the resolver is unioned WITH the
name match rather than replacing it, so nothing the old net saw is lost.

WHAT LAYER 2 STILL DOES NOT SEE: a command built into a variable first,
and one the interpreter has to assemble. Both are pinned below with
``astwalk.blind_spot``, so a disclosure that stops being true fails
here rather than rotting. Layer 1 sees the first of them.

Scoping layer 2 to subprocess arguments is what lets it stay quiet about
prose: this module and ``tests/helpers/procs.py`` both have to name the
forbidden commands to explain them, and a bare text or literal search
would need a suppression list that rots.

LAYER 1 IS NOT QUIET ABOUT PROSE, and an earlier draft of this paragraph
said it was. It folds a value and then reads each whitespace-separated
token's BASENAME, so a docstring reading "Do not use pgrep -f here." IS
a hit.
Measured, not reasoned: this file leaves itself out of its own corpus, and
``tests/helpers/procs.py`` escapes only because every mention there sits
inside RST double backticks, whose token basename is ```` ``pgrep`` ````
and not ``pgrep``. Strip the backticks and that file gains a row. That is
an over-report, the direction a guard may be wrong in, and the pinned
census is where a new one appears rather than a suppression list.
``test_layer_one_reads_prose_and_the_census_is_the_bound`` is that
measurement rather than this sentence.
"""

from __future__ import annotations

import ast
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import astwalk, procs

TESTS_DIR = astwalk.TESTS_DIR

#: Commands that answer "what else is this machine running?". A test
#: asking that is asserting about processes it did not start.
MACHINE_WIDE_PROCESS_TOOLS = frozenset({"pgrep", "pkill", "killall", "pidof"})

#: Callables whose string arguments are a command line, by their last
#: identifier. ``os.system`` is included because it is a shell string by
#: definition.
SUBPROCESS_CALLS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "system"}
)

#: The same seven as the dotted origins a resolver can decide, which is
#: what closes the alias ``from subprocess import run as r``. Both halves
#: are used: the name match keeps every site the round-1 net saw
#: (``runner.run(...)`` on some other object's method resolves to
#: nothing and would otherwise be dropped), and the origin match adds the
#: ones a spelling cannot decide.
SUBPROCESS_ORIGINS = frozenset(
    {f"subprocess.{name}" for name in SUBPROCESS_CALLS - {"system"}} | {"os.system"}
)

#: The one deliberate exception, keyed on the path relative to ``tests/``
#: rather than the bare filename, so a same-named file elsewhere in the
#: tree does not inherit the exemption. #292's regression test runs the
#: OLD machine-wide assertion against the same machine state as the new
#: group-scoped one, because running both is what proves they differ.
ALLOWED_PATHS = frozenset({"test_shutdown.py"})

#: Layer 1's pinned inventory: every file in ``tests/`` that spells a
#: machine-wide process search, and how many times. One row, and it is
#: the allowlisted demonstration in #292's own regression test. This
#: file is excluded from the corpus, so its eight mentions of the
#: forbidden commands do not appear.
#:
#: Adding a row is not forbidden, it is the point: the diff that adds one
#: is where somebody says why a test needs to ask what else the machine
#: is running.
EXPECTED_MACHINE_WIDE_SPELLINGS: dict[str, int] = {"tests/test_shutdown.py": 1}


def _names_a_tool(value: str) -> bool:
    """Does this command line name one of the four tools?

    Each whitespace-separated token's BASENAME, so ``/usr/bin/pgrep`` and
    ``timeout 5 pgrep -f x`` both count. Shared by both layers, so they
    cannot drift about what the subject is.
    """
    return any(Path(token).name in MACHINE_WIDE_PROCESS_TOOLS for token in value.split())


def _searches_the_machine(node: ast.AST) -> bool:
    """Layer 1's whole predicate: does this expression FOLD to such a line?

    It names no node type and no field, so a command held in a list, a
    tuple, a module constant or an f-string counts exactly like one
    written at the call. ``astwalk.folded_str`` is what decides
    ``"pg" "rep"`` and ``"pgrep" + " -f x"``; a value the interpreter
    has to build folds to ``None`` and is disclosed below.
    """
    return _names_a_tool(astwalk.folded_str(node) or "")


def _is_subprocess_call(node: ast.Call, table: astwalk.Bindings) -> bool:
    """Does this call spawn a process, by name OR by resolved origin?

    The union is deliberate. The name half keeps every site round 1 saw,
    including a method of that name on an object no walk can type. The
    origin half is what decides ``from subprocess import run as r`` and
    ``real_popen = subprocess.Popen``, the second of which is a real site
    in ``tests/helpers/procs.py`` and not the theoretical case the
    round-1 disclosure called it.
    """
    return (
        astwalk.leaf_name(node.func) in SUBPROCESS_CALLS
        or table.resolve(node.func) in SUBPROCESS_ORIGINS
    )


def _command_literals(source: Path) -> Iterator[tuple[str, int]]:
    """Folded string arguments of a subprocess call, with the call's line.

    ``astwalk.folded_str`` rather than ``isinstance(node, ast.Constant)``:
    a literal is one shape a command can arrive in, and ``"pgrep" +
    flags`` is another.
    """
    tree = astwalk.parsed(source)
    table = astwalk.bindings(tree, module=astwalk.module_name(source))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_subprocess_call(node, table):
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            for inner in ast.walk(arg):
                folded = astwalk.folded_str(inner)
                if folded is not None:
                    yield folded, node.lineno


def _machine_wide_hits(source: Path) -> list[str]:
    """Subprocess arguments in ``source`` naming a machine-wide search."""
    rel = astwalk.label(source, TESTS_DIR)
    return [
        f"{rel}:{lineno} {value!r}"
        for value, lineno in _command_literals(source)
        if _names_a_tool(value)
    ]


def _scannable_sources() -> list[Path]:
    """Every module in ``tests/`` but this one, which names what it forbids."""
    return astwalk.test_sources(exclude=Path(__file__))


class TestNoTestSearchesTheWholeMachineForAProcess:
    def test_no_test_file_even_spells_a_machine_wide_search(self) -> None:
        """Layer 1, the net: pin every spelling of a machine-wide search.

        A test cannot run a search it never names, so a new one has to
        change this dict whatever runs it: a command assembled into a
        variable and passed to a spawn on a later line, a helper of this
        module's own, a shell string built with ``+``. That is why this
        layer resolves nothing and enumerates no node types.

        ``control`` is what stops an empty inventory being the same
        green as a net that stopped looking. Layer 2 had no equivalent
        until this migration.
        """
        astwalk.assert_census(
            sources=_scannable_sources(),
            sees=_searches_the_machine,
            expected=EXPECTED_MACHINE_WIDE_SPELLINGS,
            # SPELLED OUT, not derived from MACHINE_WIDE_PROCESS_TOOLS.
            # A control built from the same constant co-varies with it, so
            # shrinking the set shrinks the controls and the guard goes
            # blind with everything green, which is exactly the mutation
            # this is here to fail: round 3 shrank the set to `pgrep` and
            # added real pkill, killall and pidof sweeps for 32 passed,
            # undetected, against an unmutated head of 2 failed.
            control=(
                'cmd = ["pgrep", "-f", "sleep 60"]\nsubprocess.run(cmd)\n',
                'cmd = ["pkill", "-f", "sleep 60"]\nsubprocess.run(cmd)\n',
                'cmd = ["killall", "sleep"]\nsubprocess.run(cmd)\n',
                'cmd = ["pidof", "sleep"]\nsubprocess.run(cmd)\n',
            ),
            message=(
                "The set of places naming a machine-wide process search changed. "
                "`pgrep -f 'sleep 60'` matches an unrelated `sleep 600` in any "
                "other session, and the failure points nowhere near its cause "
                "(#292). Use tests/helpers/procs.py and assert on a pid or pgid "
                "the test itself created."
            ),
        )

    def test_the_suite_is_clean(self) -> None:
        """Layer 2, the message: name the file, the line and the command."""
        offenders: list[str] = []
        for source in _scannable_sources():
            if str(source.relative_to(TESTS_DIR)) in ALLOWED_PATHS:
                continue
            offenders.extend(_machine_wide_hits(source))
        assert offenders == [], (
            f"{offenders} search the whole machine for a process. That is "
            f"#292: `pgrep -f 'sleep 60'` matches an unrelated `sleep 600` "
            f"in any other session, and the failure points nowhere near its "
            f"cause. Use tests/helpers/procs.py and assert on a pid or pgid "
            f"the test itself created."
        )

    def test_the_net_walks_a_real_suite(self) -> None:
        """Without this the sweep could be passing over nothing."""
        assert len(_scannable_sources()) > 50

    def test_the_allowlisted_file_still_needs_its_exemption(self) -> None:
        """An allowlist that outlives its reason is how a net rots.

        If #292's deliberate old-versus-new demonstration is ever
        removed, this fails and the entry should be deleted with it.
        """
        for rel in ALLOWED_PATHS:
            assert _machine_wide_hits(TESTS_DIR / rel), (
                f"{rel} is allowlisted but no longer contains a "
                f"machine-wide process search; drop it from ALLOWED_PATHS"
            )


class TestTheNetCatchesWhatItClaims:
    """Both layers' reach, measured rather than asserted in a docstring.

    The first four rows were real misses in the first version. The rest
    are the shapes this migration decides, and the two that it still
    cannot.
    """

    @staticmethod
    def _probe_file(tmp_path: Path, body: str) -> Path:
        """A probe written OUTSIDE the suite.

        Writing probes into ``tests/`` would leave one behind whenever a
        probe failed, and the next run would scan it.
        """
        source = tmp_path / "probe.py"
        source.write_text(body, encoding="utf-8")
        return source

    def _hits(self, tmp_path: Path, body: str) -> list[str]:
        """What layer 2 sees in one probe."""
        return _machine_wide_hits(self._probe_file(tmp_path, body))

    def _spelled(self, tmp_path: Path, body: str) -> dict[str, int]:
        """What layer 1 sees in one probe."""
        return astwalk.census([self._probe_file(tmp_path, body)], _searches_the_machine)

    def _either(self, tmp_path: Path, body: str) -> object:
        """What EITHER layer sees, for a blind spot that spans both."""
        return self._hits(tmp_path, body) or self._spelled(tmp_path, body)

    def test_an_absolute_path_is_caught(self, tmp_path: Path) -> None:
        """`/usr/bin/pgrep` passed the first version, which compared the
        whole token instead of its basename."""
        assert self._hits(tmp_path, 'subprocess.run(["/usr/bin/pgrep", "-f", "x"])\n')

    def test_a_shell_wrapped_command_is_caught(self, tmp_path: Path) -> None:
        """`timeout 5 pgrep ...` passed the first version, which looked
        only at the first token."""
        assert self._hits(tmp_path, 'subprocess.run("timeout 5 pgrep -f x", shell=True)\n')

    def test_every_forbidden_tool_is_caught(self, tmp_path: Path) -> None:
        for tool in sorted(MACHINE_WIDE_PROCESS_TOOLS):
            assert self._hits(tmp_path, f'subprocess.run(["{tool}", "x"])\n'), tool

    def test_layer_one_reads_prose_and_the_census_is_the_bound(self) -> None:
        """What layer 1 actually does with a docstring, measured.

        The module docstring used to claim a folded docstring never looks
        like a command line. It does: ``_names_a_tool`` splits on
        whitespace and takes each token's basename. Backticks are what
        keeps ``tests/helpers/procs.py`` out, and that is an accident of
        its RST rather than a mechanism, so it is written down here.
        """
        assert _names_a_tool("Do not use pgrep -f here.")
        assert not _names_a_tool("Do not use ``pgrep`` here.")
        assert _searches_the_machine(
            astwalk.parse('"""Do not use pgrep -f here."""\n').body[0].value  # type: ignore[attr-defined]
        )

    def test_prose_naming_the_command_is_not_a_hit(self, tmp_path: Path) -> None:
        """The reason the net reads subprocess arguments and not every
        literal: the files explaining this rule have to name it."""
        assert not self._hits(tmp_path, '"""Do not use pgrep -f here."""\nx = "pgrep"\n')

    def test_a_spawn_renamed_on_import_is_caught(self, tmp_path: Path) -> None:
        """The miss the round-1 disclosure called theoretical, closed.

        ``tests/helpers/procs.py`` binds ``real_popen`` to
        ``subprocess.Popen`` and calls it, so it was never theoretical:
        measured, that is one site of 162 in ``tests/`` that a match on
        the callee's spelling cannot decide.
        """
        body = 'from subprocess import run as r\nr(["pgrep", "-f", "x"])\n'
        assert self._hits(tmp_path, body)

    def test_a_spawn_rebound_to_a_local_is_caught(self, tmp_path: Path) -> None:
        """The shape ``procs.py`` actually has."""
        body = 'import subprocess\nspawn = subprocess.Popen\nspawn(["pgrep", "-f", "x"])\n'
        assert self._hits(tmp_path, body)

    @pytest.mark.parametrize("call", sorted(SUBPROCESS_CALLS))
    def test_every_spawn_name_is_caught(self, tmp_path: Path, call: str) -> None:
        """All seven rows of ``SUBPROCESS_CALLS``, not just ``run``.

        Before this, six of the seven had no control at all: deleting
        them from the frozenset left the file green, which is a table
        that documents rather than decides.
        """
        module = "os" if call == "system" else "subprocess"
        assert self._hits(tmp_path, f'{module}.{call}(["pgrep", "-f", "x"])\n'), call

    @pytest.mark.parametrize("origin", sorted(SUBPROCESS_ORIGINS))
    def test_every_spawn_origin_is_caught_through_an_alias(
        self, tmp_path: Path, origin: str
    ) -> None:
        """The same seven reached under a name that hides them."""
        module, name = origin.rsplit(".", 1)
        body = f'from {module} import {name} as _spawn\n_spawn(["pgrep", "-f", "x"])\n'
        assert self._hits(tmp_path, body), origin

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_command_hidden_in_a_variable_is_a_known_miss(self, tmp_path: Path) -> None:
        """Layer 2's residual, stated so it is not trusted past its reach.

        Following a list from the line that builds it to the line that
        spawns it needs dataflow the AST does not give. Layer 1 is what
        covers it: the tokens are still spelled, so the census still
        counts them, and ``test_the_net_sees_a_command_built_in_a_variable``
        is the measurement.

        THIS ROW WAS A PASSING TEST ON ``origin/main`` and is an xfail
        here, WITH THE SAME NODE ID. Round 3 of review found it, and the
        finding is about the accounting rather than the coverage: every
        table in this PR is keyed on node id, and a conversion in place
        moves no id, so it is invisible to all of them. There is no
        coverage loss, because the layer-1 sibling above covers the shape
        and is new here. What changed is the CLAIM: the old test asserted
        layer 2 sees this, which was true only because layer 2 was
        matching on a name it could not resolve, and the migration made
        it resolve. Of this branch's 34 xfails, 1 carried over from main's
        6, 32 are new disclosures, and this one is the sole conversion.
        """
        body = 'cmd = ["pgrep", "-f", "x"]\nsubprocess.run(cmd)\n'
        astwalk.blind_spot(lambda source: self._hits(tmp_path, source), body)

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_command_the_interpreter_builds_is_a_known_miss(self, tmp_path: Path) -> None:
        """The residual BOTH layers share, and the reason layer 1 is a
        net rather than a proof. A command only the interpreter can
        produce folds to ``None``, so neither layer sees it. Foldable
        assembly is not here: ``test_an_assembled_command_is_caught``
        measures that half."""
        body = 'subprocess.run(["".join(["pg", "rep"]), "-f", "x"])\n'
        astwalk.blind_spot(lambda source: self._either(tmp_path, source), body)

    def test_an_assembled_command_is_caught(self, tmp_path: Path) -> None:
        """``"pgrep" + " -f x"`` is what somebody writes to get past a
        string search, and constant folding decides it."""
        assert self._hits(tmp_path, 'subprocess.run("pgrep" + " -f x", shell=True)\n')

    def test_the_net_sees_a_command_built_in_a_variable(self, tmp_path: Path) -> None:
        """Layer 1 on layer 2's disclosed miss, measured rather than
        claimed: this is the whole reason there are two layers."""
        body = 'cmd = ["pgrep", "-f", "x"]\nsubprocess.run(cmd)\n'
        assert self._spelled(tmp_path, body) == {"probe.py": 1}


class TestTheLivenessHelperCannotPassByMeasuringNothing:
    """The same defect one level down, in the helper written to remove it.

    Why absence is only reportable by a call that proved it can see
    something is argued in ``tests/helpers/procs.py``. These pin it.

    The reading moved to ``kstrl.procgroup`` in #298, so the ``ps``
    double is ``procs.fake_ps``. What is under test here is still the
    helper's POLICY: a test that cannot see must fail. The daemon on
    the same reading degrades instead, which ``tests/test_serve.py``
    pins.
    """

    def test_it_sees_its_own_process_group(self) -> None:
        """The positive control. Without this the guard below could be
        passing because everything raises."""
        assert procs.group_has_live_member(os.getpgrp()) is True

    def test_a_failing_ps_raises_instead_of_reporting_absence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        procs.fake_ps(monkeypatch, returncode=127, stderr="ps: command not found")
        with pytest.raises(AssertionError, match="ps failed"):
            procs.group_has_live_member(os.getpgrp())

    def test_filtered_ps_output_raises_instead_of_reporting_absence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rc=0 but our own processes hidden, which is what a filtered
        listing looks like. Exit status alone is not enough.

        #298 replaced the control that decides this. It used to check
        that our own group appeared in the listing, which was satisfied
        by construction on every real ps and so could never fire. It now
        asks the kernel: the listing holds no row for this group, and
        ``killpg`` says the group is occupied, so the listing is not
        showing everything. The helper's POLICY is unchanged, which is
        the point of the test: it still refuses to convert a listing it
        cannot trust into an absence."""

        procs.fake_ps(monkeypatch, stdout="  90 517 Ss\n  91 517 Ss\n")
        with pytest.raises(AssertionError, match="did not list pid 1"):
            procs.group_has_live_member(os.getpgrp())

    def test_wait_for_group_to_die_does_not_convert_that_into_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wrapper is what an orphan assertion actually calls, so the
        guard has to survive it rather than being swallowed by the poll
        loop."""

        procs.fake_ps(monkeypatch, returncode=127, stderr="boom")
        with pytest.raises(AssertionError, match="ps failed"):
            procs.wait_for_group_to_die(os.getpgrp(), timeout=1.0)

    def test_a_dead_group_is_still_reported_dead(self) -> None:
        """The guard must not make absence unreportable, which would be
        the opposite failure."""
        child = subprocess.Popen(
            ["sleep", "30"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(child.pid)
        procs.kill_group(pgid)
        child.wait(timeout=10)
        assert procs.wait_for_group_to_die(pgid, timeout=10.0) is True
