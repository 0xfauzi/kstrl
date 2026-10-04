"""#633 slice 1: compare judges a role only against a floor somebody chose.

``python -m kstrl.calibration compare`` read every role id missing from
``MIN_ROLE_DETECTION_RATE`` against a 0.50 default. A first capture of a
new id was therefore failed against a number nobody chose, and on a project
with the autonomy ladder on that opened a "Calibration regression" inbox
item. A misspelt id passed the same way, silently. Now the table is closed:
an id it does not list is refused (exit 2) before any verdict, and an id it
lists with ``None`` is recorded, reported as not gated, and still held to
the drop check against its previous capture.

#633 slice 3 adds the false-positive ceiling: compare reads the new
baseline's ``false_positive_analysis`` block and fails a negative role above
``FP_RATE_MAX``, refuses a malformed block (exit 2), names a role measured
over fewer than three negatives as not gated, and says when no negative ran.
Those baselines carry the block the capture harness's own
``build_fp_summary`` writes.

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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from kstrl import calibration
from kstrl.inbox import ItemKind
from tests import test_calibration as tc
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
#: The two forms of the #633 slice 3 false-positive block.
FP_HEADER = (
    "false-positive rate per negative role (new baseline; ceiling 0.34, gated from 3 negatives):"
)
NO_NEGATIVES = (
    "false-positive rate: not measured (the new baseline has no false-positive block, "
    "so no negative fixture ran)"
)


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


def _negative_runs(role: str, fixture_id: str, flagged: int) -> list[dict[str, Any]]:
    """Three runs of one negative fixture, the first ``flagged`` of them a false positive."""
    return [
        {
            "role": role,
            "fixture_id": fixture_id,
            "false_positive": run < flagged,
            "error": False,
            "detail": "synthetic",
        }
        for run in range(3)
    ]


def _baseline(
    tmp_path: Path,
    timestamp: str,
    *fixtures: tuple[str, str, int],
    negatives: Sequence[tuple[str, str, int]] = (),
) -> Path:
    """A baseline written the way the capture harness writes one: the real
    ``build_report``, the harness's own ``build_fp_summary`` layered on only
    when a negative ran (``_DetectionReport._build``), the real ``save_report``."""
    records = [run for fixture in fixtures for run in _runs(*fixture)]
    report = calibration.build_report(
        records, model="haiku", timestamp=timestamp, runs_per_fixture=3
    )
    if negatives:
        fp_records = [run for negative in negatives for run in _negative_runs(*negative)]
        report["false_positive_analysis"] = tc.build_fp_summary(fp_records)
    return calibration.save_report(report, tmp_path / "baselines" / timestamp)


def _clean(role: str, count: int) -> list[tuple[str, str, int]]:
    """``count`` negative fixtures of ``role`` that no run flagged."""
    return [(role, f"{role}-{index:02d}", 0) for index in range(count)]


def _flagged(role: str, count: int) -> list[tuple[str, str, int]]:
    """``count`` negative fixtures of ``role`` that every run flagged."""
    return [(role, f"{role}-{index:02d}", 3) for index in range(count)]


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
    """``lines`` with the #633 blocks, and the blank line before each, removed."""
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line in (FIRST_MEASUREMENTS, NOT_GATED, FP_HEADER, NO_NEGATIVES):
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


def test_a_negative_role_above_the_ceiling_fails_compare(ladder_on: Path) -> None:
    """Slice 3: a capture whose ``security_negative_ts`` flagged all three of
    its clean fixtures (fp_rate 1.0) exits 1 naming the role, beside a
    ``security_negative`` that flagged none, and the regression reaches the
    inbox. Three is the fewest negatives that are gated, so a minimum read as
    "more than three" leaves this role ungated and fails here. Before #633
    compare never read the block and this exited 0."""
    old = _baseline(
        ladder_on,
        OLD_TS,
        ("security", "sec-01", 3),
        negatives=_clean("security_negative", 4) + _clean("security_negative_ts", 3),
    )
    new = _baseline(
        ladder_on,
        NEW_TS,
        ("security", "sec-01", 3),
        negatives=_clean("security_negative", 4) + _flagged("security_negative_ts", 3),
    )

    done = _cli(old, new, ladder_on)

    lines = done.stdout.splitlines()
    failure = (
        "negative role 'security_negative_ts' false-positive rate 1.00 is above the ceiling 0.34"
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert _block(lines, FP_HEADER) == [
        "security_negative          0.00  (4 negatives)",
        "security_negative_ts       1.00  (3 negatives)",
    ]
    assert f"  FAIL: {failure}" in lines
    drift = inbox_items(ladder_on, ItemKind.CALIBRATION_DRIFT)
    assert len(drift) == 1
    assert drift[0].evidence["failures"] == [failure]


def test_only_the_new_baseline_is_held_to_the_ceiling(ladder_on: Path) -> None:
    """Slice 3: the ceiling is absolute on the NEW capture, like a detection
    floor. An old capture whose ``security_negative_ts`` flagged everything,
    followed by a clean one, passes: gating the old side too would fail every
    later compare against a capture that was already bad."""
    old = _baseline(
        ladder_on,
        OLD_TS,
        ("security", "sec-01", 3),
        negatives=_flagged("security_negative_ts", 3),
    )
    new = _baseline(
        ladder_on,
        NEW_TS,
        ("security", "sec-01", 3),
        negatives=_clean("security_negative_ts", 3),
    )

    done = _cli(old, new, ladder_on)

    lines = done.stdout.splitlines()
    assert done.returncode == 0, done.stdout + done.stderr
    assert _block(lines, FP_HEADER) == ["security_negative_ts       0.00  (3 negatives)"]
    assert "PASS: no calibration regression under the codified thresholds" in lines
    assert inbox_items(ladder_on, ItemKind.CALIBRATION_DRIFT) == []


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        pytest.param("high", "'high'", id="a-string"),
        pytest.param(True, "True", id="a-bool"),
        pytest.param(1.5, "1.5", id="above-one"),
        pytest.param(None, "None", id="absent"),
    ],
)
def test_a_malformed_false_positive_block_is_refused_with_its_index(
    ladder_on: Path, value: object, shown: str
) -> None:
    """Slice 3: a recorded rate that is not a number from 0 to 1 (a string, a
    JSON boolean, a rate above one, or no ``fp_rate`` key at all, which is
    what ``None`` writes here) refuses the comparison (exit 2) and names the
    entry by index and role, before any verdict and before the inbox. Read as
    zero it would have passed. ``True`` is refused although Python counts it
    as the integer 1."""
    old = _baseline(ladder_on, OLD_TS, ("security", "sec-01", 3))
    new = _baseline(
        ladder_on,
        NEW_TS,
        ("security", "sec-01", 3),
        negatives=_clean("security_negative", 4) + _clean("security_negative_ts", 4),
    )
    document = json.loads(new.read_text(encoding="utf-8"))
    entry = document["false_positive_analysis"]["roles"]["security_negative_ts"]
    if value is None:
        del entry["fp_rate"]
    else:
        entry["fp_rate"] = value
    new.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    done = _cli(old, new, ladder_on)

    assert done.returncode == 2, done.stdout + done.stderr
    assert (
        f"false_positive_analysis.roles[1] 'security_negative_ts' has no usable 'fp_rate': {shown}"
        in done.stderr
    )
    assert "Traceback" not in done.stderr
    assert done.stdout == ""
    assert inbox_items(ladder_on, ItemKind.CALIBRATION_DRIFT) == []


def test_a_capture_with_no_negatives_is_reported_as_not_measured(ladder_on: Path) -> None:
    """Slice 3: a new baseline with no false-positive block (no negative ran)
    is said to be unmeasured, and no negative role is printed with a rate,
    so an absent block never reads as a clean 0.00."""
    old = _baseline(
        ladder_on, OLD_TS, ("security", "sec-01", 3), negatives=_clean("security_negative", 4)
    )
    new = _baseline(ladder_on, NEW_TS, ("security", "sec-01", 3))

    done = _cli(old, new, ladder_on)

    lines = done.stdout.splitlines()
    assert done.returncode == 0, done.stdout + done.stderr
    assert NO_NEGATIVES in lines
    assert FP_HEADER not in lines
    assert not [line for line in lines if line.startswith("  security_negative")]


def test_a_negative_role_under_three_negatives_is_named_and_not_gated(ladder_on: Path) -> None:
    """Owner decision 6: a role measured over fewer than three negatives can
    only read 0, 0.5 or 1, so it is printed with its count and the reason,
    and is not gated: two flagged negatives exit 0 with no inbox item."""
    old = _baseline(ladder_on, OLD_TS, ("security", "sec-01", 3))
    new = _baseline(
        ladder_on,
        NEW_TS,
        ("security", "sec-01", 3),
        negatives=_flagged("security_negative_ts", 2),
    )

    done = _cli(old, new, ladder_on)

    lines = done.stdout.splitlines()
    assert done.returncode == 0, done.stdout + done.stderr
    assert _block(lines, FP_HEADER) == [
        "security_negative_ts       1.00  (2 negatives: fewer than 3, not gated)"
    ]
    assert "PASS: no calibration regression under the codified thresholds" in lines
    assert inbox_items(ladder_on, ItemKind.CALIBRATION_DRIFT) == []


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
