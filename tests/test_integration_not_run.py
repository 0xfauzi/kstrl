"""End to end: the record-only integration review does not run (#482)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kstrl.contract import ContractConfig, ContractMode
from kstrl.factory import FactoryConfig
from kstrl.integration_state import fresh_state
from kstrl.manifest import Manifest
from kstrl.pipeline import ComponentPipeline
from tests.helpers import integration_harness as h


@pytest.mark.parametrize(
    "overrides,word",
    [
        ({"single_pr": True}, "single_pr"),
        ({"create_prs": False}, "create_prs"),
        ({"review_mode": "skip"}, "review_mode = skip"),
    ],
)
def test_not_run_modes_record_it_in_the_summary(
    tmp_path: Path, overrides: dict[str, object], word: str
) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    all_overrides = dict(overrides)
    all_overrides["contract_config"] = ContractConfig(mode=ContractMode.SKIP.value)
    result, out = h.run_factory_over(root, reviewer, **all_overrides)

    assert reviewer.calls == 0
    assert result.exit_code == 0
    assert "Integration review" in out
    assert word in out
    assert "does not gate" in out
    assert not h.state_file(root).exists()
    assert h.evidence_files(root) == []


def test_the_config_key_turns_the_review_off(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    result, out = h.run_factory_over(
        root,
        reviewer,
        contract_config=ContractConfig(mode=ContractMode.SKIP.value),
        integration_review=False,
    )

    assert reviewer.calls == 0
    assert result.exit_code == 0
    assert "integration_review = false" in out
    assert not h.state_file(root).exists()
    assert h.evidence_files(root) == []


def test_a_missing_feature_base_records_not_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    h.merged_feature(root, feature_base="")
    payload_reviewer = h.FakeReviewer("{}")

    h.run_factory_over(root, payload_reviewer)

    evidence = h.evidence_files(root)
    assert len(evidence) == 1
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert ev["outcome"] == "not_run"
    assert "no feature base" in ev["reason"]
    assert not h.state_file(root).exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b"\xff\xfe{}",
        b'{"schemaVersion": 1}',
    ],
)
def test_unreadable_state_is_refused_and_left_byte_identical(tmp_path: Path, raw: bytes) -> None:
    root = tmp_path / "repo"
    h.merged_feature(root)
    state_path = h.state_file(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(raw)
    payload_reviewer = h.FakeReviewer("{}")

    h.run_factory_over(root, payload_reviewer)

    assert payload_reviewer.calls == 0
    assert state_path.read_bytes() == raw
    evidence = h.evidence_files(root)
    assert len(evidence) == 1
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert ev["outcome"] == "not_run"
    assert "unreadable" in ev["reason"]


def test_a_state_for_another_feature_is_replaced_when_no_fix_component_exists(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    state_path = h.state_file(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    foreign = fresh_state(
        {
            "manifestPath": str(h.manifest_file(root).resolve()),
            "project": "test",
            "specFile": "spec.md",
            "featureBaseSha": "f" * 40,
        }
    )
    state_path.write_text(json.dumps(foreign), encoding="utf-8")
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    _result, out = h.run_factory_over(root, reviewer)

    assert reviewer.calls == 1
    new_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert new_state["featureBaseSha"] == base
    assert new_state["replacedBinding"]["featureBaseSha"] == "f" * 40
    assert "belonged to another feature" in out


def test_a_state_for_another_feature_is_refused_once_a_fix_component_exists(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    h.merged_feature(root, extra_components=("integration-fix-1",))
    state_path = h.state_file(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    foreign = fresh_state(
        {
            "manifestPath": str(h.manifest_file(root).resolve()),
            "project": "test",
            "specFile": "spec.md",
            "featureBaseSha": "f" * 40,
        }
    )
    raw = json.dumps(foreign).encode("utf-8")
    state_path.write_bytes(raw)
    payload_reviewer = h.FakeReviewer("{}")

    h.run_factory_over(root, payload_reviewer)

    assert state_path.read_bytes() == raw
    evidence = h.evidence_files(root)
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert "belongs to" in ev["reason"]


def test_missing_state_is_refused_once_a_fix_component_exists(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    h.merged_feature(root, extra_components=("integration-fix-1",))
    payload_reviewer = h.FakeReviewer("{}")

    h.run_factory_over(root, payload_reviewer)

    evidence = h.evidence_files(root)
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert "missing" in ev["reason"]
    assert not h.state_file(root).exists()


def test_an_exhausted_call_budget_records_not_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    with patch.object(ComponentPipeline, "adversarial_budget_ok", return_value=False):
        h.run_factory_over(root, reviewer)

    assert reviewer.calls == 0
    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["stops"][-1]["outcome"] == "not_run"
    assert "budget" in state["stops"][-1]["reason"]
    assert state["findings"] == []


def test_nothing_merged_since_the_feature_base_records_not_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _base, head = h.merged_feature(root)
    manifest = Manifest.load(h.manifest_file(root))
    manifest.feature_base_sha = head
    manifest.save(h.manifest_file(root))
    payload_reviewer = h.FakeReviewer("{}")

    h.run_factory_over(root, payload_reviewer)

    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["stops"][-1]["outcome"] == "not_run"
    assert "nothing merged" in state["stops"][-1]["reason"]


def test_integration_review_loads_from_toml_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KSTRL_FACTORY_INTEGRATION_REVIEW", raising=False)

    assert FactoryConfig().integration_review is True

    (tmp_path / "kstrl.toml").write_text(
        "[factory]\nintegration_review = false\n", encoding="utf-8"
    )
    assert FactoryConfig.load(tmp_path).integration_review is False

    monkeypatch.setenv("KSTRL_FACTORY_INTEGRATION_REVIEW", "1")
    assert FactoryConfig.load(tmp_path).integration_review is True

    (tmp_path / "kstrl.toml").write_text("[factory]\nintegration_review = true\n", encoding="utf-8")
    monkeypatch.setenv("KSTRL_FACTORY_INTEGRATION_REVIEW", "0")
    assert FactoryConfig.load(tmp_path).integration_review is False

    monkeypatch.delenv("KSTRL_FACTORY_INTEGRATION_REVIEW", raising=False)
    assert FactoryConfig.from_env().integration_review is True
    monkeypatch.setenv("KSTRL_FACTORY_INTEGRATION_REVIEW", "0")
    assert FactoryConfig.from_env().integration_review is False

    monkeypatch.delenv("KSTRL_FACTORY_INTEGRATION_REVIEW", raising=False)
    (tmp_path / "kstrl.toml").write_text(
        '[factory]\nintegration_review = "false"\n', encoding="utf-8"
    )
    from kstrl.config import ConfigError

    with pytest.raises(ConfigError):
        FactoryConfig.load(tmp_path)


def test_a_reached_cost_ceiling_records_not_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    reviewer = h.FakeReviewer(json.dumps(payload))

    with patch.object(ComponentPipeline, "breached_ceiling", return_value="max_cost_usd"):
        result, _out = h.run_factory_over(root, reviewer)

    assert reviewer.calls == 0
    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["stops"][-1]["outcome"] == "not_run"
    assert state["stops"][-1]["reason"] == "the run reached max_cost_usd"
    assert result.exit_code == 0


def test_a_state_path_that_cannot_be_read_is_refused_not_treated_as_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    h.merged_feature(root)
    state_path = h.state_file(root)
    state_path.mkdir(parents=True)
    marker = state_path / "keep.txt"
    marker.write_text("operator data\n", encoding="utf-8")
    reviewer = h.FakeReviewer("{}")

    h.run_factory_over(root, reviewer)

    assert reviewer.calls == 0
    assert state_path.is_dir()
    assert marker.read_text(encoding="utf-8") == "operator data\n"
    evidence = h.evidence_files(root)
    assert len(evidence) == 1
    ev = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert ev["outcome"] == "not_run"
    assert "unreadable" in ev["reason"]
