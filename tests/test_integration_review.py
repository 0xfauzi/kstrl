"""End to end: the record-only integration review runs (#482)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.contract import ContractConfig, ContractMode
from kstrl.evolution import INTEGRATION_RESULT_EVENT
from kstrl.integration import integration_stories
from kstrl.pipeline import ComponentPipeline
from tests.helpers import integration_harness as h


def test_a_clean_review_is_recorded_and_does_not_gate(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    result, out = h.run_factory_over(root, reviewer)

    assert reviewer.calls == 1
    assert reviewer.cwds[0] is not None
    assert reviewer.cwds[0].is_relative_to(root / ".kstrl" / "contract")
    assert not reviewer.cwds[0].exists()
    assert base in reviewer.prompts[0]
    for story in integration_stories(base):
        assert story.criterion in reviewer.prompts[0]

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["featureBaseSha"] == base
    assert state["lastReviewedSha"] == head
    assert state["findings"] == []
    assert state["stops"][-1]["outcome"] == "clean"
    assert state["stops"][-1]["gates"] is False
    assert state["project"] == "test"
    assert state["specFile"] == "spec.md"
    assert state["manifestPath"] == str(h.manifest_file(root).resolve())

    evidence = h.evidence_files(root)
    assert len(evidence) == 1
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert ev["outcome"] == "clean"
    assert ev["reviewedSha"] == head
    assert ev["phase3"]["ran"] is True
    assert ev["phase3"]["passed"] is True
    assert ev["review"]["mode"] == "hard"

    assert "Integration review" in out
    assert "does not gate" in out
    assert result.contract_failures == []


def test_a_failing_criterion_opens_one_finding_from_a_hard_review(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    h.set_verdict(payload, "IC2", "fail", f"{h.STORE}:1 re-applies request rules to stored rows")
    reviewer = h.FakeReviewer(json.dumps(payload))

    result, _out = h.run_factory_over(root, reviewer)

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    findings = state["findings"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["id"] == "IF-1"
    assert finding["kind"] == "criterion"
    assert finding["storyId"] == "IC2"
    assert finding["status"] == "open"
    assert finding["locations"] == ["src/store.py"]
    assert state["stops"][-1]["outcome"] == "open_findings"
    assert result.contract_failures == []
    assert result.exit_code == 0


def test_a_scope_creep_fail_concern_opens_nothing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    payload["concerns"] = [
        {
            "category": "scope_creep",
            "severity": "fail",
            "location": f"{h.API}:1",
            "explanation": "drive-by rename",
            "suggestion": "",
        }
    ]
    reviewer = h.FakeReviewer(json.dumps(payload))

    _result, _out = h.run_factory_over(root, reviewer)

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["findings"] == []
    assert state["stops"][-1]["outcome"] == "clean"

    evidence = h.evidence_files(root)
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    recorded = ev["recorded"]
    assert len(recorded) == 1
    assert recorded[0]["category"] == "scope_creep"


def test_an_error_handling_fail_concern_opens_one_finding(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    payload["concerns"] = [
        {
            "category": "error_handling",
            "severity": "fail",
            "location": f"{h.API}:1",
            "explanation": "drive-by rename",
            "suggestion": "",
        }
    ]
    reviewer = h.FakeReviewer(json.dumps(payload))

    _result, _out = h.run_factory_over(root, reviewer)

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    findings = state["findings"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["kind"] == "concern"
    assert finding["category"] == "error_handling"
    assert finding["locations"] == ["src/api.py"]
    assert finding["status"] == "open"
    assert state["stops"][-1]["outcome"] == "open_findings"


def test_an_integrated_test_failure_opens_a_finding(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    result, _out = h.run_factory_over(
        root,
        reviewer,
        contract_config=ContractConfig(
            mode="tier",
            test_command="echo 'FAILED tests/test_api.py::test_x - src/api.py:3 broke'; exit 1",
            timeout=60.0,
        ),
    )

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    findings = state["findings"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["kind"] == "test"
    assert finding["status"] == "open"
    assert finding["locations"] == ["src/api.py"]
    assert finding["missingLocations"] == ["tests/test_api.py"]
    assert "FAILED tests/test_api.py::test_x" in finding["text"]

    evidence = h.evidence_files(root)
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert "FAILED tests/test_api.py::test_x" in ev["phase3"]["output"]
    assert ev["phase3"]["passed"] is False

    twin = tmp_path / "twin"
    twin_base, _twin_head = h.merged_feature(twin)
    twin_reviewer = h.FakeReviewer(json.dumps(h.review_payload(twin, twin_base)))
    contract_config = ContractConfig(
        mode="tier",
        test_command="echo 'FAILED tests/test_api.py::test_x - src/api.py:3 broke'; exit 1",
        timeout=60.0,
    )
    twin_result, _twin_out = h.run_factory_over(
        twin, twin_reviewer, contract_config=contract_config, integration_review=False
    )

    assert twin_reviewer.calls == 0
    assert result.exit_code == 1
    assert twin_result.exit_code == 1
    assert result.contract_failures == twin_result.contract_failures
    assert len(result.contract_failures) == 1


def test_a_register_failure_is_handed_off(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    h.set_verdict(
        payload,
        "IC5",
        "fail",
        "decision shutdown-semantics says 10 s, US-019 says 2 s",
    )
    reviewer = h.FakeReviewer(json.dumps(payload))

    _result, _out = h.run_factory_over(root, reviewer)

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    findings = state["findings"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["kind"] == "register"
    assert finding["status"] == "handoff"
    assert finding["locations"] == []


def test_a_finding_that_cites_no_existing_file_is_handed_off(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    h.set_verdict(payload, "IC1", "fail", "lib/missing.py:3 calls save wrongly")
    reviewer = h.FakeReviewer(json.dumps(payload))

    _result, _out = h.run_factory_over(root, reviewer)

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    findings = state["findings"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["status"] == "handoff"
    assert finding["locations"] == []
    assert finding["missingLocations"] == ["lib/missing.py"]


def test_contract_mode_skip_still_runs_the_review(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    _result, _out = h.run_factory_over(
        root, reviewer, contract_config=ContractConfig(mode=ContractMode.SKIP.value)
    )

    assert reviewer.calls == 1
    evidence = h.evidence_files(root)
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert ev["phase3"] == {"ran": False}
    assert ev["reviewedSha"] == head
    assert ev["outcome"] == "clean"


def test_unparseable_review_output_is_red_and_opens_nothing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    h.merged_feature(root)
    reviewer = h.FakeReviewer("this is not json")

    result, _out = h.run_factory_over(root, reviewer)

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["stops"][-1]["outcome"] == "red"
    assert state["findings"] == []
    evidence = h.evidence_files(root)
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert any("infrastructure error" in e for e in ev["errors"])
    assert result.exit_code == 0


def test_the_review_is_metered_and_spends_one_adversarial_call(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    with patch.object(
        ComponentPipeline,
        "adversarial_budget_consume",
        autospec=True,
        side_effect=ComponentPipeline.adversarial_budget_consume,
    ) as spent:
        h.run_factory_over(root, reviewer, max_adversarial_calls=5)

    assert spent.call_count == 1
    events = sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    assert len(events) == 1
    lines = events[0].read_text(encoding="utf-8").splitlines()
    usage_lines = [json.loads(line) for line in lines if line.strip()]
    usage_lines = [line for line in usage_lines if line.get("event") == "component_usage"]
    assert len(usage_lines) == 1
    assert usage_lines[0]["component"] == "@integration"
    assert usage_lines[0]["data"]["phase"] == "integration"


def test_a_round_is_journaled(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    h.run_factory_over(root, reviewer)

    journal_path = root / ".kstrl" / "evolution.jsonl"
    entries = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    integration_entries = [e for e in entries if e["event_type"] == INTEGRATION_RESULT_EVENT]
    assert len(integration_entries) == 1
    entry = integration_entries[0]
    assert entry["outcome"] == "clean"
    assert entry["reviewed_sha"] == head
    assert entry["gates"] is False

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert entry["run_id"] == state["stops"][-1]["runId"]


def test_a_base_that_moves_during_the_review_is_recorded(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    moved: list[str] = []

    def on_prompt(_prompt: str) -> None:
        moved.append(h.commit_file(root, "late.txt", "late\n"))

    reviewer = h.FakeReviewer(json.dumps(payload), on_prompt=on_prompt)

    h.run_factory_over(root, reviewer)

    evidence = h.evidence_files(root)
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert ev["reviewedSha"] != moved[0]
    assert ev["baseMovedTo"] == moved[0]


def test_the_enrolled_criteria_reach_the_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kstrl import integration

    alt = (
        "\n".join(
            f"IC{n} | Title {n} | H3 marker criterion {n} for {{feature_base_sha}}"
            for n in range(1, 6)
        )
        + "\n"
    )
    monkeypatch.setattr(integration, "INTEGRATION_CRITERIA_PROMPT", alt)

    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    h.run_factory_over(root, reviewer)

    for n in range(1, 6):
        assert f"H3 marker criterion {n} for {base}" in reviewer.prompts[0]

    evidence = h.evidence_files(root)
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    for n in range(1, 6):
        assert ev["stories"][n - 1]["criterion"] == f"H3 marker criterion {n} for {base}"
