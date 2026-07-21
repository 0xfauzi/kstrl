"""R7.4 Linear integration.

Covers:
- LinearConfig toml/env resolution and validation ([linear] section).
- Deterministic client-generated UUIDs (the idempotency anchor).
- LinearClient: dry-run recording, defensive parsing against a faked
  transport, RATELIMITED-as-HTTP-400 retry, token hygiene (name in
  errors, value nowhere).
- sync_decompose: event-to-mutation mapping, story checklist, triage
  routing, double-fire convergence on identical UUIDs, per-component
  failure isolation.
- LinearSink: event-to-comment mapping, double-fire dedupe, unmapped
  components, exception isolation; ProgressLog fan-out isolation.
- Branch / PR-body formatting (identifier token, "Fixes <ID>").
- Manifest persistence + `ks retry` interaction: ids survive
  save/load and reset_for_retry, so retries UPDATE rather than
  duplicate.

Everything runs against the dry-run client or a faked urllib
transport - no network, no LLM.
"""

from __future__ import annotations

import io
import json
import urllib.error
import uuid
from pathlib import Path
from typing import Any

import pytest

from kstrl.decompose import SpecIssue
from kstrl.linear import (
    DecomposeSync,
    IssueRef,
    LinearClient,
    LinearConfig,
    LinearError,
    LinearSink,
    build_linear_sink,
    deterministic_uuid,
    linear_branch_name,
    resync_components,
    sync_decompose,
)
from kstrl.manifest import ComponentStatus, Manifest, validate_branch_name
from kstrl.observability import ProgressLog
from kstrl.pr import _generate_pr_body
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


def comp_data(comp_id: str, stories: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": comp_id,
        "title": comp_id.upper(),
        "description": f"Build {comp_id}",
        "userStories": [
            {"id": f"US-{i:03d}", "title": s}
            for i, s in enumerate(stories or ["do the thing"], start=1)
        ],
    }


class Warnings:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(message)


class TestLinearConfig:
    def test_defaults_disabled(self) -> None:
        config = LinearConfig()
        assert config.enabled is False
        assert config.token_env == "KSTRL_LINEAR_TOKEN"
        assert config.auth_mode == "auto"
        assert config.dry_run is False

    def test_enabled_requires_team_id(self) -> None:
        with pytest.raises(ValueError, match="team_id"):
            LinearConfig(enabled=True)

    def test_invalid_auth_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="auth_mode"):
            LinearConfig(auth_mode="bearer")

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

    def test_env_typo_surfaces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_LINEAR_AUTH_MODE", "bearrer")
        with pytest.raises(ValueError, match="auth_mode"):
            LinearConfig.load(tmp_path)

    def test_token_env_indirection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN_ENV", "MY_LINEAR_SECRET")
        config = LinearConfig.load(tmp_path)
        assert config.token_env == "MY_LINEAR_SECRET"


class TestDeterministicUuid:
    def test_stable_and_distinct(self) -> None:
        a1 = deterministic_uuid("run-1:comp-a")
        a2 = deterministic_uuid("run-1:comp-a")
        b = deterministic_uuid("run-1:comp-b")
        assert a1 == a2
        assert a1 != b

    def test_v4_format(self) -> None:
        parsed = uuid.UUID(deterministic_uuid("any-key"))
        assert parsed.version == 4
        assert parsed.variant == uuid.RFC_4122


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def fake_transport(
    monkeypatch: pytest.MonkeyPatch, payloads: list[Any]
) -> list[Any]:
    """Patch urlopen to pop canned payloads; returns the request log.

    A payload that is an Exception is raised; a dict becomes the JSON
    response body.
    """
    requests: list[Any] = []

    def fake_urlopen(request: Any, timeout: float = 0) -> FakeResponse:
        requests.append(request)
        result = payloads.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    monkeypatch.setattr("kstrl.linear.urllib.request.urlopen", fake_urlopen)
    return requests


def http_error(payload: dict[str, Any], code: int = 400) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.linear.app/graphql",
        code=code,
        msg="Bad Request",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(json.dumps(payload).encode()),
    )


class TestLinearClient:
    def test_dry_run_records_instead_of_sending(self) -> None:
        client = LinearClient(dry_config())
        ref = client.create_issue(
            team_id=TEAM_ID,
            title="T",
            description="D",
            client_id=deterministic_uuid("k:comp"),
        )
        assert client.recorded == [
            ("issueCreate", {
                "input": {
                    "id": deterministic_uuid("k:comp"),
                    "teamId": TEAM_ID,
                    "title": "T",
                    "description": "D",
                },
            }),
        ]
        assert ref.id == deterministic_uuid("k:comp")
        assert ref.identifier.startswith("DRY-")

    def test_missing_token_names_var_not_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KSTRL_LINEAR_TOKEN", raising=False)
        monkeypatch.delenv("KSTRL_LINEAR_TOKEN", raising=False)
        client = LinearClient(dry_config(dry_run=False))
        with pytest.raises(LinearError, match="KSTRL_LINEAR_TOKEN"):
            client.create_comment("issue-1", "body", deterministic_uuid("c"))

    def test_token_value_never_in_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "lin_api_supersecretvalue"
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN", secret)
        fake_transport(monkeypatch, [
            {"errors": [{"message": "boom", "extensions": {"code": "X"}}]},
        ])
        client = LinearClient(dry_config(dry_run=False))
        with pytest.raises(LinearError) as excinfo:
            client.create_comment("issue-1", "body", deterministic_uuid("c"))
        assert secret not in str(excinfo.value)

    def test_auth_header_modes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_abc")
        assert LinearClient(dry_config())._auth_header() == "lin_api_abc"
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_oauth_xyz")
        assert (
            LinearClient(dry_config())._auth_header() == "Bearer lin_oauth_xyz"
        )
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_abc")
        assert (
            LinearClient(dry_config(auth_mode="oauth"))._auth_header()
            == "Bearer lin_api_abc"
        )

    def test_non_json_response_is_linear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_abc")

        class Garbage(FakeResponse):
            def __init__(self) -> None:
                self._raw = b"<html>gateway error</html>"

        def fake_urlopen(request: Any, timeout: float = 0) -> FakeResponse:
            return Garbage()

        monkeypatch.setattr(
            "kstrl.linear.urllib.request.urlopen", fake_urlopen
        )
        client = LinearClient(dry_config(dry_run=False))
        with pytest.raises(LinearError, match="non-JSON"):
            client.create_comment("issue-1", "body", deterministic_uuid("c"))

    def test_malformed_success_payload_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_abc")
        fake_transport(monkeypatch, [
            {"data": {"issueCreate": {"success": True, "issue": "not-a-dict"}}},
            # get_issue recovery probe for the duplicate-id path:
            {"data": {"issue": None}},
        ])
        client = LinearClient(dry_config(dry_run=False))
        with pytest.raises(LinearError, match="malformed"):
            client.create_issue(TEAM_ID, "T", "D", deterministic_uuid("c"))

    def test_ratelimited_retries_once_then_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_abc")
        sleeps: list[float] = []
        monkeypatch.setattr(
            "kstrl.linear.time.sleep", lambda s: sleeps.append(s)
        )
        limited = {
            "errors": [{"message": "rl", "extensions": {"code": "RATELIMITED"}}]
        }
        requests = fake_transport(
            monkeypatch, [http_error(limited), http_error(limited)]
        )
        client = LinearClient(dry_config(dry_run=False))
        with pytest.raises(LinearError, match="rate limited"):
            client.create_comment("issue-1", "body", deterministic_uuid("c"))
        assert len(requests) == 2
        assert sleeps  # backed off between attempts

    def test_ratelimited_then_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_abc")
        monkeypatch.setattr("kstrl.linear.time.sleep", lambda s: None)
        limited = {
            "errors": [{"message": "rl", "extensions": {"code": "RATELIMITED"}}]
        }
        fake_transport(monkeypatch, [
            http_error(limited),
            {"data": {"commentCreate": {"success": True}}},
        ])
        client = LinearClient(dry_config(dry_run=False))
        client.create_comment("issue-1", "body", deterministic_uuid("c"))

    def test_duplicate_create_recovers_existing_issue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Double-fired create converges on the already-created issue."""
        monkeypatch.setenv("KSTRL_LINEAR_TOKEN", "lin_api_abc")
        client_id = deterministic_uuid("k:comp")
        fake_transport(monkeypatch, [
            http_error({"errors": [{
                "message": "id already exists",
                "extensions": {"code": "INVALID_INPUT"},
            }]}),
            {"data": {"issue": {"id": client_id, "identifier": "EXC-7"}}},
        ])
        client = LinearClient(dry_config(dry_run=False))
        ref = client.create_issue(TEAM_ID, "T", "D", client_id)
        assert ref == IssueRef(id=client_id, identifier="EXC-7")


class TestSyncDecompose:
    def make_sync(
        self,
        components: list[dict[str, Any]] | None = None,
        spec_issues: list[SpecIssue] | None = None,
        config: LinearConfig | None = None,
    ) -> tuple[DecomposeSync | None, LinearClient, Warnings]:
        config = config or dry_config()
        warn = Warnings()
        client = LinearClient(config, warn=warn)
        sync = sync_decompose(
            project_name="demo",
            components=components
            if components is not None
            else [comp_data("comp-a"), comp_data("comp-b")],
            spec_issues=spec_issues or [],
            config=config,
            client=client,
            warn=warn,
            sync_key="run-1",
        )
        return sync, client, warn

    def test_disabled_is_inert(self) -> None:
        config = LinearConfig()  # disabled
        warn = Warnings()
        client = LinearClient(config, warn=warn)
        sync = sync_decompose(
            project_name="demo",
            components=[comp_data("comp-a")],
            spec_issues=[],
            config=config,
            client=client,
            warn=warn,
        )
        assert sync is None
        assert client.recorded == []

    def test_project_and_issue_per_component(self) -> None:
        sync, client, warn = self.make_sync()
        assert sync is not None
        operations = [op for op, _ in client.recorded]
        assert operations == ["projectCreate", "issueCreate", "issueCreate"]
        assert set(sync.issues) == {"comp-a", "comp-b"}
        # The project id is threaded into every component issue.
        for _, variables in client.recorded[1:]:
            assert variables["input"]["projectId"] == sync.project_id
        assert warn.messages == []

    def test_story_checklist_and_external_key_in_description(self) -> None:
        _, client, _ = self.make_sync(
            components=[comp_data("comp-a", ["first story", "second story"])],
        )
        description = client.recorded[1][1]["input"]["description"]
        assert "- [ ] US-001: first story" in description
        assert "- [ ] US-002: second story" in description
        assert "kstrl external key: run-1:comp-a" in description

    def test_spec_issues_filed_as_triage(self) -> None:
        issue = SpecIssue(
            severity="major",
            kind="ambiguity",
            summary="What does done mean?",
            location="spec.md#goals",
            suggestion="Define acceptance",
        )
        _, client, _ = self.make_sync(
            components=[comp_data("comp-a")], spec_issues=[issue],
        )
        op, variables = client.recorded[-1]
        assert op == "issueCreate"
        triage_input = variables["input"]
        assert triage_input["title"].startswith("[spec] ")
        # No stateId and no projectId: a triage-enabled team routes the
        # issue to its Triage inbox; spec problems are not component work.
        assert "stateId" not in triage_input
        assert "projectId" not in triage_input
        assert "Severity: major" in triage_input["description"]
        assert "kstrl external key: run-1:spec:0" in triage_input["description"]

    def test_double_fire_converges_on_identical_uuids(self) -> None:
        """Same sync_key twice -> byte-identical create ids (the remote
        dedupe key), so a double-fire cannot mint second objects."""
        _, client_one, _ = self.make_sync()
        _, client_two, _ = self.make_sync()
        ids_one = [v["input"]["id"] for _, v in client_one.recorded]
        ids_two = [v["input"]["id"] for _, v in client_two.recorded]
        assert ids_one == ids_two

    def test_component_failure_isolated(self) -> None:
        config = dry_config()
        warn = Warnings()

        class FlakyClient(LinearClient):
            def create_issue(
                self,
                team_id: str,
                title: str,
                description: str,
                client_id: str,
                project_id: str = "",
            ) -> IssueRef:
                if "comp-a" in description:
                    raise LinearError("boom")
                return super().create_issue(
                    team_id, title, description, client_id, project_id
                )

        client = FlakyClient(config, warn=warn)
        sync = sync_decompose(
            project_name="demo",
            components=[comp_data("comp-a"), comp_data("comp-b")],
            spec_issues=[],
            config=config,
            client=client,
            warn=warn,
            sync_key="run-1",
        )
        assert sync is not None
        assert set(sync.issues) == {"comp-b"}
        assert any("comp-a" in m for m in warn.messages)

    def test_project_failure_returns_none(self) -> None:
        config = dry_config()
        warn = Warnings()

        class DeadClient(LinearClient):
            def create_project(
                self,
                name: str,
                team_id: str,
                client_id: str,
                description: str = "",
            ) -> str:
                raise LinearError("no project for you")

        sync = sync_decompose(
            project_name="demo",
            components=[comp_data("comp-a")],
            spec_issues=[],
            config=config,
            client=DeadClient(config, warn=warn),
            warn=warn,
            sync_key="run-1",
        )
        assert sync is None
        assert any("project creation failed" in m for m in warn.messages)

    def test_resync_updates_instead_of_creating(self) -> None:
        """A manifest that already maps issues gets issueUpdate only."""
        manifest = make_manifest([component("comp-a")])
        manifest.linear_sync_key = "run-1"
        manifest.components[0].linear_issue_id = "issue-uuid-a"
        manifest.components[0].linear_issue_identifier = "EXC-1"
        config = dry_config()
        client = LinearClient(config)
        resync_components(
            manifest,
            [comp_data("comp-a", ["revised story"])],
            config,
            client,
            warn=Warnings(),
        )
        operations = [op for op, _ in client.recorded]
        assert operations == ["issueUpdate"]
        assert client.recorded[0][1]["id"] == "issue-uuid-a"
        assert "revised story" in client.recorded[0][1]["input"]["description"]


class TestLinearSink:
    def make_sink(
        self, issue_ids: dict[str, str] | None = None
    ) -> tuple[LinearSink, LinearClient, Warnings]:
        warn = Warnings()
        client = LinearClient(dry_config(), warn=warn)
        sink = LinearSink(
            client,
            issue_ids if issue_ids is not None else {"comp-a": "issue-uuid-a"},
            run_id="run-9",
            warn=warn,
        )
        return sink, client, warn

    def failed_event(self, error: str = "tests failed") -> dict[str, Any]:
        return {
            "ts": "2026-07-19T12:00:00Z",
            "event": "component_failed",
            "run_id": "run-9",
            "component": "comp-a",
            "data": {"error": error},
        }

    def test_component_failed_maps_to_comment(self) -> None:
        sink, client, _ = self.make_sink()
        sink.handle_event(self.failed_event())
        assert len(client.recorded) == 1
        op, variables = client.recorded[0]
        assert op == "commentCreate"
        assert variables["input"]["issueId"] == "issue-uuid-a"
        assert "tests failed" in variables["input"]["body"]
        assert "run-9" in variables["input"]["body"]

    def test_budget_exceeded_maps_to_comment(self) -> None:
        sink, client, _ = self.make_sink()
        sink.handle_event({
            "ts": "2026-07-19T12:00:00Z",
            "event": "budget_exceeded",
            "component": "comp-a",
            "data": {"total_tokens": 900, "max_total_tokens": 800},
        })
        assert len(client.recorded) == 1
        assert "900" in client.recorded[0][1]["input"]["body"]

    def test_double_fire_is_deduped(self) -> None:
        sink, client, _ = self.make_sink()
        sink.handle_event(self.failed_event())
        sink.handle_event(self.failed_event())
        assert len(client.recorded) == 1
        # A DIFFERENT failure on the same component is new information.
        sink.handle_event(self.failed_event("other error"))
        assert len(client.recorded) == 2

    def test_lifecycle_events_not_mirrored(self) -> None:
        sink, client, _ = self.make_sink()
        for event_type in (
            "component_started", "component_completed",
            "verification_result", "review_result", "factory_completed",
        ):
            sink.handle_event({
                "ts": "t", "event": event_type,
                "component": "comp-a", "data": {"passed": True},
            })
        assert client.recorded == []

    def test_unmapped_component_ignored(self) -> None:
        sink, client, _ = self.make_sink({"other": "issue-x"})
        sink.handle_event(self.failed_event())
        assert client.recorded == []

    def test_client_failure_warns_never_raises(self) -> None:
        warn = Warnings()

        class DeadClient(LinearClient):
            def create_comment(
                self, issue_id: str, body: str, client_id: str
            ) -> None:
                raise LinearError("api down")

        sink = LinearSink(
            DeadClient(dry_config(), warn=warn),
            {"comp-a": "issue-uuid-a"},
            run_id="run-9",
            warn=warn,
        )
        sink.handle_event(self.failed_event())  # must not raise
        assert any("non-fatal" in m for m in warn.messages)

    def test_comment_ids_deterministic_for_same_event(self) -> None:
        """The remote dedupe key: same event -> same comment UUID even
        across separate sink instances (process restart)."""
        sink_one, client_one, _ = self.make_sink()
        sink_two, client_two, _ = self.make_sink()
        sink_one.handle_event(self.failed_event())
        sink_two.handle_event(self.failed_event())
        assert (
            client_one.recorded[0][1]["input"]["id"]
            == client_two.recorded[0][1]["input"]["id"]
        )


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


class TestBranchAndPrBody:
    def test_branch_carries_lowercase_identifier_token(self) -> None:
        branch = linear_branch_name("EXC-42", "auth-api")
        assert branch == "kstrl/factory/exc-42-auth-api"
        assert validate_branch_name(branch) is None

    def test_pr_body_gains_fixes_trailer(self) -> None:
        comp = component("comp-a")
        comp.linear_issue_identifier = "EXC-42"
        body = _generate_pr_body(comp, make_manifest([comp]))
        assert "Fixes EXC-42" in body

    def test_pr_body_without_mapping_has_no_trailer(self) -> None:
        comp = component("comp-a")
        body = _generate_pr_body(comp, make_manifest([comp]))
        assert "Fixes" not in body


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

    def test_retry_reuses_issue_no_duplicate_creates(
        self, tmp_path: Path
    ) -> None:
        """The `ks retry` interaction: after a failure, reset, and
        reload, the sink comments on the ORIGINAL issue and nothing
        creates a second one."""
        manifest = self.make_mapped_manifest()
        manifest.components[0].status = ComponentStatus.FAILED.value
        manifest.reset_for_retry("comp-a")
        assert (
            manifest.components[0].status == ComponentStatus.PENDING.value
        )
        # Linear mapping survives the reset...
        assert manifest.components[0].linear_issue_id == "issue-uuid-a"
        manifest.save(tmp_path / "manifest.json")
        reloaded = Manifest.load(tmp_path / "manifest.json")

        # ...and the sink built from the reloaded manifest targets the
        # persisted issue: zero creates across the retry.
        warn = Warnings()
        sink = build_linear_sink(
            reloaded, dry_config(), run_id="run-2", warn=warn,
        )
        assert sink is not None
        log_path = tmp_path / "p.jsonl"
        log = ProgressLog(log_path, run_id="run-2", warn=warn)
        log.attach_sink(sink)
        log.component_failed("comp-a", "still failing")
        recorded = sink._client.recorded
        assert [op for op, _ in recorded] == ["commentCreate"]
        assert recorded[0][1]["input"]["issueId"] == "issue-uuid-a"


class TestBuildLinearSink:
    def test_disabled_returns_none_silently(self) -> None:
        warn = Warnings()
        sink = build_linear_sink(
            make_manifest([component("comp-a")]),
            LinearConfig(),
            run_id="r",
            warn=warn,
        )
        assert sink is None
        assert warn.messages == []

    def test_enabled_without_mapping_warns_inactive(self) -> None:
        warn = Warnings()
        sink = build_linear_sink(
            make_manifest([component("comp-a")]),
            dry_config(),
            run_id="r",
            warn=warn,
        )
        assert sink is None
        assert any("sink inactive" in m for m in warn.messages)

    def test_live_mode_without_token_warns_inactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KSTRL_LINEAR_TOKEN", raising=False)
        monkeypatch.delenv("KSTRL_LINEAR_TOKEN", raising=False)
        manifest = make_manifest([component("comp-a")])
        manifest.components[0].linear_issue_id = "issue-uuid-a"
        warn = Warnings()
        sink = build_linear_sink(
            manifest, dry_config(dry_run=False), run_id="r", warn=warn,
        )
        assert sink is None
        assert any("KSTRL_LINEAR_TOKEN" in m for m in warn.messages)
