"""#398: a calibration run writes as it goes, and a partial capture is refused.

End to end on purpose. A child interpreter drives the REAL capture harness
(``tests.test_calibration``'s ``_gate_on_consistency``, ``_measure_detection``,
``_measure_false_positives`` and ``_DetectionReport``) with a fake agent, so no
paid call is made and no network is touched. What the tests assert on is the
file that child left behind and what the real ``kstrl.calibration.main``
compare entry point does with it. The driver itself lives in
``tests/helpers/calibration_capture.py`` (#398 simplify pass, group B2); see
its module docstring for why a shared driver belongs there rather than here.

Four child runs, because a run can stop in four ways that the code answers
differently: it finishes (mode ``complete``), it is SIGKILLed mid-fixture so
no teardown of any kind runs (mode ``sigkill``), it raises and teardown DOES
run with a fixture left dangling (mode ``raise``), or it is SIGKILLed right
after the last fixture completes and before teardown (mode
``kill_after_last``). The last one is the only test of ``complete_fixture``'s
own flush (#398 A3): every other mode passes whether or not that flush ever
ran, because either the next fixture's ``begin_fixture`` flush catches up
(not the last fixture) or ``report.save()`` at teardown does (``complete``).

``tests.test_calibration.RESULTS_DIR`` is repointed at a temp directory inside
the child before anything writes, so nothing here can add a file to the
repository's own ``tests/adversarial_fixtures/_results/``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kstrl import calibration
from tests.helpers import calibration_capture as harness
from tests.helpers.calibration_capture import FX_A, FX_B, FX_C, FX_D

COMMITTED_RESULTS_DIR = Path(__file__).resolve().parent / "adversarial_fixtures" / "_results"


@pytest.fixture(scope="module")
def killed_capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The artifact a real capture leaves when SIGKILL lands inside fx-d."""
    return harness.capture(tmp_path_factory.mktemp("killed"), "sigkill")


@pytest.fixture(scope="module")
def raised_capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The artifact a capture leaves when it raises inside fx-d and its
    teardown still runs."""
    return harness.capture(tmp_path_factory.mktemp("raised"), "raise")


@pytest.fixture(scope="module")
def complete_capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The artifact a real capture leaves when it finishes."""
    return harness.capture(tmp_path_factory.mktemp("complete"), "complete")


@pytest.fixture(scope="module")
def kill_after_last_capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The artifact a capture leaves when SIGKILL lands immediately after
    fx-d's own ``complete_fixture`` call returns, before teardown."""
    return harness.capture(tmp_path_factory.mktemp("kill_after_last"), "kill_after_last")


def test_a_killed_run_keeps_every_completed_fixture(killed_capture: Path) -> None:
    """fx-a and fx-b finish outright. fx-d is killed on its SECOND (of two)
    runs, so this is also the proof of #398 A1: the FIRST run already
    reached disk through ``record``'s own flush before the kill, which is
    exactly the "RUNS-1 survives" guarantee the issue asked for - it shows
    up here with ``runs_total=1``, not absent and not zeroed. fx-d still
    counts as dangling rather than finished: ``complete_fixture`` never ran
    for it, so it stays out of ``fixtures_completed`` and the whole baseline
    stays partial."""
    data = json.loads(killed_capture.read_text(encoding="utf-8"))
    assert [f["fixture_id"] for f in data["fixtures"]] == ["fx-a", "fx-b", "fx-d"]
    assert [f["runs_total"] for f in data["fixtures"]] == [2, 2, 1]
    assert [f["runs_detected"] for f in data["fixtures"]] == [2, 2, 1]
    assert data["fixtures_completed"] == [FX_A, FX_B, FX_C]
    assert data["fixtures_attempted"] == [FX_A, FX_B, FX_C, FX_D]
    assert data["run_complete"] is False


def test_all_three_gate_helpers_record_their_fixture(complete_capture: Path) -> None:
    """fx-b goes through ``_measure_detection`` and fx-c through
    ``_measure_false_positives``. Instrumenting only ``_gate_on_consistency``
    leaves both out of the progress keys, and only this fails."""
    data = json.loads(complete_capture.read_text(encoding="utf-8"))
    assert data["fixtures_attempted"] == [FX_A, FX_B, FX_C, FX_D]
    assert data["fixtures_completed"] == [FX_A, FX_B, FX_C, FX_D]
    assert data["run_complete"] is True
    # fx-c was a negative fixture, so it is complete without being a row in
    # the detection table.
    assert [f["fixture_id"] for f in data["fixtures"]] == ["fx-a", "fx-b", "fx-d"]
    assert "reviewer" in data["false_positive_analysis"]["roles"]


def test_a_run_that_reached_teardown_with_a_dangling_fixture_is_partial(
    raised_capture: Path,
) -> None:
    """Teardown ran, so ``_finished`` is True; fx-d was attempted and never
    completed, so the run is still partial. A ``run_complete`` that is only
    "teardown was reached" passes every other test here and fails this one."""
    data = json.loads(raised_capture.read_text(encoding="utf-8"))
    assert data["run_complete"] is False
    assert data["fixtures_attempted"] == [FX_A, FX_B, FX_C, FX_D]
    assert data["fixtures_completed"] == [FX_A, FX_B, FX_C]
    with pytest.raises(ValueError, match="partial"):
        calibration.load_baseline(raised_capture)


def test_a_kill_right_after_the_last_fixture_completes_still_records_it(
    kill_after_last_capture: Path,
) -> None:
    """#398 A3. ``fx-d``'s runs already reached disk through ``record``'s own
    flush (A1) before this kill; what is NOT yet guaranteed is that fx-d
    moved from attempted to completed, which only ``complete_fixture``'s own
    flush writes - there is no next fixture's ``begin_fixture`` to catch up
    for it, and teardown never runs. Deleting that flush call turns this
    genuinely-complete fixture into one wrongly reported as dangling."""
    data = json.loads(kill_after_last_capture.read_text(encoding="utf-8"))
    assert data["fixtures_attempted"] == [FX_A, FX_B, FX_C, FX_D]
    assert data["fixtures_completed"] == [FX_A, FX_B, FX_C, FX_D]
    # Teardown (report.save(), which sets _finished) never ran: the kill
    # landed before the `finally` block's report.save() call.
    assert data["run_complete"] is False
    assert [f["fixture_id"] for f in data["fixtures"]] == ["fx-a", "fx-b", "fx-d"]


def test_load_baseline_refuses_the_partial_capture(killed_capture: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        calibration.load_baseline(killed_capture)
    message = str(excinfo.value)
    assert "partial" in message
    assert FX_D in message


def test_compare_refuses_the_partial_capture_and_emits_no_delta(
    killed_capture: Path,
    complete_capture: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#398 B1: in-process through ``calibration.main``, the house pattern
    used at three sites in ``tests/test_calibration_compare.py`` and 20+
    times via ``tests/helpers/demotion.py::run_compare`` - no subprocess, no
    child ~30 ms, for a call this process can make directly."""
    code = calibration.main(["compare", str(complete_capture), str(killed_capture)])
    assert code == 2
    out, err = capsys.readouterr()
    assert "partial" in err
    assert FX_D in err
    # No delta of any kind reached stdout.
    assert "->" not in out
    assert "PASS" not in out
    assert "FAIL" not in out


def test_a_completed_run_loads(complete_capture: Path) -> None:
    baseline = calibration.load_baseline(complete_capture)
    assert [f.fixture_id for f in baseline.fixtures] == ["fx-a", "fx-b", "fx-d"]
    assert baseline.model == "haiku"


def test_newest_baseline_path_skips_the_partial_capture(
    tmp_path: Path,
    killed_capture: Path,
    complete_capture: Path,
) -> None:
    """The partial file is the newest by filename and must not be treated as
    the current baseline: if it were, ``model_drift_message`` would swallow
    the refusal and the H2-extended drift warning would go silent."""
    older = tmp_path / "baseline-20260101-000000.json"
    newer = tmp_path / "baseline-20991231-235959.json"
    shutil.copy(complete_capture, older)
    shutil.copy(killed_capture, newer)
    assert calibration.newest_baseline_path(tmp_path) == older
    assert calibration.model_drift_message(tmp_path, "haiku") is None
    assert calibration.model_drift_message(tmp_path, "sonnet") is not None


def test_a_newest_baseline_that_is_not_json_is_returned_not_skipped(tmp_path: Path) -> None:
    """Skipping is the CLEARING direction, so it stays narrow: only a file
    this can read and that positively says it is partial is skipped. A
    corrupt newest file keeps its existing behaviour."""
    corrupt = tmp_path / "baseline-99999999-999999.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert calibration.newest_baseline_path(tmp_path) == corrupt
    assert calibration.model_drift_message(tmp_path, "haiku") is None


def test_a_newest_baseline_that_will_not_decode_is_returned_not_raised(
    tmp_path: Path,
) -> None:
    """``UnicodeDecodeError`` is a ``ValueError``, so narrowing the skip's
    handler to ``json.JSONDecodeError`` lets it out of ``newest_baseline_path``
    and past ``model_drift_message``'s ``except ValueError``, which sits
    around ``load_baseline`` only. That takes down every caller of the drift
    check instead of warning."""
    bad = tmp_path / "baseline-99999999-999999.json"
    bad.write_bytes(b'{"model": "\xe9\xe9", "fixtures": []}')
    assert calibration.newest_baseline_path(tmp_path) == bad
    assert calibration.model_drift_message(tmp_path, "haiku") is None


def test_save_report_writes_through_atomicio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#398 rewrites one file many times per run, so the write must be the
    atomic one. This pins the CALL, and says so: both writers' visible output
    is identical for a payload that lands whole, so there is no behavioural
    assertion available here and this is a shape check, not a proof."""
    calls: list[Path] = []
    real = calibration.atomic_write_json

    def spy(target: Path, payload: object) -> None:
        calls.append(target)
        real(target, payload)

    monkeypatch.setattr(calibration, "atomic_write_json", spy)
    report = calibration.build_report(
        [],
        model="haiku",
        timestamp="20260101-000000",
        runs_per_fixture=1,
    )
    out = calibration.save_report(report, tmp_path)
    assert calls == [out]


def test_every_committed_baseline_is_a_complete_capture() -> None:
    """#398 A2: the guard that catches instance N+1. Before this change a
    crashed run left no file at all; it can now leave one inside this
    git-tracked directory. A reviewer planted a partial baseline carrying a
    WRONG model here and ran the existing model-drift guard over it: it
    reported ``1 passed, 35 deselected`` because that guard routes through
    ``newest_baseline_path``, which SKIPS partials rather than refusing them
    - so the one control that reads this directory never looked at the exact
    artifact this change introduces. This reads every committed baseline
    directly and refuses to let any of them be partial."""
    baselines = sorted(COMMITTED_RESULTS_DIR.glob("baseline-*.json"))
    assert baselines, f"no committed baselines found under {COMMITTED_RESULTS_DIR}"
    partial: list[str] = []
    for path in baselines:
        data = json.loads(path.read_text(encoding="utf-8"))
        reason = calibration.partial_capture_reason(data)
        if reason is not None:
            partial.append(f"{path.name}: {reason}")
    assert partial == [], "committed baseline(s) are partial captures:\n" + "\n".join(partial)
