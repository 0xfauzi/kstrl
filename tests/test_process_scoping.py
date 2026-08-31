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

WHAT THIS NET SEES, precisely, because a guard whose reach is not stated
gets trusted past it. It inspects string literals that are arguments to a
subprocess call, and matches on each whitespace-separated token's
BASENAME, so ``/usr/bin/pgrep`` and ``timeout 5 pgrep -f x`` are both
caught. The first version matched only the first token of a literal
regardless of context; measured, it missed both of those and it also
exempted any ``test_shutdown.py`` added under a subdirectory, because it
keyed the allowlist on the bare filename.

WHAT IT DOES NOT SEE: a command built into a variable first
(``cmd = ["pgrep", ...]`` then ``subprocess.run(cmd)``), or one assembled
at runtime. Following those needs dataflow the AST does not give. The net
raises the cost of the mistake; it is not a proof.

Scoping to subprocess arguments is what lets it stay quiet about prose:
this module and ``tests/helpers/procs.py`` both have to name the
forbidden commands to explain them, and a bare text or literal search
would need a suppression list that rots.
"""

from __future__ import annotations

import ast
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import procs

TESTS_DIR = Path(__file__).resolve().parent

#: Commands that answer "what else is this machine running?". A test
#: asking that is asserting about processes it did not start.
MACHINE_WIDE_PROCESS_TOOLS = frozenset({"pgrep", "pkill", "killall", "pidof"})

#: Callables whose string arguments are a command line. ``os.system`` is
#: included because it is a shell string by definition.
SUBPROCESS_CALLS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "system"}
)

#: The one deliberate exception, keyed on the path relative to ``tests/``
#: rather than the bare filename, so a same-named file elsewhere in the
#: tree does not inherit the exemption. #292's regression test runs the
#: OLD machine-wide assertion against the same machine state as the new
#: group-scoped one, because running both is what proves they differ.
ALLOWED_PATHS = frozenset({"test_shutdown.py"})


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in SUBPROCESS_CALLS


def _command_literals(source: Path) -> Iterator[tuple[str, int]]:
    """String literals passed as arguments to a subprocess call."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_subprocess_call(node):
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            for inner in ast.walk(arg):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    yield inner.value, node.lineno


def _machine_wide_hits(source: Path, label: str | None = None) -> list[str]:
    """Subprocess literals in ``source`` naming a machine-wide search."""
    hits: list[str] = []
    rel = label or str(source.relative_to(TESTS_DIR))
    for value, lineno in _command_literals(source):
        for token in value.split():
            if Path(token).name in MACHINE_WIDE_PROCESS_TOOLS:
                hits.append(f"{rel}:{lineno} {value!r}")
                break
    return hits


def _scannable_sources() -> list[Path]:
    return [p for p in sorted(TESTS_DIR.rglob("*.py")) if p != Path(__file__).resolve()]


class TestNoTestSearchesTheWholeMachineForAProcess:
    def test_the_suite_is_clean(self) -> None:
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
    """The net's own reach, measured rather than asserted in a docstring.

    Every case here was a real miss in the first version.
    """

    def _hits(self, tmp_path: Path, body: str) -> list[str]:
        """Run the net over a probe file, written OUTSIDE the suite.

        Writing probes into ``tests/`` would leave one behind whenever a
        probe failed, and the next run would scan it.
        """
        source = tmp_path / "probe.py"
        source.write_text(body, encoding="utf-8")
        return _machine_wide_hits(source, label="probe.py")

    def test_a_bare_command_is_caught(self, tmp_path: Path) -> None:
        assert self._hits(tmp_path, 'subprocess.run(["pgrep", "-f", "x"])\n')

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

    def test_prose_naming_the_command_is_not_a_hit(self, tmp_path: Path) -> None:
        """The reason the net reads subprocess arguments and not every
        literal: the files explaining this rule have to name it."""
        assert not self._hits(tmp_path, '"""Do not use pgrep -f here."""\nx = "pgrep"\n')

    def test_a_command_hidden_in_a_variable_is_a_known_miss(self, tmp_path: Path) -> None:
        """Stated so the net is not trusted past its reach."""
        body = 'cmd = ["pgrep", "-f", "x"]\nsubprocess.run(cmd)\n'
        assert not self._hits(tmp_path, body)


class TestTheLivenessHelperCannotPassByMeasuringNothing:
    """The same defect one level down, in the helper written to remove it.

    ``group_has_live_member`` shells out to ``ps`` and walks stdout. If
    ``ps`` is missing, restricted or errors, stdout is empty, so the
    original returned False, ``wait_for_group_to_die`` returned True, and
    the orphan assertion passed having measured nothing. Measured with
    ``ps`` forced to rc=127: the helper reported the CALLER'S OWN live
    process group as dead.

    ``ps`` really is absent or filtered in the places this matters:
    ``hidepid`` mounts and minimal containers. So the helper now proves
    it can see something before it is allowed to report absence, and the
    thing it proves it can see is the caller's own process group, which
    is alive by construction.
    """

    def test_it_sees_its_own_process_group(self) -> None:
        """The positive control. Without this the guard below could be
        passing because everything raises."""
        assert procs.group_has_live_member(os.getpgrp()) is True

    def test_a_failing_ps_raises_instead_of_reporting_absence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["ps"], returncode=127, stdout="", stderr="ps: command not found"
            )

        monkeypatch.setattr(procs.subprocess, "run", broken)
        with pytest.raises(AssertionError, match="ps failed"):
            procs.group_has_live_member(os.getpgrp())

    def test_filtered_ps_output_raises_instead_of_reporting_absence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rc=0 but our own processes hidden, which is what a ``hidepid``
        mount looks like. Exit status alone is not enough."""

        def filtered(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["ps"], returncode=0, stdout="    1 Ss\n  517 Ss\n", stderr=""
            )

        monkeypatch.setattr(procs.subprocess, "run", filtered)
        with pytest.raises(AssertionError, match="own group"):
            procs.group_has_live_member(os.getpgrp())

    def test_wait_for_group_to_die_does_not_convert_that_into_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wrapper is what an orphan assertion actually calls, so the
        guard has to survive it rather than being swallowed by the poll
        loop."""

        def broken(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["ps"], returncode=127, stdout="", stderr="boom"
            )

        monkeypatch.setattr(procs.subprocess, "run", broken)
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
