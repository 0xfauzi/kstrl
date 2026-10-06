"""End to end: the blocking integration loop hands every finding off and stops (#483, #696).

Every test drives the real ``run_factory`` through ``tests/helpers/integration_loop``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import integration_harness as h
from tests.helpers import integration_loop as lp

IC5_FAIL = {"IC5": ("fail", "decision shutdown-semantics says 10 s, US-019 says 2 s")}


def test_blocking_off_builds_nothing_and_keeps_the_exit_code(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [lp.IC2_FAIL])
    rig = lp.Rig(root, reviewer)

    result, out = lp.run_loop(root, rig, integration_blocking=False)

    assert rig.launched == []
    assert reviewer.calls == 1
    assert lp.manifest_ids(root) == ["comp-a", "comp-b"]
    assert lp.state(root).get("fixes", []) == []
    assert lp.state(root)["stops"][-1]["gates"] is False
    assert result.exit_code == 0
    assert result.contract_failures == []
    assert lp.run_halts(root) == []
    assert "does not gate" in out


def test_a_register_finding_stops_the_loop_red(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    ic5 = {"IC5": ("fail", "decision shutdown-semantics says 10 s, US-019 says 2 s")}
    rig = lp.Rig(root, lp.ScriptedReviewer(base, [ic5]))

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    assert lp.manifest_ids(root) == ["comp-a", "comp-b"]
    assert lp.state(root).get("fixes", []) == []
    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "red"
    assert "IF-1" in stop["reason"]
    assert result.exit_code == 1
    assert result.contract_failures == [
        "integration IF-1: handoff: IC5 failed: decision shutdown-semantics says 10 s, "
        "US-019 says 2 s"
    ]
    halts = lp.run_halts(root)
    assert len(halts) == 1
    assert halts[0].evidence["open_findings"] == ["IF-1"]


@pytest.mark.parametrize(
    "owned",
    [
        ["src/api.py"],
        ["src/"],
        ["src/api.py", "tests/test_api.py"],
        ["src/api.py", "tests/"],
        ["src/api.py", "src/api.test.ts"],
        ["src/api.py", "src/__tests__/"],
        ["src/api.py", "src/api_test.go"],
        ["src/api.py", "spec/"],
    ],
    ids=[
        "file",
        "bare-prefix",
        "test-file",
        "tests-dir",
        "dotted-test",
        "tests-subdir",
        "underscore-test",
        "spec-dir",
    ],
)
def test_every_open_code_finding_is_handed_off(tmp_path: Path, owned: list[str]) -> None:
    """#696 decision 6: kstrl reads no test-path convention, so no fix is
    scoped or built, whatever test paths the owning component's
    allowedPaths name. Two code findings, one per component: each is handed
    off. The blocking loop stops red and the run fails."""
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root, {**lp.SCOPES, "comp-b": owned})
    rig = lp.Rig(root, lp.ScriptedReviewer(base, [{**lp.IC1_FAIL, **lp.IC2_FAIL}]))

    result, out = lp.run_loop(root, rig)

    assert rig.launched == []
    state = lp.state(root)
    assert [(f["id"], f["status"]) for f in state["findings"]] == [
        ("IF-1", "handoff"),
        ("IF-2", "handoff"),
    ]
    for finding in state["findings"]:
        assert finding["handoffReason"].startswith("kstrl reads no test-path convention")
    assert state.get("fixes", []) == []
    assert lp.manifest_ids(root) == ["comp-a", "comp-b"]
    assert state["stops"][-1]["outcome"] == "red"
    assert result.exit_code == 1
    assert len(lp.run_halts(root)) == 1
    assert "Integration fix" not in out


def test_a_clean_review_with_blocking_on_is_clean(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    moved: list[str] = []
    reviewer = lp.ScriptedReviewer(
        base, [{}], on_prompt=lambda _p: moved.append(h.commit_file(root, "late.txt", "late\n"))
    )
    rig = lp.Rig(root, reviewer)

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    assert lp.state(root)["stops"][-1]["outcome"] == "clean"
    evidence = h.evidence_files(root)
    assert '"baseMovedTo": "' + moved[0] in evidence[-1].read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert lp.run_halts(root) == []


def test_a_review_that_cannot_be_read_fails_a_blocking_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    h.merged_feature(root)
    rig = lp.Rig(root, lp.ScriptedReviewer("", []))

    result, _out = lp.run_loop(root, rig)

    assert lp.state(root)["stops"][-1]["outcome"] == "red"
    assert result.exit_code == 1
    assert result.contract_failures[0].startswith("integration red:")
    assert len(lp.run_halts(root)) == 1


def test_blocking_with_the_review_off_fails_the_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [])
    rig = lp.Rig(root, reviewer)

    result, _out = lp.run_loop(root, rig, integration_review=False)

    assert reviewer.calls == 0
    assert rig.launched == []
    assert result.contract_failures == ["integration not_run: [factory] integration_review = false"]
    assert result.exit_code == 1
    assert len(lp.run_halts(root)) == 1


def test_a_finding_citing_kstrl_files_is_handed_off(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    h.commit_file(root, "scripts/kstrl/notes.md", "harness notes\n")
    cites = {"IC2": ("fail", f"{h.STORE}:1 and scripts/kstrl/notes.md:1 disagree")}
    rig = lp.Rig(root, lp.ScriptedReviewer(base, [cites]))

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    finding = lp.state(root)["findings"][0]
    assert finding["status"] == "handoff"
    assert finding["handoffReason"] == "it cites kstrl's own files: scripts/kstrl/notes.md"
    assert lp.manifest_ids(root) == ["comp-a", "comp-b"]
    assert result.exit_code == 1


def test_a_handoff_from_an_earlier_run_does_not_fail_a_clean_run(tmp_path: Path) -> None:
    """#497: only this run's handoffs fail it. Nothing closes a handed-off
    finding, so counting an earlier run's would fail every later run."""
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    first, _out = lp.run_loop(root, lp.Rig(root, lp.ScriptedReviewer(base, [IC5_FAIL])))
    assert first.exit_code == 1
    rig = lp.Rig(root, lp.ScriptedReviewer(base, [{}]))

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    state = lp.state(root)
    assert [(f["id"], f["status"]) for f in state["findings"]] == [("IF-1", "handoff")]
    assert state["stops"][-1]["outcome"] == "clean"
    assert result.exit_code == 0
    assert result.contract_failures == []
    assert len(lp.run_halts(root)) == 1
