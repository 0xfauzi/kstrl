"""R2.4 preflight honesty tests (H-12).

Two gates, both BEFORE any agent spend:
- The agent preflight checks for whichever agent the resolved config
  selects (claude/codex/custom), not codex-only.
- ``ralph run`` validates prd.json existence + schema before the factory
  pipeline (and therefore the agent) ever starts.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

import ralph_py.cli as cli_mod
from ralph_py.agents import ClaudeCodeAgent, CodexAgent
from ralph_py.cli import _agent_preflight, cli
from ralph_py.factory import FactoryResult


def _availability(
    monkeypatch: pytest.MonkeyPatch, *, claude: bool, codex: bool,
) -> None:
    monkeypatch.setattr(
        ClaudeCodeAgent, "is_available", classmethod(lambda cls: claude)
    )
    monkeypatch.setattr(
        CodexAgent, "is_available", classmethod(lambda cls: codex)
    )


def _stub_run_loop(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace run_loop so understand/feature stop right after preflight."""
    calls: list[dict[str, Any]] = []

    def fake_run_loop(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(completed=True, iterations=0, exit_code=0)

    monkeypatch.setattr(cli_mod, "run_loop", fake_run_loop)
    return calls


VALID_PRD = {
    "branchName": "ralph/test",
    "userStories": [
        {
            "id": "US-1",
            "title": "story",
            "acceptanceCriteria": ["works"],
            "priority": 1,
            "passes": False,
            "notes": "",
        },
    ],
}


def _scaffold(root: Path, prd: dict[str, Any] | str | None = VALID_PRD) -> Path:
    ralph_dir = root / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    (ralph_dir / "prompt.md").write_text("prompt")
    (ralph_dir / "understand_prompt.md").write_text("prompt")
    (ralph_dir / "codebase_map.md").write_text("# Map\n")
    if prd is not None:
        content = prd if isinstance(prd, str) else json.dumps(prd)
        (ralph_dir / "prd.json").write_text(content)
    return ralph_dir


class TestAgentPreflightResolver:
    """Unit matrix for _agent_preflight."""

    def test_claude_only_type_claude_passes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=True, codex=False)
        canonical, error, _ = _agent_preflight(None, "claude")
        assert error is None
        assert canonical == "claude-code"

    def test_claude_only_type_claude_code_passes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=True, codex=False)
        canonical, error, _ = _agent_preflight(None, "claude-code")
        assert error is None
        assert canonical == "claude-code"

    def test_codex_only_type_codex_passes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=False, codex=True)
        canonical, error, _ = _agent_preflight(None, "codex")
        assert error is None
        assert canonical == "codex"

    def test_claude_only_type_codex_errors_naming_codex(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=True, codex=False)
        _, error, _ = _agent_preflight(None, "codex")
        assert error is not None
        assert "codex not found in PATH" in error
        assert "claude not found" not in error

    def test_codex_only_type_claude_errors_naming_claude(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=False, codex=True)
        _, error, _ = _agent_preflight(None, "claude")
        assert error is not None
        assert "claude not found in PATH" in error
        assert "codex not found" not in error

    def test_auto_accepts_claude_only_machine(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=True, codex=False)
        canonical, error, _ = _agent_preflight(None, None)
        assert error is None
        assert canonical == "auto"

    def test_auto_neither_available_names_both(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=False, codex=False)
        _, error, _ = _agent_preflight(None, "auto")
        assert error is not None
        assert "codex" in error and "claude" in error

    def test_custom_command_skips_path_check(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=False, codex=False)
        _, error, _ = _agent_preflight("echo hi", "custom")
        assert error is None

    def test_custom_type_without_command_errors(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=True, codex=True)
        _, error, hint = _agent_preflight(None, "custom")
        assert error is not None
        assert "custom" in error
        assert hint is not None and "--agent-cmd" in hint

    def test_unknown_type_errors_loudly(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=True, codex=True)
        _, error, _ = _agent_preflight(None, "gemini")
        assert error is not None
        assert "Unknown agent type" in error and "gemini" in error


class TestUnderstandPreflightWiring:
    """CLI-level matrix through `ralph understand` (run_loop stubbed)."""

    def test_claude_only_toml_type_claude_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=True, codex=False)
        calls = _stub_run_loop(monkeypatch)
        _scaffold(tmp_path)
        (tmp_path / "ralph.toml").write_text('[agent]\ntype = "claude"\n')

        result = CliRunner().invoke(
            cli, ["understand", "1", "--root", str(tmp_path), "--ui", "plain"],
        )

        assert result.exit_code == 0, result.output
        assert "not found in PATH" not in result.output
        assert len(calls) == 1

    def test_codex_only_env_type_codex_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=False, codex=True)
        calls = _stub_run_loop(monkeypatch)
        _scaffold(tmp_path)
        monkeypatch.setenv("RALPH_AGENT_TYPE", "codex")

        result = CliRunner().invoke(
            cli, ["understand", "1", "--root", str(tmp_path), "--ui", "plain"],
        )

        assert result.exit_code == 0, result.output
        assert len(calls) == 1

    def test_claude_only_type_codex_blocks_naming_codex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=True, codex=False)
        calls = _stub_run_loop(monkeypatch)
        _scaffold(tmp_path)
        monkeypatch.setenv("RALPH_AGENT_TYPE", "codex")

        result = CliRunner().invoke(
            cli,
            ["understand", "1", "--root", str(tmp_path), "--ui", "plain",
             "--no-color"],
        )

        assert result.exit_code == 1
        assert "codex not found in PATH" in result.output
        assert calls == []

    def test_codex_only_type_claude_blocks_naming_claude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=False, codex=True)
        calls = _stub_run_loop(monkeypatch)
        _scaffold(tmp_path)
        (tmp_path / "ralph.toml").write_text('[agent]\ntype = "claude"\n')

        result = CliRunner().invoke(
            cli,
            ["understand", "1", "--root", str(tmp_path), "--ui", "plain",
             "--no-color"],
        )

        assert result.exit_code == 1
        assert "claude not found in PATH" in result.output
        assert calls == []


class TestFeaturePreflightWiring:
    def test_codex_only_type_claude_blocks_before_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _availability(monkeypatch, claude=False, codex=True)
        calls = _stub_run_loop(monkeypatch)
        ralph_dir = _scaffold(tmp_path)
        (tmp_path / "ralph.toml").write_text('[agent]\ntype = "claude"\n')

        result = CliRunner().invoke(
            cli,
            ["feature", "--root", str(tmp_path),
             "--prd", str(ralph_dir / "prd.json"),
             "--ui", "plain", "--no-color"],
        )

        assert result.exit_code == 1
        assert "claude not found in PATH" in result.output
        assert calls == []


class TestRunPrdPreflight:
    """`ralph run` gates on prd.json BEFORE the factory/agent starts."""

    def _stub_run_factory(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        def fake_run_factory(*args: Any, **kwargs: Any) -> FactoryResult:
            calls.append({"args": args, "kwargs": kwargs})
            return FactoryResult()

        monkeypatch.setattr(cli_mod, "run_factory", fake_run_factory)
        return calls

    def _invoke_run(self, root: Path, marker: Path) -> Any:
        # The counting fake agent: if any pipeline phase ever invokes the
        # agent, the marker file appears.
        return CliRunner().invoke(
            cli,
            ["run", "1", "--root", str(root),
             "--agent-cmd", f"touch {marker}",
             "--ui", "plain", "--no-color"],
        )

    def test_missing_prd_blocks_before_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory_calls = self._stub_run_factory(monkeypatch)
        _scaffold(tmp_path, prd=None)
        marker = tmp_path / "agent-ran.marker"

        result = self._invoke_run(tmp_path, marker)

        assert result.exit_code == 1
        assert "PRD file not found" in result.output
        assert factory_calls == []
        assert not marker.exists()

    def test_invalid_json_blocks_before_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory_calls = self._stub_run_factory(monkeypatch)
        _scaffold(tmp_path, prd="{not json")
        marker = tmp_path / "agent-ran.marker"

        result = self._invoke_run(tmp_path, marker)

        assert result.exit_code == 1
        assert "Invalid JSON" in result.output
        assert factory_calls == []
        assert not marker.exists()

    def test_schema_error_blocks_with_per_field_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory_calls = self._stub_run_factory(monkeypatch)
        bad_prd = {
            "branchName": "ralph/test",
            "userStories": [{"id": "US-1", "title": "incomplete"}],
        }
        _scaffold(tmp_path, prd=bad_prd)
        marker = tmp_path / "agent-ran.marker"

        result = self._invoke_run(tmp_path, marker)

        assert result.exit_code == 1
        assert "PRD schema validation failed" in result.output
        assert "userStories[0]" in result.output
        assert factory_calls == []
        assert not marker.exists()

    def test_valid_prd_reaches_factory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        factory_calls = self._stub_run_factory(monkeypatch)
        _scaffold(tmp_path)
        marker = tmp_path / "agent-ran.marker"

        result = self._invoke_run(tmp_path, marker)

        assert result.exit_code == 0, result.output
        assert len(factory_calls) == 1

    def test_run_accepts_claude_only_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The old preflight exited 'codex not found in PATH' here."""
        _availability(monkeypatch, claude=True, codex=False)
        factory_calls = self._stub_run_factory(monkeypatch)
        _scaffold(tmp_path)

        result = CliRunner().invoke(
            cli,
            ["run", "1", "--root", str(tmp_path), "--ui", "plain",
             "--no-color"],
        )

        assert result.exit_code == 0, result.output
        assert "not found in PATH" not in result.output
        assert len(factory_calls) == 1
        # The canonicalized agent type flows into the factory config so
        # downstream get_agent calls select the agent preflight verified.
        base_config = factory_calls[0]["args"][2]
        assert base_config.agent_type == "auto"


class TestFactoryPreflightWiring:
    """R2.4 mirror on the factory path (measured 2026-07-20 on the first
    real factory run): toml ``[agent] type = "claude"`` reached
    ``get_agent`` RAW in every engineer worker and silently fell through
    to the codex default - inverting the R7.1 rotation's family
    detection along the way."""

    def test_factory_canonicalizes_toml_claude_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ralph_py.manifest import Component, Manifest

        _availability(monkeypatch, claude=True, codex=True)
        root = tmp_path / "proj"
        root.mkdir()
        _scaffold(root)
        (root / "ralph.toml").write_text('[agent]\ntype = "claude"\n')

        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="p",
            base_branch="main", single_pr=False,
            components=[Component(
                "alpha", "Alpha", "First", [],
                "scripts/ralph/feature/alpha/prd.json",
                "ralph/factory/alpha",
            )],
        )
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text("{}")  # click existence check only
        monkeypatch.setattr(
            cli_mod.Manifest, "load",
            classmethod(lambda cls, path: manifest),
        )

        captured: dict[str, Any] = {}

        def fake_run_factory(
            manifest_arg: Any, factory_config: Any, base_config: Any,
            *args: Any, **kwargs: Any,
        ) -> SimpleNamespace:
            captured["agent_type"] = base_config.agent_type
            return SimpleNamespace(exit_code=0)

        monkeypatch.setattr(cli_mod, "run_factory", fake_run_factory)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "factory", "--manifest", str(manifest_file),
                "--root", str(root), "--yes",
            ],
            catch_exceptions=False,
        )

        assert captured.get("agent_type") == "claude-code", result.output

    def test_factory_blocks_unknown_agent_type_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The other half of the fallthrough hazard: a typo'd type must
        exit with the R2.4 error, never reach a codex engineer."""
        _availability(monkeypatch, claude=True, codex=True)
        root = tmp_path / "proj"
        root.mkdir()
        _scaffold(root)
        (root / "ralph.toml").write_text('[agent]\ntype = "clade"\n')

        from ralph_py.manifest import Component, Manifest

        manifest = Manifest(
            version="1", spec_file="spec.md", project_name="p",
            base_branch="main", single_pr=False,
            components=[Component(
                "alpha", "Alpha", "First", [],
                "scripts/ralph/feature/alpha/prd.json",
                "ralph/factory/alpha",
            )],
        )
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text("{}")
        monkeypatch.setattr(
            cli_mod.Manifest, "load",
            classmethod(lambda cls, path: manifest),
        )

        called: list[bool] = []
        monkeypatch.setattr(
            cli_mod, "run_factory",
            lambda *a, **k: called.append(True) or SimpleNamespace(exit_code=0),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "factory", "--manifest", str(manifest_file),
                "--root", str(root), "--yes",
            ],
        )

        assert result.exit_code != 0
        assert "Unknown agent type" in result.output
        assert not called
