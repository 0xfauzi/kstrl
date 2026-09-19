"""R8.5 Layer 1 (#152): patch coverage, advisory, no floor.

Most tests drive :func:`run_mechanical_verification` - the real entry
point Layer 0 uses too - over a real temp git repository and assert on
what the phase produced. A few are unit tests on pure functions; each
names the end-to-end test that covers the same property on a real run,
so a unit test is never the only evidence for a claim.

The shared fixture (:data:`BASE_FILES` + :data:`FEAT_FILES`, built
through ``tests.conftest.make_review_repo``) is a base commit on
``main`` and a feature commit on ``feature`` that adds six executable
lines to ``mod.py``, three of them covered by a new test. Measured
numbers this fixture produces (critic-verified from a real git repo and
a real coverage JSON):

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
from typing import TYPE_CHECKING

import pytest

from kstrl.adequacy import (
    AdequacyConfig,
    added_line_numbers,
    analyze_test_diff,
    measure_patch_coverage,
)
from kstrl.verify import VerifyConfig, run_mechanical_verification, run_scrubbed
from tests.conftest import make_review_repo

if TYPE_CHECKING:
    from kstrl.verify import NotMeasured, VerificationResult

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

#: Committed on ``main`` by every repo this file builds (#152 simplify
#: pass: the single home for what used to be six hand-rolled builders,
#: now one call to ``tests.conftest.make_review_repo`` per scenario).
BASE_FILES = {"conftest.py": CONFTEST, "mod.py": BASE_MOD, "test_mod.py": BASE_TEST}

#: The shared half-covered feature commit the module docstring's table
#: describes: three of six added lines in ``mod.py`` executed.
FEAT_FILES = {"mod.py": FEAT_MOD, "test_mod.py": FEAT_TEST}

#: A branch whose only change to ``mod.py`` is two appended comment
#: lines; ``test_mod.py`` is untouched (stays at ``BASE_TEST``).
COMMENT_ONLY_FILES = {"mod.py": BASE_MOD + "# a comment\n# another comment\n"}

#: A branch that adds a test function; ``mod.py`` is untouched.
TEST_ONLY_FILES = {"test_mod.py": BASE_TEST + "\n\ndef test_extra():\n    assert True\n"}

#: The half-covered feature, plus one added test that fails outright.
FAILING_FEATURE_FILES = {
    "mod.py": FEAT_MOD,
    "test_mod.py": FEAT_TEST + "\n\ndef test_false():\n    assert False\n",
}


def _repo(tmp_path: Path, files: dict[str, str] | None = None) -> None:
    """``BASE_FILES`` committed on ``main``, plus ``files`` (default
    :data:`FEAT_FILES`) committed on a ``feature`` branch, through
    ``make_review_repo`` - the branch name is never read by anything in
    this file, only ``base_branch="main"`` is. Operates on ``tmp_path``
    in place, the same calling convention the six retired builders used."""
    make_review_repo(
        tmp_path, base_files=BASE_FILES, files=files if files is not None else FEAT_FILES
    )


def _run(
    root: Path,
    *,
    base_branch: str = "main",
    test_command: str | None = None,
    subprocess_timeout: float = 120.0,
    enabled: bool = True,
    patch_coverage: bool = True,
) -> VerificationResult:
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


def _only_gap(result: VerificationResult, reason: str) -> NotMeasured:
    """The single ``patch_coverage`` gap in ``result``: no row alongside
    it, exactly one gap, and it carries ``reason``.

    #152 simplify pass: one helper for the seven "sidecar, not a row"
    tests in this file, which all asserted this same shape by hand."""
    assert [c for c in result.checks if c.name == "patch_coverage"] == []
    gaps = [g for g in result.not_measured if g.check == "patch_coverage"]
    assert len(gaps) == 1, gaps
    assert gaps[0].reason == reason, gaps[0]
    return gaps[0]


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

    gap = _only_gap(result, "tool_missing")
    assert gap.as_token() == "patch_coverage:tool_missing"
    assert [
        f for c in result.checks for f in c.findings if f.category == "adequacy_patch_coverage"
    ] == []


@pytest.mark.parametrize(
    ("enabled", "patch_coverage"),
    [(True, False), (False, True)],
    ids=["patch_coverage-off", "adequacy-disabled"],
)
def test_off_by_default_runs_the_test_command_once(
    tmp_path: Path, enabled: bool, patch_coverage: bool
) -> None:
    """Both switches are required: ``[adequacy] enabled`` (Layer 0's
    master switch) and ``[adequacy] patch_coverage`` (Layer 1's own
    opt-in on top of it). An implementation reading only one of the two
    would pass whichever case here it does not cover - the empty gap
    list is load-bearing: a check nobody asked for records NOTHING."""
    _repo(tmp_path)
    result = _run(tmp_path, enabled=enabled, patch_coverage=patch_coverage)

    assert _runs_count(tmp_path) == 1
    assert [c for c in result.checks if c.name == "patch_coverage"] == []
    assert [g for g in result.not_measured if g.check == "patch_coverage"] == []


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
    _repo(tmp_path, files=COMMENT_ONLY_FILES)
    result = _run(tmp_path)

    _only_gap(result, "no_target")
    assert _runs_count(tmp_path) == 2


def test_a_diff_with_no_non_test_python_file_costs_no_second_run(tmp_path: Path) -> None:
    """The pre-flight `if not targets` path: `is_test_path` filters
    test_mod.py out, `coverage_targets` is empty, and the check returns
    BEFORE spending a second full test run (runs.txt == 1)."""
    _repo(tmp_path, files=TEST_ONLY_FILES)
    result = _run(tmp_path)

    _only_gap(result, "no_target")
    assert _runs_count(tmp_path) == 1


def test_a_failing_suite_is_a_sidecar_not_a_number(tmp_path: Path) -> None:
    _repo(tmp_path, files=FAILING_FEATURE_FILES)
    result = _run(tmp_path)

    _only_gap(result, "command_failed")


def test_an_empty_report_is_not_a_hundred_percent() -> None:
    """Unit: a real coverage run cannot cheaply produce a report that omits
    a file the diff changed. The same property on a real run is
    test_a_comment_only_change_is_no_target_not_a_hundred_percent, which
    reaches `total == 0` through `run_mechanical_verification` and a real
    coverage JSON.

    #152 simplify pass dropped `PatchCoverage.percent`: its only caller
    (`verify.check_patch_coverage`) already returns `NotMeasured` before
    computing a percentage whenever `total == 0`, so the property's own
    raise was unreachable in practice. `covered_lines` and `files` both
    come back empty for the same reason: `mod.py` never made it past the
    `unmeasured` branch."""
    pc = measure_patch_coverage({"mod.py": {1, 2, 3}}, {"files": {}})
    assert (pc.covered, pc.total) == (0, 0)
    assert pc.unmeasured == ("mod.py",)
    assert pc.covered_lines == ()
    assert pc.files == ()


def test_covered_lines_is_what_files_is_derived_from() -> None:
    """#152 simplify pass: `PatchCoverage` gains `covered_lines`, the
    actual EXECUTED line numbers per measured target - Layer 2
    (diff-scoped mutation, not yet built) needs to know WHICH lines to
    mutate, not only how many - and `files`'s covered count for a path
    is `len(...)` of that same path's `covered_lines` entry, so the two
    cannot disagree: they come from one computation, not two."""
    report = {
        "files": {
            "mod.py": {"executed_lines": [1, 2, 5], "missing_lines": [3, 4]},
        }
    }
    pc = measure_patch_coverage({"mod.py": {1, 2, 3, 4, 5}}, report)
    assert pc.covered_lines == (("mod.py", frozenset({1, 2, 5})),)
    assert pc.files == (("mod.py", 3, 5),)
    assert pc.covered == 3
    assert pc.total == 5


def test_a_command_that_cannot_be_started_is_a_sidecar(tmp_path: Path) -> None:
    """The OSError test: the coverage command runs as a LIST with no
    shell, so a configured `PYTHONPATH=. pytest` splits to
    `["PYTHONPATH=.", "pytest"]` and `execvp` raises `FileNotFoundError`.

    It is NOT the list-versus-string test (blocker 1, PR #388 round 3 of
    independent verification, #152). It was believed to be: stringify
    EITHER coverage spawn alone and this test still passes, because the
    OTHER spawn still runs as a list and its own `FileNotFoundError`
    satisfies `command_failed` in its place - proved on the shipped head
    with `__pycache__` purged and `PYTHONDONTWRITEBYTECODE=1`: stringifying
    just `_coverage_data_command` leaves all 19 tests green, and so does
    stringifying just `_coverage_json_command`. Only mutating BOTH at
    once goes red, which this single test cannot tell apart from the
    property it names. `test_each_coverage_spawn_runs_as_a_list_never_a_string`
    is the one that actually pins list-versus-string, per spawn."""
    _repo(tmp_path)
    result = _run(tmp_path, test_command="PYTHONPATH=. pytest")

    _only_gap(result, "command_failed")


def test_each_coverage_spawn_runs_as_a_list_never_a_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 1 (PR #388, round 3 of independent verification, #152):
    the property `test_a_command_that_cannot_be_started_is_a_sidecar`'s
    docstring used to claim - that every coverage spawn runs as a list,
    never a shell string, so a file name out of an agent-authored diff
    can never reach `/bin/sh` - needs its own pin, because that test's
    single `PYTHONPATH=. pytest` plant is satisfied by either spawn's
    `FileNotFoundError` alone and cannot tell "both spawns are lists"
    from "at least one is".

    Wraps `run_scrubbed` to RECORD every `cmd` it is handed and delegate
    to the real function, so the coverage run still measures for real.
    `check_test_suite`, `check_typecheck` and `check_linter` also go
    through `run_scrubbed` in this same run, each with the OPERATOR'S
    own command handed through unchanged - legitimately a shell string,
    since a project's own multi-step command line (`pytest && ...`) is
    meant to reach a shell. Stringifying is only ever a defect on the
    two commands THIS check builds itself out of `targets`, so the pin
    filters to those two by CONTENT (`--cov=.` for the data spawn, the
    literal `"coverage"` token for the json spawn), not by call order -
    a stringified spawn drops out of the filter entirely (it is no
    longer a list), so the count assertion below is what catches it,
    directly, with no dependence on which `FileNotFoundError` happens to
    fire first.

    Proved red-then-green (`find . -name __pycache__ ... -exec rm -rf
    {} +`, `PYTHONDONTWRITEBYTECODE=1`, each with the other spawn
    reverted): stringifying `_coverage_data_command`'s return -
    `" ".join(shlex.quote(t) for t in [*tokens, "--cov=.",
    "--cov-report="])` in place of the list - drops `coverage_calls` to
    1 and this test fails; stringifying `_coverage_json_command`'s
    return the same way does too; the unmodified tree passes both."""
    _repo(tmp_path)
    recorded: list[str | list[str]] = []
    real_run_scrubbed = run_scrubbed

    def _recording_run_scrubbed(cmd: str | list[str], **kwargs: object) -> object:
        recorded.append(cmd)
        return real_run_scrubbed(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("kstrl.verify.run_scrubbed", _recording_run_scrubbed)
    _run(tmp_path)

    coverage_calls = [
        c for c in recorded if isinstance(c, list) and ("--cov=." in c or "coverage" in c)
    ]
    assert len(coverage_calls) >= 2, recorded
    assert all(isinstance(c, list) for c in coverage_calls)


def test_added_line_numbers_is_context_size_agnostic() -> None:
    """Unit. Four cases, all confirmed against a reference implementation
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


def test_a_header_lookalike_added_line_is_not_read_as_a_new_file() -> None:
    """Unit, on `analyze_test_diff`. #152 simplify pass: it and
    `added_line_numbers` share `_iter_diff_lines`'s header walk.
    `added_line_numbers`'s own header-lookalike case above pins the
    `prev.startswith("--- ")` guard through ITS OWN redundant check on
    the yielded line, which stays correct even if the shared walk's copy
    of the guard is dropped - so THIS test is what actually pins the
    guard living in `_iter_diff_lines` itself: with it dropped, a
    content line reading `+++ b/evil_test.py` gets read as a real file
    header and the added test below is attributed to `evil_test.py`
    instead of the file the hunk is actually in."""
    diff = (
        "diff --git a/test_mod.py b/test_mod.py\n"
        "--- a/test_mod.py\n+++ b/test_mod.py\n"
        "@@ -1,1 +1,4 @@\n"
        " x = 1\n"
        "+++ b/evil_test.py\n"
        "+def test_added_fn():\n"
        "     pass\n"
    )
    result = analyze_test_diff(diff)
    assert result.added_tests == {("test_mod.py", "test_added_fn")}
    assert not any(path == "evil_test.py" for path, _ in result.added_tests)


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

    gap = _only_gap(result, "timed_out")
    assert gap.as_token() == "patch_coverage:timed_out"


def test_a_hanging_coverage_json_spawn_is_a_timed_out_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 1 (PR #388 review, #152): the data spawn and the ``coverage
    json`` spawn share ONE ``[verify] subprocess_timeout`` budget rather
    than each getting the full amount handed to
    :func:`kstrl.verify.check_patch_coverage`.

    Deliberately makes the DATA spawn itself slow (``slow.txt``: 8s,
    within the 10s budget, so it succeeds) rather than instant, because a
    fast data spawn does NOT distinguish the fix from the bug: fixed and
    buggy code differ only by however long the data spawn itself took,
    and with a near-instant data spawn that difference is too small for
    a wall-clock assertion to catch (measured: a fast-data-spawn version
    of this test stayed green under the exact plant below, at both a 5s
    and a 15s budget).

    With the data spawn burning 8 of the 10s: fixed code hands the JSON
    spawn whatever remains (about 2s), so total coverage-phase time
    tracks the ORIGINAL 10s budget regardless of how the 10s split
    between the two spawns (measured: ~10.2s). The bug hands the JSON
    spawn a second full 10s on top of the 8 already spent (measured:
    ~18.2s) - proved by planting ``timeout`` back in at the JSON spawn's
    ``_run_coverage_step`` call (kstrl/verify.py, the line right after
    the ``remaining <= 0`` check). Neither figure includes
    ``verify._SCRUB_TERM_GRACE_SECONDS``: a plain ``time.sleep`` has no
    SIGTERM handler, so the kill is immediate and ``proc.wait(term_grace)``
    returns long before its own timeout. 14 sits with about 4s of margin
    on both sides of the fixed (~10.2s) and buggy (~18.2s) figures.
    """
    _repo(tmp_path)
    (tmp_path / "slow.txt").write_text("8", encoding="utf-8")
    monkeypatch.setattr(
        "kstrl.verify._coverage_json_command",
        lambda tokens, data_file, targets, json_path: [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
    )

    start = time.monotonic()
    result = _run(tmp_path, subprocess_timeout=10.0)
    elapsed = time.monotonic() - start
    assert elapsed < 14, f"took {elapsed:.1f}s; the shared budget did not bound the run"

    gap = _only_gap(result, "timed_out")
    assert gap.as_token() == "patch_coverage:timed_out"


def test_an_undecodable_coverage_report_is_a_command_failed_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``UnicodeDecodeError`` is a ``ValueError``, is NOT a
    ``json.JSONDecodeError``, and ``except (OSError, ValueError)`` around
    the coverage report read in :func:`kstrl.verify._coverage_report` is
    the only thing standing between a bad byte on disk and a traceback
    out of :func:`kstrl.verify.run_mechanical_verification`. Narrowing
    that clause to ``except json.JSONDecodeError`` clears every other
    test in this file (proved: the plant left ``tests/test_patch_coverage.py``
    at "17 passed" before this test existed) because nothing else writes
    invalid utf-8 into the report file.

    The stub makes the JSON spawn exit 0 (so the ``returncode != 0``
    check above the read does not fire) and write bytes that ARE a valid
    file but are NOT valid utf-8, which is exactly the shape ``coverage
    json`` itself would never produce and a narrower except clause would
    let through as an unhandled exception."""
    _repo(tmp_path)
    monkeypatch.setattr(
        "kstrl.verify._coverage_json_command",
        lambda tokens, data_file, targets, json_path: [
            sys.executable,
            "-c",
            "import pathlib, sys; pathlib.Path(sys.argv[1]).write_bytes(b'\\xff\\xfe not json')",
            str(json_path),
        ],
    )

    result = _run(tmp_path)

    _only_gap(result, "command_failed")


def test_a_shell_operator_in_the_test_command_is_refused_before_any_run(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    result = _run(
        tmp_path,
        test_command=f"{shlex.quote(sys.executable)} -m pytest && echo done",
    )

    _only_gap(result, "tool_missing")
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
