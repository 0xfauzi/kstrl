"""#523: every agent reply a calibration run scores is kept beside its baseline.

End to end, and no paid call anywhere. Two halves:

- A child interpreter drives the REAL capture harness (the three gate helpers,
  ``_collect`` and ``_DetectionReport``) with a fake agent, through
  ``tests/helpers/calibration_capture.py``, in every way a run can stop. The
  assertion is on the directory the child leaves: exactly one reply file per
  run the baseline records, holding that run's reply, and a baseline that
  loads only when every reply was kept.
- In this process, every paid test in ``tests/test_calibration.py`` is called
  with ``kstrl.agents.get_agent`` replaced by a stub, so the arms that drain
  their agent somewhere other than ``_collect`` (the integration review) are
  covered too. ``PATH`` holds only ``git`` while they run, so a real agent
  CLI cannot start even if the stub were bypassed.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import kstrl.agents
from kstrl import calibration_baseline
from tests import test_calibration as tc
from tests.helpers import calibration_capture as harness
from tests.helpers.calibration_capture import FX_D
from tests.helpers.calibration_replies import keep_call, replies_dir

ALL_MODES = tuple(harness.EXPECTED_EXIT)


def _recorded_runs(baseline: dict[str, Any]) -> set[tuple[str, str, int]]:
    """``(role, fixture_id, run)`` for every run the baseline records, from
    the detection table and the false-positive block alike."""
    runs = {
        (f["role"], f["fixture_id"], n)
        for f in baseline["fixtures"]
        for n in range(1, len(f["runs"]) + 1)
    }
    for role, block in baseline.get("false_positive_analysis", {}).get("roles", {}).items():
        for f in block["fixtures"]:
            runs |= {(role, f["fixture_id"], n) for n in range(1, f["runs_total"] + 1)}
    return runs


def _kept(directory: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Every reply file under ``directory``, keyed by what its path says."""
    kept: dict[tuple[str, str, int], dict[str, Any]] = {}
    for path in sorted(directory.glob("*/*/run-*.json")):
        run = int(path.stem.removeprefix("run-"))
        kept[(path.parent.parent.name, path.parent.name, run)] = json.loads(
            path.read_text(encoding="utf-8")
        )
    return kept


@pytest.mark.parametrize("mode", ALL_MODES)
def test_every_recorded_run_has_its_reply_on_disk(tmp_path: Path, mode: str) -> None:
    """One reply file per recorded run, and none for a run the baseline does
    not record, however the capture stopped. The file's path, its fields and
    the reply it holds all name the same fixture and run."""
    saved = harness.capture(tmp_path, mode)
    baseline = json.loads(saved.read_text(encoding="utf-8"))
    kept = _kept(replies_dir(saved.parent, baseline["timestamp"]))

    assert set(kept) == _recorded_runs(baseline)
    assert kept, "the capture recorded no run at all"
    for (role, fixture_id, run), reply in kept.items():
        assert (reply["role"], reply["fixture_id"], reply["run"]) == (role, fixture_id, run)
        text = harness.reply_text(fixture_id, run)
        assert reply["calls"] == [{"streamed": [text], "final_message": text}]


def test_a_complete_capture_keeps_all_eight_replies(tmp_path: Path) -> None:
    """The mode whose count is known in advance: four fixtures, two runs each."""
    saved = harness.capture(tmp_path, "complete")
    baseline = json.loads(saved.read_text(encoding="utf-8"))
    assert len(_kept(replies_dir(saved.parent, baseline["timestamp"]))) == 8
    calibration_baseline.load_baseline(saved)


@pytest.mark.parametrize("mode", ["no_reply", "reply_unwritable"])
def test_a_run_whose_reply_was_not_kept_leaves_a_refused_capture(tmp_path: Path, mode: str) -> None:
    """``no_reply``: fx-d's run asks no agent through ``_collect``, so there is
    nothing to keep. ``reply_unwritable``: a file sits where fx-d's replies
    directory must go. Either way the run is not recorded, fx-d never
    completes, and the saved baseline is a partial capture refused by name."""
    saved = harness.capture(tmp_path, mode)
    baseline = json.loads(saved.read_text(encoding="utf-8"))
    assert baseline["run_complete"] is False
    assert FX_D not in baseline["fixtures_completed"]
    assert "fx-d" not in [f["fixture_id"] for f in baseline["fixtures"]]
    with pytest.raises(ValueError, match=f"partial capture.*{FX_D}"):
        calibration_baseline.load_baseline(saved)


# --------------------------------------------------------------- every paid arm


class _StubAgent:
    """Answers every call with numbered text that no matcher accepts, and
    appends that text to ``made`` so a test can say which calls happened."""

    def __init__(self, made: list[str]) -> None:
        self.name = "stub"
        self.final_message: str | None = None
        self._made = made

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.final_message = f"stub reply {len(self._made) + 1}"
        self._made.append(self.final_message)
        yield self.final_message


def _first(params: list[Any]) -> tuple[Any, ...]:
    head = params[0]
    return tuple(head.values) if hasattr(head, "values") else tuple(head)


#: Every paid test, the arguments it is driven with, and whether it gates (a
#: stub reply is a miss, so a gated test fails its consistency assertion).
PAID_ARMS: dict[str, tuple[Callable[[], tuple[Any, ...]], bool]] = {
    "test_security_role_catches_planted_bug": (
        lambda: _first(tc._security_positive_easy_fixtures()),
        True,
    ),
    "test_security_role_hard_positive": (
        lambda: _first(tc._security_positive_hard_fixtures()),
        False,
    ),
    "test_security_role_no_false_positive": (
        lambda: _first(tc._security_negative_fixtures()),
        False,
    ),
    "test_reviewer_role_catches_planted_concern": (
        lambda: _first(tc._concern_fixtures()),
        True,
    ),
    "test_reviewer_role_no_false_positive": (
        lambda: _first(tc._concern_negative_fixtures()),
        False,
    ),
    "test_architect_role_flags_vague_spec": (
        lambda: _first(tc._halting_spec_fixtures()),
        True,
    ),
    "test_architect_emits_sensible_allowed_paths": (
        lambda: _first(tc._allowed_paths_fixtures()),
        True,
    ),
    "test_architect_reuses_what_the_repository_already_has": (
        lambda: _first(tc.REUSE_ARM_PARAMS),
        False,
    ),
    "test_integration_review_detects_planted_defect": (
        lambda: _first(tc.INTEGRATION_POSITIVE_PARAMS),
        True,
    ),
    "test_integration_review_opens_nothing_on_a_clean_twin": (
        lambda: _first(tc.INTEGRATION_CLEAN_PARAMS),
        True,
    ),
}


def _paid_tests() -> set[str]:
    """Every test in ``tests/test_calibration.py`` that only runs under
    ``KSTRL_RUN_CALIBRATION=1``, found by its skip mark rather than listed."""
    return {
        name
        for name, obj in vars(tc).items()
        if name.startswith("test_")
        and any(
            mark.name == "skipif" and "KSTRL_RUN_CALIBRATION" in str(mark.kwargs.get("reason"))
            for mark in getattr(obj, "pytestmark", [])
        )
    }


def test_every_paid_test_is_driven_here() -> None:
    """A paid test added later fails here until it is enrolled in PAID_ARMS,
    so no arm can ship whose replies nobody checked are kept."""
    assert _paid_tests() == set(PAID_ARMS)


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """The real getters, a stub agent under them, and a PATH with only git.
    Returns the list every stub call appends its reply to."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    os.symlink(git, bin_dir / "git")
    monkeypatch.setenv("PATH", str(bin_dir))
    made: list[str] = []
    monkeypatch.setattr(kstrl.agents, "get_agent", lambda **_kwargs: _StubAgent(made))
    monkeypatch.setattr(tc, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(tc, "CALIBRATION_RUNS", 2)
    return made


@pytest.mark.parametrize("name", sorted(PAID_ARMS))
def test_every_paid_arm_keeps_the_reply_it_scored(
    name: str, stubbed: list[str], tmp_path: Path
) -> None:
    """Both runs of the arm's first fixture are kept, each holding the stub
    reply that run was scored on, and no stub reply is kept twice or lost."""
    build_args, gated = PAID_ARMS[name]
    report = tc._DetectionReport()
    work = tmp_path / "work"
    work.mkdir()
    # A call an earlier run made and never handed over: it must not be kept
    # under this arm's first run.
    keep_call(["left over"], "left over")
    try:
        getattr(tc, name)(*build_args(), work, report)
    except AssertionError as exc:
        if not gated or "missed planted issue" not in str(exc):
            raise
    else:
        assert not gated, f"{name} passed its gate on stub replies"

    recorded = [(r["role"], r["fixture_id"]) for r in report.records + report.fp_records]
    assert len(recorded) == 2 and len(set(recorded)) == 1, recorded
    role, fixture_id = recorded[0]
    kept = _kept(replies_dir(tc.RESULTS_DIR, report.timestamp))
    assert sorted(kept) == [(role, fixture_id, 1), (role, fixture_id, 2)]
    assert len(stubbed) == 2, stubbed
    finals = [call["final_message"] for key in sorted(kept) for call in kept[key]["calls"]]
    assert finals == stubbed


class _CrashingAgent:
    """Streams one line, then raises, the way a CLI that dies mid-reply does.
    Appends to ``made`` so a test can count the calls."""

    def __init__(self, made: list[str]) -> None:
        self.name = "crashing"
        self.final_message: str | None = None
        self._made = made

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self._made.append("crashed")
        yield "partial line"
        raise RuntimeError("agent died mid-reply")


@pytest.mark.parametrize("name", sorted(PAID_ARMS))
def test_a_run_whose_agent_crashed_keeps_its_partial_reply(
    name: str, stubbed: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An agent that dies mid-reply is an infrastructure error, which every
    arm records with ``error`` set and excludes from its denominator. It must
    stay a recorded run with the line it did stream kept, not turn into a
    refused capture because the call was never handed over."""
    monkeypatch.setattr(kstrl.agents, "get_agent", lambda **_kwargs: _CrashingAgent(stubbed))
    build_args, _gated = PAID_ARMS[name]
    report = tc._DetectionReport()
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(pytest.skip.Exception, match="agent unavailable for all 2 runs"):
        getattr(tc, name)(*build_args(), work, report)

    records = report.records + report.fp_records
    assert [r["error"] for r in records] == [True, True], records
    role, fixture_id = records[0]["role"], records[0]["fixture_id"]
    kept = _kept(replies_dir(tc.RESULTS_DIR, report.timestamp))
    assert sorted(kept) == [(role, fixture_id, 1), (role, fixture_id, 2)]
    assert len(stubbed) == 2, stubbed
    for reply in kept.values():
        assert reply["calls"] == [{"streamed": ["partial line"], "final_message": None}]
