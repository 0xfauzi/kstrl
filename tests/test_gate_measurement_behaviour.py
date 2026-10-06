"""What a ``[stack]`` check says it MEASURED, driven from the real runner.

``tests/test_check_result_measurement.py`` pins the inventory of rows;
``tests/test_check_result_measurement_behaviour.py`` drives the rest of the
checks. This file is the command gates, which since #696 slice 4 are all
``[stack]`` checks run by ``verify.check_stack_command``.

The rule under test is #696 decision 4(a): the exit status alone. 0 passes
and measured. Any other completed exit is a MEASURED failure. 126, 127, a
timeout and output that is not utf-8 fail UNMEASURED. kstrl reads none of the
output to decide, so a launcher that reports its own failure with a status of
its own (``uv run <missing>`` exits 2) reads as a measured failure: the loss
decision 4 accepted, pinned below so that it is stated rather than found.

Every command here is a real subprocess. The missing-tool cases need a name
that is on no PATH and the timeout case needs a real sleep; ``run_scrubbed``
signals the process group, so nothing outlives the assertion.
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
    check_stack_command,
    run_mechanical_verification,
)
from tests.helpers.measurement import assert_measured, assert_unmeasured
from tests.helpers.stack_confirmation import in_process_stack

#: A tool that is not on any PATH, and the two command shapes a check takes
#: it in, with the exit status each really produces and what that status
#: means under decision 4(a).
MISSING_TOOL = "kstrl-there-is-no-such-tool-227"
MISSING_BINARY = f"{MISSING_TOOL} check ."
MISSING_COMMANDS: dict[str, tuple[str, int, bool]] = {
    "a bare missing binary": (MISSING_BINARY, 127, False),
    "uv run with a missing tool": (f"uv run {MISSING_TOOL} check .", 2, True),
}

#: Long enough that a sub-second timeout always fires first.
SLOW_COMMAND = f"{sys.executable} -c 'import time; time.sleep(30)'"

TINY_TIMEOUT = 0.2

PASSING_COMMAND = f"{sys.executable} -c 'pass'"
FAILING_LINT_COMMAND = (
    f"""{sys.executable} -c 'import sys; print("x.py:1:1: E501 long"); sys.exit(1)'"""
)


def _run(cwd: Path, command: str, timeout: float) -> CheckResult:
    stack = in_process_stack({"lint": command})
    return check_stack_command(cwd, stack, "lint", command, timeout)


@pytest.mark.parametrize("shape", sorted(MISSING_COMMANDS))
def test_a_check_whose_tool_is_not_installed(shape: str, tmp_path: Path) -> None:
    """Both command shapes, and the exit code each really produces.

    The shell's 127 measured nothing. uv's own 2 is a completed exit, so it
    is a measured failure: the exit code is asserted so that a uv whose spawn
    failure stopped being 2 fails LOUDLY rather than quietly changing which
    half of the rule this shape exercises.
    """
    command, expected_code, measured = MISSING_COMMANDS[shape]
    if command.startswith("uv ") and shutil.which("uv") is None:
        pytest.skip("uv is not on PATH, so this shape cannot be measured here")

    row = _run(tmp_path, command, 60)

    assert row.passed is False
    assert row.name == "stack:lint"
    assert f"exited {expected_code}" in row.message
    if measured:
        assert_measured(row)
    else:
        assert_unmeasured(row)


def test_a_check_that_timed_out_measured_nothing(tmp_path: Path) -> None:
    row = _run(tmp_path, SLOW_COMMAND, TINY_TIMEOUT)

    assert "timed out" in row.message
    assert_unmeasured(row)


def test_a_check_that_ran_and_failed_did_measure(tmp_path: Path) -> None:
    """The control for the unmeasured cases above. Without it,
    ``measured=False`` on every failing check would pass them."""
    row = _run(tmp_path, FAILING_LINT_COMMAND, 30)

    assert row.passed is False
    assert_measured(row)


def test_a_check_that_passed_did_measure(tmp_path: Path) -> None:
    """A command that never started cannot exit 0, so exit 0 is measured."""
    assert_measured(_run(tmp_path, PASSING_COMMAND, 30))


# --- the verdict is a function of `passed` alone ---------------------------


def _verdict_config(lint_command: str) -> VerifyConfig:
    """Three gates, only the linter varying, and no diff-reading check.

    ``check_diff_scope`` and ``check_bad_patterns`` are off so the verdict is
    decided by the row under test and nothing else. ``tmp_path`` is not a git
    repository, and leaving them on would add rows whose own outcome would
    then be what the assertion measured.
    """
    return VerifyConfig(
        project_stack=in_process_stack(
            {"tests": PASSING_COMMAND, "typecheck": PASSING_COMMAND, "lint": lint_command}
        ),
        check_diff_scope=False,
        check_bad_patterns=False,
        subprocess_timeout=30.0,
    )


def _linter_row(result: VerificationResult) -> CheckResult:
    rows = [check for check in result.checks if check.name == "stack:lint"]
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
    """``docs/baseline.md`` prints the two rows this file drives.

    The doc has been wrong about this rule for two review rounds running: it
    stated the design when the code did something else, and then stated the
    code for a command shape this repository does not use. So its two rows are
    read back here and compared against the parametrisation that measures them,
    exit codes included. Whitespace is collapsed because the doc aligns its
    columns and the alignment is not the claim.
    """
    doc = (Path(__file__).resolve().parents[1] / "docs" / "baseline.md").read_text(encoding="utf-8")
    flattened = " ".join(doc.split())

    for command, code, measured in MISSING_COMMANDS.values():
        row = f"{command.replace(MISSING_TOOL, '<missing>')} -> exit {code}, measured={measured}"
        assert " ".join(row.split()) in flattened, (
            f"docs/baseline.md does not carry the measured row {row!r}. The doc "
            "and this test have to move together, or the doc goes back to "
            "stating a rule the code does not implement."
        )
