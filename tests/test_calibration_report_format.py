"""The v2 baseline format as a contract between its two halves (#421).

`kstrl/calibration_baseline.py` is BOTH halves of the document: its
`fixture_entry` and `baseline_document` are the only places a v2 document
key is written, and its `load_baseline` is the only place one is read back.
`kstrl/calibration.py` computes values from per-run RECORDS and calls the
two writer functions - it never spells a document key itself. This file
pins what keeps that design from drifting apart in silence, at the level
of the real CLI and the real checked-in baselines:

- `python -m kstrl.calibration compare` REFUSES (exit 2, no traceback,
  no PASS) an OLD baseline whose run counts are absent or null, whose
  `format_version`, `runs_per_fixture` or `role` is null, whose v1
  `caught` is absent, or that detected nothing and so bounds nothing
  (#421 Groups A1-A4). Zero is the fail-open direction: `compare_baselines`
  reports `newly_missed` only for a fixture the OLD baseline detected;
- every checked-in baseline under `tests/adversarial_fixtures/_results/`
  still loads, carries a boolean `caught` where it is v1, and detects at
  least one fixture;
- `save_report` writes bytes this file pins, so nothing above can have
  moved a document key or a value;
- the writer never spells a string the reader owns, so a rename in the
  reader's document literals leaves the writer visibly out of step
  instead of silently agreeing with a key nobody writes any more. The
  writer's OWN literals are per-run RECORD field names (`role`,
  `fixture_id`, `category`, `cwe`, `caught`, `error`), a contract
  documented at `build_report`'s own docstring that happens to share six
  spellings with the document without being the document.

`tests/test_calibration_compare.py` is the comparison thresholds; this is
the format.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kstrl import calibration
from kstrl.calibration_baseline import load_baseline
from tests.helpers.astwalk import (
    KSTRL_PACKAGE,
    REPO_ROOT,
    all_nodes,
    assert_census,
    folded_str,
    label,
    parsed,
)

WRITER = KSTRL_PACKAGE / "calibration.py"
READER = KSTRL_PACKAGE / "calibration_baseline.py"
RESULTS_DIR = REPO_ROOT / "tests" / "adversarial_fixtures" / "_results"

PINNED_RECORDS: list[dict[str, Any]] = [
    {
        "role": "security",
        "fixture_id": "sec-a",
        "category": "injection",
        "cwe": "CWE-89",
        "caught": True,
        "error": False,
        "detail": "hit",
    },
    {
        "role": "security",
        "fixture_id": "sec-a",
        "category": "injection",
        "cwe": "CWE-89",
        "caught": False,
        "error": True,
        "detail": "infra",
    },
    {
        "role": "architect",
        "fixture_id": "spec-a",
        "category": "spec_issues",
        "cwe": None,
        "caught": False,
        "error": False,
        "detail": "miss",
    },
]

#: What `save_report` writes for PINNED_RECORDS, byte for byte, captured on
#: main at 5db5cee before the constants change. Every one of the 27 report
#: keys appears here.
PINNED_DOCUMENT = """{
  "format_version": 2,
  "model": "haiku",
  "timestamp": "20260901-000000",
  "runs_per_fixture": 2,
  "run_complete": true,
  "fixtures_attempted": [
    "security/sec-a",
    "architect/spec-a"
  ],
  "fixtures_completed": [
    "security/sec-a",
    "architect/spec-a"
  ],
  "summary": {
    "security": {
      "fixtures_total": 1,
      "fixtures_detected": 1,
      "detection_rate": 1.0,
      "by_category": {
        "injection": {
          "fixtures_total": 1,
          "detection_rate": 1.0
        }
      },
      "by_cwe": {
        "CWE-89": {
          "fixtures_total": 1,
          "detection_rate": 1.0
        }
      }
    },
    "architect": {
      "fixtures_total": 1,
      "fixtures_detected": 0,
      "detection_rate": 0.0,
      "by_category": {
        "spec_issues": {
          "fixtures_total": 1,
          "detection_rate": 0.0
        }
      }
    }
  },
  "fixtures": [
    {
      "role": "security",
      "fixture_id": "sec-a",
      "category": "injection",
      "cwe": "CWE-89",
      "runs_total": 2,
      "runs_errored": 1,
      "runs_detected": 1,
      "consistency": 1.0,
      "detected": true,
      "runs": [
        {
          "caught": true,
          "error": false,
          "detail": "hit"
        },
        {
          "caught": false,
          "error": true,
          "detail": "infra"
        }
      ]
    },
    {
      "role": "architect",
      "fixture_id": "spec-a",
      "category": "spec_issues",
      "cwe": null,
      "runs_total": 1,
      "runs_errored": 0,
      "runs_detected": 0,
      "consistency": 0.0,
      "detected": false,
      "runs": [
        {
          "caught": false,
          "error": false,
          "detail": "miss"
        }
      ]
    }
  ]
}
"""


def _write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


class TestCheckedInBaselines:
    """The real baselines under `tests/adversarial_fixtures/_results/`,
    re-derived over every checked-in file rather than assumed."""

    def test_every_checked_in_baseline_still_loads(self) -> None:
        """Ten baselines were checked in at 5db5cee and all ten load. A
        capture only adds files, so the bound is >=, not ==."""
        found = sorted(RESULTS_DIR.glob("baseline-*.json"))
        assert len(found) >= 10
        for path in found:
            assert load_baseline(path).fixtures

    def test_no_checked_in_v1_baseline_would_be_refused(self) -> None:
        """All 18 checked-in v1 entries carry a boolean `caught`,
        re-derived here rather than assumed."""
        found = sorted(RESULTS_DIR.glob("baseline-*.json"))
        assert len(found) >= 10
        v1_checked = 0
        for path in found:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "format_version" in data:
                continue
            for entry in data["fixtures"]:
                v1_checked += 1
                assert isinstance(entry.get("caught"), bool), (path, entry)
        assert v1_checked >= 18

    def test_no_checked_in_baseline_would_be_refused_as_bounding_nothing(self) -> None:
        """Every real baseline has at least one detected fixture,
        re-derived over all ten checked-in files rather than assumed."""
        found = sorted(RESULTS_DIR.glob("baseline-*.json"))
        assert len(found) >= 10
        for path in found:
            baseline = load_baseline(path)
            assert any(f.detected for f in baseline.fixtures), path


def _hole(document: dict[str, Any]) -> None:
    for entry in document["fixtures"]:
        del entry["runs_detected"]


def _null(document: dict[str, Any]) -> None:
    for entry in document["fixtures"]:
        entry["runs_total"] = None


def _null_format_version(document: dict[str, Any]) -> None:
    document["format_version"] = None


def _null_runs_per_fixture(document: dict[str, Any]) -> None:
    document["runs_per_fixture"] = None


def _null_role(document: dict[str, Any]) -> None:
    document["fixtures"][0]["role"] = None


def _all_runs_total_zero(document: dict[str, Any]) -> None:
    for entry in document["fixtures"]:
        entry["runs_total"] = 0
        entry["runs_errored"] = 0
        entry["runs_detected"] = 0


def _all_runs_detected_zero(document: dict[str, Any]) -> None:
    for entry in document["fixtures"]:
        entry["runs_detected"] = 0


def _no_fixtures(document: dict[str, Any]) -> None:
    document["fixtures"] = []


class TestCompareRefusesAHoledBaseline:
    @pytest.mark.parametrize(
        ("damage", "expect_in_stderr"),
        [
            (_hole, "runs_detected"),
            (_null, "runs_total"),
            (_null_format_version, "format_version"),
            (_null_runs_per_fixture, "runs_per_fixture"),
            (_null_role, "role"),
            (_all_runs_total_zero, "bounds nothing"),
            (_all_runs_detected_zero, "bounds nothing"),
            (_no_fixtures, "bounds nothing"),
        ],
        ids=[
            "absent-run-count",
            "null-run-count",
            "null-format-version",
            "null-runs-per-fixture",
            "null-role",
            "all-runs-total-zero",
            "all-runs-detected-zero",
            "no-fixtures",
        ],
    )
    def test_an_old_baseline_with_a_bad_run_count_exits_2(
        self, tmp_path: Path, damage: Any, expect_in_stderr: str
    ) -> None:
        """The whole defect, through the real CLI, for every #421 Group
        A shape (A1-A4).

        `null` cases are here on their own because they used to raise
        `TypeError` out of `int()`, and `main`'s `except ValueError` does
        not catch a `TypeError`: it reached the terminal as a traceback,
        not exit 2. The `_all_runs_*_zero` and `_no_fixtures` cases are
        Group A4: an OLD baseline with no detected fixture bounds
        nothing, so a regression it should catch instead passes.
        """
        real = json.loads(
            (RESULTS_DIR / "baseline-20260831-034641.json").read_text(encoding="utf-8")
        )
        damaged = json.loads(json.dumps(real))
        damage(damaged)
        new = json.loads(json.dumps(real))
        new["timestamp"] = "20260902-000000"
        for entry in new["fixtures"]:
            if entry["fixture_id"] == "sec-01-sql-injection":
                entry["runs_detected"] = 0
                entry["consistency"] = 0.0
                entry["detected"] = False
        old_path = _write(tmp_path / "baseline-old.json", damaged)
        new_path = _write(tmp_path / "baseline-new.json", new)

        finished = subprocess.run(
            [sys.executable, "-m", "kstrl.calibration", "compare", str(old_path), str(new_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            start_new_session=True,
        )
        assert finished.returncode == 2, f"stdout={finished.stdout}\nstderr={finished.stderr}"
        assert expect_in_stderr in finished.stderr
        assert "Traceback" not in finished.stderr
        assert "PASS" not in finished.stdout

    def test_an_old_v1_baseline_with_absent_caught_exits_2(self, tmp_path: Path) -> None:
        """#421 Group A2, through the real CLI: reuse.md F1's exact
        reproduction of the #421 symptom one format version over."""
        old = {
            "model": "haiku",
            "timestamp": "20260527-000000",
            "fixtures": [
                {"role": "security", "fixture_id": "sec-01-sql-injection"},
                {"role": "security", "fixture_id": "sec-02-xss", "caught": True},
            ],
        }
        new = {
            "model": "haiku",
            "timestamp": "20260527-000001",
            "fixtures": [
                {"role": "security", "fixture_id": "sec-01-sql-injection", "caught": False},
                {"role": "security", "fixture_id": "sec-02-xss", "caught": False},
            ],
        }
        old_path = _write(tmp_path / "baseline-old.json", old)
        new_path = _write(tmp_path / "baseline-new.json", new)

        finished = subprocess.run(
            [sys.executable, "-m", "kstrl.calibration", "compare", str(old_path), str(new_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            start_new_session=True,
        )
        assert finished.returncode == 2, f"stdout={finished.stdout}\nstderr={finished.stderr}"
        assert "caught" in finished.stderr
        assert "Traceback" not in finished.stderr
        assert "PASS" not in finished.stdout


class TestTheDocumentIsUnchanged:
    def test_save_report_writes_the_pinned_bytes(self, tmp_path: Path) -> None:
        report = calibration.build_report(
            PINNED_RECORDS,
            model="haiku",
            timestamp="20260901-000000",
            runs_per_fixture=2,
            fixtures_attempted=["security/sec-a", "architect/spec-a"],
            fixtures_completed=["security/sec-a", "architect/spec-a"],
        )
        written = calibration.save_report(report, tmp_path)
        assert written.read_text(encoding="utf-8") == PINNED_DOCUMENT


def _reader_literals() -> frozenset[str]:
    return frozenset(
        folded for node in all_nodes(parsed(READER)) if (folded := folded_str(node)) is not None
    )


#: The RECORD vocabulary `build_report`'s input reads spell (its own
#: docstring names the full record shape as `{"role", "fixture_id",
#: "category", "cwe", "caught", "error", "detail"}`). `detail` is read
#: only inside `fixture_entry`, which moved into the reader with the rest
#: of the v2 per-fixture write (#421 Group B1), so it does not appear as a
#: writer literal any more. These six happen to share a spelling with the
#: reader's own document keys without being document keys themselves -
#: `kstrl/calibration_baseline.py:49-69`'s comment discloses the
#: coincidence. Spelled here as literals on purpose, same reasoning as
#: `RUN_COUNT_KEYS`: a guard that read these off the module it is
#: checking would go quiet with them.
RECORD_FIELD_NAMES = ("role", "fixture_id", "category", "cwe", "caught", "error")


class TestTheKeysHaveOneOwner:
    def test_the_writer_spells_no_string_the_reader_also_spells(self) -> None:
        """After #421 Group B, the v2 document has one owner BY
        CONSTRUCTION: `fixture_entry` and `baseline_document` in
        `kstrl/calibration_baseline.py` are the only places a document key
        is spelled, so there is no second module to police it against.
        What is still worth a net is the opposite direction: does the
        WRITER (`kstrl/calibration.py`) independently respell a string the
        reader owns? A rename of a document key that also touched the
        writer would be exactly that.

        The net enumerates no key name at all: it counts every expression
        in the writer whose folded value is ALSO spelled somewhere in the
        reader, so a key nobody enrolled would still show up as a row
        (mutation M1 in the plan proved this: spelling `"consistency"`, a
        writer-only key with no constant, in the reader fires this net
        with nobody having named it).

        The surviving rows are exactly `RECORD_FIELD_NAMES` - the
        per-run RECORD contract `build_report` reads, which is a
        different schema from the document that happens to share six
        spellings with it - plus nothing else. The empty string is
        excluded from the net entirely: both halves use it as a `.get`
        default, it is never a key, and pinning its count ties this test
        to an unrelated line in a 500+ line file (altitude.md Finding 6).
        """
        owned = frozenset(value for value in _reader_literals() if value != "")
        assert set(RECORD_FIELD_NAMES) <= owned, (
            "a record field name is no longer spelled anywhere in the reader; "
            "RECORD_FIELD_NAMES needs updating alongside the reader, not the "
            "writer"
        )
        # The control is derived from what the reader actually owns right
        # now, rather than a value hard-coded independently of it, so a
        # document-key rename cannot also silently break the control
        # (altitude.md Finding 5).
        control = f"X = {sorted(owned)[0]!r}"
        assert_census(
            sources=[WRITER],
            sees=lambda node: (
                (folded := folded_str(node)) is not None and folded != "" and folded in owned
            ),
            # Pinned by running the test and reading its `Found:` line -
            # never computed here, which would make this an assertion
            # that the file agrees with itself.
            expected={
                "calibration.py:'role'": 1,
                "calibration.py:'fixture_id'": 1,
                "calibration.py:'category'": 2,
                "calibration.py:'cwe'": 2,
                "calibration.py:'caught'": 1,
                "calibration.py:'error'": 2,
            },
            control=control,
            message=(
                "kstrl/calibration.py spells a string kstrl/calibration_baseline.py "
                "also spells, and it is not one of the record fields build_report's "
                "own docstring names. A report key with two independent spellings "
                "is #421: route it through kstrl.calibration_baseline's fixture_entry "
                "or baseline_document instead."
            ),
            key=lambda source, node: f"{label(source)}:{folded_str(node)!r}",
        )
