"""R7.6: claude-sdk adapter + sdk_runner unit tests (no LLM calls).

Covers the seams that do NOT need a subprocess:

- the adapter <-> runner prefixed-line contract (parse, degrade on
  malformed payloads, UsageRecord mapping with R3.1 lower-bound
  semantics);
- the run() flow against a fake DeadlineStreamer (contract lines
  consumed, display lines passed through, timeout path);
- the stdin config document (sandbox settings parity with the CLI
  adapter, permission-mode drop mirroring, budget/cwd pass-through);
- the runner's workspace guard (path containment incl. symlink escape,
  deny shape, non-guarded tools untouched);
- ResultMessage -> contract-record emission using the real SDK types;
- registration: get_agent dispatch, R7.1 family mapping + identity,
  config plumb ([agent] budget_usd + KSTRL_AGENT_BUDGET_USD), cli
  preflight.

The kill semantics live in test_timeout_enforcement.py (R0.1 battery,
real subprocesses); the live end-to-end path is a measured smoke in the
R7.6 PR (H4: what was tested vs assumed is stated there).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from kstrl.agents import ClaudeSdkAgent, get_agent, sdk_runner
from kstrl.agents import claude_sdk as claude_sdk_module
from kstrl.agents.claude_sdk import (
    RESULT_PREFIX,
    USAGE_PREFIX,
    _parse_contract_line,
    _usage_record_from_payload,
)
from kstrl.agents.proc import TIMEOUT_MESSAGE_PREFIX
from kstrl.config import (
    KstrlConfig,
    _apply_env_overrides,
    _apply_toml_overrides,
)
from kstrl.factory import (
    _CROSS_FAMILY_TYPE,
    _agent_identity,
    _cli_family,
)
from kstrl.sandbox import (
    SandboxConfig,
    claude_sandbox_args,
    claude_sandbox_settings,
)

# ---------------------------------------------------------------------------
# Contract-line parsing + UsageRecord mapping
# ---------------------------------------------------------------------------


class TestContractParsing:
    def test_valid_payload_parses(self) -> None:
        line = USAGE_PREFIX + json.dumps({"input_tokens": 5})
        assert _parse_contract_line(line, USAGE_PREFIX) == {"input_tokens": 5}

    def test_malformed_json_degrades_to_none(self) -> None:
        assert _parse_contract_line(USAGE_PREFIX + "{oops", USAGE_PREFIX) is None

    def test_non_dict_payload_degrades_to_none(self) -> None:
        assert _parse_contract_line(USAGE_PREFIX + "[1,2]", USAGE_PREFIX) is None

    def test_full_usage_maps_to_typed_record(self) -> None:
        record = _usage_record_from_payload(
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_tokens": 30,
                "cache_creation_tokens": 40,
                "cost_usd": 0.05,
            },
            duration_seconds=1.5,
        )
        assert record.input_tokens == 10
        assert record.output_tokens == 20
        assert record.cache_read_tokens == 30
        assert record.cache_creation_tokens == 40
        assert record.total_tokens == 100
        assert record.cost_usd == 0.05
        assert record.source == "claude-sdk-typed"

    def test_missing_payload_is_unavailable(self) -> None:
        record = _usage_record_from_payload(None, duration_seconds=2.0)
        assert record.source == "unavailable"
        assert record.total_tokens is None
        assert record.duration_seconds == 2.0

    def test_malformed_fields_become_none_not_crash(self) -> None:
        record = _usage_record_from_payload(
            {
                "input_tokens": "many",
                "output_tokens": True,  # bool is not a count
                "cost_usd": -1,
            },
            duration_seconds=0.1,
        )
        assert record.input_tokens is None
        assert record.output_tokens is None
        assert record.cost_usd is None
        # Still attributed to the typed source: the payload existed.
        assert record.source == "claude-sdk-typed"


# ---------------------------------------------------------------------------
# run() against a fake DeadlineStreamer
# ---------------------------------------------------------------------------


class _FakeStreamer:
    """Records constructor args; yields scripted lines."""

    instances: list[dict[str, Any]] = []
    scripted_lines: list[str] = []
    timed_out_flag = False

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:
        self.timed_out = False
        type(self).instances.append({"cmd": cmd, **kwargs})

    def lines(self) -> Any:
        yield from type(self).scripted_lines
        self.timed_out = type(self).timed_out_flag

    def finish(self, timeout: float = 10.0) -> None:
        pass


@pytest.fixture
def fake_streamer(monkeypatch: pytest.MonkeyPatch) -> type[_FakeStreamer]:
    _FakeStreamer.instances = []
    _FakeStreamer.scripted_lines = []
    _FakeStreamer.timed_out_flag = False
    monkeypatch.setattr(claude_sdk_module, "DeadlineStreamer", _FakeStreamer)
    return _FakeStreamer


class TestAdapterRun:
    def test_contract_lines_consumed_display_passed_through(
        self, fake_streamer: type[_FakeStreamer], tmp_path: Path,
    ) -> None:
        fake_streamer.scripted_lines = [
            "[Write] hello.py",
            USAGE_PREFIX + json.dumps({
                "input_tokens": 1, "output_tokens": 2, "cost_usd": 0.01,
            }),
            "done text",
            RESULT_PREFIX + json.dumps({
                "subtype": "success", "is_error": False, "result": "final answer",
            }),
        ]
        agent = ClaudeSdkAgent(model="haiku")
        lines = list(agent.run("prompt", cwd=tmp_path, timeout=60.0))

        assert lines == ["[Write] hello.py", "done text"]
        assert agent.final_message == "final answer"
        record = agent.usage_records[0]
        assert record.input_tokens == 1
        assert record.output_tokens == 2
        assert record.cost_usd == 0.01
        assert record.source == "claude-sdk-typed"

    def test_timeout_yields_timeout_line_and_record(
        self, fake_streamer: type[_FakeStreamer], tmp_path: Path,
    ) -> None:
        fake_streamer.scripted_lines = ["partial output"]
        fake_streamer.timed_out_flag = True
        agent = ClaudeSdkAgent()
        lines = list(agent.run("prompt", cwd=tmp_path, timeout=1.0))

        assert lines[0] == "partial output"
        assert lines[-1].startswith(TIMEOUT_MESSAGE_PREFIX)
        assert agent.usage_records[0].source == "timeout"
        assert agent.final_message is None

    def test_stdin_config_document(
        self, fake_streamer: type[_FakeStreamer], tmp_path: Path,
    ) -> None:
        sandbox = SandboxConfig(enabled=True, allow_network=False)
        agent = ClaudeSdkAgent(
            model="haiku", effort="low", sandbox=sandbox, max_budget_usd=2.5,
        )
        list(agent.run("the prompt", cwd=tmp_path, timeout=5.0))

        call = fake_streamer.instances[0]
        config = json.loads(call["stdin_text"])
        assert config["prompt"] == "the prompt"
        assert config["model"] == "haiku"
        assert config["effort"] == "low"
        assert config["max_budget_usd"] == 2.5
        assert config["cwd"] == str(tmp_path)
        assert config["workspace_guard"] is True
        # Sandbox parity: the SAME payload the CLI adapter passes via
        # --settings rides the SDK config, and the no-network mode drops
        # bypassPermissions exactly like the CLI drops
        # --dangerously-skip-permissions (R7.5, measured).
        assert config["settings"] == claude_sandbox_settings(sandbox)
        assert config["bypass_permissions"] is False
        assert call["timeout"] == 5.0
        assert call["cmd"][1:] == ["-u", "-m", "kstrl.agents.sdk_runner"]

    def test_no_sandbox_keeps_bypass_permissions(
        self, fake_streamer: type[_FakeStreamer], tmp_path: Path,
    ) -> None:
        agent = ClaudeSdkAgent()
        list(agent.run("p", cwd=tmp_path))
        config = json.loads(fake_streamer.instances[0]["stdin_text"])
        assert config["bypass_permissions"] is True
        assert config["settings"] is None
        assert config["max_budget_usd"] is None

    def test_sandbox_args_and_settings_stay_in_sync(self) -> None:
        sandbox = SandboxConfig(enabled=True, allow_network=False)
        args = claude_sandbox_args(sandbox)
        assert args == ["--settings", claude_sandbox_settings(sandbox)]
        assert claude_sandbox_settings(SandboxConfig(enabled=False)) is None
        assert claude_sandbox_args(None) == []


# ---------------------------------------------------------------------------
# Runner: workspace guard + config + ResultMessage emission
# ---------------------------------------------------------------------------


class TestWorkspaceGuard:
    def test_outside_path_is_denied(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        guard = sdk_runner._make_workspace_guard(workspace)
        out = asyncio.run(guard(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(tmp_path / "outside.py")}},
            None, None,
        ))
        specific = out["hookSpecificOutput"]
        assert specific["hookEventName"] == "PreToolUse"
        assert specific["permissionDecision"] == "deny"
        assert "outside the run workspace" in specific["permissionDecisionReason"]

    def test_inside_path_is_allowed(self, tmp_path: Path) -> None:
        guard = sdk_runner._make_workspace_guard(tmp_path)
        out = asyncio.run(guard(
            {"tool_name": "Edit",
             "tool_input": {"file_path": str(tmp_path / "src" / "a.py")}},
            None, None,
        ))
        assert out == {}

    def test_relative_path_resolves_against_workspace(
        self, tmp_path: Path,
    ) -> None:
        guard = sdk_runner._make_workspace_guard(tmp_path)
        inside = asyncio.run(guard(
            {"tool_name": "Write", "tool_input": {"file_path": "src/a.py"}},
            None, None,
        ))
        escape = asyncio.run(guard(
            {"tool_name": "Write",
             "tool_input": {"file_path": "../escape.py"}},
            None, None,
        ))
        assert inside == {}
        assert escape["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_symlink_escape_is_denied(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (workspace / "link").symlink_to(outside)
        guard = sdk_runner._make_workspace_guard(
            Path(os.path.realpath(workspace)),
        )
        out = asyncio.run(guard(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(workspace / "link" / "x.py")}},
            None, None,
        ))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_unguarded_tool_passes(self, tmp_path: Path) -> None:
        guard = sdk_runner._make_workspace_guard(tmp_path)
        out = asyncio.run(guard(
            {"tool_name": "Bash", "tool_input": {"command": "touch /tmp/x"}},
            None, None,
        ))
        assert out == {}

    def test_notebook_path_key_is_guarded(self, tmp_path: Path) -> None:
        guard = sdk_runner._make_workspace_guard(tmp_path)
        out = asyncio.run(guard(
            {"tool_name": "NotebookEdit",
             "tool_input": {"notebook_path": "/etc/nb.ipynb"}},
            None, None,
        ))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestRunnerConfigAndEmission:
    def test_read_config_rejects_non_object(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2]"))
        with pytest.raises(ValueError):
            sdk_runner._read_config()

    def test_read_config_requires_prompt(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        with pytest.raises(ValueError):
            sdk_runner._read_config()

    def test_main_reports_bad_config_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
        code = sdk_runner.main()
        out = capsys.readouterr().out
        assert code == sdk_runner._EXIT_BAD_CONFIG
        assert "ERROR: invalid sdk-runner config" in out

    def test_result_message_emits_both_contract_records(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        sdk = pytest.importorskip("claude_agent_sdk")
        message = sdk.ResultMessage(
            subtype="success",
            duration_ms=1200,
            duration_api_ms=900,
            is_error=False,
            num_turns=3,
            session_id="s1",
            total_cost_usd=0.0262,
            usage={
                "input_tokens": 7,
                "output_tokens": 11,
                "cache_read_input_tokens": 13,
                "cache_creation_input_tokens": 17,
            },
            result="all done",
        )
        sdk_runner._emit_result_message(message)
        out_lines = capsys.readouterr().out.splitlines()

        usage_lines = [ln for ln in out_lines if ln.startswith(USAGE_PREFIX)]
        result_lines = [ln for ln in out_lines if ln.startswith(RESULT_PREFIX)]
        assert len(usage_lines) == 1 and len(result_lines) == 1

        usage = json.loads(usage_lines[0][len(USAGE_PREFIX):])
        assert usage["input_tokens"] == 7
        assert usage["cache_read_tokens"] == 13
        assert usage["cache_creation_tokens"] == 17
        assert usage["cost_usd"] == 0.0262

        result = json.loads(result_lines[0][len(RESULT_PREFIX):])
        assert result["subtype"] == "success"
        assert result["is_error"] is False
        assert result["result"] == "all done"
        assert result["num_turns"] == 3


# ---------------------------------------------------------------------------
# Registration: dispatch, family, config, preflight
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_get_agent_dispatches_claude_sdk(self) -> None:
        agent = get_agent(
            agent_type="claude-sdk", model="haiku", max_budget_usd=1.0,
        )
        assert isinstance(agent, ClaudeSdkAgent)
        assert agent.name == "claude-sdk (haiku)"

    def test_auto_never_selects_claude_sdk(self) -> None:
        # "auto" resolves to a subprocess adapter regardless of the SDK
        # being installed - claude-sdk is opt-in only.
        agent = get_agent(agent_type="auto")
        assert not isinstance(agent, ClaudeSdkAgent)

    def test_family_maps_to_claude_for_rotation(self) -> None:
        family = _cli_family(None, "claude-sdk", claude_available=True)
        assert family == "claude-code"
        # An SDK engineer gets codex reviewers - same as a CLI engineer.
        assert _CROSS_FAMILY_TYPE[family] == "codex"

    def test_identity_keeps_sdk_label(self) -> None:
        assert _agent_identity(
            None, "claude-sdk", "haiku", claude_available=True,
        ) == "claude-sdk (haiku)"
        assert _agent_identity(
            None, "claude-sdk", None, claude_available=True,
        ) == "claude-sdk"

    def test_toml_budget_usd(self, tmp_path: Path) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text(
            '[agent]\ntype = "claude-sdk"\nbudget_usd = 2.5\n'
        )
        config = KstrlConfig()
        _apply_toml_overrides(config, toml, tmp_path)
        assert config.agent_type == "claude-sdk"
        assert config.agent_budget_usd == 2.5

    def test_toml_budget_rejects_bool_and_nonpositive(
        self, tmp_path: Path,
    ) -> None:
        toml = tmp_path / "kstrl.toml"
        toml.write_text("[agent]\nbudget_usd = true\n")
        config = KstrlConfig()
        _apply_toml_overrides(config, toml, tmp_path)
        assert config.agent_budget_usd is None
        toml.write_text("[agent]\nbudget_usd = 0\n")
        _apply_toml_overrides(config, toml, tmp_path)
        assert config.agent_budget_usd is None

    def test_env_budget_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_AGENT_BUDGET_USD", "3.75")
        config = KstrlConfig()
        _apply_env_overrides(config, tmp_path)
        assert config.agent_budget_usd == 3.75

    def test_env_budget_ignores_garbage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_AGENT_BUDGET_USD", "lots")
        config = KstrlConfig()
        _apply_env_overrides(config, tmp_path)
        assert config.agent_budget_usd is None

    def test_preflight_accepts_available_sdk(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl import cli

        monkeypatch.setattr(
            cli.ClaudeSdkAgent, "is_available", classmethod(lambda _: True),
        )
        canonical, error, hint = cli._agent_preflight(None, "claude-sdk")
        assert canonical == "claude-sdk"
        assert error is None and hint is None

    def test_preflight_names_missing_sdk_extra(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kstrl import cli

        monkeypatch.setattr(
            cli.ClaudeSdkAgent, "is_available", classmethod(lambda _: False),
        )
        canonical, error, hint = cli._agent_preflight(None, "claude-sdk")
        assert error is not None and "claude-agent-sdk" in error
        assert hint is not None and "--extra sdk" in hint
