"""R5.1/R5.5: unit tests for the calibration tooling in kstrl.calibration.

No LLM calls anywhere in this file. These tests prove:

- consistency math (errored runs excluded from the denominator,
  majority threshold gating);
- the v2 report format (per-fixture runs, per-role and per-category
  rates, model id) and its roundtrip through save/load;
- v1 baseline normalization, including against the real v1 files
  checked into tests/adversarial_fixtures/_results/;
- the compare tool's codified thresholds: regression detected (role
  drop, category drop, absolute floor), improvement passes, partial
  runs and cross-model comparisons warn instead of failing;
- the CLI exit-code contract (0 pass / 1 regression / 2 load error);
- the R5.5 model-drift helper that backs the always-run warning test.
"""

from __future__ import annotations

import json
from pathlib import Path

from kstrl import calibration
from kstrl.calibration import Baseline, FixtureStats

REPO_RESULTS_DIR = (
    Path(__file__).parent / "adversarial_fixtures" / "_results"
)


def _fixture(
    role: str,
    fixture_id: str,
    runs_detected: int,
    *,
    runs_total: int = 3,
    runs_errored: int = 0,
    category: str | None = None,
    cwe: str | None = None,
) -> FixtureStats:
    return FixtureStats(
        role=role,
        fixture_id=fixture_id,
        category=category,
        cwe=cwe,
        runs_total=runs_total,
        runs_errored=runs_errored,
        runs_detected=runs_detected,
    )


def _baseline(*fixtures: FixtureStats, model: str = "haiku") -> Baseline:
    return Baseline(
        path=None,
        model=model,
        timestamp="20260718-000000",
        format_version=2,
        runs_per_fixture=3,
        fixtures=fixtures,
    )


# ---------------------------------------------------------------------------
# Consistency math
# ---------------------------------------------------------------------------


class TestConsistencyMath:
    def test_consistency_is_detected_over_completed(self) -> None:
        assert calibration.consistency(2, 3) == 2 / 3
        assert calibration.consistency(0, 3) == 0.0
        assert calibration.consistency(3, 3) == 1.0

    def test_zero_completed_runs_yield_zero(self) -> None:
        assert calibration.consistency(0, 0) == 0.0

    def test_errored_runs_excluded_from_denominator(self) -> None:
        """1 caught + 1 missed + 1 agent error = consistency 1/2, and
        1/2 meets the majority threshold, so infra noise cannot turn a
        detected fixture into a miss."""
        fixture = _fixture("security", "sec-x", 1, runs_total=3, runs_errored=1)
        assert fixture.runs_completed == 2
        assert fixture.consistency == 0.5
        assert fixture.detected

    def test_minority_detection_is_not_detected(self) -> None:
        assert not _fixture("architect", "spec-x", 1, runs_total=3).detected
        assert _fixture("architect", "spec-x", 2, runs_total=3).detected

    def test_all_runs_errored_is_not_detected(self) -> None:
        fixture = _fixture(
            "security", "sec-x", 0, runs_total=3, runs_errored=3,
        )
        assert not fixture.detected

    def test_role_detection_rate_is_mean_consistency(self) -> None:
        rate = calibration.role_detection_rate([
            _fixture("architect", "a", 3),  # 1.0
            _fixture("architect", "b", 1),  # 1/3
        ])
        assert rate == (1.0 + 1 / 3) / 2

    def test_role_detection_rate_empty_is_zero(self) -> None:
        assert calibration.role_detection_rate([]) == 0.0


# ---------------------------------------------------------------------------
# Report building + save/load roundtrip
# ---------------------------------------------------------------------------


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


class TestBuildReport:
    def _report(self) -> dict:
        records = [
            # sec-a: 3 runs, 2 caught -> consistency 2/3, detected
            _run_record("security", "sec-a", True, category="injection",
                        cwe="CWE-89"),
            _run_record("security", "sec-a", True, category="injection",
                        cwe="CWE-89"),
            _run_record("security", "sec-a", False, category="injection",
                        cwe="CWE-89"),
            # sec-b: 1 caught, 1 missed, 1 errored -> consistency 1/2
            _run_record("security", "sec-b", True, category="auth_bypass",
                        cwe="CWE-347"),
            _run_record("security", "sec-b", False, category="auth_bypass",
                        cwe="CWE-347"),
            _run_record("security", "sec-b", False, error=True,
                        category="auth_bypass", cwe="CWE-347"),
            # architect fixture, no cwe
            _run_record("architect", "spec-a", False, category="spec_issues"),
            _run_record("architect", "spec-a", False, category="spec_issues"),
            _run_record("architect", "spec-a", True, category="spec_issues"),
        ]
        return calibration.build_report(
            records, model="haiku", timestamp="20260718-120000",
            runs_per_fixture=3,
        )

    def test_report_header_records_model_and_format(self) -> None:
        report = self._report()
        assert report["format_version"] == calibration.REPORT_FORMAT_VERSION
        assert report["model"] == "haiku"
        assert report["runs_per_fixture"] == 3

    def test_per_fixture_consistency_and_detail_kept(self) -> None:
        report = self._report()
        by_id = {f["fixture_id"]: f for f in report["fixtures"]}
        assert by_id["sec-a"]["runs_detected"] == 2
        assert by_id["sec-a"]["consistency"] == 2 / 3
        assert by_id["sec-a"]["detected"] is True
        assert len(by_id["sec-a"]["runs"]) == 3
        # errored run excluded from denominator
        assert by_id["sec-b"]["runs_errored"] == 1
        assert by_id["sec-b"]["consistency"] == 0.5
        # minority detection is not detected
        assert by_id["spec-a"]["consistency"] == 1 / 3
        assert by_id["spec-a"]["detected"] is False

    def test_role_summary_rates(self) -> None:
        report = self._report()
        security = report["summary"]["security"]
        assert security["fixtures_total"] == 2
        assert security["fixtures_detected"] == 2
        assert security["detection_rate"] == (2 / 3 + 0.5) / 2
        architect = report["summary"]["architect"]
        assert architect["fixtures_detected"] == 0

    def test_per_category_and_per_cwe_rates(self) -> None:
        report = self._report()
        by_category = report["summary"]["security"]["by_category"]
        assert by_category["injection"]["detection_rate"] == 2 / 3
        assert by_category["auth_bypass"]["detection_rate"] == 0.5
        by_cwe = report["summary"]["security"]["by_cwe"]
        assert by_cwe["CWE-89"]["detection_rate"] == 2 / 3
        assert "by_cwe" not in report["summary"]["architect"]

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        report = self._report()
        saved = calibration.save_report(report, tmp_path)
        assert saved.name == "baseline-20260718-120000.json"
        loaded = calibration.load_baseline(saved)
        assert loaded.format_version == 2
        assert loaded.model == "haiku"
        assert loaded.runs_per_fixture == 3
        rates = loaded.role_rates()
        assert rates["security"] == (2 / 3 + 0.5) / 2
        assert loaded.category_rates()["security"]["injection"] == 2 / 3


class TestLoadV1Baseline:
    def test_v1_fixture_normalizes_to_single_run(self, tmp_path: Path) -> None:
        v1 = {
            "model": "haiku",
            "timestamp": "20260527-161822",
            "summary": {},
            "fixtures": [
                {"role": "architect", "fixture_id": "spec-01",
                 "caught": True, "detail": "..."},
                {"role": "architect", "fixture_id": "spec-02",
                 "caught": False, "detail": "..."},
            ],
        }
        path = tmp_path / "baseline-20260527-161822.json"
        path.write_text(json.dumps(v1))
        baseline = calibration.load_baseline(path)
        assert baseline.format_version == 1
        assert baseline.runs_per_fixture == 1
        assert baseline.role_rates() == {"architect": 0.5}
        # v1 predates category recording
        assert baseline.category_rates() == {}

    def test_loads_real_checked_in_v1_baselines(self) -> None:
        """The three recorded 20260527 baselines must stay loadable so
        the first new-format capture can be compared against them."""
        path = REPO_RESULTS_DIR / "baseline-20260527-161822.json"
        baseline = calibration.load_baseline(path)
        assert baseline.model == "haiku"
        rates = baseline.role_rates()
        assert rates["security"] == 1.0
        assert rates["reviewer"] == 1.0
        assert rates["architect"] == 2 / 3

    def test_malformed_baseline_raises_value_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "baseline-x.json"
        bad.write_text("{not json")
        try:
            calibration.load_baseline(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for malformed JSON")


# ---------------------------------------------------------------------------
# Comparison thresholds
# ---------------------------------------------------------------------------


class TestCompareBaselines:
    def test_improvement_passes(self) -> None:
        old = _baseline(_fixture("architect", "spec-01", 2),
                        _fixture("architect", "spec-02", 2))
        new = _baseline(_fixture("architect", "spec-01", 3),
                        _fixture("architect", "spec-02", 3))
        comparison = calibration.compare_baselines(old, new)
        assert comparison.passed
        assert comparison.failures == ()

    def test_role_drop_beyond_threshold_fails(self) -> None:
        # 1.0 -> 2/3 is a drop of 1/3 > MAX_ROLE_DETECTION_DROP
        old = _baseline(_fixture("architect", "spec-01", 3),
                        _fixture("architect", "spec-02", 3),
                        _fixture("architect", "spec-03", 3))
        new = _baseline(_fixture("architect", "spec-01", 3),
                        _fixture("architect", "spec-02", 3),
                        _fixture("architect", "spec-03", 0))
        comparison = calibration.compare_baselines(old, new)
        assert not comparison.passed
        assert any("architect" in f and "dropped" in f
                   for f in comparison.failures)
        assert "architect/spec-03" in comparison.newly_missed

    def test_single_run_flip_is_within_tolerance(self) -> None:
        """One run flipping on one fixture (drop 1/9 ~ 0.11) is
        run-to-run variance, not a regression."""
        old = _baseline(_fixture("architect", "spec-01", 3),
                        _fixture("architect", "spec-02", 3),
                        _fixture("architect", "spec-03", 3))
        new = _baseline(_fixture("architect", "spec-01", 3),
                        _fixture("architect", "spec-02", 3),
                        _fixture("architect", "spec-03", 2))
        comparison = calibration.compare_baselines(old, new)
        assert comparison.passed

    def test_new_rate_below_floor_fails_even_without_drop(self) -> None:
        """The absolute floor stops a slow slide across successive
        comparisons: old and new are equally bad, but the floor trips."""
        old = _baseline(_fixture("security", "sec-01", 1),
                        _fixture("security", "sec-02", 1))
        new = _baseline(_fixture("security", "sec-01", 1),
                        _fixture("security", "sec-02", 1))
        comparison = calibration.compare_baselines(old, new)
        assert not comparison.passed
        assert any("floor" in f for f in comparison.failures)

    def test_category_drop_beyond_threshold_fails(self) -> None:
        """Category cat-a drops 1.0 -> 1/3 (drop 2/3 > 0.40) while the
        role-level mean (drop 0.13, rate 0.87) stays inside both the
        role allowance and the floor: the per-category gate catches a
        regression the role-level averages would hide."""
        old = _baseline(*[
            _fixture("reviewer", f"c-{i}", 3, category=f"cat-{i}")
            for i in "abcde"
        ])
        new = _baseline(
            _fixture("reviewer", "c-a", 1, category="cat-a"),
            *[
                _fixture("reviewer", f"c-{i}", 3, category=f"cat-{i}")
                for i in "bcde"
            ],
        )
        comparison = calibration.compare_baselines(old, new)
        assert not comparison.passed
        assert any("reviewer/cat-a" in f for f in comparison.failures)
        assert not any("floor" in f or "role" in f for f in comparison.failures)

    def test_category_single_run_flip_tolerated(self) -> None:
        old = _baseline(
            _fixture("security", "sec-01", 3, category="injection"),
            _fixture("security", "sec-02", 3, category="hardcoded_secret"),
            _fixture("security", "sec-03", 3, category="auth_bypass"),
        )
        new = _baseline(
            _fixture("security", "sec-01", 2, category="injection"),
            _fixture("security", "sec-02", 3, category="hardcoded_secret"),
            _fixture("security", "sec-03", 3, category="auth_bypass"),
        )
        comparison = calibration.compare_baselines(old, new)
        assert comparison.passed

    def test_role_missing_from_new_warns_but_passes(self) -> None:
        """Partial runs are legitimate (e.g. architect-only re-run
        after a DECOMPOSE_PROMPT edit): warn, do not fail."""
        old = _baseline(_fixture("security", "sec-01", 3),
                        _fixture("architect", "spec-01", 3))
        new = _baseline(_fixture("architect", "spec-01", 3))
        comparison = calibration.compare_baselines(old, new)
        assert comparison.passed
        assert any("not exercised" in w for w in comparison.warnings)

    def test_cross_model_comparison_warns(self) -> None:
        old = _baseline(_fixture("security", "sec-01", 3), model="haiku")
        new = _baseline(_fixture("security", "sec-01", 3), model="sonnet")
        comparison = calibration.compare_baselines(old, new)
        assert comparison.passed
        assert any("H2-extended" in w for w in comparison.warnings)

    def test_v1_to_v2_comparison_works(self, tmp_path: Path) -> None:
        """The first new-format capture will be compared against a
        checked-in v1 baseline; that path must work end to end."""
        old = calibration.load_baseline(
            REPO_RESULTS_DIR / "baseline-20260527-161822.json",
        )
        new = _baseline(
            _fixture("security", "sec-01-sql-injection", 3),
            _fixture("security", "sec-02-command-injection", 3),
            _fixture("security", "sec-03-hardcoded-secret", 3),
            _fixture("security", "sec-04-predictable-token", 3),
            _fixture("security", "sec-05-broken-jwt-verify", 3),
            _fixture("reviewer", "concern-01-dead-code", 3),
            _fixture("reviewer", "concern-02-tautological-test", 3),
            _fixture("reviewer", "concern-03-scope-creep", 3),
            _fixture("architect", "spec-01-no-error-handling", 3),
            _fixture("architect", "spec-02-unspecified-auth", 3),
            _fixture("architect", "spec-03-ambiguous-perf", 3),
        )
        comparison = calibration.compare_baselines(old, new)
        assert comparison.passed

    def test_format_comparison_mentions_verdict_and_rates(self) -> None:
        old = _baseline(_fixture("architect", "spec-01", 3))
        new = _baseline(_fixture("architect", "spec-01", 0))
        text = calibration.format_comparison(
            calibration.compare_baselines(old, new),
        )
        assert "FAIL" in text
        assert "architect" in text

    def test_min_role_rate_falls_back_to_default(self) -> None:
        assert (
            calibration.min_role_rate("brand_new_role")
            == calibration.DEFAULT_MIN_ROLE_DETECTION_RATE
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCompareCli:
    def _write(self, tmp_path: Path, name: str, fixtures: list[dict]) -> Path:
        report = calibration.build_report(
            fixtures, model="haiku", timestamp=name, runs_per_fixture=3,
        )
        return calibration.save_report(report, tmp_path / name)

    def test_regression_exits_1(self, tmp_path: Path, capsys) -> None:
        old = self._write(tmp_path, "old", [
            _run_record("architect", "spec-01", True) for _ in range(3)
        ])
        new = self._write(tmp_path, "new", [
            _run_record("architect", "spec-01", False) for _ in range(3)
        ])
        code = calibration.main(["compare", str(old), str(new)])
        assert code == 1
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_improvement_exits_0(self, tmp_path: Path, capsys) -> None:
        records_old = [
            _run_record("architect", "spec-01", i > 0) for i in range(3)
        ]
        records_new = [
            _run_record("architect", "spec-01", True) for _ in range(3)
        ]
        old = self._write(tmp_path, "old", records_old)
        new = self._write(tmp_path, "new", records_new)
        code = calibration.main(["compare", str(old), str(new)])
        assert code == 0
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_unreadable_baseline_exits_2(self, tmp_path: Path, capsys) -> None:
        good = self._write(tmp_path, "old", [
            _run_record("architect", "spec-01", True) for _ in range(3)
        ])
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
        self, results_dir: Path, timestamp: str, model: str,
    ) -> Path:
        report = calibration.build_report(
            [_run_record("architect", "spec-01", True)],
            model=model, timestamp=timestamp, runs_per_fixture=1,
        )
        return calibration.save_report(report, results_dir)

    def test_no_results_dir_is_silent(self, tmp_path: Path) -> None:
        assert calibration.model_drift_message(
            tmp_path / "missing", "haiku",
        ) is None

    def test_no_baselines_is_silent(self, tmp_path: Path) -> None:
        assert calibration.model_drift_message(tmp_path, "haiku") is None

    def test_matching_model_is_silent(self, tmp_path: Path) -> None:
        self._write_baseline(tmp_path, "20260718-000000", "haiku")
        assert calibration.model_drift_message(tmp_path, "haiku") is None

    def test_differing_model_warns_citing_h2(self, tmp_path: Path) -> None:
        self._write_baseline(tmp_path, "20260718-000000", "haiku")
        message = calibration.model_drift_message(tmp_path, "sonnet")
        assert message is not None
        assert "H2-extended" in message
        assert "haiku" in message and "sonnet" in message

    def test_newest_baseline_by_filename_wins(self, tmp_path: Path) -> None:
        """Filename timestamps sort chronologically; only the newest
        baseline's model matters."""
        self._write_baseline(tmp_path, "20260101-000000", "sonnet")
        self._write_baseline(tmp_path, "20260718-000000", "haiku")
        assert calibration.model_drift_message(tmp_path, "haiku") is None
        assert calibration.model_drift_message(tmp_path, "sonnet") is not None

    def test_malformed_newest_baseline_is_silent(self, tmp_path: Path) -> None:
        """The always-run structural test must never fail on a corrupt
        results file: warn-path only, silence on unreadable input."""
        (tmp_path / "baseline-99999999-999999.json").write_text("{not json")
        assert calibration.model_drift_message(tmp_path, "haiku") is None

    def test_repo_baselines_match_default_model(self) -> None:
        """The checked-in baselines were captured with the default
        calibration model; if this fails, someone changed the default
        without re-calibrating (H2-extended)."""
        assert calibration.model_drift_message(
            REPO_RESULTS_DIR, "haiku",
        ) is None
