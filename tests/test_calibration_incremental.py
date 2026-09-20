"""#398: a calibration run writes as it goes, and a partial capture is refused.

End to end on purpose. A child interpreter drives the REAL capture harness
(``tests.test_calibration``'s ``_gate_on_consistency``, ``_measure_detection``,
``_measure_false_positives`` and ``_DetectionReport``) with a fake agent, so no
paid call is made and no network is touched. What the tests assert on is the
file that child left behind and what the real ``python -m kstrl.calibration
compare`` entry point does with it.

Three child runs, because a run can stop in three ways that the code answers
differently: it finishes (mode ``complete``), it is SIGKILLed so no teardown of
any kind runs (mode ``sigkill``), or it raises and teardown DOES run with a
fixture left dangling (mode ``raise``). The last one is the only test of the
dangling check, and an implementation whose ``run_complete`` is just "teardown
was reached" passes every other test in this file.

``tests.test_calibration.RESULTS_DIR`` is repointed at a temp directory inside
the child before anything writes, so nothing here can add a file to the
repository's own ``tests/adversarial_fixtures/_results/``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from kstrl import calibration

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every fixture the child drives, as ``role/fixture_id`` in run order.
#: fx-c goes through the false-positive helper, so it is recorded and
#: completed but never appears in the report's ``fixtures`` list.
FX_A, FX_B, FX_C, FX_D = "security/fx-a", "reviewer/fx-b", "reviewer/fx-c", "security/fx-d"

#: Runs the real harness against a fake agent. ``KSTRL_TEST_MODE`` decides
#: how the run ends. All four helpers are driven so that instrumenting only
#: one of the three gate helpers fails here rather than silently shipping.
_DRIVER = """
from __future__ import annotations

import os
import signal
from pathlib import Path

import tests.test_calibration as tc

tc.RESULTS_DIR = Path(os.environ["KSTRL_TEST_RESULTS_DIR"])
MODE = os.environ["KSTRL_TEST_MODE"]
report = tc._DetectionReport()


def run_once_for(fixture_id):
    def run_once():
        if fixture_id == "fx-d" and MODE == "sigkill":
            os.kill(os.getpid(), signal.SIGKILL)
        if fixture_id == "fx-d" and MODE == "raise":
            raise RuntimeError("interrupted inside fx-d")
        return True, "fake result for " + fixture_id

    return run_once


try:
    tc._gate_on_consistency(
        "security", "fx-a", report, run_once_for("fx-a"), category="injection"
    )
    tc._measure_detection(
        "reviewer", "fx-b", report, run_once_for("fx-b"), category="scope_creep"
    )
    tc._measure_false_positives("reviewer", "fx-c", report, run_once_for("fx-c"))
    tc._gate_on_consistency(
        "security", "fx-d", report, run_once_for("fx-d"), category="injection"
    )
finally:
    report.save()
"""

#: What each mode's child exits with. ``sigkill`` cannot be caught, so the
#: child dies on the signal and ``subprocess`` reports the negated number.
_EXPECTED_EXIT = {"complete": 0, "sigkill": -signal.SIGKILL, "raise": 1}


def _capture(tmp_dir: Path, mode: str) -> Path:
    """Run one fake capture in a child and return its single baseline file.

    The child spawns nothing itself, so ``subprocess.run(timeout=)`` bounds
    the only process there is, and a mutation that removes a write shows up
    as a failed assertion rather than as a hang.
    """
    results = tmp_dir / "results"
    results.mkdir(parents=True, exist_ok=True)
    driver = tmp_dir / "capture_driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["KSTRL_TEST_RESULTS_DIR"] = str(results)
    env["KSTRL_TEST_MODE"] = mode
    # Pinned rather than inherited: the report's model label and the run
    # count both come from the environment, so a developer's exported value
    # would otherwise change what these tests are looking at.
    env["KSTRL_CALIBRATION_RUNS"] = "2"
    env["KSTRL_CALIBRATION_MODEL"] = "haiku"
    env["KSTRL_CALIBRATION_CHANGE_SOURCE"] = "repo"
    env["KSTRL_RUN_CALIBRATION"] = "0"
    env.pop("KSTRL_CALIBRATION_REVIEWER_AGENT_TYPE", None)
    env.pop("KSTRL_CALIBRATION_REVIEWER_MODEL", None)

    proc = subprocess.run(
        [sys.executable, str(driver)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == _EXPECTED_EXIT[mode], (
        f"child exit {proc.returncode}, expected {_EXPECTED_EXIT[mode]} for mode "
        f"{mode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    files = sorted(results.glob("baseline-*.json"))
    assert len(files) == 1, (
        f"a run owns exactly one file; found {[p.name for p in files]}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return files[0]


@pytest.fixture(scope="module")
def killed_capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The artifact a real capture leaves when SIGKILL lands inside fx-d."""
    return _capture(tmp_path_factory.mktemp("killed"), "sigkill")


@pytest.fixture(scope="module")
def raised_capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The artifact a capture leaves when it raises inside fx-d and its
    teardown still runs."""
    return _capture(tmp_path_factory.mktemp("raised"), "raise")


@pytest.fixture(scope="module")
def complete_capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The artifact a real capture leaves when it finishes."""
    return _capture(tmp_path_factory.mktemp("complete"), "complete")


def test_a_killed_run_keeps_every_completed_fixture(killed_capture: Path) -> None:
    data = json.loads(killed_capture.read_text(encoding="utf-8"))
    assert [f["fixture_id"] for f in data["fixtures"]] == ["fx-a", "fx-b"]
    assert [f["runs_detected"] for f in data["fixtures"]] == [2, 2]
    assert data["fixtures_completed"] == [FX_A, FX_B, FX_C]
    assert data["fixtures_attempted"] == [FX_A, FX_B, FX_C, FX_D]
    assert data["run_complete"] is False
    # An unmeasured fixture is unmeasured, not a score of 0.0: fx-d is named
    # in fixtures_attempted and appears nowhere in fixtures.
    assert "fx-d" not in {f["fixture_id"] for f in data["fixtures"]}


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


def test_load_baseline_refuses_the_partial_capture(killed_capture: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        calibration.load_baseline(killed_capture)
    message = str(excinfo.value)
    assert "partial" in message
    assert FX_D in message


def test_compare_refuses_the_partial_capture_and_emits_no_delta(
    killed_capture: Path,
    complete_capture: Path,
) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kstrl.calibration",
            "compare",
            str(complete_capture),
            str(killed_capture),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 2, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "partial" in proc.stderr
    assert FX_D in proc.stderr
    # No delta of any kind reached stdout.
    assert "->" not in proc.stdout
    assert "PASS" not in proc.stdout
    assert "FAIL" not in proc.stdout


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
