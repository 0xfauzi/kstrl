"""Tests for the OS-level sandbox pass-through (R7.5).

Every assertion here targets the argv the adapters BUILD - the flag
mappings themselves were verified against the live CLIs (claude 2.1.215,
codex 0.134.0) in probe runs recorded in the R7.5 PR; these tests pin
the pass-through so a regression cannot silently drop the operator's
sandbox intent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kstrl.agents import get_agent
from kstrl.agents.claude_code import ClaudeCodeAgent
from kstrl.agents.codex import CodexAgent
from kstrl.agents.custom import CustomAgent
from kstrl.sandbox import (
    SandboxConfig,
    claude_sandbox_args,
    claude_sandbox_drops_skip_permissions,
    codex_sandbox_args,
)


class TestSandboxConfig:
    def test_defaults_off(self) -> None:
        config = SandboxConfig()
        assert config.enabled is False
        assert config.allow_network is False

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RALPH_SANDBOX_ENABLED", "1")
        monkeypatch.setenv("RALPH_SANDBOX_ALLOW_NETWORK", "true")
        config = SandboxConfig.from_env()
        assert config.enabled is True
        assert config.allow_network is True

    def test_load_toml_and_env_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "ralph.toml").write_text(
            "[sandbox]\nenabled = true\nallow_network = true\n"
        )
        config = SandboxConfig.load(tmp_path)
        assert config.enabled is True
        assert config.allow_network is True
        monkeypatch.setenv("RALPH_SANDBOX_ALLOW_NETWORK", "0")
        config = SandboxConfig.load(tmp_path)
        assert config.enabled is True
        assert config.allow_network is False


class TestCodexArgs:
    def test_disabled_is_empty(self) -> None:
        assert codex_sandbox_args(None) == []
        assert codex_sandbox_args(SandboxConfig(enabled=False)) == []

    def test_enabled_denies_network_explicitly(self) -> None:
        """The override must be explicit: a global ~/.codex/config.toml
        with network_access=true would otherwise silently win
        (measured on a machine with exactly that config)."""
        args = codex_sandbox_args(SandboxConfig(enabled=True))
        assert args == [
            "--sandbox", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=false",
        ]

    def test_enabled_with_network(self) -> None:
        args = codex_sandbox_args(
            SandboxConfig(enabled=True, allow_network=True),
        )
        assert args == [
            "--sandbox", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=true",
        ]


class TestClaudeArgs:
    def test_disabled_is_empty(self) -> None:
        assert claude_sandbox_args(None) == []
        assert claude_sandbox_args(SandboxConfig(enabled=False)) == []
        assert claude_sandbox_drops_skip_permissions(None) is False
        assert claude_sandbox_drops_skip_permissions(
            SandboxConfig(enabled=False),
        ) is False

    def test_enabled_with_network_keeps_skip_permissions(self) -> None:
        config = SandboxConfig(enabled=True, allow_network=True)
        assert claude_sandbox_drops_skip_permissions(config) is False
        args = claude_sandbox_args(config)
        assert args[0] == "--settings"
        settings = json.loads(args[1])
        assert settings["sandbox"]["enabled"] is True
        # The escape hatch that reruns failed commands UNSANDBOXED must
        # always be closed.
        assert settings["sandbox"]["allowUnsandboxedCommands"] is False
        assert "permissions" not in settings

    def test_enabled_without_network_drops_skip_permissions(self) -> None:
        """Network deny rides the permission layer (measured): the flag
        must go, and the file tools must be re-allowed via settings."""
        config = SandboxConfig(enabled=True, allow_network=False)
        assert claude_sandbox_drops_skip_permissions(config) is True
        args = claude_sandbox_args(config)
        settings = json.loads(args[1])
        allow = settings["permissions"]["allow"]
        for tool in ("Read", "Write", "Edit", "Glob", "Grep"):
            assert tool in allow
        # This mode exists to deny network: no network tools, no Bash
        # blanket rule (sandboxed Bash auto-runs without one).
        assert "WebFetch" not in allow
        assert "WebSearch" not in allow
        assert "Bash" not in allow


def _mock_proc(lines: list[str]) -> MagicMock:
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = iter(lines)
    proc.wait.return_value = 0
    return proc


def _claude_cmd(sandbox: SandboxConfig | None) -> list[str]:
    with patch(
        "subprocess.Popen", return_value=_mock_proc(["done\n"]),
    ) as popen:
        agent = ClaudeCodeAgent(sandbox=sandbox)
        list(agent.run("test", cwd=Path("/tmp")))
    cmd = popen.call_args[0][0]
    assert isinstance(cmd, list)
    return list(cmd)


class TestClaudeAdapterPassThrough:
    def test_default_unsandboxed_command(self) -> None:
        cmd = _claude_cmd(None)
        assert "--dangerously-skip-permissions" in cmd
        assert "--settings" not in cmd

    def test_sandboxed_with_network(self) -> None:
        cmd = _claude_cmd(SandboxConfig(enabled=True, allow_network=True))
        assert "--dangerously-skip-permissions" in cmd
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        assert settings["sandbox"]["enabled"] is True

    def test_sandboxed_without_network(self) -> None:
        cmd = _claude_cmd(SandboxConfig(enabled=True, allow_network=False))
        assert "--dangerously-skip-permissions" not in cmd
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        assert settings["sandbox"]["enabled"] is True
        assert "Write" in settings["permissions"]["allow"]


class TestCodexAdapterPassThrough:
    def _codex_cmd(
        self, monkeypatch: pytest.MonkeyPatch, sandbox: SandboxConfig | None,
    ) -> list[str]:
        monkeypatch.setattr(
            CodexAgent, "_supports_output_last_message", False,
        )
        with patch(
            "subprocess.Popen", return_value=_mock_proc(["done\n"]),
        ) as popen:
            agent = CodexAgent(sandbox=sandbox)
            list(agent.run("test", cwd=Path("/tmp")))
        cmd = popen.call_args[0][0]
        assert isinstance(cmd, list)
        return list(cmd)

    def test_default_unsandboxed_command(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cmd = self._codex_cmd(monkeypatch, None)
        assert "--sandbox" not in cmd

    def test_sandboxed_without_network(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cmd = self._codex_cmd(monkeypatch, SandboxConfig(enabled=True))
        idx = cmd.index("--sandbox")
        assert cmd[idx + 1] == "workspace-write"
        assert "sandbox_workspace_write.network_access=false" in cmd

    def test_sandboxed_with_network(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cmd = self._codex_cmd(
            monkeypatch, SandboxConfig(enabled=True, allow_network=True),
        )
        assert "sandbox_workspace_write.network_access=true" in cmd


class TestGetAgentPassThrough:
    def test_claude_code_receives_sandbox(self) -> None:
        config = SandboxConfig(enabled=True)
        agent = get_agent(agent_type="claude-code", sandbox=config)
        assert isinstance(agent, ClaudeCodeAgent)
        assert agent._sandbox is config

    def test_codex_receives_sandbox(self) -> None:
        config = SandboxConfig(enabled=True)
        agent = get_agent(agent_type="codex", sandbox=config)
        assert isinstance(agent, CodexAgent)
        assert agent._sandbox is config

    def test_custom_agent_has_no_sandbox_surface(self) -> None:
        agent = get_agent(
            agent_cmd="echo hi", sandbox=SandboxConfig(enabled=True),
        )
        assert isinstance(agent, CustomAgent)


class TestFactoryWorkerPassThrough:
    def test_run_component_forwards_sandbox_intent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_run_component rebuilds SandboxConfig from its pickled args
        and hands it to get_agent."""
        import kstrl.agents as agents_mod
        import kstrl.loop as loop_mod
        from kstrl.factory import _run_component
        from kstrl.loop import LoopResult

        captured: dict[str, Any] = {}

        def fake_get_agent(*args: Any, **kwargs: Any) -> Any:
            captured["sandbox"] = kwargs.get("sandbox")
            return MagicMock(usage_records=[])

        def fake_run_loop(*args: Any, **kwargs: Any) -> LoopResult:
            return LoopResult(completed=True, iterations=1, exit_code=0)

        monkeypatch.setattr(agents_mod, "get_agent", fake_get_agent)
        monkeypatch.setattr(loop_mod, "run_loop", fake_run_loop)

        result = _run_component(
            "comp-a", "prd.json", str(tmp_path), str(tmp_path),
            "prompt.md", None, None, None, "claude-code", 0.0,
            sandbox_enabled=True, sandbox_allow_network=True,
        )
        assert result.success is True
        assert captured["sandbox"] == SandboxConfig(
            enabled=True, allow_network=True,
        )
