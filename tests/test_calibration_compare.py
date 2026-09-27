"""R5.1/R5.5: the calibration tooling in kstrl.calibration, on real files.

No LLM calls anywhere in this file. These tests prove:

- the compare CLI's exit-code contract (0 pass / 1 regression / 2 load
  error) over reports `save_report` wrote to disk;
- v1 baseline normalization, including against the real v1 files
  checked into tests/adversarial_fixtures/_results/;
- the R5.5 model-drift helper that backs the always-run warning test,
  over results directories on disk and the checked-in baselines.

The consistency math and the comparison thresholds (role drop, category
drop, absolute floor, single-run flip tolerance) are carried by
``tests/test_calibration_ladder.py`` running ``compare --root`` over
hand-written baselines.
"""

from __future__ import annotations

import json
from pathlib import Path

from kstrl import calibration, calibration_baseline

REPO_RESULTS_DIR = Path(__file__).parent / "adversarial_fixtures" / "_results"


def _run_record(
    role: str,
    fixture_id: str,
    caught: bool,
    *,
    error: bool = False,
    category: str | None = None,
    cwe: str | None = None,
) -> dict:
    return {
        "role": role,
        "fixture_id": fixture_id,
        "category": category,
        "cwe": cwe,
        "caught": caught,
        "error": error,
        "detail": "synthetic",
    }


class TestLoadV1Baseline:
    def test_v1_fixture_normalizes_to_single_run(self, tmp_path: Path) -> None:
        v1 = {
            "model": "haiku",
            "timestamp": "20260527-161822",
            "summary": {},
            "fixtures": [
                {"role": "architect", "fixture_id": "spec-01", "caught": True, "detail": "..."},
                {"role": "architect", "fixture_id": "spec-02", "caught": False, "detail": "..."},
            ],
        }
        path = tmp_path / "baseline-20260527-161822.json"
        path.write_text(json.dumps(v1))
        baseline = calibration_baseline.load_baseline(path)
        assert baseline.format_version == 1
        assert baseline.runs_per_fixture == 1
        assert baseline.role_rates() == {"architect": 0.5}
        # v1 predates category recording
        assert baseline.category_rates() == {}

    def test_loads_real_checked_in_v1_baselines(self) -> None:
        """The three recorded 20260527 baselines must stay loadable so
        the first new-format capture can be compared against them."""
        path = REPO_RESULTS_DIR / "baseline-20260527-161822.json"
        baseline = calibration_baseline.load_baseline(path)
        assert baseline.model == "haiku"
        rates = baseline.role_rates()
        assert rates["security"] == 1.0
        assert rates["reviewer"] == 1.0
        assert rates["architect"] == 2 / 3


class TestCompareCli:
    def _write(self, tmp_path: Path, name: str, fixtures: list[dict]) -> Path:
        report = calibration.build_report(
            fixtures,
            model="haiku",
            timestamp=name,
            runs_per_fixture=3,
        )
        return calibration.save_report(report, tmp_path / name)

    def test_regression_exits_1(self, tmp_path: Path, capsys) -> None:
        old = self._write(
            tmp_path, "old", [_run_record("architect", "spec-01", True) for _ in range(3)]
        )
        new = self._write(
            tmp_path, "new", [_run_record("architect", "spec-01", False) for _ in range(3)]
        )
        code = calibration.main(["compare", str(old), str(new)])
        assert code == 1
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_improvement_exits_0(self, tmp_path: Path, capsys) -> None:
        records_old = [_run_record("architect", "spec-01", i > 0) for i in range(3)]
        records_new = [_run_record("architect", "spec-01", True) for _ in range(3)]
        old = self._write(tmp_path, "old", records_old)
        new = self._write(tmp_path, "new", records_new)
        code = calibration.main(["compare", str(old), str(new)])
        assert code == 0
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_unreadable_baseline_exits_2(self, tmp_path: Path, capsys) -> None:
        good = self._write(
            tmp_path, "old", [_run_record("architect", "spec-01", True) for _ in range(3)]
        )
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        code = calibration.main(["compare", str(good), str(bad)])
        assert code == 2
        err = capsys.readouterr().err
        assert "error:" in err


# ---------------------------------------------------------------------------
# Model drift (R5.5)
# ---------------------------------------------------------------------------


class TestModelDrift:
    def _write_baseline(
        self,
        results_dir: Path,
        timestamp: str,
        model: str,
    ) -> Path:
        report = calibration.build_report(
            [_run_record("architect", "spec-01", True)],
            model=model,
            timestamp=timestamp,
            runs_per_fixture=1,
        )
        return calibration.save_report(report, results_dir)

    def test_no_results_dir_is_silent(self, tmp_path: Path) -> None:
        assert (
            calibration_baseline.model_drift_message(
                tmp_path / "missing",
                "haiku",
            )
            is None
        )

    def test_no_baselines_is_silent(self, tmp_path: Path) -> None:
        assert calibration_baseline.model_drift_message(tmp_path, "haiku") is None

    def test_matching_model_is_silent(self, tmp_path: Path) -> None:
        self._write_baseline(tmp_path, "20260718-000000", "haiku")
        assert calibration_baseline.model_drift_message(tmp_path, "haiku") is None

    def test_differing_model_warns_citing_h2(self, tmp_path: Path) -> None:
        self._write_baseline(tmp_path, "20260718-000000", "haiku")
        message = calibration_baseline.model_drift_message(tmp_path, "sonnet")
        assert message is not None
        assert "H2-extended" in message
        assert "haiku" in message and "sonnet" in message

    def test_newest_baseline_by_filename_wins(self, tmp_path: Path) -> None:
        """Filename timestamps sort chronologically; only the newest
        baseline's model matters."""
        self._write_baseline(tmp_path, "20260101-000000", "sonnet")
        self._write_baseline(tmp_path, "20260718-000000", "haiku")
        assert calibration_baseline.model_drift_message(tmp_path, "haiku") is None
        assert calibration_baseline.model_drift_message(tmp_path, "sonnet") is not None

    def test_malformed_newest_baseline_is_silent(self, tmp_path: Path) -> None:
        """The always-run structural test must never fail on a corrupt
        results file: warn-path only, silence on unreadable input."""
        (tmp_path / "baseline-99999999-999999.json").write_text("{not json")
        assert calibration_baseline.model_drift_message(tmp_path, "haiku") is None

    def test_repo_baselines_match_default_model(self) -> None:
        """The checked-in baselines were captured with the default
        calibration model; if this fails, someone changed the default
        without re-calibrating (H2-extended)."""
        assert (
            calibration_baseline.model_drift_message(
                REPO_RESULTS_DIR,
                "haiku",
            )
            is None
        )
