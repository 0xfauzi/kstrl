"""R8.5 Layer 2 (#152): diff-scoped mutation. Advisory, no floor.

Every end-to-end test here drives the real :func:`run_mechanical_verification`
over a real temp git repository built by :func:`tests.conftest.make_review_repo`,
with a FAKE ``mutmut`` on PATH (:mod:`tests.helpers.fakemutmut`) whose canned
output is copied from the REAL mutmut 2.5.1 runs recorded in the lane's
``measurements.md`` section 2e. Four unit tests (17, 18, 20, 21 in the plan
this file implements) each name the end-to-end test that covers the same
property, so a unit test is never the only evidence for a claim.

The shared fixture is `tests/test_patch_coverage.py`'s, on purpose - it is
already measured there: ``mod.py`` on the feature branch adds executable
lines 5, 6, 9, 10, 11, 12, of which 5, 6 and 9 are COVERED by the new test
(``def`` lines execute at import) and 10, 11, 12 are missing. Layer 1 reports
3/6 = 50.0%, and Layer 2's target set is therefore exactly
``{"mod.py": {5, 6, 9}}`` - 10, 11 and 12 must never appear in the patch this
check writes or in anything it reports.
"""

from __future__ import annotations

import shlex
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kstrl.adequacy import AdequacyConfig, mutation_patch, parse_mutant_report
from kstrl.config import ConfigError
from kstrl.config_preflight import preflight_config
from kstrl.verify import (
    VerifyConfig,
    _mutation_run_command,
    run_mechanical_verification,
)
from tests.conftest import make_review_repo
from tests.helpers.fakemutmut import junit, put_failing_mutmut, put_mutmut_on_path

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

#: A plain, empty conftest.py - NOT `tests/test_patch_coverage.py`'s, whose
#: `pytest_sessionstart` hook is there to count runs and stall the SECOND
#: (coverage) spawn for its own timeout test. This file needs neither: it
#: needs `mod.py` importable, nothing more.
BASE_FILES = {"conftest.py": "", "mod.py": BASE_MOD, "test_mod.py": BASE_TEST}
FEAT_FILES = {"mod.py": FEAT_MOD, "test_mod.py": FEAT_TEST}

#: The exact synthetic patch this shared fixture's target set produces
#: (measurements.md 2c: no `a/`/`b/` prefixes). Tests 2 and 17 both pin it -
#: end to end and as a unit - so it is spelled once here.
TARGET_PATCH = (
    "--- mod.py\n+++ mod.py\n@@ -4,0 +5,1 @@\n+x\n@@ -5,0 +6,1 @@\n+x\n@@ -8,0 +9,1 @@\n+x\n"
)


def _repo(tmp_path: Path, files: dict[str, str] | None = None) -> None:
    """``BASE_FILES`` on ``main``, plus ``files`` (default :data:`FEAT_FILES`)
    on a ``feature`` branch, in place on ``tmp_path`` - `tests/test_patch_coverage.py`'s
    ``_repo`` convention."""
    make_review_repo(
        tmp_path, base_files=BASE_FILES, files=files if files is not None else FEAT_FILES
    )


def _run(
    root: Path,
    *,
    base_branch: str = "main",
    test_command: str | None = None,
    subprocess_timeout: float = 120.0,
    mutation_timeout: float = 120.0,
    enabled: bool = True,
    patch_coverage: bool = True,
    diff_mutation: bool = True,
    read_only: bool = False,
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
            mutation_timeout=mutation_timeout,
        ),
        adequacy_config=AdequacyConfig(
            enabled=enabled, patch_coverage=patch_coverage, diff_mutation=diff_mutation
        ),
        read_only=read_only,
    )


def _only_gap(result: VerificationResult, check: str, reason: str) -> NotMeasured:
    """The single ``check`` gap in ``result``: no row alongside it, exactly
    one gap, and it carries ``reason`` - the three-argument form of
    `tests/test_patch_coverage.py`'s ``_only_gap`` (#152: this file needs it
    for two check names, ``diff_mutation`` and, in test 12, ``patch_coverage``
    too)."""
    assert [c for c in result.checks if c.name == check] == []
    gaps = [g for g in result.not_measured if g.check == check]
    assert len(gaps) == 1, gaps
    assert gaps[0].reason == reason, gaps[0]
    return gaps[0]


# ---------------------------------------------------------------------------
# End to end, through run_mechanical_verification
# ---------------------------------------------------------------------------


def test_the_verify_phase_records_the_diff_mutation_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D11: mutmut exits 2 with survivors, and that is NOT a failure - the
    fake's ``run_exit=2`` is the exact code real mutmut returns with one or
    more mutants surviving (measurements.md section 1b)."""
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit(
            (1, "mod.py", 6, "killed"), (2, "mod.py", 6, "killed"), (3, "mod.py", 6, "survived")
        ),
        run_exit=2,
    )
    result = _run(tmp_path)
    rows = [c for c in result.checks if c.name == "diff_mutation"]
    assert len(rows) == 1
    row = rows[0]
    assert row.passed is True
    assert row.measured is True
    assert "100.0%" in row.message
    # The exact expression kstrl/pipeline.py:2797 uses to lift check
    # findings into the component's finding stream.
    findings = [f for c in result.checks for f in c.findings]
    diff_findings = [f for f in findings if f.category == "adequacy_diff_mutation"]
    assert len(diff_findings) == 1
    assert diff_findings[0].severity == "advisory"
    assert diff_findings[0].phase == "adequacy"
    assert result.passed is True
    assert [g for g in result.not_measured if g.check == "diff_mutation"] == []
    assert not (tmp_path / ".mutmut-cache").exists()
    assert not (tmp_path / "mod.py.bak").exists()
    assert (tmp_path / "mod.py").read_text() == FEAT_MOD


def test_only_changed_and_covered_lines_are_offered_to_mutmut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 1 (changed AND covered only). measurements.md 2c: a
    synthetic patch naming exactly the changed-and-covered lines selects
    exactly those lines and nothing mutmut was not asked for."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    _run(tmp_path)
    patch_text = (recdir / "patch.diff").read_text()
    assert patch_text == TARGET_PATCH
    assert "+10," not in patch_text
    assert "+11," not in patch_text
    assert "+12," not in patch_text
    assert "test_mod.py" not in patch_text
    argv = (recdir / "argv-run.txt").read_text()
    assert "--paths-to-mutate=mod.py" in argv
    assert "test_mod.py" not in argv


def test_a_changed_line_the_suite_never_ran_is_not_in_the_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 1: mutmut mutating MORE than it was asked - a
    changed-but-UNCOVERED line, 11 - must not leak into the score. D3's
    filter-back-to-``targets`` rule is what holds this, not mutmut's own
    scoping."""
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "killed"), (2, "mod.py", 11, "survived")),
        run_exit=2,
    )
    result = _run(tmp_path)
    row = next(c for c in result.checks if c.name == "diff_mutation")
    assert "100.0%" in row.message
    assert "1/1" in row.message
    details = "\n".join(row.details)
    assert "mod.py:11" not in row.message
    assert "mod.py:11" not in details
    finding = next(
        f for c in result.checks for f in c.findings if f.category == "adequacy_diff_mutation"
    )
    assert "mod.py:11" not in finding.explanation
    assert "3 changed+covered line(s) targeted" in details


@pytest.mark.parametrize(
    "rows",
    [
        ((1, "mod.py", 6, "survived"), (2, "mod.py", 6, "killed"), (3, "mod.py", 6, "killed")),
        ((1, "mod.py", 6, "untested"), (2, "mod.py", 6, "survived"), (3, "mod.py", 6, "killed")),
    ],
    ids=["all-definite", "untested-first"],
)
def test_at_most_one_mutant_per_line_and_the_lowest_definite_id_decides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: tuple[tuple[int, str, int, str], ...],
) -> None:
    """Requirement 2 (D4): the selected mutant is the LOWEST id among
    those with a killed-or-survived status, not the bare lowest id.
    ``untested-first`` is the parametrisation that separates the two
    readings - see :func:`kstrl.adequacy.score_mutants` - and is plant 13;
    ``all-definite`` alone would also pass under the wrong "lowest id,
    whatever its status" rule, since id 1 there is already definite."""
    _repo(tmp_path)
    put_mutmut_on_path(tmp_path, monkeypatch, junit=junit(*rows), run_exit=2)
    result = _run(tmp_path)
    row = next(c for c in result.checks if c.name == "diff_mutation")
    assert "0.0%" in row.message
    assert "0/1" in row.message
    # What "any killed on the line wins" (100.0%) and "count every
    # mutant" (66.7%) would each have reported instead.
    assert "100.0%" not in row.message
    assert "66.7%" not in row.message
    details = "\n".join(row.details)
    assert details.count("mod.py:6") == 1
    assert "1 measured" in details


def test_the_cap_bounds_the_run_and_the_tree_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 3 (hard wall-clock cap) and D7 (the tree is restored
    from mutmut's own ``.bak``, measurements 2h). ``mutation_timeout=2.0``
    is the cap (D5); the fake sleeps 30s so it is the TIMEOUT that bounds
    this run, not the fake finishing early. The 20s assertion bound
    separates "the cap fired" from "the cap did not exist" (the fake sleeps
    30s) - it is not a performance assertion; the run_scrubbed timeout path
    costs the cap plus up to two `_SCRUB_TERM_GRACE_SECONDS` (5.0)."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "killed"), (2, "mod.py", 9, "untested")),
        sleep=30,
        mutate="mod.py",
    )
    start = time.monotonic()
    result = _run(tmp_path, mutation_timeout=2.0)
    elapsed = time.monotonic() - start
    assert elapsed < 20
    row = next(c for c in result.checks if c.name == "diff_mutation")
    assert (tmp_path / "mod.py").read_text() == FEAT_MOD
    assert not (tmp_path / "mod.py.bak").exists()
    assert not (tmp_path / ".mutmut-cache").exists()
    assert "sampled" in row.message
    assert "100.0%" in row.message
    details = "\n".join(row.details)
    assert "3 changed+covered line(s) targeted; 2 produced a mutant; 1 measured" in details
    assert result.passed is True
    # The report spawn still runs AFTER the cap fires - what makes a
    # truncated run scoreable at all.
    assert (recdir / "argv-junitxml.txt").exists()


def test_a_truncated_run_that_measured_nothing_is_a_timed_out_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 3: a run that measured ZERO lines within the cap is a
    ``timed_out`` sidecar (D6) - never a ``0.0%`` row. Zero killed out of
    zero measured is not a score."""
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "untested"), (2, "mod.py", 9, "untested")),
        sleep=30,
        mutate="mod.py",
    )
    result = _run(tmp_path, mutation_timeout=2.0)
    _only_gap(result, "diff_mutation", "timed_out")
    assert (tmp_path / "mod.py").read_text() == FEAT_MOD
    assert not (tmp_path / "mod.py.bak").exists()
    assert not (tmp_path / ".mutmut-cache").exists()


def test_survivors_are_recorded_as_file_and_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 4: surviving lines are concrete ``path:line`` targets,
    in both the row's details and the advisory finding's explanation that
    reaches the PR body and the component's finding stream."""
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "survived"), (2, "mod.py", 9, "survived")),
        run_exit=2,
    )
    result = _run(tmp_path)
    row = next(c for c in result.checks if c.name == "diff_mutation")
    details = "\n".join(row.details)
    assert "mod.py:6" in details
    assert "mod.py:9" in details
    finding = next(
        f for c in result.checks for f in c.findings if f.category == "adequacy_diff_mutation"
    )
    assert "mod.py:6" in finding.explanation
    assert "mod.py:9" in finding.explanation
    assert "0.0%" in row.message
    assert "0/2" in row.message


def test_mutmut_missing_is_a_sidecar_not_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATH containing ONLY a symlink to the real `git`, built
    deterministically rather than `skipif`d, so `shutil.which("mutmut")`
    is None on any machine including one with mutmut installed. Also
    asserts Layer 1's OWN row still ran at 50.0%: without that second
    assertion, this test would pass just as well if the git-only PATH had
    broken Layer 1 and Layer 2 was never reached at all - a measurement
    that returns a plausible number for the wrong reason.

    Why a git-only PATH does not break Layer 1: `_coverage_data_command`
    and `_coverage_json_command` both begin with the absolute
    `sys.executable`, and this file's `typecheck_command="true"` /
    `lint_command="true"` are shell builtins - nothing in this run needs a
    PATH lookup except `git`.
    """
    _repo(tmp_path)
    git_path = shutil.which("git")
    assert git_path is not None
    bindir = tmp_path / "gitonly"
    bindir.mkdir()
    (bindir / "git").symlink_to(Path(git_path))
    monkeypatch.setenv("PATH", str(bindir))
    result = _run(tmp_path)
    gap = _only_gap(result, "diff_mutation", "tool_missing")
    assert "mutmut" in gap.detail
    coverage_row = next(c for c in result.checks if c.name == "patch_coverage")
    assert "50.0%" in coverage_row.message


def test_a_fatal_mutmut_exit_is_a_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D11: bit 1 of the exit code is fatal. The real failure measured
    without the ``whatthepatch`` extra (measurements.md section 2a) -
    exit 1, before any test runs. The detail carries mutmut's own remedy
    line, and no report spawn happens over a run that never produced one."""
    _repo(tmp_path)
    recdir = put_failing_mutmut(
        tmp_path,
        monkeypatch,
        stderr=(
            "ImportError: The --use-patch feature requires the whatthepatch "
            'library. Run "pip install --force-reinstall mutmut[patch]"'
        ),
        exit_code=1,
    )
    result = _run(tmp_path)
    gap = _only_gap(result, "diff_mutation", "command_failed")
    assert "whatthepatch" in gap.detail
    assert not (recdir / "argv-junitxml.txt").exists()


def test_an_unparseable_report_is_a_sidecar_not_a_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md, and the exact defect #260 shipped twice: what cannot be
    parsed is rejected, never counted as zero."""
    _repo(tmp_path)
    put_mutmut_on_path(tmp_path, monkeypatch, junit="not xml at all")
    result = _run(tmp_path)
    _only_gap(result, "diff_mutation", "command_failed")


def test_no_mutable_target_line_is_no_mutants_not_a_hundred_percent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fix3's real shape (measurements.md 2c): every target line was a
    ``def`` line, so mutmut generated no mutant on any of them - a valid,
    empty junitxml report."""
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=(
            '<?xml version="1.0" ?><testsuites disabled="0" errors="0" '
            'failures="0" tests="0" time="0.0">'
            '<testsuite name="mutmut" tests="0"></testsuite></testsuites>'
        ),
    )
    result = _run(tmp_path)
    _only_gap(result, "diff_mutation", "no_mutants")


def test_a_diff_with_no_changed_and_covered_line_never_spawns_mutmut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inheritance rule in `_diff_mutation_checks`, observed end to
    end: Layer 1 gapping ``no_target`` means Layer 2 has nothing to
    mutate, with the SAME reason token - never a second, independently
    guessed reason."""
    _repo(tmp_path, files={"mod.py": BASE_MOD + "# a comment\n"})
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(tmp_path)
    _only_gap(result, "diff_mutation", "no_target")
    _only_gap(result, "patch_coverage", "no_target")
    assert not (recdir / "argv-run.txt").exists()


@pytest.mark.parametrize(
    ("enabled", "patch_coverage", "diff_mutation"),
    [(True, True, False), (False, True, True)],
    ids=["diff_mutation-off", "adequacy-disabled"],
)
def test_off_by_default_never_spawns_mutmut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    patch_coverage: bool,
    diff_mutation: bool,
) -> None:
    """The ``adequacy-disabled`` case is what caught plant 5 on PR #388
    (a check reading only its own sub-toggle, not the section's master
    switch too); ``diff_mutation`` reads both for the same reason."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(
        tmp_path, enabled=enabled, patch_coverage=patch_coverage, diff_mutation=diff_mutation
    )
    assert [c for c in result.checks if c.name == "diff_mutation"] == []
    assert [g for g in result.not_measured if g.check == "diff_mutation"] == []
    assert not (recdir / "argv-run.txt").exists()


def test_read_only_is_a_sidecar_because_mutmut_rewrites_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D8, the OPPOSITE of Layer 1's D8: mutmut rewrites the file it
    mutates, so this cannot run under ``ks sense`` (``read_only=True``) at
    all, where Layer 1's coverage run writes nothing into the tree."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(tmp_path, read_only=True)
    _only_gap(result, "diff_mutation", "read_only")
    assert not (recdir / "argv-run.txt").exists()


def test_a_contradictory_config_is_refused_at_preflight_with_no_traceback(tmp_path: Path) -> None:
    """D2, end to end through the surface every other pre-spend config
    refusal goes through. `kstrl/config_preflight.py` already lists
    ``ConfigSection(("adequacy",), AdequacyConfig.load)`` and turns a
    ``ValueError`` out of ``__post_init__`` into a named refusal rather
    than a traceback - CLAUDE.md is explicit that a refusal must go out
    through that surface, not out of the top of the run."""
    (tmp_path / "kstrl.toml").write_text(
        "[adequacy]\nenabled = true\ndiff_mutation = true\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as exc:
        preflight_config(tmp_path, warn=lambda _m: None)
    assert "diff_mutation" in str(exc.value)
    assert "patch_coverage" in str(exc.value)
    assert "adequacy" in str(exc.value)


def test_the_config_constructor_itself_refuses_the_contradiction() -> None:
    """Unit half of
    ``test_a_contradictory_config_is_refused_at_preflight_with_no_traceback``,
    which is the end-to-end evidence that this reaches an operator as a
    legible exit rather than only failing inside the dataclass."""
    with pytest.raises(ValueError, match="patch_coverage"):
        AdequacyConfig(enabled=True, patch_coverage=False, diff_mutation=True)


def test_a_project_with_a_mutmut_config_hook_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``pre_mutation`` hook in a project's own ``mutmut_config.py`` is
    the ONLY route to mutmut's ``skipped`` status (measurements.md 2g),
    and `mutmut/cache.py::create_junitxml_report` renders a skipped mutant
    IDENTICALLY to a killed one - so a report from such a project cannot
    be trusted, and this check refuses before any spawn instead."""
    _repo(tmp_path)
    (tmp_path / "mutmut_config.py").write_text("def pre_mutation(context):\n    pass\n")
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(tmp_path)
    gap = _only_gap(result, "diff_mutation", "command_failed")
    assert "mutmut_config.py" in gap.detail
    assert not (recdir / "argv-run.txt").exists()


def test_a_backup_file_already_beside_a_target_is_refused_before_the_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7 step 1, plant 14: a pre-existing ``.bak`` beside a target is
    refused, never silently overwritten. Without this pre-flight, an
    implementation that restores unconditionally passes every OTHER test
    in this file while silently destroying a project's own file on a real
    run - this is the test that makes that impossible."""
    _repo(tmp_path, files={**FEAT_FILES, "mod.py.bak": "not mutmut's\n"})
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(tmp_path)
    gap = _only_gap(result, "diff_mutation", "command_failed")
    assert "mod.py.bak" in gap.detail
    assert not (recdir / "argv-run.txt").exists()
    assert (tmp_path / "mod.py.bak").read_text() == "not mutmut's\n"
    assert (tmp_path / "mod.py").read_text() == FEAT_MOD


def test_a_truncated_run_whose_every_mutable_line_was_measured_is_still_sampled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 3 of #152, the half no other test separates: the cap
    fired, and EVERY line mutmut reported a mutant for still reached a
    definite status, so ``measured_lines < mutable_lines`` is FALSE and
    ``truncated`` is the only thing that can label this score sampled.
    The shipped ``test_the_cap_bounds_the_run_and_the_tree_is_restored``
    cannot make that separation: its report leaves line 9 ``untested``, so
    the second disjunct holds its ``sampled`` assertion up on its own.
    1.73 mutants per mutable line (measurements.md) is what makes this
    shape ordinary rather than exotic - a cap that fires once every target
    line has one verdict leaves the rest of that line's mutants
    ``untested`` and off the denominator. Canned junit rows are mutmut
    2.5.1's own rendering (measurements.md section 2e)."""
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "killed"), (2, "mod.py", 9, "survived")),
        sleep=30,
        mutate="mod.py",
    )
    start = time.monotonic()
    result = _run(tmp_path, mutation_timeout=2.0)
    assert time.monotonic() - start < 20
    row = next(c for c in result.checks if c.name == "diff_mutation")
    details = "\n".join(row.details)
    assert "3 changed+covered line(s) targeted; 2 produced a mutant; 2 measured" in details
    assert "50.0%" in row.message
    assert "[sampled: 2 of 3 changed+covered lines measured" in row.message


def test_the_layer_one_gap_reason_is_inherited_not_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inheritance rule in ``_diff_mutation_checks``, in the only case
    that separates it from its default: Layer 1 gaps ``tool_missing`` (a
    ``test_command`` it cannot extend), so Layer 2's gap must read
    ``tool_missing`` too. ``no_target`` there would tell an operator the
    diff was empty when the real cause was a test command nothing could
    measure. ``test_a_diff_with_no_changed_and_covered_line_never_spawns_mutmut``
    cannot make this separation: Layer 1 gaps ``no_target`` there, which is
    also the hard-coded default, so the two readings agree. The
    ``argv-run.txt`` assertion is the second half of the claim: the
    inherited gap is returned BEFORE any spawn, so mutmut is never run."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(tmp_path, test_command="true")
    _only_gap(result, "patch_coverage", "tool_missing")
    gap = _only_gap(result, "diff_mutation", "tool_missing")
    assert "tool_missing" in gap.detail
    assert not (recdir / "argv-run.txt").exists()


@pytest.mark.parametrize(
    ("restore", "mutation_timeout", "sleep"),
    [(True, 120.0, 0), (False, 2.0, 30)],
    ids=["mutmut-restored-it", "cap-fired"],
)
def test_the_mode_of_a_mutated_file_survives_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore: bool,
    mutation_timeout: float,
    sleep: int,
) -> None:
    """mutmut 2.5.1 writes its backup with ``open(path + '.bak', 'w')`` -
    the umask default - and restores it with ``shutil.move``, which
    renames (both read out of the installed 2.5.1 package:
    ``mutmut.mutate_file`` and ``mutmut.run_mutation``'s ``finally``).
    Either way the backup's mode lands on the source file, so a 0755
    module comes back 0644 with its content correct: a mode change git
    can see and ``[verify] dead_code_cleanup``'s ``git add -A`` can
    commit, and, at 0600, a loosening git cannot see at all. Both paths
    are covered because they lose the mode in different places -
    ``mutmut-restored-it`` inside mutmut, where kstrl never sees a
    ``.bak`` at all, and ``cap-fired`` inside ``_restore_mutated_sources``
    where kstrl's own ``os.replace`` does it. The assertion is robust to
    the developer's umask: a file created by redirection is never
    executable whatever the umask, so it can never accidentally equal
    0o755."""
    _repo(tmp_path)
    (tmp_path / "mod.py").chmod(0o755)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "killed")),
        sleep=sleep,
        mutate="mod.py",
        restore=restore,
    )
    result = _run(tmp_path, mutation_timeout=mutation_timeout)
    assert (tmp_path / "mod.py").read_text() == FEAT_MOD
    assert not (tmp_path / "mod.py.bak").exists()
    assert stat.S_IMODE((tmp_path / "mod.py").stat().st_mode) == 0o755
    assert [c for c in result.checks if c.name == "diff_mutation"] != []


# ---------------------------------------------------------------------------
# Unit tests on the pure functions. Each names the end-to-end test that
# covers the same property, so a unit test is never the only evidence.
# ---------------------------------------------------------------------------


def test_the_synthetic_patch_selects_exactly_the_lines_given() -> None:
    """Unit evidence for requirement 1's mechanism (D3); end-to-end
    evidence is ``test_only_changed_and_covered_lines_are_offered_to_mutmut``.
    measurements.md 2c: no ``a/``/``b/`` prefixes, or whatthepatch resolves
    the wrong path and selects nothing. The two-file case pins the
    ordering: files sorted, lines sorted within a file."""
    assert mutation_patch({"mod.py": frozenset({5, 6, 9})}) == TARGET_PATCH
    assert mutation_patch({"b.py": {3}, "a.py": {1, 10}}) == (
        "--- a.py\n+++ a.py\n"
        "@@ -0,0 +1,1 @@\n+x\n"
        "@@ -9,0 +10,1 @@\n+x\n"
        "--- b.py\n+++ b.py\n"
        "@@ -2,0 +3,1 @@\n+x\n"
    )


def test_the_patch_is_valid_for_a_target_on_line_one() -> None:
    """The one arithmetic edge in ``mutation_patch``: ``n - 1`` at
    ``n == 1``. An added covered line CAN be a module's first line (an
    import), and a negative old-side line number would not be a valid
    hunk header."""
    assert mutation_patch({"a.py": frozenset({1})}) == "--- a.py\n+++ a.py\n@@ -0,0 +1,1 @@\n+x\n"


def test_the_report_parser_classifies_every_status_mutmut_emits() -> None:
    """Unit evidence for the status table :func:`tests.helpers.fakemutmut.junit`
    encodes (measurements.md section 2e); end-to-end evidence for
    killed/survived/untested is tests 1, 5 and 10 in the plan this file
    implements. A ``bad_timeout`` row classifies :data:`MUTANT_INCONCLUSIVE`,
    NOT killed."""
    xml_text = junit(
        (1, "mod.py", 6, "killed"),
        (2, "mod.py", 9, "survived"),
        (3, "mod.py", 12, "untested"),
        (4, "mod.py", 15, "timeout"),
    )
    mutants = parse_mutant_report(xml_text)
    by_id = {m.mutant_id: m.status for m in mutants}
    assert by_id == {1: "killed", 2: "survived", 3: "untested", 4: "inconclusive"}

    with pytest.raises(ValueError):
        parse_mutant_report(
            '<?xml version="1.0" ?><testsuites><testsuite>'
            '<testcase name="Mutant #1" file="mod.py"></testcase>'
            "</testsuite></testsuites>"
        )
    with pytest.raises(ValueError):
        parse_mutant_report("not xml at all")


def test_the_runner_value_survives_a_test_command_token_with_a_space(tmp_path: Path) -> None:
    """D10, plant 15: the ``--runner`` value is built with `shlex.join`,
    never ``" ".join`` - a token with a space (an interpreter path under a
    directory whose name has one) must not fracture into two shell words,
    which would make mutmut report every mutant SURVIVING (a wrong
    NUMBER, not a failure - nothing would go red). End-to-end evidence
    that the value reaches mutmut at all is
    ``test_only_changed_and_covered_lines_are_offered_to_mutmut``, which
    asserts the run argv; a fixture whose OWN interpreter path has a space
    in it would be measuring the fixture, not this function."""
    tokens = ["/tmp/a b/python", "-m", "pytest"]
    command = _mutation_run_command(
        tokens, {"mod.py": {6}}, tmp_path / "targets.diff", tmp_path / "tests"
    )
    runner_args = [c for c in command if c.startswith("--runner=")]
    assert len(runner_args) == 1
    assert runner_args[0] == "--runner=" + shlex.join(["/tmp/a b/python", "-m", "pytest", "-x"])
    assert "--runner=/tmp/a b/python -m pytest -x" not in command
