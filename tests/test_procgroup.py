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
from pathlib import Path

import pytest

from kstrl.procgroup import (
    PS_ARGV,
    PS_TIMEOUT_SECONDS,
    GroupLiveness,
    _count_group_rows,
    _kernel_says_group_is_empty,
    read_group_liveness,
    signal_probe_alive,
)
from tests.helpers import procs

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestTheScanReadsStatesNotJustGroups:
    """``_count_group_rows`` returns two ints positionally, so both are pinned."""

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
        rows, running = _count_group_rows(f"7 {state}\n9 Ss\n", pgid=7)
        assert rows == 1, "the row must be counted whatever its state"
        assert bool(running) is counts_as_running

    def test_the_two_counts_are_not_transposed(self) -> None:
        """Both fields are int, so a swap type-checks. Only a case with
        the two answers DIFFERENT can catch it."""
        assert _count_group_rows("7 Z\n", pgid=7) == (1, 0)
        assert _count_group_rows("7 Ss\n7 Z\n", pgid=7) == (2, 1)

    def test_a_ragged_row_is_skipped_without_dropping_its_neighbours(self) -> None:
        """A row with no state column would IndexError. The rows either
        side of it must still be read, or the skip is a silent
        truncation."""
        assert _count_group_rows("\n  7\n9 Ss\n7 Ss\n", pgid=7) == (1, 1)

    def test_a_group_id_is_matched_whole_not_as_a_prefix(self) -> None:
        """#292 in miniature: 7 must not match 70."""
        assert _count_group_rows("70 Ss\n9 Ss\n", pgid=7) == (0, 0)


class TestAGoneIsOnlyReportedWhenItIsEvidence:
    """The safety argument, which the own-group control did not carry.

    That control asked whether the caller's own group appeared in the
    listing. Measured on this tree: ``subprocess.run`` does not setpgid,
    so the ``ps`` child runs in the caller's group and ``ps -A`` always
    lists it - our own pgid appeared four times in every real listing,
    the last row being ``ps`` itself. It was satisfied by construction
    and could never fire, so a listing missing a live target was reported
    as a confident "gone". These pin what replaced it.
    """

    def test_a_listing_that_saw_only_zombies_is_evidence_of_gone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#298's case. ps demonstrably sees the group, so its verdict on
        that group's members is evidence and no kernel check is needed."""
        procs.fake_ps(monkeypatch, stdout="4242 Z\n4242 Z+\n")
        assert read_group_liveness(4242) == GroupLiveness(False)

    def test_a_missing_group_the_kernel_calls_empty_is_gone(self) -> None:
        """The other trustworthy route: the kernel, not the listing."""
        assert read_group_liveness(procs.dead_group()) == GroupLiveness(False)

    def test_a_missing_group_the_kernel_calls_occupied_is_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bug the own-group control could not catch.

        A live group absent from the listing - a descendant that changed
        uid, a restricted ps - used to read as a confident "gone", which
        makes `terminate_process_group` report reaped and `serve_cycle`
        requeue onto a repo a factory is still writing to (#186 F1).
        """
        # Our own group is alive by construction, and the listing omits it.
        procs.fake_ps(monkeypatch, stdout="1 Ss\n")
        liveness = read_group_liveness(os.getpgrp())
        assert liveness.live is None
        assert "kernel reports" in liveness.reason
        assert "not empty" in liveness.reason

    def test_a_running_row_still_reads_live(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, stdout="4242 Ss\n")
        assert read_group_liveness(4242) == GroupLiveness(True)


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
    """The timeout is the only thing stopping a wedged ps hanging the daemon.

    Deleting ``timeout=PS_TIMEOUT_SECONDS`` from ``read_group_liveness``
    left the whole suite green before this class existed: the wedged-ps
    case raises a pre-built TimeoutExpired from the fake whether or not
    the kwarg was ever passed, so it could not detect the loss. A ps
    stalled on a D-state read (NFS, a hung container runtime) would then
    block ``subprocess.run`` forever inside the daemon's reap path while
    PS_TIMEOUT_SECONDS still read as a tested constant.
    """

    def test_the_read_passes_its_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = procs.fake_ps(monkeypatch, stdout="4242 Ss\n")
        read_group_liveness(4242)
        assert calls, "the fake must have intercepted the ps call"
        assert calls[0]["kwargs"]["timeout"] == PS_TIMEOUT_SECONDS

    def test_the_read_pins_its_encoding(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Left to the locale, a stray byte under LC_ALL=C raises a
        ValueError out of a function whose contract is not to raise."""
        calls = procs.fake_ps(monkeypatch, stdout="4242 Ss\n")
        read_group_liveness(4242)
        assert calls[0]["kwargs"]["encoding"] == "utf-8"
        assert calls[0]["kwargs"]["errors"] == "replace"


class TestTheFakeDoesNotAnswerForEveryCommand:
    """`fake_ps` replaces the STDLIB `subprocess.run`, not procgroup's.

    `procgroup.subprocess` is the stdlib module object, so a setattr on
    it is process-wide. Measured before the delegation guard existed: a
    plain `subprocess.run(["git", "rev-parse", "HEAD"])` under
    `fake_ps(stdout="1 Ss\\n")` returned that stdout and
    `args=['ps','-A','-o','pgid=,stat=']`. A test that combined the
    helper with any other subprocess call would have measured nothing and
    passed, which is the #292 class the helper exists to prevent.
    """

    def test_another_command_reaches_the_real_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs.fake_ps(monkeypatch, stdout="4242 Ss\n")
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
        procs.fake_ps(monkeypatch, stdout="4242 Ss\n")
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

    def test_any_other_oserror_reads_as_gone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pinned rather than endorsed. This is the pre-#298 mapping,
        carried over unchanged: "gone" is the UNSAFE direction here,
        because `terminate_process_group` turns it into "reaped" and the
        item is released for another attempt. #298 shrank its reach from
        every call to only the calls where `ps` also gave no answer, and
        changing the mapping is a separate decision with its own
        reasoning."""

        def broken(pgid: int, sig: int) -> None:
            raise OSError(22, "Invalid argument")

        monkeypatch.setattr("kstrl.procgroup.os.killpg", broken)
        assert signal_probe_alive(4242) is False


# ---------------------------------------------------------------------------
# The centralisation has a mechanism, not just a docstring.
# ---------------------------------------------------------------------------

#: Callables whose string arguments are a command line.
_SUBPROCESS_CALLS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "system"}
)

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
    roots = (REPO_ROOT / "kstrl", REPO_ROOT / "tests")
    return [p for root in roots for p in sorted(root.rglob("*.py"))]


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

    @pytest.mark.parametrize(
        "body",
        [
            'subprocess.run(["ps", "-A"])',
            'subprocess.run(["/bin/ps", "-eo", "pid="])',
            'subprocess.Popen("ps -A", shell=True)',
            'sp.check_output(("ps", "-A"))',
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
