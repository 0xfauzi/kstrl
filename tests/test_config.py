"""Tests for ralph.config module."""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph.config import (
    RalphConfig,
    config_to_display,
    load_config,
    save_config,
)


def test_default_config() -> None:
    config = RalphConfig()
    assert config.agent.type == "claude"
    assert config.run.max_iterations == 10
    assert config.run.sleep_seconds == 2
    assert config.run.interactive is False
    assert config.paths.prompt == "scripts/ralph/prompt.md"
    assert config.paths.allowed == []
    assert config.git.auto_checkout is True


def test_load_missing_toml(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nonexistent.toml")
    # Should return defaults
    assert config.agent.type == "claude"
    assert config.run.max_iterations == 10


def test_load_and_save_round_trip(tmp_path: Path) -> None:
    original = RalphConfig()
    original.agent.type = "codex"
    original.agent.model = "o3"
    original.run.max_iterations = 25
    original.run.interactive = True
    original.paths.allowed = ["src/", "tests/"]
    original.git.branch = "ralph/test"

    toml_path = tmp_path / "ralph.toml"
    save_config(original, toml_path)

    loaded = load_config(toml_path)
    assert loaded.agent.type == "codex"
    assert loaded.agent.model == "o3"
    assert loaded.run.max_iterations == 25
    assert loaded.run.interactive is True
    assert loaded.paths.allowed == ["src/", "tests/"]
    assert loaded.git.branch == "ralph/test"


def test_env_var_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml_path = tmp_path / "ralph.toml"
    config = RalphConfig()
    save_config(config, toml_path)

    monkeypatch.setenv("MODEL", "opus")
    monkeypatch.setenv("SLEEP_SECONDS", "5")
    monkeypatch.setenv("INTERACTIVE", "true")
    monkeypatch.setenv("ALLOWED_PATHS", "src/,tests/")

    loaded = load_config(toml_path)
    assert loaded.agent.model == "opus"
    assert loaded.run.sleep_seconds == 5
    assert loaded.run.interactive is True
    assert loaded.paths.allowed == ["src/", "tests/"]


def test_agent_cmd_env_sets_custom_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CMD", "my-agent --stdin")
    loaded = load_config(tmp_path / "nonexistent.toml")
    assert loaded.agent.type == "custom"
    assert loaded.agent.command == "my-agent --stdin"


def test_cli_overrides() -> None:
    config = load_config(
        cli_overrides={
            "run.max_iterations": 50,
            "agent.model": "haiku",
        }
    )
    assert config.run.max_iterations == 50
    assert config.agent.model == "haiku"


def test_config_to_display() -> None:
    config = RalphConfig()
    config.agent.type = "claude"
    config.agent.model = "sonnet"
    display = config_to_display(config)
    assert display["Agent"] == "claude (sonnet)"
    assert display["Max iterations"] == "10"
    assert display["Interactive"] == "no"
