"""R7.4 Linear integration, the parts that touch disk.

- LinearConfig.load reads the [linear] section of kstrl.toml and lets the
  environment override it.
- ProgressLog fan-out: an attached sink receives every emitted event, a
  dying sink neither raises out of emit nor loses the JSONL line, and the
  journal write precedes the fan-out.
- Manifest persistence and the `ks retry` interaction: Linear ids survive
  save/load and reset_for_retry, a pre-Linear manifest still loads, and a
  sink built from the reloaded manifest comments on the ORIGINAL issue so
  retries UPDATE rather than duplicate.

Everything runs against the dry-run client: no network, no LLM. The
client's transport behaviour (RATELIMITED retry, token hygiene, duplicate
recovery) and sync_decompose's mutation mapping have no end-to-end path
until a fake Linear transport exists, and are deliberately not pinned
here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kstrl.linear import LinearConfig, build_linear_sink
from kstrl.manifest import ComponentStatus, Manifest
from kstrl.observability import ProgressLog
from tests.spine_utils import component, make_manifest

TEAM_ID = "540e2302-e91c-42a7-92d7-e2f274bbf298"


def dry_config(**overrides: Any) -> LinearConfig:
    config = LinearConfig(
        enabled=True,
        team_id=TEAM_ID,
        dry_run=True,
        min_request_interval=0.0,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class Warnings:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(message)


class TestLinearConfig:
    def test_toml_then_env_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[linear]\n"
            "enabled = true\n"
            f'team_id = "{TEAM_ID}"\n'
            "min_request_interval = 2.0\n"
            'auth_mode = "api_key"\n'
        )
        config = LinearConfig.load(tmp_path)
        assert config.enabled is True
        assert config.team_id == TEAM_ID
        assert config.min_request_interval == 2.0
        assert config.auth_mode == "api_key"

        monkeypatch.setenv("KSTRL_LINEAR_MIN_INTERVAL", "0.25")
        monkeypatch.setenv("KSTRL_LINEAR_AUTH_MODE", "oauth")
        config = LinearConfig.load(tmp_path)
        assert config.min_request_interval == 0.25
        assert config.auth_mode == "oauth"


class TestProgressLogFanout:
    def test_sink_receives_emitted_events(self, tmp_path: Path) -> None:
        received: list[dict[str, Any]] = []

        class Recorder:
            def handle_event(self, event: dict[str, Any]) -> None:
                received.append(event)

        log = ProgressLog(tmp_path / "p.jsonl", run_id="run-1")
        log.attach_sink(Recorder())
        log.component_failed("comp-a", "boom")
        assert len(received) == 1
        assert received[0]["event"] == "component_failed"
        assert received[0]["component"] == "comp-a"
        assert received[0]["data"] == {"error": "boom"}

    def test_sink_exception_is_isolated(self, tmp_path: Path) -> None:
        """A dying sink neither raises out of emit nor loses the JSONL
        line (the failure-isolation requirement)."""
        warn = Warnings()

        class Bomb:
            def handle_event(self, event: dict[str, Any]) -> None:
                raise RuntimeError("sink exploded")

        log = ProgressLog(tmp_path / "p.jsonl", run_id="run-1", warn=warn)
        log.attach_sink(Bomb())
        log.component_failed("comp-a", "boom")  # must not raise
        events = log.read_events()
        assert len(events) == 1
        assert events[0]["event"] == "component_failed"
        assert any("Bomb" in m and "non-fatal" in m for m in warn.messages)

    def test_journal_write_precedes_fanout(self, tmp_path: Path) -> None:
        log = ProgressLog(tmp_path / "p.jsonl", run_id="run-1")
        seen_at_fanout: list[int] = []

        class Reader:
            def handle_event(self, event: dict[str, Any]) -> None:
                seen_at_fanout.append(len(log.read_events()))

        log.attach_sink(Reader())
        log.component_failed("comp-a", "boom")
        assert seen_at_fanout == [1]


class TestManifestPersistenceAndRetry:
    def make_mapped_manifest(self) -> Manifest:
        manifest = make_manifest([component("comp-a"), component("comp-b")])
        manifest.linear_project_id = "project-uuid"
        manifest.linear_sync_key = "run-1"
        manifest.components[0].linear_issue_id = "issue-uuid-a"
        manifest.components[0].linear_issue_identifier = "EXC-1"
        manifest.components[1].linear_issue_id = "issue-uuid-b"
        manifest.components[1].linear_issue_identifier = "EXC-2"
        return manifest

    def test_roundtrip_preserves_linear_fields(self, tmp_path: Path) -> None:
        manifest = self.make_mapped_manifest()
        manifest.save(tmp_path / "manifest.json")
        loaded = Manifest.load(tmp_path / "manifest.json")
        assert loaded.linear_project_id == "project-uuid"
        assert loaded.linear_sync_key == "run-1"
        assert loaded.components[0].linear_issue_id == "issue-uuid-a"
        assert loaded.components[0].linear_issue_identifier == "EXC-1"

    def test_pre_linear_manifest_still_loads(self, tmp_path: Path) -> None:
        manifest = make_manifest([component("comp-a")])
        manifest.save(tmp_path / "manifest.json")
        raw = json.loads((tmp_path / "manifest.json").read_text())
        del raw["linearProjectId"]
        del raw["linearSyncKey"]
        del raw["components"][0]["linearIssueId"]
        del raw["components"][0]["linearIssueIdentifier"]
        (tmp_path / "manifest.json").write_text(json.dumps(raw))
        loaded = Manifest.load(tmp_path / "manifest.json")
        assert loaded.components[0].linear_issue_id == ""
        assert loaded.linear_sync_key == ""

    def test_retry_reuses_issue_no_duplicate_creates(self, tmp_path: Path) -> None:
        """The `ks retry` interaction: after a failure, reset, and
        reload, the sink comments on the ORIGINAL issue and nothing
        creates a second one."""
        manifest = self.make_mapped_manifest()
        manifest.components[0].status = ComponentStatus.FAILED.value
        manifest.reset_for_retry("comp-a")
        assert manifest.components[0].status == ComponentStatus.PENDING.value
        # Linear mapping survives the reset...
        assert manifest.components[0].linear_issue_id == "issue-uuid-a"
        manifest.save(tmp_path / "manifest.json")
        reloaded = Manifest.load(tmp_path / "manifest.json")

        # ...and the sink built from the reloaded manifest targets the
        # persisted issue: zero creates across the retry.
        warn = Warnings()
        sink = build_linear_sink(
            reloaded,
            dry_config(),
            run_id="run-2",
            warn=warn,
        )
        assert sink is not None
        log_path = tmp_path / "p.jsonl"
        log = ProgressLog(log_path, run_id="run-2", warn=warn)
        log.attach_sink(sink)
        log.component_failed("comp-a", "still failing")
        recorded = sink._client.recorded
        assert [op for op, _ in recorded] == ["commentCreate"]
        assert recorded[0][1]["input"]["issueId"] == "issue-uuid-a"
