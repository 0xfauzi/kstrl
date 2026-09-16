"""R8.5 Layer 1 (#152): patch coverage, advisory, no floor.

Fourteen tests. Twelve drive :func:`run_mechanical_verification` - the
real entry point Layer 0 uses too - over a real temp git repository and
assert on what the phase produced. Two are unit tests on pure functions;
each names the end-to-end test that covers the same property on a real
run, so a unit test is never the only evidence for a claim.

The shared fixture (``_base_repo`` + ``_feature_branch``) is a base
commit on ``main`` and a feature commit on ``feature`` that adds six
executable lines to ``mod.py``, three of them covered by a new test.
Measured numbers this fixture produces (critic-verified from a real git
repo and a real coverage JSON):

| number | value |
|---|---|
| patch coverage over changed non-test lines (CORRECT) | 3/6 = 50.0% |
| including the changed test file | 6/9 = 66.7% |
| ``files["mod.py"].summary.percent_covered`` | 62.5% |
| ``totals.percent_covered`` | 76.9% |
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from kstrl.adequacy import AdequacyConfig, added_line_numbers, measure_patch_coverage
from kstrl.verify import VerifyConfig, run_mechanical_verification, run_scrubbed
from tests.helpers import gitrepo

BASE_MOD = "def covered_before(n):\n    return n + 1\n"
BASE_TEST = (
    "from mod import covered_before\n\n\ndef test_before():\n    assert covered_before(1) == 2\n"
)
FEAT_MOD = (
    "def covered_before(n):\n    return n + 1\n\n\ndef added_covered(n):\n    return n * 2\n"
    "\n\ndef added_missing(n):\n    x = n - 1\n    y = x * 3\n    return y\n"
)
FEAT_TEST = (
    "from mod import added_covered, covered_before\n\n\ndef test_before():\n"
    "    assert covered_before(1) == 2\n\n\ndef test_added():\n    assert added_covered(3) == 6\n"
)

# The BASE commit's conftest.py, never changed afterwards so it never
# enters the diff and never becomes a coverage target. Two jobs only:
# `runs.txt` counts how many times the project's test command actually
# ran (appended on every run, coverage or not); the `slow.txt` branch is
# how the timeout test makes only the SECOND (coverage) run hang, and it
# is also a live check that the coverage command really carries `--cov=`.
CONFTEST = r"""import sys
import time
from pathlib import Path


def pytest_sessionstart(session):
    with Path(__file__).parent.joinpath("runs.txt").open("a", encoding="utf-8") as fh:
        fh.write("run\n")
    if any(a.startswith("--cov=") for a in sys.argv):
        time.sleep(
            int(Path(__file__).parent.joinpath("slow.txt").read_text())
            if Path(__file__).parent.joinpath("slow.txt").exists()
            else 0
        )
"""


def _base_repo(root: Path) -> None:
    gitrepo.git_in(root, "init", "-b", "main")
    gitrepo.set_identity(root)
    (root / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (root / "mod.py").write_text(BASE_MOD, encoding="utf-8")
    (root / "test_mod.py").write_text(BASE_TEST, encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-m", "base")


def _feature_branch(root: Path) -> None:
    """Half-covered feature commit: the shared fixture the table above describes."""
    gitrepo.git_in(root, "checkout", "-b", "feature")
    (root / "mod.py").write_text(FEAT_MOD, encoding="utf-8")
    (root / "test_mod.py").write_text(FEAT_TEST, encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-m", "half covered")


def _repo(root: Path) -> None:
    _base_repo(root)
    _feature_branch(root)


def _comment_only_branch(root: Path) -> None:
    """A branch whose only change to mod.py is two appended comment lines."""
    gitrepo.git_in(root, "checkout", "-b", "comment-only")
    (root / "mod.py").write_text(BASE_MOD + "# a comment\n# another comment\n", encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-m", "comments only")


def _test_only_branch(root: Path) -> None:
    """A branch that adds a test function; mod.py is untouched."""
    gitrepo.git_in(root, "checkout", "-b", "test-only")
    extra = BASE_TEST + "\n\ndef test_extra():\n    assert True\n"
    (root / "test_mod.py").write_text(extra, encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-m", "test only")


def _failing_feature_branch(root: Path) -> None:
    """The half-covered feature, plus one added test that fails outright."""
    gitrepo.git_in(root, "checkout", "-b", "feature-fail")
    (root / "mod.py").write_text(FEAT_MOD, encoding="utf-8")
    failing = FEAT_TEST + "\n\ndef test_false():\n    assert False\n"
    (root / "test_mod.py").write_text(failing, encoding="utf-8")
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-m", "half covered, failing")


def _run(
    root: Path,
    *,
    base_branch: str = "main",
    test_command: str | None = None,
    subprocess_timeout: float = 120.0,
    enabled: bool = True,
    patch_coverage: bool = True,
):
    return run_mechanical_verification(
        root,
        None,
        base_branch,
        None,
        VerifyConfig(
            test_command=test_command or f"{shlex.quote(sys.executable)} -m pytest",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=subprocess_timeout,
        ),
        adequacy_config=AdequacyConfig(enabled=enabled, patch_coverage=patch_coverage),
    )


def _runs_count(root: Path) -> int:
    return (root / "runs.txt").read_text(encoding="utf-8").count("run")


# ---------------------------------------------------------------------------
# End to end, through run_mechanical_verification
# ---------------------------------------------------------------------------
def test_the_verify_phase_records_the_patch_coverage_finding(tmp_path: Path) -> None:
    _repo(tmp_path)
    result = _run(tmp_path)

    row = next(c for c in result.checks if c.name == "patch_coverage")
    assert row.passed is True
    assert row.measured is True
    assert "50.0%" in row.message
    findings = [f for f in row.findings if f.category == "adequacy_patch_coverage"]
    assert len(findings) == 1
    assert findings[0].severity == "advisory"
    assert findings[0].phase == "adequacy"
    assert "50.0%" in findings[0].explanation
    # the exact expression kstrl/pipeline.py:2797 uses to lift check
    # findings into the component's stream, so this asserts the finding
    # lands where Layer 0's lands rather than only on the row.
    lifted = [f for c in result.checks for f in c.findings]
    assert findings[0] in lifted
    assert result.passed is True  # advisory never fails the run
    assert [g for g in result.not_measured if g.check == "patch_coverage"] == []
    assert not (tmp_path / ".coverage").exists()  # D3: nothing written into the tree


def test_the_number_counts_only_changed_lines_in_non_test_files(tmp_path: Path) -> None:
    _repo(tmp_path)
    result = _run(tmp_path)
    row = next(c for c in result.checks if c.name == "patch_coverage")

    assert "3/6" in row.message
    assert "mod.py" in row.message or "mod.py" in "\n".join(row.details)
    assert "test_mod.py" not in row.message
    assert "test_mod.py" not in "\n".join(row.details)
    for wrong in ("66.7", "62.5", "76.9"):
        assert wrong not in row.message, f"denominator is wrong: {row.message}"


def test_a_non_pytest_test_command_is_a_sidecar_not_a_row(tmp_path: Path) -> None:
    _repo(tmp_path)
    result = _run(tmp_path, test_command="true")

    assert [c for c in result.checks if c.name == "patch_coverage"] == []
    gaps = [g for g in result.not_measured if g.check == "patch_coverage"]
    assert len(gaps) == 1
    assert gaps[0].reason == "tool_missing"
    assert gaps[0].as_token() == "patch_coverage:tool_missing"
    assert [
        f for c in result.checks for f in c.findings if f.category == "adequacy_patch_coverage"
    ] == []


def test_off_by_default_runs_the_test_command_once(tmp_path: Path) -> None:
    off_root = tmp_path / "off"
    off_root.mkdir()
    _repo(off_root)
    result = _run(off_root, patch_coverage=False)

    assert _runs_count(off_root) == 1
    assert [c for c in result.checks if c.name == "patch_coverage"] == []
    assert [g for g in result.not_measured if g.check == "patch_coverage"] == []

    # Mirror case: [adequacy] enabled=False with patch_coverage=True also
    # runs once and records nothing. Both switches are required - an
    # implementation reading only `patch_coverage` and ignoring `enabled`
    # would pass every other test in this file.
    disabled_root = tmp_path / "disabled"
    disabled_root.mkdir()
    _repo(disabled_root)
    result2 = _run(disabled_root, enabled=False, patch_coverage=True)

    assert _runs_count(disabled_root) == 1
    assert [c for c in result2.checks if c.name == "patch_coverage"] == []
    assert [g for g in result2.not_measured if g.check == "patch_coverage"] == []


def test_turning_it_on_runs_the_test_command_twice(tmp_path: Path) -> None:
    _repo(tmp_path)
    _run(tmp_path, patch_coverage=True)
    assert _runs_count(tmp_path) == 2


def test_a_comment_only_change_is_no_target_not_a_hundred_percent(tmp_path: Path) -> None:
    """Two paths lead to `no_target`; this is the AFTER-the-run one. The
    comment lines are still added lines to a non-test .py file, so
    `coverage_targets` is non-empty, the pre-flight does not fire, the
    coverage run happens (runs.txt == 2), and `no_target` is reached
    through `coverage.total == 0` once the report is read (D5)."""
    _base_repo(tmp_path)
    _comment_only_branch(tmp_path)
    result = _run(tmp_path, base_branch="main")

    assert [c for c in result.checks if c.name == "patch_coverage"] == []
    gaps = [g for g in result.not_measured if g.check == "patch_coverage"]
    assert len(gaps) == 1 and gaps[0].reason == "no_target"
    assert _runs_count(tmp_path) == 2


def test_a_diff_with_no_non_test_python_file_costs_no_second_run(tmp_path: Path) -> None:
    """The pre-flight `if not targets` path: `is_test_path` filters
    test_mod.py out, `coverage_targets` is empty, and the check returns
    BEFORE spending a second full test run (runs.txt == 1)."""
    _base_repo(tmp_path)
    _test_only_branch(tmp_path)
    result = _run(tmp_path, base_branch="main")

    assert [c for c in result.checks if c.name == "patch_coverage"] == []
    gaps = [g for g in result.not_measured if g.check == "patch_coverage"]
    assert len(gaps) == 1 and gaps[0].reason == "no_target"
    assert _runs_count(tmp_path) == 1


def test_a_failing_suite_is_a_sidecar_not_a_number(tmp_path: Path) -> None:
    _base_repo(tmp_path)
    _failing_feature_branch(tmp_path)
    result = _run(tmp_path, base_branch="main")

    gaps = [g for g in result.not_measured if g.check == "patch_coverage"]
    assert len(gaps) == 1 and gaps[0].reason == "command_failed"
    assert [c for c in result.checks if c.name == "patch_coverage"] == []


def test_an_empty_report_is_not_a_hundred_percent() -> None:
    """Unit: a real coverage run cannot cheaply produce a report that omits
    a file the diff changed. The same property on a real run is
    test_a_comment_only_change_is_no_target_not_a_hundred_percent, which
    reaches `total == 0` through `run_mechanical_verification` and a real
    coverage JSON."""
    pc = measure_patch_coverage({"mod.py": {1, 2, 3}}, {"files": {}})
    assert (pc.covered, pc.total) == (0, 0)
    assert pc.unmeasured == ("mod.py",)
    with pytest.raises(ValueError):
        _ = pc.percent


def test_a_command_that_cannot_be_started_is_a_sidecar(tmp_path: Path) -> None:
    """Two things at once. It is the OSError test: the coverage command
    runs as a LIST with no shell, so a configured `PYTHONPATH=. pytest`
    splits to `["PYTHONPATH=.", "pytest"]` and `execvp` raises
    `FileNotFoundError`. It is ALSO the list-versus-string test: handed
    to `run_scrubbed` as a STRING, `/bin/sh` reads `PYTHONPATH=.` as an
    env assignment and the run SUCCEEDS, producing a row - which is what
    the plant for this test trips."""
    _repo(tmp_path)
    result = _run(tmp_path, test_command="PYTHONPATH=. pytest")

    gaps = [g for g in result.not_measured if g.check == "patch_coverage"]
    assert len(gaps) == 1 and gaps[0].reason == "command_failed"
    assert [c for c in result.checks if c.name == "patch_coverage"] == []


def test_added_line_numbers_is_context_size_agnostic() -> None:
    """Unit. Three cases, all confirmed against a reference implementation
    of the spec. The same parser on a real diff is
    test_the_verify_phase_records_the_patch_coverage_finding and
    test_a_comment_only_change_is_no_target_not_a_hundred_percent, whose
    50.0% is only correct if the parse of a real `git diff main...HEAD`
    is correct."""
    diff = (
        "diff --git a/mod.py b/mod.py\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,3 +1,5 @@\n"
        " def a():\n"
        "     return 1\n"
        "+\n"
        "+def b():\n"
        " # tail\n"
    )
    assert added_line_numbers(diff) == {"mod.py": {3, 4}}

    deleted = (
        "diff --git a/gone.py b/gone.py\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-line one\n"
        "-line two\n"
    )
    assert added_line_numbers(deleted) == {}

    # Added content lines that LOOK like file headers. "+++ b/evil.py" is
    # what a `line.startswith("+++ ")` test alone misreads as a header,
    # and only the `prev.startswith("--- ")` guard saves it; "++++
    # b/evil2.py" is what a `line.startswith("+++")` test without the
    # space misreads.
    header_lookalike = (
        "diff --git a/mod.py b/mod.py\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,1 +1,4 @@\n"
        " x = 1\n"
        "+++ b/evil.py\n"
        "++++ b/evil2.py\n"
        "+y = 2\n"
    )
    result = added_line_numbers(header_lookalike)
    assert result == {"mod.py": {2, 3, 4}}
    assert "evil.py" not in result
    assert "evil2.py" not in result

    removals = (
        "diff --git a/mod.py b/mod.py\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,5 +1,5 @@\n"
        " def a():\n"
        "-    x = 1\n"
        "-    y = 2\n"
        "+    x = 10\n"
        "+    y = 20\n"
        " # tail\n"
    )
    # a `-` line is old-side only, so it must not advance the new-side
    # counter; with it advancing this reads {4, 5}.
    assert added_line_numbers(removals) == {"mod.py": {2, 3}}


def test_a_hanging_coverage_run_is_a_timed_out_sidecar(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "slow.txt").write_text("30", encoding="utf-8")

    start = time.monotonic()
    result = _run(tmp_path, subprocess_timeout=5.0)
    elapsed = time.monotonic() - start
    # subprocess_timeout (5.0) + verify._SCRUB_TERM_GRACE_SECONDS (5.0) is
    # the real ceiling; 25 is slack, not a prediction. A missing timeout
    # handler does not fail an assertion, it makes the assertion never
    # run, and an unbounded test cannot tell "caught" from "still going".
    assert elapsed < 25, f"took {elapsed:.1f}s; the timeout handler did not bound the run"

    gaps = [g for g in result.not_measured if g.check == "patch_coverage"]
    assert len(gaps) == 1 and gaps[0].reason == "timed_out"
    assert gaps[0].as_token() == "patch_coverage:timed_out"
    assert [c for c in result.checks if c.name == "patch_coverage"] == []


def test_a_shell_operator_in_the_test_command_is_refused_before_any_run(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    result = _run(
        tmp_path,
        test_command=f"{shlex.quote(sys.executable)} -m pytest && echo done",
    )

    gaps = [g for g in result.not_measured if g.check == "patch_coverage"]
    assert len(gaps) == 1 and gaps[0].reason == "tool_missing"
    assert [c for c in result.checks if c.name == "patch_coverage"] == []
    # refused BEFORE spending a run: only check_test_suite ran
    assert _runs_count(tmp_path) == 1


def test_extra_env_does_not_reopen_the_scrub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one test that answers for the only change to a shared,
    security-relevant function. Same shape as
    tests/test_hitl_env_scrub.py::test_subprocess_sees_no_api_keys, which
    this extends to the new `extra_env` argument."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    result = run_scrubbed(
        [sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
        cwd=tmp_path,
        timeout=30,
        extra_env={"COVERAGE_FILE": "/tmp/kstrl-test-coverage"},
    )
    child = json.loads(result.stdout)
    assert child["COVERAGE_FILE"] == "/tmp/kstrl-test-coverage"
    assert "ANTHROPIC_API_KEY" not in child
    assert not any("API_KEY" in name for name in child)
