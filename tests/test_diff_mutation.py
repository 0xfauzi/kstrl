"""R8.5 Layer 2 (#152): diff-scoped mutation. Advisory, no floor.

Every end-to-end test here drives the real :func:`run_mechanical_verification`
over a real temp git repository built by :func:`tests.conftest.make_review_repo`,
with a FAKE ``mutmut`` on PATH (:mod:`tests.helpers.fakemutmut`) whose canned
output is copied from the REAL mutmut 2.5.1 runs recorded in the lane's
``measurements.md`` section 2e. Four unit tests (17, 18, 20, 21 in the plan
this file implements) each name the end-to-end test that covers the same
property, so a unit test is never the only evidence for a claim.

The shared fixture is `tests/test_patch_coverage.py`'s, in fact rather than
only in name (#152 simplify pass, D1: both files now import the source
constants and the ``_repo``/``_run``/``_only_gap`` builders from
``tests/helpers/adequacy_fixture.py``, so editing the fixture in one place
cannot leave the other suite silently testing something else). It is
already measured there: ``mod.py`` on the feature branch adds executable
lines 5, 6, 9, 10, 11, 12, of which 5, 6 and 9 are COVERED by the new test
(``def`` lines execute at import) and 10, 11, 12 are missing. Layer 1 reports
3/6 = 50.0%, and Layer 2's target set is therefore exactly
``{"mod.py": {5, 6, 9}}`` - 10, 11 and 12 must never appear in the patch this
check writes or in anything it reports.
"""

from __future__ import annotations

import functools
import shlex
import shutil
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kstrl.adequacy import AdequacyConfig, mutation_patch, parse_mutant_report
from kstrl.config import ConfigError
from kstrl.config_preflight import preflight_config
from kstrl.verify import _mutation_run_command
from tests.helpers.adequacy_fixture import (
    BASE_MOD,
    BASE_TEST,
    FEAT_MOD,
    FEAT_TEST,
    only_gap,
    repo_builder,
    run_adequacy,
)
from tests.helpers.executables import put_on_path
from tests.helpers.fakemutmut import junit, put_failing_mutmut, put_mutmut_on_path

if TYPE_CHECKING:
    from kstrl.findings import Finding
    from kstrl.verify import CheckResult, NotMeasured, VerificationResult

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

#: The minimal report that just gets a green row: one mutant, line 6,
#: killed. Spelled eight times before #152 simplify pass, D4, in every
#: test that needs mutmut to succeed but does not care about the score.
ONE_LINE_KILLED = junit((1, "mod.py", 6, "killed"))

#: `tests/helpers/adequacy_fixture.py`'s builder, closed over this file's
#: own base/feature commits (#152 simplify pass, D1).
_repo = repo_builder(BASE_FILES, FEAT_FILES)

#: `run_adequacy` with ONE default flipped - `diff_mutation=True`, since
#: nearly every test in this file wants Layer 2 on - rather than a second
#: copy of its body (#152 simplify pass, D1). Every parameter past `root`
#: is keyword-only, so this bound keyword can never collide with a
#: positional fill the way a bound POSITIONAL default could.
_run = functools.partial(run_adequacy, diff_mutation=True)


def _only_gap(result: VerificationResult, check: str, reason: str) -> NotMeasured:
    """`tests/helpers/adequacy_fixture.only_gap`, re-exported under this
    file's existing name so its ~15 call sites need no edit (#152
    simplify pass, D1)."""
    return only_gap(result, check, reason)


def _row(result: VerificationResult) -> CheckResult:
    """The single ``diff_mutation`` row in ``result`` (#152 simplify
    pass, D4: this exact expression was repeated five times)."""
    return next(c for c in result.checks if c.name == "diff_mutation")


def _finding(result: VerificationResult) -> Finding:
    """The single ``adequacy_diff_mutation`` finding in ``result``,
    wherever it was lifted (#152 simplify pass, D4: repeated three times,
    one of them already checking uniqueness by hand - this does that
    check for all three rather than for one)."""
    findings = [
        f for c in result.checks for f in c.findings if f.category == "adequacy_diff_mutation"
    ]
    assert len(findings) == 1, findings
    return findings[0]


def _no_spawn_gap(result: VerificationResult, recdir: Path, reason: str, fragment: str) -> None:
    """The pre-spend refusal shape (#152 simplify pass, D4: repeated
    three times): a single ``diff_mutation`` gap carrying ``reason``,
    whose detail names ``fragment``, and mutmut was never spawned at all
    - the refusal is BEFORE any spawn, so ``argv-run.txt`` does not
    exist."""
    gap = _only_gap(result, "diff_mutation", reason)
    assert fragment in gap.detail
    assert not (recdir / "argv-run.txt").exists()


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
    row = _row(result)
    assert row.passed is True
    assert row.measured is True
    assert "100.0%" in row.message
    # _finding's own expression is kstrl/pipeline.py:2797's, which lifts
    # check findings into the component's finding stream.
    finding = _finding(result)
    assert finding.severity == "advisory"
    assert finding.phase == "adequacy"
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
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=ONE_LINE_KILLED)
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
    row = _row(result)
    assert "100.0%" in row.message
    assert "1/1" in row.message
    details = "\n".join(row.details)
    assert "mod.py:11" not in row.message
    assert "mod.py:11" not in details
    finding = _finding(result)
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
    row = _row(result)
    assert "0.0%" in row.message
    assert "0/1" in row.message
    # What "any killed on the line wins" (100.0%) and "count every
    # mutant" (66.7%) would each have reported instead.
    assert "100.0%" not in row.message
    assert "66.7%" not in row.message
    details = "\n".join(row.details)
    assert details.count("mod.py:6") == 1
    assert "1 measured" in details


@pytest.mark.parametrize(
    "first_status",
    ["killed", "untested"],
    ids=["one-line-measured", "nothing-measured"],
)
def test_the_cap_bounds_the_run_and_the_tree_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_status: str
) -> None:
    """Requirement 3 (hard wall-clock cap) and D7 (the tree is restored
    from mutmut's own ``.bak``, measurements 2h). ``mutation_timeout=2.0``
    is the cap (D5); the fake sleeps 30s so it is the TIMEOUT that bounds
    this run, not the fake finishing early. The 20s assertion bound
    separates "the cap fired" from "the cap did not exist" (the fake sleeps
    30s) - it is not a performance assertion; the run_scrubbed timeout path
    costs the cap plus up to two `_SCRUB_TERM_GRACE_SECONDS` (5.0).

    #152 simplify pass, D4: the two ids differ only in line 6's status -
    ``one-line-measured`` (``killed``) is a sampled 100.0% row; line 9
    stays ``untested`` either way, so ``nothing-measured``'s two
    ``untested`` lines are D6's OTHER truncated shape, zero definite
    verdicts, which is a ``timed_out`` SIDECAR rather than a ``0.0%``
    row - zero killed out of zero measured is not a score."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, first_status), (2, "mod.py", 9, "untested")),
        sleep=30,
        mutate="mod.py",
    )
    start = time.monotonic()
    result = _run(tmp_path, mutation_timeout=2.0)
    elapsed = time.monotonic() - start
    assert elapsed < 20
    assert (tmp_path / "mod.py").read_text() == FEAT_MOD
    assert not (tmp_path / "mod.py.bak").exists()
    assert not (tmp_path / ".mutmut-cache").exists()
    if first_status == "killed":
        row = _row(result)
        assert "sampled" in row.message
        assert "100.0%" in row.message
        details = "\n".join(row.details)
        assert "3 changed+covered line(s) targeted; 2 produced a mutant; 1 measured" in details
        assert result.passed is True
        # The report spawn still runs AFTER the cap fires - what makes a
        # truncated run scoreable at all.
        assert (recdir / "argv-junitxml.txt").exists()
    else:
        _only_gap(result, "diff_mutation", "timed_out")


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
    row = _row(result)
    details = "\n".join(row.details)
    assert "mod.py:6" in details
    assert "mod.py:9" in details
    finding = _finding(result)
    assert "mod.py:6" in finding.explanation
    assert "mod.py:9" in finding.explanation
    assert "0.0%" in row.message
    assert "0/2" in row.message


def test_mutmut_missing_is_a_sidecar_not_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATH containing ONLY a wrapper that execs the real `git`, built
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

    `put_on_path(..., prepend=False)` (#152 simplify pass, D3) REPLACES
    PATH outright rather than merely putting the wrapper first: a symlink
    ahead of a real mutmut on some other PATH entry would still leave
    `shutil.which("mutmut")` finding that real one, which is exactly what
    this test exists to rule out.
    """
    _repo(tmp_path)
    git_path = shutil.which("git")
    assert git_path is not None
    put_on_path(
        tmp_path,
        monkeypatch,
        "git",
        f'#!/bin/sh\nexec {shlex.quote(git_path)} "$@"\n',
        dirname="gitonly",
        prepend=False,
    )
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
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=ONE_LINE_KILLED)
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
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=ONE_LINE_KILLED)
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
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=ONE_LINE_KILLED)
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
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=ONE_LINE_KILLED)
    result = _run(tmp_path)
    _no_spawn_gap(result, recdir, "command_failed", "mutmut_config.py")


def test_a_backup_file_already_beside_a_target_is_refused_before_the_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7 step 1, plant 14: a pre-existing ``.bak`` beside a target is
    refused, never silently overwritten. Without this pre-flight, an
    implementation that restores unconditionally passes every OTHER test
    in this file while silently destroying a project's own file on a real
    run - this is the test that makes that impossible."""
    _repo(tmp_path, files={**FEAT_FILES, "mod.py.bak": "not mutmut's\n"})
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=ONE_LINE_KILLED)
    result = _run(tmp_path)
    _no_spawn_gap(result, recdir, "command_failed", "mod.py.bak")
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
    row = _row(result)
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
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=ONE_LINE_KILLED)
    result = _run(tmp_path, test_command="true")
    _only_gap(result, "patch_coverage", "tool_missing")
    _no_spawn_gap(result, recdir, "tool_missing", "tool_missing")


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
        junit=ONE_LINE_KILLED,
        sleep=sleep,
        mutate="mod.py",
        restore=restore,
    )
    result = _run(tmp_path, mutation_timeout=mutation_timeout)
    assert (tmp_path / "mod.py").read_text() == FEAT_MOD
    assert not (tmp_path / "mod.py.bak").exists()
    assert stat.S_IMODE((tmp_path / "mod.py").stat().st_mode) == 0o755
    assert [c for c in result.checks if c.name == "diff_mutation"] != []


def test_a_sampled_score_is_tagged_not_only_worded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A3: sampled must be readable as DATA, not only a headline
    substring - a floor-setter needs to separate sampled from complete
    without parsing prose. Reuses the cap-bounded scenario
    ``test_the_cap_bounds_the_run_and_the_tree_is_restored`` (sampled,
    100.0%). The prose stays; this adds a second, machine-readable place
    the same fact is true."""
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "killed"), (2, "mod.py", 9, "untested")),
        sleep=30,
        mutate="mod.py",
    )
    result = _run(tmp_path, mutation_timeout=2.0)
    row = _row(result)
    finding = _finding(result)
    assert "sampled" in row.message
    assert "sampled" in finding.tags


def test_a_complete_score_is_not_tagged_sampled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of A3: NOT sampled must not carry the tag either,
    or the previous test would be evidence of nothing. Reuses the plain,
    uncapped scenario ``test_the_verify_phase_records_the_diff_mutation_finding``
    already measures at 100.0% with every line definite."""
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "killed"), (2, "mod.py", 9, "killed")),
    )
    result = _run(tmp_path)
    row = _row(result)
    finding = _finding(result)
    assert "sampled" not in row.message
    assert "sampled" not in finding.tags


def test_layer_ones_own_duration_refuses_before_spending_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: mutmut always pays its baseline test-suite run in full before
    mutating a line - the same suite Layer 1 just measured coverage of -
    and the inlined `_remove_mutation_cache` deletes `.mutmut-cache`
    before every run, so mutmut's own cache-hit early return is
    unreachable. A cap already <= Layer 1's own measured duration would
    therefore certainly be exhausted by the baseline alone, so this check
    refuses before spending it. `mutation_timeout=0.001` guarantees the
    comparison without a sleep: no real pytest spawn completes in 1ms."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=ONE_LINE_KILLED)
    result = _run(tmp_path, mutation_timeout=0.001)
    coverage_row = next(c for c in result.checks if c.name == "patch_coverage")
    assert coverage_row.passed is True
    assert coverage_row.duration_seconds >= 0.001
    _no_spawn_gap(result, recdir, "timed_out", "mutation_timeout")


#: Module-level code (not a `conftest.py` hook, so `test_mod.py` - already
#: excluded from coverage targets by `is_test_path` - is the only file
#: that differs from the standard fixture) counts pytest invocations and
#: fails ONLY the first, so `[verify] test_suite` (invocation 1) fails
#: while Layer 1's coverage run (invocation 2) PASSES - the decoupling
#: A2's test needs, since Layer 1 gapping is what already stops Layer 2
#: when the two runs agree.
_FLAKY_ONCE_TEST = (
    "from pathlib import Path\n"
    "from mod import added_covered, covered_before\n\n"
    "_COUNTER = Path(__file__).parent / 'runs.txt'\n"
    "_N = int(_COUNTER.read_text()) if _COUNTER.exists() else 0\n"
    "_COUNTER.write_text(str(_N + 1))\n\n\n"
    "def test_before():\n"
    "    assert covered_before(1) == 2\n\n\n"
    "def test_added():\n"
    "    assert added_covered(3) == 6\n\n\n"
    "def test_flaky_gate():\n"
    "    assert _N != 0\n"
)


def test_a_failing_test_suite_never_spawns_mutmut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2: mutmut's baseline run is the whole suite again, so spawning it
    once `[verify] test_suite` already failed runs the suite a THIRD time
    only to abort. :data:`_FLAKY_ONCE_TEST` makes the scenario reachable:
    `check_test_suite` is invocation 1 (fails), Layer 1's coverage run is
    invocation 2 (PASSES) - so Layer 1's own gap is not what stops Layer
    2 here. Layer 1's row is asserted passing for that reason: without
    it this test passes just as well via the pre-existing `coverage is
    None` branch, which is not the case A2 covers."""
    _repo(tmp_path, files={"mod.py": FEAT_MOD, "test_mod.py": _FLAKY_ONCE_TEST})
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=ONE_LINE_KILLED)
    result = _run(tmp_path)
    test_row = next(c for c in result.checks if c.name == "test_suite")
    assert test_row.passed is False
    coverage_row = next(c for c in result.checks if c.name == "patch_coverage")
    assert coverage_row.passed is True
    _no_spawn_gap(result, recdir, "command_failed", "test_suite")


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
