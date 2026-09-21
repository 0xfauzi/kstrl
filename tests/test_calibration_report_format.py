"""The v2 baseline format as a contract between its two halves (#421).

`kstrl/calibration.py` writes the document and `kstrl/calibration_baseline.py`
reads it back. This file pins the three things that keep those halves from
drifting apart in silence:

- the reader REFUSES a v2 entry whose run counts are absent or are not
  integers, instead of reading an absent count as zero. Zero is the
  fail-open direction: `compare_baselines` reports `newly_missed` only for
  a fixture the OLD baseline detected, so an old baseline that reads as
  zero passes the comparison having compared against nothing;
- `save_report` writes bytes this file pins, so the constants change below
  cannot have moved a key or a value;
- each shared key is spelled once, in the reader, and nowhere else in
  either module, which is how a rename in one half leaves the other
  reading a key nobody writes.

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

from kstrl import calibration, calibration_baseline
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

#: Spelled here as literals on purpose. A guard that reads the constants it
#: is guarding would go quiet with them.
RUN_COUNT_KEYS = ("runs_total", "runs_errored", "runs_detected")

#: How many report keys both halves of the format spell. Measured at
#: 5db5cee: 27 distinct keys in a built document, 16 of them spelled
#: independently in both modules. Pinned so that DELETING a constant fails
#: here instead of quietly shrinking the census below into an assertion
#: about fewer keys.
SHARED_KEY_COUNT = 16

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


def _v2_document() -> dict[str, Any]:
    return {
        "format_version": 2,
        "model": "haiku",
        "timestamp": "20260901-000000",
        "runs_per_fixture": 3,
        "fixtures": [
            {
                "role": "security",
                "fixture_id": "sec-a",
                "category": "injection",
                "cwe": "CWE-89",
                "runs_total": 3,
                "runs_errored": 0,
                "runs_detected": 3,
            }
        ],
    }


def _write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


class TestV2RunCountsAreRequired:
    @pytest.mark.parametrize("key", RUN_COUNT_KEYS)
    def test_a_missing_run_count_is_refused(self, tmp_path: Path, key: str) -> None:
        document = _v2_document()
        del document["fixtures"][0][key]
        path = _write(tmp_path / "baseline-20260901-000000.json", document)
        with pytest.raises(ValueError) as caught:
            load_baseline(path)
        message = str(caught.value)
        assert key in message
        assert "sec-a" in message
        assert str(path) in message

    @pytest.mark.parametrize("key", RUN_COUNT_KEYS)
    @pytest.mark.parametrize("value", ["3", True, 3.5, None])
    def test_a_non_integer_run_count_is_refused(
        self, tmp_path: Path, key: str, value: object
    ) -> None:
        document = _v2_document()
        document["fixtures"][0][key] = value
        path = _write(tmp_path / "baseline-20260901-000000.json", document)
        with pytest.raises(ValueError) as caught:
            load_baseline(path)
        assert key in str(caught.value)
        assert "sec-a" in str(caught.value)

    @pytest.mark.parametrize("key", RUN_COUNT_KEYS)
    def test_zero_is_a_legal_value_when_the_key_is_present(self, tmp_path: Path, key: str) -> None:
        document = _v2_document()
        document["fixtures"][0][key] = 0
        path = _write(tmp_path / "baseline-20260901-000000.json", document)
        loaded = load_baseline(path)
        assert getattr(loaded.fixtures[0], key) == 0

    def test_every_checked_in_baseline_still_loads(self) -> None:
        """Ten baselines were checked in at 5db5cee and all ten load. A
        capture only adds files, so the bound is >=, not ==."""
        found = sorted(RESULTS_DIR.glob("baseline-*.json"))
        assert len(found) >= 10
        for path in found:
            assert load_baseline(path).fixtures


def _hole(document: dict[str, Any]) -> None:
    for entry in document["fixtures"]:
        del entry["runs_detected"]


def _null(document: dict[str, Any]) -> None:
    for entry in document["fixtures"]:
        entry["runs_total"] = None


class TestCompareRefusesAHoledBaseline:
    @pytest.mark.parametrize(
        ("damage", "key"),
        [(_hole, "runs_detected"), (_null, "runs_total")],
        ids=["absent", "null"],
    )
    def test_an_old_baseline_with_a_bad_run_count_exits_2(
        self, tmp_path: Path, damage: Any, key: str
    ) -> None:
        """The whole defect, through the real CLI.

        `null` is here on its own because it used to raise `TypeError` out
        of `int()`, and `main`'s `except ValueError` does not catch a
        `TypeError`: it reached the terminal as a traceback, not exit 2.
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
        assert key in finished.stderr
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


def _key_constants() -> dict[str, str]:
    """The ``KEY_*`` constants the reader owns, read off the module."""
    return {
        name: value
        for name, value in vars(calibration_baseline).items()
        if name.startswith("KEY_") and isinstance(value, str)
    }


class TestTheKeysHaveOneOwner:
    def test_each_shared_key_is_spelled_once_and_only_in_the_reader(self) -> None:
        """Closed by construction over the enrolled keys, across BOTH
        modules. Each ``KEY_*`` constant must fold to exactly one
        expression in `kstrl/calibration_baseline.py` (its own definition)
        and none in `kstrl/calibration.py`. A second spelling anywhere in
        either module raises a count or adds a row, and a DELETED constant
        fails the SHARED_KEY_COUNT assertion above rather than shrinking
        the net in silence."""
        constants = _key_constants()
        assert len(constants) == SHARED_KEY_COUNT, sorted(constants)
        values = frozenset(constants.values())
        assert len(values) == SHARED_KEY_COUNT, "two KEY_* constants share a value"
        assert_census(
            sources=[READER, WRITER],
            sees=lambda node: folded_str(node) in values,
            expected={f"calibration_baseline.py:{value!r}": 1 for value in values},
            control='X = "runs_detected"',
            message=(
                "a shared report key is spelled somewhere other than its one "
                "KEY_* definition in kstrl/calibration_baseline.py. A key with "
                "two spellings is #421: import the constant instead. Re-derive "
                "this pin by running the test, never by editing the dict."
            ),
            key=lambda source, node: f"{label(source)}:{folded_str(node)!r}",
        )

    def test_the_writer_spells_no_string_the_reader_also_spells(self) -> None:
        """The wider net, which enumerates no key name at all: it counts
        every expression in the writer whose folded value is also spelled
        in the reader, so a key that is NOT enrolled as a KEY_* constant
        and gets a second spelling still shows up as a row.

        The one surviving row is the empty string, which both halves use
        as a `.get` default and which is never a key."""
        owned = _reader_literals()
        assert_census(
            sources=[WRITER],
            sees=lambda node: folded_str(node) in owned,
            expected={"calibration.py:''": 6},
            control='X = "runs_detected"',
            message=(
                "kstrl/calibration.py spells a string kstrl/calibration_baseline.py "
                "also spells. A report key with two independent spellings is #421: "
                "import the KEY_* constant instead."
            ),
            key=lambda source, node: f"{label(source)}:{folded_str(node)!r}",
        )
