"""What the three verify gates say they MEASURED, driven from the real checks.

``tests/test_check_result_measurement.py`` pins the inventory of rows;
``tests/test_check_result_measurement_behaviour.py`` drives the rest of the
checks. This file is the three gates, which are their own subject because
``measured`` is decided differently there: every other check knows from its own
control flow whether it looked at anything, while a gate has to read its tool's
output to find out.

The rule under test. On a nonzero exit, ``measured`` is POSITIVE evidence -
some parser for that gate recognised its own tool reporting a failure. The
status alone cannot say: round 1 of #357 read it as ``returncode not in {126,
127}``, which is the POSIX shell's vocabulary for a command word it could not
run, and the gate commands this repository resolves go through ``uv run``,
which spawns the child itself and reports its own exit 2. On a ZERO exit the
status settles it, because a command that never started cannot return 0.

Every command here is a real subprocess. The missing-tool cases need a name
that is on no PATH and the timeout cases need a real sleep, and both cost about
a second in total; ``run_scrubbed`` signals the process group, so nothing
outlives the assertion.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from kstrl.verify import (
    CheckResult,
    VerificationResult,
    VerifyConfig,
    check_linter,
    check_test_suite,
    check_typecheck,
    run_mechanical_verification,
)
from tests.helpers.measurement import assert_measured, assert_unmeasured

#: Long enough that a sub-second timeout always fires first.
SLOW_COMMAND = f"{sys.executable} -c 'import time; time.sleep(30)'"

TINY_TIMEOUT = 0.2


#: A tool that is not on any PATH, and the two command shapes a gate takes it
#: in. Both, because round 2 of review on #357 measured them and they do not
#: agree: ``run_scrubbed`` runs a string through the shell, so the bare word
#: comes back as the shell's 127, while ``resolve_verify_commands`` returns
#: ``uv run pytest`` / ``uv run mypy .`` / ``uv run ruff check .`` for this
#: repository and every project ``ks init`` scaffolds - and uv spawns the child
#: itself and reports its OWN status, which is 2. The rule that keyed on
#: {126, 127} therefore fired for the shape nobody runs and never for the shape
#: everybody runs.
MISSING_TOOL = "kstrl-there-is-no-such-tool-227"
MISSING_BINARY = f"{MISSING_TOOL} check ."
MISSING_COMMANDS: dict[str, tuple[str, int]] = {
    "a bare missing binary": (MISSING_BINARY, 127),
    "uv run with a missing tool": (f"uv run {MISSING_TOOL} check .", 2),
}

#: Long enough that a sub-second timeout always fires first.
SLOW_COMMAND = f"{sys.executable} -c 'import time; time.sleep(30)'"

TINY_TIMEOUT = 0.2

#: A gate command that succeeds, and three that fail having produced a finding
#: in the tool's own format - one per gate, because each gate dispatches to its
#: own parsers. These are the CONTROLS for the missing-tool cases: a tool that
#: ran and reported something measured, and its row must say so.
PASSING_COMMAND = f"{sys.executable} -c 'pass'"
FAILING_LINT_COMMAND = (
    f"""{sys.executable} -c 'import sys; print("x.py:1:1: E501 long"); sys.exit(1)'"""
)
_PRINT_TEST_FAILURE = (
    'print("FAILED t/test_a.py::test_b - AssertionError: no");'
    ' print("=========== 1 failed, 2 passed in 0.10s ===========")'
)
FAILING_TEST_COMMAND = f"""{sys.executable} -c 'import sys; {_PRINT_TEST_FAILURE}; sys.exit(1)'"""
FAILING_TYPECHECK_COMMAND = (
    f"""{sys.executable} -c 'import sys; print("x.py:1: error: Bad types [assignment]");"""
    """ print("Found 1 error in 1 file (checked 1 source file)"); sys.exit(1)'"""
)
GATE_FINDINGS: dict[str, str] = {
    "test_suite": FAILING_TEST_COMMAND,
    "typecheck": FAILING_TYPECHECK_COMMAND,
    "linter": FAILING_LINT_COMMAND,
}

#: A command that RAN, failed, and said nothing either parser understands. It
#: measured nothing either: a status is the launcher's, and only the tool's own
#: report is evidence that the tool looked at the artifact.
UNPARSEABLE_FAILURE_COMMAND = (
    f"""{sys.executable} -c 'import sys; sys.stderr.write("Traceback (most recent call last):"""
    """\n  boom\n"); sys.exit(1)'"""
)


# --- the three gates ------------------------------------------------------


GATES: list[tuple[object, str]] = [
    (check_test_suite, "test_suite"),
    (check_typecheck, "typecheck"),
    (check_linter, "linter"),
]


@pytest.mark.parametrize("shape", sorted(MISSING_COMMANDS))
@pytest.mark.parametrize(("check", "name"), GATES)
def test_a_gate_whose_tool_is_not_installed_measured_nothing(
    check: object,
    name: str,
    shape: str,
    tmp_path: Path,
) -> None:
    """Both command shapes, and the exit code each really produces.

    Measured on the head of #357: ``check_linter`` against a nonexistent binary
    returned ``measured=True`` and the comparison reported
    ``fixed={'linter:E501': 12, 'linter:F401': 3}`` - uninstalling a linter read
    as fixing every one of its findings, which is precisely what
    ``docs/dampener.md`` claimed the rule prevented.

    Round 1 fixed that by refusing exit 126 and 127, and round 2 of review
    measured what that is worth on the commands this repository ships: `uv run
    <missing>` is exit 2, so the bare-word case in this test was the only shape
    the rule could ever see and the defect was live for every real gate. The
    exit code is asserted here so that a uv whose spawn failure stopped being 2
    fails LOUDLY rather than quietly retiring half the parametrisation.
    """
    command, expected_code = MISSING_COMMANDS[shape]
    if command.startswith("uv ") and shutil.which("uv") is None:
        pytest.skip("uv is not on PATH, so this shape cannot be measured here")

    row = check(tmp_path, command=command, timeout=60)  # type: ignore[operator]

    assert row.passed is False
    assert f"exit code {expected_code}" in row.message
    assert row.name == name
    assert_unmeasured(row)


@pytest.mark.parametrize(("check", "name"), GATES)
def test_a_gate_whose_output_no_parser_understood_measured_nothing(
    check: object,
    name: str,
    tmp_path: Path,
) -> None:
    """The general rule the two shapes above are instances of.

    ``measured`` is POSITIVE evidence: the parser saw its own tool reporting a
    failure. Everything else - a traceback, an empty stream, a launcher's
    complaint - is a status, and a status belongs to whatever ran last.
    """
    row = check(tmp_path, command=UNPARSEABLE_FAILURE_COMMAND, timeout=30)  # type: ignore[operator]

    assert row.passed is False
    assert row.name == name
    assert_unmeasured(row)


#: A test gate that exited NONZERO having printed nothing but a PASSING
#: footer. This is the shape ``uv run pytest && npm test`` takes when pytest
#: passed and the half after it failed, and it is the case that separates
#: recognition from "a footer matched": both footers below DO match their
#: parser's summary pattern and fill ``raw_summary``, and neither is evidence
#: about whatever failed the gate. One per test parser, because the claim is
#: written into both ``parse_pytest_output`` and ``parse_vitest_output`` and a
#: rule that held only for pytest would pass a single-parser case.
_PRINT_PYTEST_PASS = 'print("=========== 5 passed in 0.10s ===========")'
_PRINT_VITEST_PASS = 'print(" Test Files  1 passed (1)"); print("      Tests  3 passed (3)")'
PASSING_FOOTER_COMMANDS: dict[str, tuple[str, str]] = {
    "pytest": (
        f"""{sys.executable} -c 'import sys; {_PRINT_PYTEST_PASS}; sys.exit(1)'""",
        "5 passed",
    ),
    "vitest": (
        f"""{sys.executable} -c 'import sys; {_PRINT_VITEST_PASS}; sys.exit(1)'""",
        "3 passed",
    ),
}


@pytest.mark.parametrize("parser", sorted(PASSING_FOOTER_COMMANDS))
def test_a_failing_gate_that_printed_only_a_passing_footer_measured_nothing(
    parser: str,
    tmp_path: Path,
) -> None:
    """A passing footer is not evidence, and it is the widening that survives.

    ``ParsedOutput.recognised`` says in its own comment that a PASSING footer
    deliberately does not set it. Round 2 measured what defended that claim:
    widening the pytest rule from a nonzero failure COUNT to
    ``bool(raw_summary)`` left the whole target set green, because every other
    case here either has a diagnostic to parse or prints nothing a summary
    pattern matches. So this is the only shape that separates the two rules,
    and without it the rule can be weakened back with nothing turning red.

    What that weakening costs, once: on ``uv run pytest && npm test`` with
    pytest green and npm red, the gate would call itself measured on the
    strength of the half that worked, and the dampener would then read every
    signature the failed half used to report as FIXED.

    The second assertion is the control that stops this passing for the wrong
    reason. The footer has to have been MATCHED - to be in ``raw_summary`` -
    or the case is measuring output no parser matched, and it would hold with
    the recognition rule deleted entirely.
    """
    command, footer = PASSING_FOOTER_COMMANDS[parser]

    row = check_test_suite(tmp_path, command=command, timeout=30)

    assert row.passed is False
    assert row.parsed is not None
    assert footer in row.parsed.raw_summary, (
        f"the {parser} footer never reached raw_summary, so this case is not "
        "the one it claims to be: it is testing output no parser matched"
    )
    assert_unmeasured(row)


@pytest.mark.parametrize(
    "check",
    [check_test_suite, check_typecheck, check_linter],
)
def test_a_gate_that_timed_out_measured_nothing(check: object, tmp_path: Path) -> None:
    row = check(tmp_path, command=SLOW_COMMAND, timeout=TINY_TIMEOUT)  # type: ignore[operator]

    assert "timed out" in row.message
    assert_unmeasured(row)


@pytest.mark.parametrize(("check", "name"), GATES)
def test_a_gate_that_ran_and_reported_findings_did_measure(
    check: object,
    name: str,
    tmp_path: Path,
) -> None:
    """The control for every unmeasured gate case above, one per gate.

    Without it, ``measured=False`` on every failing gate would pass them, and
    that mistake empties a baseline instead of filling it. Per gate rather than
    once, because each gate dispatches to its own parsers and a recognition
    rule that worked only for ruff would pass a single-gate control.
    """
    row = check(tmp_path, command=GATE_FINDINGS[name], timeout=30)  # type: ignore[operator]

    assert row.passed is False
    assert_measured(row)


#: A lint gate whose output only the SECONDARY parser understands: eslint's
#: footer, which it prints only when there are problems, and which ruff's
#: patterns do not match. No diagnostic line, so no parser finds a failure.
ESLINT_FOOTER_COMMAND = (
    f"""{sys.executable} -c 'import sys; print("\u2716 3 problems (2 errors, 1 warning)");"""
    """ sys.exit(1)'"""
)


def test_a_gate_whose_secondary_parser_understood_the_output_measured(
    tmp_path: Path,
) -> None:
    """Recognition is the gate's answer, not the primary parser's.

    On the auto path a gate runs every parser registered for it and returns the
    PRIMARY's result when none of them found a failure, because that result
    carries the raw tail. Recognition cannot travel that way: a footer the
    secondary understood would be dropped with the rest of its result, and a
    polyglot repository whose lint command runs eslint would have every clean
    linter row read as a sensor that stopped measuring.
    """
    row = check_linter(tmp_path, command=ESLINT_FOOTER_COMMAND, timeout=30)

    assert row.passed is False
    assert_measured(row)


def test_a_gate_that_passed_did_measure(tmp_path: Path) -> None:
    """Exit 0 is the other half of the rule, and it is decided differently.

    A command that never started cannot return 0: measured on this machine, the
    two shapes are uv's 2 and the shell's 127. So the status alone settles the
    passing case, while on a nonzero exit it settles nothing and the tool's own
    report is what says the gate measured. Recognition cannot serve here: it is
    a failure report by construction, so requiring it would mark every clean
    run unmeasured and put every sensor permanently in the dark.
    """
    assert_measured(check_linter(tmp_path, command=PASSING_COMMAND, timeout=30))


# --- the verdict is a function of `passed` alone ---------------------------


def _verdict_config(lint_command: str) -> VerifyConfig:
    """Three gates, only the linter varying, and no diff-reading check.

    ``check_diff_scope`` and ``check_bad_patterns`` are off so the verdict is
    decided by the row under test and nothing else. ``tmp_path`` is not a git
    repository, and leaving them on would add rows whose own outcome would
    then be what the assertion measured.
    """
    return VerifyConfig(
        test_command=PASSING_COMMAND,
        typecheck_command=PASSING_COMMAND,
        lint_command=lint_command,
        check_diff_scope=False,
        check_bad_patterns=False,
        subprocess_timeout=30.0,
    )


def _linter_row(result: VerificationResult) -> CheckResult:
    rows = [check for check in result.checks if check.name == "linter"]
    assert len(rows) == 1, [check.name for check in result.checks]
    return rows[0]


def test_an_unmeasured_gate_still_fails_the_run(tmp_path: Path) -> None:
    """``measured`` decides what a COMPARISON may call fixed, and nothing else.

    ``run_mechanical_verification`` computes ``passed = all(c.passed for c in
    checks)``. Adding ``if c.measured`` to that comprehension is the #227
    fail-open: this repository's own test suite times out at the default
    timeout, and a verdict that skipped unmeasured rows would report the
    timeout as a passing run.

    So: the same failing linter, once having measured nothing (its binary is
    missing) and once having measured something (it ran and printed a
    finding). The rows differ in ``measured`` and agree on ``passed``, and both
    runs fail.
    """
    unmeasured = run_mechanical_verification(
        tmp_path, None, "main", None, _verdict_config(MISSING_BINARY)
    )
    measured = run_mechanical_verification(
        tmp_path, None, "main", None, _verdict_config(FAILING_LINT_COMMAND)
    )

    assert _linter_row(unmeasured).measured is False
    assert _linter_row(measured).measured is True
    assert _linter_row(unmeasured).passed is False
    assert _linter_row(measured).passed is False

    assert unmeasured.passed is False
    assert measured.passed is False


def test_a_run_whose_gates_all_passed_still_passes(tmp_path: Path) -> None:
    """The control. Without it, a verdict wired to ``False`` would pass the
    test above, and that mistake fails every run in the factory."""
    result = run_mechanical_verification(
        tmp_path, None, "main", None, _verdict_config(PASSING_COMMAND)
    )

    assert [check.passed for check in result.checks] == [True, True, True]
    assert result.passed is True


def test_the_documented_missing_tool_rows_are_the_ones_measured() -> None:
    """``docs/dampener.md`` prints the two rows this file drives.

    The doc has been wrong about this rule for two review rounds running: it
    stated the design when the code did something else, and then stated the
    code for a command shape this repository does not use. So its two rows are
    read back here and compared against the parametrisation that measures them,
    exit codes included. Whitespace is collapsed because the doc aligns its
    columns and the alignment is not the claim.
    """
    doc = (Path(__file__).resolve().parents[1] / "docs" / "dampener.md").read_text(encoding="utf-8")
    flattened = " ".join(doc.split())

    for command, code in MISSING_COMMANDS.values():
        row = f"{command.replace(MISSING_TOOL, '<missing>')} -> exit {code}, measured=False"
        assert " ".join(row.split()) in flattened, (
            f"docs/dampener.md does not carry the measured row {row!r}. The doc "
            "and this test have to move together, or the doc goes back to "
            "stating a rule the code does not implement."
        )
