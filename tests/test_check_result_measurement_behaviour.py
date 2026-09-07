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

The three verify gates are in ``tests/test_gate_measurement_behaviour.py``,
which is a file rather than a section because they decide ``measured``
differently: every check here knows from its own control flow whether it looked
at anything, while a gate has to read its tool's output to find out.

The fixture-timeout cases use a real subprocess and a small timeout, which
costs about a second in total; ``run_scrubbed`` signals the process group, so
nothing outlives the assertion.
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
    NotMeasured,
    VerificationResult,
    check_bad_patterns,
    check_dead_code_ruff,
    check_diff_scope,
    check_policy_envelope,
    check_prd_stories,
    check_scope_unreadable,
    check_self_critique,
    check_test_adequacy,
)
from tests.helpers.measurement import assert_measured, assert_unmeasured

#: Long enough that a sub-second timeout always fires first.
SLOW_COMMAND = f"{sys.executable} -c 'import time; time.sleep(30)'"

TINY_TIMEOUT = 0.2


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


# --- diff-driven checks ---------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(root: Path) -> Path:
    """A real repository on ``main``, with one empty commit as the base.

    Real git, not a stub: what is under test is what the two diff-driven checks
    do with the file list git gives them, and a stub would be the test deciding
    that list.
    """
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "kstrl@example.com", cwd=root)
    _git("config", "user.name", "kstrl", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "base", cwd=root)
    return root


def test_diff_scope_with_no_allowed_paths_measured_nothing(tmp_path: Path) -> None:
    """``ks sense`` with no --allowed-path takes this branch every time, and
    ``diff_scope`` was in this repository's own committed baseline."""
    row = check_diff_scope(tmp_path, "main", allowed_paths=None)

    assert row.passed is True
    assert "No scope constraints" in row.message
    assert_unmeasured(row)


def test_the_two_diff_driven_checks_agree_on_an_empty_diff(tmp_path: Path) -> None:
    """One diff, two checks, one answer.

    Round 2 of review on #357 ran them side by side on an empty diff and they
    disagreed: ``diff_scope`` reported ``measured=True`` for "0 files, all
    within scope" while ``bad_patterns`` reported ``measured=False`` for
    "Scanned 0 Python files". Both apply a rule to nothing, so an adopter who
    sets --allowed-path had every ``diff_scope`` baseline signature CLEARED by
    a pull request whose diff touched none of the allowed globs.

    The MESSAGES are asserted equal too, not only the flag: the dampener turns
    a row's message into the reason a check is unmeasured, so two spellings of
    one fact reach the operator as two different holes.
    """
    repo = _repo(tmp_path)

    scope = check_diff_scope(repo, "main", allowed_paths=["src/**"])
    patterns = check_bad_patterns(repo, "main")

    assert scope.passed is True
    assert patterns.passed is True
    assert scope.message == patterns.message
    assert_unmeasured(scope)
    assert_unmeasured(patterns)


def test_a_deletion_only_commit_measures_scope_and_not_content(tmp_path: Path) -> None:
    """The two checks part company here, and this is why they are separate.

    ``diff_scope`` decides on the file NAMES git reported, and it has three of
    them, so it measured. ``bad_patterns`` has to OPEN each file, and every one
    is gone from the worktree, so it measured nothing: round 2 of review
    measured "Scanned 3 Python files, no issues" with ``measured=True`` from a
    check that opened none of them, and a deleted file cannot be shown to be
    free of secrets.
    """
    repo = _repo(tmp_path)
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text("value = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add three", cwd=repo)
    _git("checkout", "-q", "-b", "work", cwd=repo)
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).unlink()
    _git("commit", "-q", "-a", "-m", "delete three", cwd=repo)

    scope = check_diff_scope(repo, "main", allowed_paths=["*.py"])
    patterns = check_bad_patterns(repo, "main")

    assert_measured(scope)
    assert patterns.message == "Scanned 0 of 3 changed Python files, no issues"
    assert_unmeasured(patterns)


def test_bad_patterns_that_opened_a_file_did_measure(tmp_path: Path) -> None:
    """The control for the two above on the scanning side."""
    repo = _repo(tmp_path)
    _git("checkout", "-q", "-b", "work", cwd=repo)
    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add one", cwd=repo)

    row = check_bad_patterns(repo, "main")

    assert row.message == "Scanned 1 of 1 changed Python files, no issues"
    assert_measured(row)


def test_policy_envelope_that_could_not_read_the_diff_measured_nothing(tmp_path: Path) -> None:
    row = check_policy_envelope(tmp_path, "no-such-base-227", PolicyConfig(enabled=True))

    assert row.passed is False
    assert "could not read the diff" in row.message
    assert_unmeasured(row)


def test_test_adequacy_that_could_not_read_the_diff_measured_nothing(tmp_path: Path) -> None:
    row = check_test_adequacy(tmp_path, "no-such-base-227", AdequacyConfig(enabled=True))

    assert row.passed is False
    assert "could not read the diff" in row.message
    assert_unmeasured(row)


def test_a_scope_that_could_not_be_read_measured_nothing() -> None:
    """Marked rather than exempted.

    Its check name exists ONLY in this state, so exempting it would put the
    name in ``measured_checks`` on the one run that produces it, and its
    absence from the next run would then read as a sensor that went dark on a
    harness somebody had just repaired.
    """
    assert_unmeasured(check_scope_unreadable("the pre-run PRD could not be parsed"))


# --- PRD-driven checks ----------------------------------------------------


def test_prd_stories_that_could_not_load_the_prd_measured_nothing(tmp_path: Path) -> None:
    row = check_prd_stories(tmp_path / "absent.json")

    assert row.passed is False
    assert "Failed to load PRD" in row.message
    assert_unmeasured(row)


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

    assert_measured(check_prd_stories(prd))


def test_a_progress_file_that_could_not_be_read_measured_nothing(tmp_path: Path) -> None:
    row = check_self_critique(tmp_path / "absent.txt")

    assert "Could not read progress file" in row.message
    assert_unmeasured(row)


def test_a_progress_file_that_is_not_utf8_measured_nothing(tmp_path: Path) -> None:
    progress = tmp_path / "progress.txt"
    progress.write_bytes(b"## [2026-01-01] - [S1]\n- **Self-Critique:**\n  - \xff\xfe\n")

    row = check_self_critique(progress)

    assert "not valid UTF-8" in row.message
    assert_unmeasured(row)


def test_a_progress_file_with_no_self_critique_did_measure(tmp_path: Path) -> None:
    progress = tmp_path / "progress.txt"
    progress.write_text("## [2026-01-01] - [S1]\n- **Learnings:** none\n", encoding="utf-8")

    assert_measured(check_self_critique(progress))


# --- fixtures -------------------------------------------------------------


def test_a_run_with_no_fixtures_measured_nothing(tmp_path: Path) -> None:
    row = check_fixtures([], tmp_path, FixturesConfig())

    assert row.passed is True
    assert row.message == "No fixtures defined"
    assert_unmeasured(row)


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
    assert_unmeasured(row)


def test_a_fixture_that_ran_and_failed_did_measure(tmp_path: Path) -> None:
    fixtures = [
        Fixture(
            description="a fixture that fails honestly",
            fixture_type="cli",
            input_data={"command": f"{sys.executable} -c 'raise SystemExit(3)'"},
            expected={"exit_code": 0},
        ),
    ]

    assert_measured(check_fixtures(fixtures, tmp_path, FixturesConfig()))


def test_a_fixture_whose_process_could_not_be_launched_measured_nothing(
    tmp_path: Path,
) -> None:
    """The other half of the command fixture's environment failure.

    Deleting this site's ``measured=False`` left the suite green: the timeout
    branch beside it was driven and this one was not. A directory that is not
    there is the honest way in - ``run_scrubbed`` hands ``cwd`` to ``Popen``,
    which raises ``FileNotFoundError`` - and it is a real failure mode, since a
    worktree can be removed under a run.
    """
    fixtures = [
        Fixture(
            description="a fixture with nowhere to run",
            fixture_type="cli",
            input_data={"command": f"{sys.executable} -c 'pass'"},
            expected={"exit_code": 0},
        ),
    ]

    row = check_fixtures(fixtures, tmp_path / "gone", FixturesConfig())

    assert row.passed is False
    assert "Failed to run command" in "".join(row.details)
    assert_unmeasured(row)


def test_a_function_fixture_that_timed_out_measured_nothing(tmp_path: Path) -> None:
    """The function fixture runs its own subprocess and has its own two sites.

    Both were unpinned: the command fixture's timeout was driven and stood in
    for all four. ``time.sleep`` is the module and function, so the fixture
    needs no file on disk to import.
    """
    fixtures = [
        Fixture(
            description="a function that sleeps",
            fixture_type="function",
            input_data={"module": "time", "function": "sleep", "args": [30]},
            expected={"returns": None},
        ),
    ]

    row = check_fixtures(fixtures, tmp_path, FixturesConfig(timeout=TINY_TIMEOUT))

    assert row.passed is False
    assert "Function fixture timed out" in "".join(row.details)
    assert_unmeasured(row)


def test_a_function_fixture_that_could_not_be_launched_measured_nothing(
    tmp_path: Path,
) -> None:
    fixtures = [
        Fixture(
            description="a function fixture with nowhere to run",
            fixture_type="function",
            input_data={"module": "time", "function": "time", "args": []},
            expected={},
        ),
    ]

    row = check_fixtures(fixtures, tmp_path / "gone", FixturesConfig())

    assert row.passed is False
    assert "Failed to launch fixture subprocess" in "".join(row.details)
    assert_unmeasured(row)


def test_a_function_fixture_that_ran_and_failed_did_measure(tmp_path: Path) -> None:
    """The control for the three above, on the function path specifically.

    Without it, marking every function-fixture row unmeasured would pass them,
    and that mistake empties a baseline instead of filling it.
    """
    fixtures = [
        Fixture(
            description="a function whose answer is wrong",
            fixture_type="function",
            input_data={"module": "time", "function": "time", "args": []},
            expected={"returns": "not a timestamp"},
        ),
    ]

    assert_measured(check_fixtures(fixtures, tmp_path, FixturesConfig()))


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

    assert_measured(check_fixtures(fixtures, tmp_path, FixturesConfig()))


def test_a_prd_the_fixtures_check_could_not_read_measured_nothing(tmp_path: Path) -> None:
    row = check_fixtures_from_prd(tmp_path / "absent.json", tmp_path, FixturesConfig())

    assert row.passed is False
    assert "could not be read" in row.message
    assert_unmeasured(row)


def test_a_schema_invalid_prd_measured_no_fixtures(tmp_path: Path) -> None:
    prd = tmp_path / "prd.json"
    prd.write_text(json.dumps({"not": "a prd"}), encoding="utf-8")

    row = check_fixtures_from_prd(prd, tmp_path, FixturesConfig())

    assert row.passed is False
    assert "schema validation" in row.message
    assert_unmeasured(row)


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
