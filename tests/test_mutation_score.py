"""``[verify] mutation_testing`` (#391): one mutmut driver, grounded in
recorded real output.

Every test here drives the real :func:`run_mechanical_verification` over a
real temp git repository built by :func:`tests.conftest.make_review_repo`,
with a FAKE ``mutmut`` on PATH (:mod:`tests.helpers.fakemutmut`) whose canned
output is copied from real mutmut 2.5.1 runs recorded in the lane's
``measurements.md``. ``check_mutation_score`` is never called directly:
every assertion goes through ``run_adequacy`` -> ``run_mechanical_verification``,
the path a real factory run takes.

The fixture is the one ``tests/helpers/adequacy_fixture.py`` shares with
``tests/test_diff_mutation.py``: ``mod.py`` on the feature branch is
``FEAT_MOD`` - line 2 ``return n + 1``, line 6 ``return n * 2``, lines
10-12 the body of ``added_missing``. The diff against ``main`` touches
``mod.py`` and ``test_mod.py``; ``_changed_non_test_python`` drops the
test file, so Layer 1's target is exactly ``["mod.py"]``. Tests sit at
the repo ROOT with no ``tests/`` directory, which is why this fixture is
also the ``--tests-dir`` case (test 4).

The driver-behaviour cases this file shares byte-for-byte with
``tests/test_diff_mutation.py`` (a fatal mutmut exit, an unparseable
report, a pre-existing backup, read-only, a project ``mutmut_config.py``
hook and the before-the-run cache delete) live in
``tests/test_mutation_driver_shared.py`` instead, parametrised over the
check name and its runner (#391 simplify pass on PR #392, B1/B2): one
test, not a fork re-forking on every edit.
"""

from __future__ import annotations

import functools
import shlex
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.helpers.adequacy_fixture import (
    EMPTY_CONFTEST_BASE_FILES,
    FEAT_FILES,
    FEAT_MOD,
    FEAT_TEST,
    no_spawn_gap,
    only_gap,
    only_row,
    repo_builder,
    run_adequacy,
)
from tests.helpers.fakemutmut import junit, put_mutmut_on_path

if TYPE_CHECKING:
    from kstrl.verify import CheckResult, VerificationResult

#: ``tests/helpers/adequacy_fixture.py``'s builder, closed over this
#: file's own base/feature commits (#391 simplify pass on PR #392, B5:
#: both commits are the shared fixture's own constants now, not this
#: file's copies).
_repo = repo_builder(EMPTY_CONFTEST_BASE_FILES, FEAT_FILES)

#: ``run_adequacy`` with ONLY Layer 1's gate on: ``mutation_testing=True``,
#: and both R8.5 knobs off so a Layer 2 run can never be what a test in
#: this file is actually measuring.
_run = functools.partial(run_adequacy, mutation_testing=True, patch_coverage=False, enabled=False)

#: The runner value every test in this file expects: ``run_adequacy``'s
#: default ``test_command`` (``sys.executable -m pytest``), with the
#: ``-x`` :func:`kstrl.verify._mutation_run_command` always appends.
_EXPECTED_RUNNER = "--runner=" + shlex.join([sys.executable, "-m", "pytest", "-x"])


def _only_row(result: VerificationResult) -> CheckResult:
    """`tests/helpers/adequacy_fixture.only_row`, bound to this file's
    own check name (#391 simplify pass on PR #392, B3)."""
    return only_row(result, "mutation_testing")


def _row(result: VerificationResult) -> CheckResult:
    """The single ``mutation_testing`` row in ``result``."""
    return next(c for c in result.checks if c.name == "mutation_testing")


def _argv_blocks(argv_lines: list[str]) -> list[list[str]]:
    """Split ``argv-run.txt`` lines into per-invocation blocks: a new
    block starts at every ``--paths-to-mutate=`` element, and every line
    belongs to the block opened most recently. Used by
    :func:`test_both_mutation_checks_spawn_the_same_driver`."""
    blocks: list[list[str]] = []
    for line in argv_lines:
        if line.startswith("--paths-to-mutate="):
            blocks.append([])
        assert blocks, "argv before the first --paths-to-mutate= element"
        blocks[-1].append(line)
    return blocks


def _normalised(block: list[str]) -> list[str]:
    """``block`` with the target-selector axis (``--paths-to-mutate=``,
    ``--use-patch-file=``) removed and ``--tests-dir=`` reduced to its
    basename, since each check builds its own
    ``tempfile.TemporaryDirectory`` and the two absolute paths differ.
    Used by :func:`test_both_mutation_checks_spawn_the_same_driver`."""
    out: list[str] = []
    for element in block:
        if element.startswith(("--paths-to-mutate=", "--use-patch-file=")):
            continue
        if element.startswith("--tests-dir="):
            out.append("--tests-dir=" + Path(element.split("=", 1)[1]).name)
            continue
        out.append(element)
    return out


def test_a_mutmut_run_that_exits_two_is_scored_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1/D11: a correct mutmut run with survivors exits 2, and that is a
    SCORE, never ``command_failed``. Reproduction 1: this exact shape was
    the defect the issue reports (measurements 1a).

    RED before the fix, measured: no row at all, and one gap
    ``('command_failed', 'mutmut run exited 2 and reported no counts: no output')``.
    """
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit(
            (1, "mod.py", 2, "killed"), (2, "mod.py", 6, "killed"), (3, "mod.py", 10, "survived")
        ),
        run_exit=2,
    )
    result = _run(tmp_path)
    row = _only_row(result)
    assert row.passed is True
    assert "66.7%" in row.message
    assert "mod.py:10" in "\n".join(row.details)
    assert not (tmp_path / ".mutmut-cache").exists()
    assert not (tmp_path / "mod.py.bak").exists()


@pytest.mark.parametrize(
    ("threshold", "passed"),
    [(50.0, False), (30.0, True)],
    ids=["at-threshold-fails", "below-threshold-passes"],
)
def test_the_threshold_is_the_only_thing_that_decides_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, threshold: float, passed: bool
) -> None:
    """D5: the threshold is a policy over the same score, nothing else.
    One killed and two survived lines score 33.3%.

    RED before the fix: no row, as in test 1.
    """
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit(
            (1, "mod.py", 2, "killed"), (2, "mod.py", 6, "survived"), (3, "mod.py", 10, "survived")
        ),
        run_exit=2,
    )
    result = _run(tmp_path, mutation_threshold=threshold)
    row = _row(result)
    assert row.passed is passed
    assert "33.3%" in row.message
    if not passed:
        assert "below threshold" in row.message
    assert result.passed is passed


def test_the_whole_changed_file_is_the_target_and_no_patch_file_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: whole-file scope is ``--paths-to-mutate`` alone, no synthetic
    patch. mutmut's own ``--use-patch-file`` requires the ``mutmut[patch]``
    extra plain mutmut does not ship (measured: ImportError, exit 1), and
    whole-file scope has nothing to say ``--paths-to-mutate`` does not.

    RED before the fix: the argv has no ``--tests-dir`` and no
    ``--runner`` line, the fake refuses the run, and no row appears.
    """
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(tmp_path)
    row = _row(result)
    assert "%" in row.message
    argv = (recdir / "argv-run.txt").read_text(encoding="utf-8")
    lines = argv.splitlines()
    assert "--paths-to-mutate=mod.py" in lines
    assert not any(line.startswith("--use-patch-file=") for line in lines)
    assert not (recdir / "patch.diff").exists()
    assert "test_mod.py" not in argv
    runner_lines = [line for line in lines if line.startswith("--runner=")]
    assert runner_lines == [_EXPECTED_RUNNER]


def test_the_run_names_its_own_tests_dir_and_not_the_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: ``--tests-dir`` is an empty temp directory. This fixture's
    tests sit at the repo root, so real mutmut's default ``tests/:test/``
    raises (measurements 1b) and the fake now refuses the same way.

    RED before the fix, mechanism: the fake exits 1 with mutmut's own
    ``FileNotFoundError`` line, so the check gaps ``command_failed`` and
    there is no row.
    """
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(tmp_path)
    _only_row(result)
    argv_lines = (recdir / "argv-run.txt").read_text(encoding="utf-8").splitlines()
    tests_dir_lines = [line for line in argv_lines if line.startswith("--tests-dir=")]
    assert len(tests_dir_lines) == 1
    tests_dir_value = tests_dir_lines[0].split("=", 1)[1]
    assert not Path(tests_dir_value).is_relative_to(tmp_path)
    assert Path(tests_dir_value).name == "empty-tests"


def test_mutmut_is_never_asked_for_a_text_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1: the driver reads ``mutmut junitxml``, never ``mutmut results``
    (which prints no killed count under any flag - measurements 1a).

    RED before the fix: ``argv-other.txt`` exists and contains
    ``results``, and ``argv-junitxml.txt`` does not exist.

    Also the second argv-content home for A3 (#391 simplify pass on PR
    #392, the first is ``tests/test_diff_mutation.py``'s
    ``test_the_verify_phase_records_the_diff_mutation_finding``): the
    report spawn's own argv must carry ``--untested-policy=error``, the
    entire fail-closed guarantee behind D4 - see
    ``kstrl.verify._UNTESTED_POLICY_ERROR``'s own comment. Written as an
    independent literal, not imported from production.
    """
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    _run(tmp_path)
    assert (recdir / "argv-run.txt").exists()
    assert (recdir / "argv-junitxml.txt").exists()
    assert not (recdir / "argv-other.txt").exists()
    argv_junitxml = (recdir / "argv-junitxml.txt").read_text(encoding="utf-8")
    assert "--untested-policy=error" in argv_junitxml


def test_a_mutant_outside_the_changed_files_is_not_in_the_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: whole-file scope's target set is derived from the report's own
    rows, restricted to the files kstrl asked for - a mutant mutmut
    reported outside those files is dropped, never trusted.

    RED before the fix: no row.
    """
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "killed"), (2, "other.py", 3, "survived")),
        run_exit=2,
    )
    result = _run(tmp_path)
    row = _row(result)
    assert "100.0%" in row.message
    assert "other.py" not in row.message
    assert "other.py" not in "\n".join(row.details)


def test_the_cap_is_a_sidecar_and_the_tree_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: a truncated run is ``timed_out``, never a score, and the tree
    is restored from mutmut's own ``.bak``. The report spawn does not
    follow a fired cap - a driver that always calls
    ``_mutmut_report_spawn`` before checking the run's own gap would
    still pass every OTHER assertion here.

    RED before the fix, measured: ``MOD RESTORED: False``,
    ``BAK LEFT: True``, ``CACHE LEFT: True``.
    """
    _repo(tmp_path)
    recdir = put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit((1, "mod.py", 6, "killed")),
        sleep=30,
        mutate="mod.py",
    )
    start = time.monotonic()
    result = _run(tmp_path, mutation_timeout=2.0)
    elapsed = time.monotonic() - start
    assert elapsed < 20
    only_gap(result, "mutation_testing", "timed_out")
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == FEAT_MOD
    assert not (tmp_path / "mod.py.bak").exists()
    assert not (tmp_path / ".mutmut-cache").exists()
    assert not (recdir / "argv-junitxml.txt").exists()


def test_both_mutation_checks_spawn_the_same_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property this issue is about, stated so that re-forking the
    driver fails here: both checks build their ``mutmut run`` argv
    through the identical sequence of elements, differing only on the
    target-selector axis (``--paths-to-mutate`` alone vs. plus
    ``--use-patch-file``).

    RED before the fix: the Layer 1 block is
    ``--paths-to-mutate=mod.py, --no-progress`` and the Layer 2 block
    carries ``--tests-dir``, ``--simple-output`` and ``--runner=``, so
    the comparison fails.
    """
    _repo(tmp_path)
    recdir = put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit(
            (1, "mod.py", 2, "killed"), (2, "mod.py", 6, "killed"), (3, "mod.py", 9, "killed")
        ),
    )
    result = run_adequacy(
        tmp_path,
        mutation_testing=True,
        enabled=True,
        patch_coverage=True,
        diff_mutation=True,
    )
    assert result.passed is True

    argv_lines = (recdir / "argv-run.txt").read_text(encoding="utf-8").splitlines()
    blocks = _argv_blocks(argv_lines)
    assert len(blocks) == 2

    assert _normalised(blocks[0]) == _normalised(blocks[1])
    assert _normalised(blocks[0]) == [
        "--tests-dir=empty-tests",
        "--no-progress",
        "--simple-output",
        _EXPECTED_RUNNER,
    ]
    patch_blocks = [b for b in blocks if any(e.startswith("--use-patch-file=") for e in b)]
    assert len(patch_blocks) == 1
    for block in blocks:
        assert "--paths-to-mutate=mod.py" in block


def test_a_test_command_mutmuts_runner_cannot_wrap_is_tool_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6, B2 (#391 simplify pass on PR #392): ``[verify] mutation_testing``
    now requires a ``test_command`` mutmut's ``--runner`` can wrap, and
    this is the one case NEITHER mutation-driver test file reached
    directly before this test existed. Layer 2 cannot reach it either:
    ``AdequacyConfig`` refuses ``diff_mutation=true`` with
    ``patch_coverage=false``, and ``check_patch_coverage`` validates the
    identical ``test_command`` through its OWN ``_pytest_tokens_or_gap``
    call first, so a bad command always gaps ``patch_coverage`` before
    ``_diff_mutation_preflight`` is ever reached -
    ``test_the_layer_one_gap_reason_is_inherited_not_guessed`` in
    ``tests/test_diff_mutation.py`` is the direct evidence for that
    inheritance. Layer 1 has no such intermediary: its own
    ``_mutmut_tool_preflight`` is the FIRST thing ``check_mutation_score``
    calls, so this is reachable, and no test claimed it."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(tmp_path, test_command="true")
    no_spawn_gap(result, recdir, "mutation_testing", "tool_missing", "mutmut's runner can wrap")


def test_a_failing_test_suite_refuses_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1 (#391 simplify pass on PR #392): ``[verify] test_suite`` already
    failed, so mutmut's own baseline run would only run the suite a third
    time to abort - this check now carries the byte-for-byte identical
    guard R8.5 Layer 2's ``_diff_mutation_checks`` already had, since #391
    is the change that made the two reach mutmut through ONE driver and
    the guards were the only asymmetry left. ``FEAT_MOD``'s
    ``covered_before`` is changed to break ``FEAT_TEST``'s own
    ``test_before`` assertion, without touching any of the lines this
    file's other tests mutate."""
    broken_mod = FEAT_MOD.replace("return n + 1", "return n + 2")
    _repo(tmp_path, files={"mod.py": broken_mod, "test_mod.py": FEAT_TEST})
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = _run(tmp_path)
    test_row = next(c for c in result.checks if c.name == "test_suite")
    assert test_row.passed is False
    no_spawn_gap(result, recdir, "mutation_testing", "command_failed", "test_suite")


def test_layer_ones_own_share_of_the_cap_refuses_before_spending_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1's second refusal (#391 simplify pass on PR #392), the twin of
    ``tests/test_diff_mutation.py::test_layer_ones_own_duration_refuses_before_spending_the_cap``:
    mutmut always pays its baseline test-suite run in full before
    mutating a line - the SAME suite the coverage run just measured - so
    a cap already at or below that measured duration would certainly be
    exhausted by the baseline alone, and this check refuses before
    spending it. ``mutation_timeout=0.001`` guarantees the comparison
    without a sleep: no real pytest spawn completes in 1ms, and
    ``diff_mutation`` stays off so this isolates Layer 1's OWN share of
    the budget from A2's decrement (covered separately)."""
    _repo(tmp_path)
    recdir = put_mutmut_on_path(tmp_path, monkeypatch, junit=junit((1, "mod.py", 6, "killed")))
    result = run_adequacy(
        tmp_path,
        mutation_testing=True,
        enabled=True,
        patch_coverage=True,
        diff_mutation=False,
        mutation_timeout=0.001,
    )
    coverage_row = next(c for c in result.checks if c.name == "patch_coverage")
    assert coverage_row.passed is True
    assert coverage_row.duration_seconds >= 0.001
    # "already took" is this refusal's OWN wording, deliberately distinct
    # from `_mutmut_run_spawn`'s fallback timeout detail ("cap fired, and
    # mutmut 2.5.1 cannot report a truncated run..."): at `cap=0.001` a
    # spawn that fell through to the fallback would very likely be killed
    # before it ever wrote `argv-run.txt` too, so the file-absence half of
    # `no_spawn_gap` alone would not reliably separate "refused before any
    # spawn" from "spawned and was killed near-instantly" - the DETAIL text
    # does, deterministically.
    no_spawn_gap(result, recdir, "mutation_testing", "timed_out", "already took")


def test_layer_twos_own_spend_shrinks_layer_ones_share_of_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2 (#391 simplify pass on PR #392): ONE phase-level mutation
    budget, not two independent copies of ``[verify] mutation_timeout``.
    Both checks share the identical fake mutmut on PATH, so a ``sleep=``
    on its ``run`` subcommand costs real wall-clock on EVERY invocation,
    Layer 2's included - Layer 2 runs first
    (``run_mechanical_verification`` calls ``_diff_mutation_checks``
    before ``_mutation_checks``, pinned by
    ``test_both_mutation_checks_spawn_the_same_driver``'s own comment) and
    is bounded by the FULL ``mutation_timeout``, so at ``mutation_timeout
    = 12.0`` and ``sleep = 7`` it spends about 7s and change of that cap
    before Layer 1 even starts. Under the correct, decremented budget
    Layer 1 has under 5s left, less than the fake's own 7s sleep, so its
    run times out. Under the bug this test catches - each check getting
    the FULL 12.0s again - Layer 1's run would finish inside its own
    budget and score normally instead.
    """
    _repo(tmp_path)
    put_mutmut_on_path(
        tmp_path,
        monkeypatch,
        junit=junit(
            (1, "mod.py", 2, "killed"), (2, "mod.py", 6, "killed"), (3, "mod.py", 9, "killed")
        ),
        sleep=7,
    )
    start = time.monotonic()
    result = run_adequacy(
        tmp_path,
        mutation_testing=True,
        enabled=True,
        patch_coverage=True,
        diff_mutation=True,
        mutation_timeout=12.0,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 40
    only_gap(result, "mutation_testing", "timed_out")
    diff_mutation_row = next(c for c in result.checks if c.name == "diff_mutation")
    assert diff_mutation_row.passed is True
