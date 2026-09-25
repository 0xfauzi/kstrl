"""End to end: the blocking integration loop builds a fix and closes it (#483).

Every test drives the real ``run_factory`` through ``tests/helpers/integration_loop``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.contract import ContractConfig
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.prd import PRD
from tests.helpers import integration_harness as h
from tests.helpers import integration_loop as lp

NARROW = ["src/store.py", "tests/test_store.py", "scripts/kstrl/feature/integration-fix-1/"]
IC5_FAIL = {"IC5": ("fail", "decision shutdown-semantics says 10 s, US-019 says 2 s")}


@pytest.mark.parametrize("worktrees", [False, True])
def test_a_fix_is_built_merged_and_its_finding_closes_on_a_pass(
    tmp_path: Path, worktrees: bool
) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [lp.IC2_FAIL, {}])
    rig = lp.Rig(root, reviewer)

    result, out = lp.run_loop(root, rig, use_worktrees=worktrees)

    assert rig.launched == [lp.FIX_1]
    assert reviewer.calls == 2
    assert "IF-1" in reviewer.prompts[1]
    assert f"The defect IC2 failed: {h.STORE}:1 re-applies" in reviewer.prompts[1]
    manifest = Manifest.load(h.manifest_file(root))
    fix = manifest.get_component(lp.FIX_1)
    assert fix is not None
    assert fix.status == ComponentStatus.COMPLETED.value
    assert fix.dependencies == ["comp-a", "comp-b"]
    state = lp.state(root)
    assert state["fixes"][0]["id"] == lp.FIX_1
    assert state["fixes"][0]["findings"] == ["IF-1"]
    assert state["findings"][0]["status"] == "closed"
    assert [e["event"] for e in state["findings"][0]["history"]] == ["opened", "closed"]
    assert state["stops"][-1]["outcome"] == "clean"
    assert state["stops"][-1]["gates"] is True
    assert result.exit_code == 0
    assert result.contract_failures == []
    assert lp.run_halts(root) == []
    assert "Blocking" in out


def test_the_fix_prd_and_scope_are_narrow(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    rig = lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC2_FAIL, {}]))

    lp.run_loop(root, rig)

    prd = PRD.load(root / "scripts" / "kstrl" / "feature" / lp.FIX_1 / "prd.json")
    assert prd.allowed_paths == NARROW
    assert [s.id for s in prd.user_stories] == ["IF-1"]
    criteria = prd.user_stories[0].acceptance_criteria
    assert criteria[-1] == lp.TOOLING
    assert f"{h.STORE}" in criteria[1]
    assert lp.state(root)["fixes"][0]["scope"] == NARROW
    assert rig.scopes[lp.FIX_1].allowed_paths == NARROW
    assert rig.scopes[lp.FIX_1].source == "component_prd"
    resolved = [
        e for e in lp.events(root, "component_scope_resolved") if e["component"] == lp.FIX_1
    ]
    assert len(resolved) == 1
    assert resolved[0]["data"]["allowed_paths"] == NARROW
    plans = lp.events(root, "run_plan")
    assert [c["id"] for c in plans[-1]["data"]["components"]] == ["comp-a", "comp-b", lp.FIX_1]


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


@pytest.mark.parametrize("verdict", ["advisory", "fail"])
def test_a_carried_finding_closes_only_on_a_pass(tmp_path: Path, verdict: str) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    kept = {"IF-1": (verdict, f"{h.STORE}:1 still re-applies the rule")}
    reviewer = lp.ScriptedReviewer(base, [lp.IC2_FAIL, kept])
    rig = lp.Rig(root, reviewer)

    result, _out = lp.run_loop(root, rig, integration_max_rounds=2)

    state = lp.state(root)
    assert state["findings"][0]["status"] == "open"
    assert state["stops"][-1]["reason"].startswith("no progress")
    assert lp.manifest_ids(root) == ["comp-a", "comp-b", lp.FIX_1]
    assert result.exit_code == 1
    assert len(result.contract_failures) == 1
    assert result.contract_failures[0].startswith("integration IF-1: open:")
    halts = lp.run_halts(root)
    assert len(halts) == 1
    assert halts[0].evidence["open_findings"] == ["IF-1"]


def test_the_bound_stops_the_loop_with_a_record(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [lp.IC2_FAIL, lp.IC1_FAIL])
    rig = lp.Rig(root, reviewer)

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == [lp.FIX_1]
    assert lp.manifest_ids(root) == ["comp-a", "comp-b", lp.FIX_1]
    state = lp.state(root)
    assert [f["status"] for f in state["findings"]] == ["closed", "open"]
    assert state["stops"][-1]["reason"] == "bound reached: 1 of 1 fix components built"
    assert result.exit_code == 1
    assert [line.split(":")[0] for line in result.contract_failures] == ["integration IF-2"]
    assert lp.run_halts(root)[0].evidence["open_findings"] == ["IF-2"]


def test_a_resumed_run_counts_the_bound_from_the_manifest(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    lp.run_loop(root, lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC2_FAIL, lp.IC1_FAIL])))
    ic3 = {"IC3": ("fail", f"{h.API}:1 redefines the limit")}
    second = lp.Rig(root, lp.ScriptedReviewer(base, [ic3]))

    result, _out = lp.run_loop(root, second)

    assert second.launched == []
    assert lp.manifest_ids(root) == ["comp-a", "comp-b", lp.FIX_1]
    assert lp.state(root)["stops"][-1]["reason"] == "bound reached: 1 of 1 fix components built"
    assert result.exit_code == 1


def test_a_failed_fix_stops_the_loop_before_another_review(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [lp.IC2_FAIL])
    rig = lp.Rig(root, reviewer, fix_succeeds=False)

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == [lp.FIX_1]
    assert reviewer.calls == 1
    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "not_run"
    assert "integration-fix-1 ended failed" in stop["reason"]
    assert result.failed == [lp.FIX_1]
    assert result.exit_code == 1
    assert any(line.startswith("integration not_run:") for line in result.contract_failures)
    assert len(lp.run_halts(root)) == 1


@pytest.mark.parametrize("how", ["parked", "pending"])
def test_a_fix_that_is_not_merged_stops_the_loop(tmp_path: Path, how: str) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [lp.IC2_FAIL])
    rig = lp.Rig(root, reviewer, merge="pending" if how == "pending" else "merged")

    result, _out = lp.run_loop(root, rig, pause_before_pr_merge=how == "parked")

    assert reviewer.calls == 1
    status = Manifest.load(h.manifest_file(root)).get_component(lp.FIX_1)
    assert status is not None
    assert status.status in ("awaiting_approval", "merge_pending")
    assert f"ended {status.status}" in lp.state(root)["stops"][-1]["reason"]
    assert result.exit_code == 1
    assert len(lp.run_halts(root)) == 1


def test_an_integrated_test_failure_is_repaired_and_its_line_removed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [{}, {}])
    rig = lp.Rig(
        root,
        reviewer,
        fix_file=h.API,
        fix_text="from src.store import save\n\n\ndef handle(x: int) -> int:\n    return save(x)\n",
    )
    failing = (
        "grep -q integration-fix src/api.py || "
        "{ echo 'FAILED tests/test_api.py::t - src/api.py:3 broke'; exit 1; }"
    )

    result, out = lp.run_loop(
        root,
        rig,
        contract_config=ContractConfig(mode="tier", test_command=failing, timeout=60.0),
    )

    assert rig.launched == [lp.FIX_1]
    finding = lp.state(root)["findings"][0]
    assert finding["kind"] == "test"
    assert finding["status"] == "closed"
    assert result.contract_failures == []
    assert result.exit_code == 0
    assert "Contract failure recorded for tier" in out
    assert f"Integration fix {lp.FIX_1} built for IF-1" in out


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


def test_a_finding_with_no_test_path_is_handed_off(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root, {**lp.SCOPES, "comp-b": ["src/api.py"]})
    rig = lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC1_FAIL]))

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    finding = lp.state(root)["findings"][0]
    assert finding["status"] == "handoff"
    assert finding["handoffReason"].startswith("no test path could be determined")
    assert lp.manifest_ids(root) == ["comp-a", "comp-b"]
    assert result.exit_code == 1


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


def test_a_feature_that_is_not_complete_builds_no_fix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    manifest = Manifest.load(h.manifest_file(root))
    comp_b = manifest.get_component("comp-b")
    assert comp_b is not None
    comp_b.status = ComponentStatus.FAILED.value
    manifest.save(h.manifest_file(root))
    reviewer = lp.ScriptedReviewer(base, [lp.IC2_FAIL])
    rig = lp.Rig(root, reviewer)

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    assert reviewer.calls == 1
    assert lp.manifest_ids(root) == ["comp-a", "comp-b"]
    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "red"
    assert stop["reason"].startswith("the feature is not complete (comp-b)")
    assert result.exit_code == 1
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


def test_a_feature_prd_that_cannot_be_read_stops_the_loop_red(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    (root / "scripts" / "kstrl" / "feature" / "comp-b" / "prd.json").write_text(
        "{not json", encoding="utf-8"
    )
    rig = lp.Rig(root, lp.ScriptedReviewer(base, [lp.IC2_FAIL]))

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == []
    assert lp.manifest_ids(root) == ["comp-a", "comp-b"]
    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "red"
    assert stop["reason"].startswith("a feature PRD could not be read")
    assert lp.state(root).get("fixes", []) == []
    assert result.exit_code == 1
    assert len(lp.run_halts(root)) == 1


def test_a_register_finding_does_not_stop_a_code_fix(tmp_path: Path) -> None:
    """#497: round 1 has one code finding (IF-1) and one register finding
    (IF-2). The fix is built from IF-1; IF-2 stays handed off."""
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [{**lp.IC2_FAIL, **IC5_FAIL}, {}])
    rig = lp.Rig(root, reviewer)

    lp.run_loop(root, rig)

    assert rig.launched == [lp.FIX_1]
    assert reviewer.calls == 2
    state = lp.state(root)
    assert state["fixes"][0]["findings"] == ["IF-1"]
    prd = PRD.load(root / "scripts" / "kstrl" / "feature" / lp.FIX_1 / "prd.json")
    assert [s.id for s in prd.user_stories] == ["IF-1"]
    assert [(f["id"], f["status"]) for f in state["findings"]] == [
        ("IF-1", "closed"),
        ("IF-2", "handoff"),
    ]


def test_a_register_finding_from_an_earlier_round_fails_the_run(tmp_path: Path) -> None:
    """#497: the fix closes IF-1 and round 2 is clean, but IF-2 was handed
    off in round 1 of this run, so the run ends not clean."""
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [{**lp.IC2_FAIL, **IC5_FAIL}, {}])
    rig = lp.Rig(root, reviewer)

    result, _out = lp.run_loop(root, rig)

    stop = lp.state(root)["stops"][-1]
    assert stop["outcome"] == "red"
    assert stop["gates"] is True
    assert "IF-2" in stop["reason"]
    assert result.exit_code == 1
    ifs = [line for line in result.contract_failures if line.startswith("integration IF-")]
    assert len(ifs) == 1
    assert ifs[0].startswith("integration IF-2: handoff: IC5 failed:")
    halts = lp.run_halts(root)
    assert len(halts) == 1
    assert halts[0].evidence["open_findings"] == ["IF-2"]


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


def test_a_later_stop_names_its_own_cause_and_this_runs_handoff(tmp_path: Path) -> None:
    """#497: the fix built from IF-1 fails, so round 2 is not run. The run
    names that cause and IF-2, which round 1 handed off."""
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [{**lp.IC2_FAIL, **IC5_FAIL}])
    rig = lp.Rig(root, reviewer, fix_succeeds=False)

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == [lp.FIX_1]
    assert lp.state(root)["stops"][-1]["outcome"] == "not_run"
    assert result.exit_code == 1
    assert result.contract_failures == [
        "integration not_run: the last fix integration-fix-1 ended failed, not merged, "
        "so no tree holds it",
        "integration IF-2: handoff: IC5 failed: decision shutdown-semantics says 10 s, "
        "US-019 says 2 s",
    ]
    halts = lp.run_halts(root)
    assert len(halts) == 1
    assert halts[0].evidence["open_findings"] == ["IF-2"]


def test_a_bound_stop_names_this_rounds_finding_and_an_earlier_handoff(tmp_path: Path) -> None:
    """#497: round 2 opens IF-3 and the bound stops the loop. The run names
    IF-3 and IF-2, which round 1 handed off."""
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = lp.ScriptedReviewer(base, [{**lp.IC2_FAIL, **IC5_FAIL}, lp.IC1_FAIL])
    rig = lp.Rig(root, reviewer)

    result, _out = lp.run_loop(root, rig)

    assert rig.launched == [lp.FIX_1]
    state = lp.state(root)
    assert [(f["id"], f["status"]) for f in state["findings"]] == [
        ("IF-1", "closed"),
        ("IF-2", "handoff"),
        ("IF-3", "open"),
    ]
    assert state["stops"][-1]["reason"] == "bound reached: 1 of 1 fix components built"
    assert result.exit_code == 1
    assert [line.split(":")[0] for line in result.contract_failures] == [
        "integration IF-3",
        "integration IF-2",
    ]
    halts = lp.run_halts(root)
    assert len(halts) == 1
    assert halts[0].evidence["open_findings"] == ["IF-3", "IF-2"]
