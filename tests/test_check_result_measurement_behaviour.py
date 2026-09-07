"""Each unmeasured row, driven from the REAL check, landing where it should.

``tests/test_check_result_measurement.py`` pins the inventory. This file
answers the other question: for every site that now says ``measured=False``,
does the real function actually take that branch, and does the dampener then
put the baseline's signature in ``unmeasured`` rather than ``fixed``?

Nothing here builds a ``CheckResult`` by hand. Every row comes out of the
function that ships it, because the defect this closes was six correct-looking
``measured=False`` lines that no test drove. Where a tool has to be absent or
present, PATH is what says so: a directory of two-line stubs, not a patched
``shutil.which``.

The gate-timeout cases use a real subprocess and a small timeout, which costs
about a second in total; ``run_scrubbed`` signals the process group, so nothing
outlives the assertion.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl import dampener
from kstrl.adequacy import AdequacyConfig
from kstrl.fixtures import Fixture, FixturesConfig, check_fixtures, check_fixtures_from_prd
from kstrl.policy import PolicyConfig
from kstrl.verify import (
    CheckResult,
    NotMeasured,
    VerificationResult,
    VerifyConfig,
    check_bad_patterns,
    check_dead_code_ruff,
    check_diff_scope,
    check_linter,
    check_policy_envelope,
    check_prd_stories,
    check_scope_unreadable,
    check_self_critique,
    check_test_adequacy,
    check_test_suite,
    check_typecheck,
    run_mechanical_verification,
)

#: A command that is not on any PATH. Runs through the shell, so a missing
#: binary is exit 127 rather than FileNotFoundError.
MISSING_BINARY = "kstrl-there-is-no-such-tool-227"

#: Long enough that a sub-second timeout always fires first.
SLOW_COMMAND = f"{sys.executable} -c 'import time; time.sleep(30)'"

TINY_TIMEOUT = 0.2

#: A gate command that succeeds, and one that fails having produced a
#: parseable finding. The second is the CONTROL for the first: a linter
#: that ran and reported something measured, and its row must say so.
PASSING_COMMAND = f"{sys.executable} -c 'pass'"
FAILING_LINT_COMMAND = (
    f"""{sys.executable} -c 'import sys; print("x.py:1:1: E501 long"); sys.exit(1)'"""
)


def _stub(directory: Path, name: str, body: str) -> None:
    """A real executable on PATH. The stubs stand in for tools, not for kstrl."""
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def only_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH containing exactly one directory, which starts empty.

    Environment, not a patched ``shutil.which``: the thing under test is what
    ``check_dead_code`` does when a tool is or is not installed, and an
    operator's machine says that through PATH.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    # git is stubbed rather than left off PATH: check_dead_code asks git for the
    # changed files before it looks for a detector, and a FileNotFoundError
    # there would end the test before the branch under test is reached. Exit 0
    # with no output is an empty diff, which is what these cases want.
    _stub(tools, "git", "exit 0")
    monkeypatch.setenv("PATH", str(tools))
    return tools


def _assert_unmeasured(row: CheckResult) -> None:
    """The row measured nothing, and the dampener treats it accordingly.

    Both halves, because either alone is satisfiable by a broken mechanism: a
    ``measured=False`` nothing reads is a comment, and a comparison that never
    fills ``fixed`` would pass the second assertion with the field deleted.

    The baseline is built to hold one signature this check produced earlier, so
    the question "did this get fixed?" is live. It must be answered no.
    """
    assert row.measured is False, f"{row.name} claims to have measured: {row.message!r}"

    signature = f"{row.name}:an-earlier-finding"
    current = dampener.baseline_from_result(
        VerificationResult(passed=row.passed, checks=[row], not_measured=[]),
        base_ref="0" * 40,
        project="proj",
        generated_at="2026-09-07T00:00:00Z",
        sense_schema_version=3,
        digest="d" * 16,
    )
    baseline = dampener.Baseline(
        generated_at="2026-09-06T00:00:00Z",
        base_ref="1" * 40,
        project="proj",
        passed=False,
        sense_schema_version=3,
        verify_digest="d" * 16,
        measured_checks=(row.name,),
        unmeasured_checks=(),
        unmeasured_reasons={},
        signatures={signature: 3},
    )

    comparison = dampener.compare(baseline, current)

    assert comparison.unmeasured == {signature: 3}
    assert comparison.fixed == {}
    assert comparison.new == {}
    # The flagging half of the same fact: the sensor went dark, so the verdict
    # is a regression rather than "no regression" and exit 0.
    assert row.name in comparison.stopped_measuring
    assert comparison.regressed is True
    # And the row contributes no signature of its own, which is what stops a
    # timeout message becoming a finding that later "gets fixed".
    assert current.signatures == {}


def _assert_measured(row: CheckResult) -> None:
    """The control: the same check, having actually measured something."""
    assert row.measured is True, f"{row.name} claims it measured nothing: {row.message!r}"


# --- the three gates ------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "name"),
    [
        (check_test_suite, "test_suite"),
        (check_typecheck, "typecheck"),
        (check_linter, "linter"),
    ],
)
def test_a_gate_whose_tool_is_not_installed_measured_nothing(
    check: object,
    name: str,
    tmp_path: Path,
) -> None:
    """Exit 127 from the shell means the command never started.

    Measured on the head of #357: ``check_linter`` against a nonexistent binary
    returned ``measured=True`` and the comparison reported
    ``fixed={'linter:E501': 12, 'linter:F401': 3}`` - uninstalling a linter read
    as fixing every one of its findings, which is precisely what
    ``docs/dampener.md`` claimed the rule prevented.
    """
    row = check(tmp_path, command=MISSING_BINARY, timeout=30)  # type: ignore[operator]

    assert row.passed is False
    assert "127" in row.message
    assert row.name == name
    _assert_unmeasured(row)


@pytest.mark.parametrize(
    "check",
    [check_test_suite, check_typecheck, check_linter],
)
def test_a_gate_that_timed_out_measured_nothing(check: object, tmp_path: Path) -> None:
    row = check(tmp_path, command=SLOW_COMMAND, timeout=TINY_TIMEOUT)  # type: ignore[operator]

    assert "timed out" in row.message
    _assert_unmeasured(row)


def test_a_gate_that_ran_and_failed_did_measure(tmp_path: Path) -> None:
    """The control for both tests above.

    Without it, ``measured=False`` on every failing gate would pass them, and
    that mistake empties a baseline instead of filling it.
    """
    _assert_measured(check_linter(tmp_path, command=FAILING_LINT_COMMAND, timeout=30))


# --- diff-driven checks ---------------------------------------------------


def test_diff_scope_with_no_allowed_paths_measured_nothing(tmp_path: Path) -> None:
    """``ks sense`` with no --allowed-path takes this branch every time, and
    ``diff_scope`` was in this repository's own committed baseline."""
    row = check_diff_scope(tmp_path, "main", allowed_paths=None)

    assert row.passed is True
    assert "No scope constraints" in row.message
    _assert_unmeasured(row)


def test_diff_scope_with_a_scope_to_apply_did_measure(tmp_path: Path) -> None:
    _assert_measured(check_diff_scope(tmp_path, "main", allowed_paths=["src/**"]))


def test_bad_patterns_having_scanned_nothing_measured_nothing(tmp_path: Path) -> None:
    """ "Scanned 0 Python files" cannot prove a secret went away."""
    row = check_bad_patterns(tmp_path, "main")

    assert row.message == "Scanned 0 Python files, no issues"
    _assert_unmeasured(row)


def test_policy_envelope_that_could_not_read_the_diff_measured_nothing(tmp_path: Path) -> None:
    row = check_policy_envelope(tmp_path, "no-such-base-227", PolicyConfig(enabled=True))

    assert row.passed is False
    assert "could not read the diff" in row.message
    _assert_unmeasured(row)


def test_test_adequacy_that_could_not_read_the_diff_measured_nothing(tmp_path: Path) -> None:
    row = check_test_adequacy(tmp_path, "no-such-base-227", AdequacyConfig(enabled=True))

    assert row.passed is False
    assert "could not read the diff" in row.message
    _assert_unmeasured(row)


def test_a_scope_that_could_not_be_read_measured_nothing() -> None:
    """Marked rather than exempted.

    Its check name exists ONLY in this state, so exempting it would put the
    name in ``measured_checks`` on the one run that produces it, and its
    absence from the next run would then read as a sensor that went dark on a
    harness somebody had just repaired.
    """
    _assert_unmeasured(check_scope_unreadable("the pre-run PRD could not be parsed"))


# --- PRD-driven checks ----------------------------------------------------


def test_prd_stories_that_could_not_load_the_prd_measured_nothing(tmp_path: Path) -> None:
    row = check_prd_stories(tmp_path / "absent.json")

    assert row.passed is False
    assert "Failed to load PRD" in row.message
    _assert_unmeasured(row)


def test_prd_stories_that_read_the_prd_did_measure(tmp_path: Path) -> None:
    prd = tmp_path / "prd.json"
    prd.write_text(
        json.dumps(
            {
                "branchName": "feat/x",
                "userStories": [
                    {
                        "id": "S1",
                        "title": "t",
                        "acceptanceCriteria": ["a"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    _assert_measured(check_prd_stories(prd))


def test_a_progress_file_that_could_not_be_read_measured_nothing(tmp_path: Path) -> None:
    row = check_self_critique(tmp_path / "absent.txt")

    assert "Could not read progress file" in row.message
    _assert_unmeasured(row)


def test_a_progress_file_that_is_not_utf8_measured_nothing(tmp_path: Path) -> None:
    progress = tmp_path / "progress.txt"
    progress.write_bytes(b"## [2026-01-01] - [S1]\n- **Self-Critique:**\n  - \xff\xfe\n")

    row = check_self_critique(progress)

    assert "not valid UTF-8" in row.message
    _assert_unmeasured(row)


def test_a_progress_file_with_no_self_critique_did_measure(tmp_path: Path) -> None:
    progress = tmp_path / "progress.txt"
    progress.write_text("## [2026-01-01] - [S1]\n- **Learnings:** none\n", encoding="utf-8")

    _assert_measured(check_self_critique(progress))


# --- fixtures -------------------------------------------------------------


def test_a_run_with_no_fixtures_measured_nothing(tmp_path: Path) -> None:
    row = check_fixtures([], tmp_path, FixturesConfig())

    assert row.passed is True
    assert row.message == "No fixtures defined"
    _assert_unmeasured(row)


def test_one_fixture_that_timed_out_makes_the_whole_row_unmeasured(tmp_path: Path) -> None:
    """``all``, not ``any``: this field gates the clearing side.

    A run in which one fixture timed out cannot prove that a different
    baseline signature went away, so the aggregate row is the narrow one.
    """
    fixtures = [
        Fixture(
            description="a fixture that hangs",
            fixture_type="cli",
            input_data={"command": SLOW_COMMAND},
            expected={"exit_code": 0},
        ),
    ]

    row = check_fixtures(fixtures, tmp_path, FixturesConfig(timeout=TINY_TIMEOUT))

    assert row.passed is False
    assert "timed out" in "".join(row.details)
    _assert_unmeasured(row)


def test_a_fixture_that_ran_and_failed_did_measure(tmp_path: Path) -> None:
    fixtures = [
        Fixture(
            description="a fixture that fails honestly",
            fixture_type="cli",
            input_data={"command": f"{sys.executable} -c 'raise SystemExit(3)'"},
            expected={"exit_code": 0},
        ),
    ]

    _assert_measured(check_fixtures(fixtures, tmp_path, FixturesConfig()))


def test_a_malformed_fixture_definition_still_counts_as_measured(tmp_path: Path) -> None:
    """The line the sweep draws, pinned from the other side.

    A definition error is a stable property of the PRD and its disappearance is
    a real fix, so it keeps the default. ``measured=False`` marks the
    environment failing, not the artifact.
    """
    fixtures = [
        Fixture(
            description="no command at all",
            fixture_type="cli",
            input_data={},
            expected={"exit_code": 0},
        ),
    ]

    _assert_measured(check_fixtures(fixtures, tmp_path, FixturesConfig()))


def test_a_prd_the_fixtures_check_could_not_read_measured_nothing(tmp_path: Path) -> None:
    row = check_fixtures_from_prd(tmp_path / "absent.json", tmp_path, FixturesConfig())

    assert row.passed is False
    assert "could not be read" in row.message
    _assert_unmeasured(row)


def test_a_schema_invalid_prd_measured_no_fixtures(tmp_path: Path) -> None:
    prd = tmp_path / "prd.json"
    prd.write_text(json.dumps({"not": "a prd"}), encoding="utf-8")

    row = check_fixtures_from_prd(prd, tmp_path, FixturesConfig())

    assert row.passed is False
    assert "schema validation" in row.message
    _assert_unmeasured(row)


# --- the other spelling of the same rule ---------------------------------


def test_a_not_measured_gap_lands_where_an_unmeasured_row_does(
    tmp_path: Path, only_path: Path
) -> None:
    """A gap and a ``measured=False`` row are the same fact in two shapes.

    #335 split the dead-code check so that every way it can measure nothing
    returns a :class:`NotMeasured` gap rather than a passing row, which is why
    nothing in that function carries ``measured=`` at all. The dampener has to
    read both through one path, or half the mechanism is missing depending on
    which check produced it.

    Driven from the real function with ruff off PATH, so the gap is the
    tool-missing one rather than a constructed object.
    """
    outcome = check_dead_code_ruff(tmp_path, 30.0, read_only=True)

    assert isinstance(outcome, NotMeasured)
    assert outcome.reason == "tool_missing"

    signature = f"{outcome.check}:an-earlier-finding"
    current = dampener.baseline_from_result(
        VerificationResult(passed=True, checks=[], not_measured=[outcome]),
        base_ref="0" * 40,
        project="owner/repo",
        generated_at="2026-09-07T00:00:00Z",
        sense_schema_version=3,
        digest="d" * 16,
    )
    baseline = dampener.Baseline(
        generated_at="2026-09-06T00:00:00Z",
        base_ref="1" * 40,
        project="owner/repo",
        passed=False,
        sense_schema_version=3,
        verify_digest="d" * 16,
        measured_checks=(outcome.check,),
        unmeasured_checks=(),
        unmeasured_reasons={},
        signatures={signature: 2},
    )

    comparison = dampener.compare(baseline, current)

    assert comparison.fixed == {}
    assert comparison.unmeasured == {signature: 2}
    assert outcome.check in comparison.stopped_measuring
    assert comparison.regressed is True


def test_the_stubs_are_real_executables(only_path: Path) -> None:
    """The control for the PATH-driven test above.

    A stub that is not executable makes ``shutil.which`` return None, which is
    the same answer as "not installed" - so the test would pass for the wrong
    reason and assert nothing at all.
    """
    _stub(only_path, "ruff", 'echo "Found 3 errors."')

    completed = subprocess.run(
        ["ruff"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(only_path)},
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "Found 3 errors."


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
