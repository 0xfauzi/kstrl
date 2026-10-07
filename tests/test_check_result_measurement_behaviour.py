"""Each unmeasured row, driven from the REAL check, landing where it should.

``tests/test_check_result_measurement.py`` pins the inventory. This file
answers the other question: for every site that now says ``measured=False``,
does the real function actually take that branch, and does the baseline then
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
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from kstrl import baseline
from kstrl.policy import PolicyConfig
from kstrl.verify import (
    LAYER0_NOT_MEASURED,
    VerificationResult,
    check_bad_patterns,
    check_diff_scope,
    check_policy_envelope,
    check_prd_stories,
    check_scope_unreadable,
    check_self_critique,
)
from tests.helpers import gitrepo
from tests.helpers.measurement import assert_measured, assert_unmeasured


def _stub(directory: Path, name: str, body: str) -> None:
    """A real executable on PATH. The stubs stand in for tools, not for kstrl."""
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def only_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH containing exactly one directory, holding a stub git.

    Environment, not a patched function: the thing under test is what a
    check does with what git says, and an operator's machine says that
    through PATH. Each test replaces the stub with the git answer it needs.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
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
    gitrepo.set_identity(root)
    _git("commit", "-q", "--allow-empty", "-m", "base", cwd=root)
    return root


def test_diff_scope_with_no_allowed_paths_measured_nothing(tmp_path: Path) -> None:
    """``ks check`` with no --allowed-path takes this branch every time, and
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

    The MESSAGES are asserted equal too, not only the flag: the baseline turns
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
    check that opened none of them. Since #696 slice 8 the check reads only
    added lines, and a deletion adds none.
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
    assert patterns.message == "secrets: the 3 changed files add no lines, nothing scanned"
    assert_unmeasured(patterns)


def test_bad_patterns_that_opened_a_file_did_measure(tmp_path: Path) -> None:
    """The control for the two above on the scanning side."""
    repo = _repo(tmp_path)
    _git("checkout", "-q", "-b", "work", cwd=repo)
    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add one", cwd=repo)

    row = check_bad_patterns(repo, "main")

    assert row.message == "secrets: scanned the lines added to 1 changed files, no issues"
    assert_measured(row)


def test_policy_envelope_that_could_not_read_the_diff_measured_nothing(tmp_path: Path) -> None:
    row = check_policy_envelope(tmp_path, "no-such-base-227", PolicyConfig(enabled=True))

    assert row.passed is False
    assert "could not read the diff" in row.message
    assert_unmeasured(row)


def test_policy_envelope_that_could_not_decode_the_diff_measured_nothing(
    tmp_path: Path, only_path: Path
) -> None:
    """#399 blocker 1b: ``get_diff_content`` succeeds (the header is ASCII
    text, backslash-octal and all) but ``evaluate_policy`` calls
    ``policy.parse_added_lines`` on that text, which unquotes the header
    path and can raise ``UnicodeDecodeError`` on bytes that are not valid
    utf-8 - a ``ValueError``, not a ``GitDiffError`` and not a
    ``PolicyConfigError``, so neither existing except clause caught it.
    A real executable on PATH stands in for git: what is under test is
    what the check does with a diff git itself could produce, and a
    stubbed diff is how that diff is built without a non-utf-8 file on
    disk (APFS refuses one; see the blocker report)."""
    _stub(
        only_path,
        "git",
        'if [ "$1" = "rev-parse" ]; then\n'
        "  exit 1\n"
        "fi\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "--name-status" ]; then\n'
        '    printf "M\\0scanned.py\\0"\n'
        "    exit 0\n"
        "  fi\n"
        '  if [ "$a" = "--numstat" ]; then\n'
        '    printf "1\\t1\\tscanned.py\\n"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "printf '%s\\n' '--- a/scanned.py' '+++ \"b/x\\351.py\"' "
        "'@@ -0,0 +1 @@' '+x = 1'\n"
        "exit 0\n",
    )

    row = check_policy_envelope(tmp_path, "main", PolicyConfig(enabled=True))

    assert row.passed is False
    assert any("codec can't decode" in detail for detail in row.details)
    assert [f.is_infrastructure_error for f in row.findings] == [True]
    assert_unmeasured(row)


def test_bad_patterns_that_could_not_read_the_diff_measured_nothing(
    tmp_path: Path, only_path: Path
) -> None:
    """``get_diff_names`` is LENIENT and ``get_diff_content`` raises, so the
    two can disagree: the file list arrives and the diff does not. A real
    executable on PATH rather than a patched function, because what is under
    test is what the check does when git fails, and git failing is something
    PATH can say."""
    _stub(
        only_path,
        "git",
        'for a in "$@"; do\n'
        '  if [ "$a" = "--name-status" ]; then\n'
        '    printf "M\\0scanned.py\\0"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        'echo "git diff exploded" >&2\n'
        "exit 128",
    )

    row = check_bad_patterns(tmp_path, "main")

    assert row.passed is False
    assert "could not read the diff" in row.message
    assert any("exploded" in detail for detail in row.details)
    assert [f.is_infrastructure_error for f in row.findings] == [True]
    assert_unmeasured(row)


def test_bad_patterns_that_could_not_scan_the_diff_measured_nothing(tmp_path: Path) -> None:
    """A secret pattern that will not compile raises ``PolicyConfigError``,
    which is a ``ValueError`` and not a ``git.GitDiffError``. This is the test
    that stops the refusal's ``except Exception`` being narrowed to the one
    exception family the stub above can raise. It used a latin-1 byte until
    #695, when the secret rules began reading the diff with ``as_stored=True``,
    which keeps such a byte rather than refusing it."""
    repo = _repo(tmp_path)
    (repo / "seed.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    _git("checkout", "-q", "-b", "work", cwd=repo)
    (repo / "seed.py").write_text("x = 2\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "change seed", cwd=repo)

    row = check_bad_patterns(repo, "main", ["("])

    assert row.passed is False
    assert "could not read the diff" in row.message
    assert [f.is_infrastructure_error for f in row.findings] == [True]
    assert_unmeasured(row)


def test_a_scope_that_could_not_be_read_measured_nothing() -> None:
    """Marked rather than exempted.

    Its check name exists ONLY in this state, so exempting it would put the
    name in ``measured_checks`` on the one run that produces it, and its
    absence from the next run would then read as a check that went dark on a
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


# --- the other spelling of the same rule ---------------------------------


def test_a_not_measured_gap_lands_where_an_unmeasured_row_does() -> None:
    """A gap and a ``measured=False`` row are the same fact in two shapes.

    The baseline has to read both through one path, or half the mechanism is
    missing depending on which check produced it. The gap is the one Phase 1
    ships (``LAYER0_NOT_MEASURED``, #696 decision 7), not a constructed one.
    Its check is one kstrl retired, so the comparison names it in ``retired``
    and never in ``stopped_measuring``: an operator cannot bring it back.
    """
    outcome = LAYER0_NOT_MEASURED

    signature = f"{outcome.check}:an-earlier-finding"
    current = baseline.baseline_from_result(
        VerificationResult(passed=True, checks=[], not_measured=[outcome]),
        base_ref="0" * 40,
        project="owner/repo",
        generated_at="2026-09-07T00:00:00Z",
        check_schema_version=3,
        digest="d" * 16,
    )
    stored = baseline.Baseline(
        generated_at="2026-09-06T00:00:00Z",
        base_ref="1" * 40,
        project="owner/repo",
        passed=False,
        check_schema_version=3,
        verify_digest="d" * 16,
        measured_checks=(outcome.check,),
        unmeasured_checks=(),
        unmeasured_reasons={},
        signatures={signature: 2},
    )

    comparison = baseline.compare(stored, current)

    assert comparison.fixed == {}
    assert comparison.unmeasured == {signature: 2}
    assert comparison.stopped_measuring == {}
    assert comparison.retired == (outcome.check,)


def test_the_stubs_are_real_executables(only_path: Path) -> None:
    """The control for the PATH-driven tests above.

    A stub that is not executable makes ``shutil.which`` return None, which is
    the same answer as "not installed" - so the test would pass for the wrong
    reason and assert nothing at all.
    """
    _stub(only_path, "probe", 'echo "Found 3 errors."')

    completed = subprocess.run(
        ["probe"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(only_path)},
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "Found 3 errors."
