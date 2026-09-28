"""#633 slice 1: compare judges a role only against a floor somebody chose.

``python -m kstrl.calibration compare`` read every role id missing from
``MIN_ROLE_DETECTION_RATE`` against a 0.50 default. A first capture of a
new id was therefore failed against a number nobody chose, and on a project
with the autonomy ladder on that opened a "Calibration regression" inbox
item. A misspelt id passed the same way, silently. Now the table is closed:
an id it does not list is refused (exit 2) before any verdict, and an id it
lists with ``None`` is recorded, reported as not gated, and still held to
the drop check against its previous capture.

End to end: baselines are written by the real ``build_report`` and
``save_report``, compare runs as the real CLI (in a child for the refusal and
the saved pairs, through ``main`` in process where the table has to hold a
``None`` role, since slice 1 lists none), and the inbox is read back from a
project with ``[autonomy] enabled`` and ``demote_on_calibration_regression``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kstrl import calibration
from kstrl.inbox import ItemKind
from tests.helpers.demotion import NEW_TS, OLD_TS, inbox_items, write_config

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path("tests") / "adversarial_fixtures" / "_results"

#: Exit code and stdout of compare for each consecutive pair of saved
#: baselines, captured at a96a986e, before #633 changed compare. Paths are
#: relative to the repository root and the ``--root`` directory reads
#: ``<root>``.
SAVED_PAIRS = REPO_ROOT / "tests" / "fixtures" / "calibration_compare_saved_pairs.json"

FIRST_MEASUREMENTS = "first measurements (the old baseline has no rate for these roles):"
NOT_GATED = "not gated (MIN_ROLE_DETECTION_RATE sets no floor for these roles):"


def _runs(role: str, fixture_id: str, caught: int) -> list[dict[str, Any]]:
    """Three runs of one fixture, the first ``caught`` of them detected."""
    return [
        {
            "role": role,
            "fixture_id": fixture_id,
            "category": "injection",
            "cwe": "CWE-89",
            "caught": run < caught,
            "error": False,
            "detail": "synthetic",
        }
        for run in range(3)
    ]


def _baseline(tmp_path: Path, timestamp: str, *fixtures: tuple[str, str, int]) -> Path:
    records = [run for fixture in fixtures for run in _runs(*fixture)]
    report = calibration.build_report(
        records, model="haiku", timestamp=timestamp, runs_per_fixture=3
    )
    return calibration.save_report(report, tmp_path / "baselines" / timestamp)


def _cli(old: Path | str, new: Path | str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "kstrl.calibration", "compare", str(old), str(new)]
        + ["--root", str(root)],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )


def _block(lines: list[str], header: str) -> list[str]:
    """The indented lines under ``header``, or [] when the report has none."""
    if header not in lines:
        return []
    names: list[str] = []
    for line in lines[lines.index(header) + 1 :]:
        if not line.startswith("  "):
            break
        names.append(line.strip())
    return names


def _without_new_blocks(lines: list[str]) -> list[str]:
    """``lines`` with the two #633 blocks, and the blank line before each, removed."""
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line in (FIRST_MEASUREMENTS, NOT_GATED):
            assert kept and kept[-1] == "", lines
            kept.pop()
            skipping = True
            continue
        if skipping and line.startswith("  "):
            continue
        skipping = False
        kept.append(line)
    return kept


@pytest.fixture
def ladder_on(tmp_path: Path) -> Path:
    write_config(tmp_path, demote_on_calibration=True)
    return tmp_path


def test_a_first_capture_of_an_ungated_role_is_reported_not_failed(
    ladder_on: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(a) A role listed with None, captured for the first time at 0.33: exit 0,
    ``(no floor set)`` on its row, named in both blocks, and no inbox item."""
    monkeypatch.setitem(calibration.MIN_ROLE_DETECTION_RATE, "security_ts", None)
    old = _baseline(ladder_on, OLD_TS, ("security", "sec-01", 3))
    new = _baseline(ladder_on, NEW_TS, ("security", "sec-01", 3), ("security_ts", "sec-ts-01", 1))

    code = calibration.main(["compare", str(old), str(new), "--root", str(ladder_on)])

    lines = capsys.readouterr().out.splitlines()
    assert code == 0, lines
    assert "  security_ts                - -> 0.33  (no floor set)" in lines
    assert "  security                   1.00 -> 1.00  (floor 0.80)" in lines
    assert _block(lines, FIRST_MEASUREMENTS) == ["security_ts"]
    assert _block(lines, NOT_GATED) == ["security_ts"]
    assert lines[-1] == "PASS: no calibration regression under the codified thresholds"
    assert inbox_items(ladder_on, ItemKind.CALIBRATION_DRIFT) == []


def test_a_role_id_the_table_does_not_list_is_refused_before_any_verdict(
    ladder_on: Path,
) -> None:
    """(b) An unlisted id on either side, each at a rate its old default floor
    passed: exit 2, both ids named on stderr, no report, no inbox item."""
    old = _baseline(ladder_on, OLD_TS, ("security", "sec-01", 3), ("security_go", "sec-go-01", 3))
    new = _baseline(ladder_on, NEW_TS, ("security", "sec-01", 3), ("security-ts", "sec-02", 3))

    done = _cli(old, new, ladder_on)

    assert done.returncode == 2, done.stdout + done.stderr
    assert "security_go" in done.stderr
    assert "security-ts" in done.stderr
    assert "MIN_ROLE_DETECTION_RATE" in done.stderr
    assert done.stdout == ""
    assert inbox_items(ladder_on, ItemKind.CALIBRATION_DRIFT) == []


def test_an_ungated_role_is_still_held_to_the_drop_check(
    ladder_on: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(c) Two captures of a None role, 1.00 then 0.67: exit 1 with the drop
    failure, and one inbox item carrying it."""
    monkeypatch.setitem(calibration.MIN_ROLE_DETECTION_RATE, "security_ts", None)
    old = _baseline(ladder_on, OLD_TS, ("security", "sec-01", 3), ("security_ts", "sec-ts-01", 3))
    new = _baseline(ladder_on, NEW_TS, ("security", "sec-01", 3), ("security_ts", "sec-ts-01", 2))

    code = calibration.main(["compare", str(old), str(new), "--root", str(ladder_on)])

    lines = capsys.readouterr().out.splitlines()
    drop = "role 'security_ts' detection rate dropped 1.00 -> 0.67 (drop 0.33 > 0.15)"
    assert code == 1, lines
    assert "  security_ts                1.00 -> 0.67  (no floor set)" in lines
    assert f"  FAIL: {drop}" in lines
    assert _block(lines, FIRST_MEASUREMENTS) == []
    assert _block(lines, NOT_GATED) == ["security_ts"]
    drift = inbox_items(ladder_on, ItemKind.CALIBRATION_DRIFT)
    assert len(drift) == 1
    assert drift[0].evidence["failures"] == [drop]


def _saved_pairs() -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = json.loads(SAVED_PAIRS.read_text(encoding="utf-8"))
    return pairs


@pytest.mark.parametrize("pair", _saved_pairs(), ids=lambda p: f"{p['old'][9:24]}-{p['new'][9:24]}")
def test_every_saved_pair_keeps_its_verdict_and_its_report(
    pair: dict[str, Any], tmp_path: Path
) -> None:
    """(d) Each consecutive pair of saved baselines exits as it did before #633
    and prints the same report once the two new blocks are taken out."""
    root = tmp_path.resolve()

    done = _cli(RESULTS / pair["old"], RESULTS / pair["new"], root)

    assert done.returncode == pair["exit"], done.stdout + done.stderr
    assert done.stderr == ""
    lines = done.stdout.replace(str(root), "<root>").splitlines()
    assert _without_new_blocks(lines) == pair["stdout"]
