"""Tests for ralph.models module."""

from __future__ import annotations

import pytest

from ralph.models import (
    KNOWN_AGENTS,
    agent_display_name,
    build_agent_command,
    get_default_model,
    get_models_for_agent,
)


def test_known_agents_exist() -> None:
    assert "claude" in KNOWN_AGENTS
    assert "codex" in KNOWN_AGENTS


def test_claude_has_models() -> None:
    models = get_models_for_agent("claude")
    assert len(models) > 0
    model_ids = [m.id for m in models]
    assert "sonnet" in model_ids
    assert "opus" in model_ids
    assert "haiku" in model_ids


def test_codex_has_models() -> None:
    models = get_models_for_agent("codex")
    assert len(models) > 0
    model_ids = [m.id for m in models]
    assert "o3" in model_ids


def test_unknown_agent_has_no_models() -> None:
    assert get_models_for_agent("nonexistent") == []


def test_get_default_model() -> None:
    assert get_default_model("claude") == "sonnet"
    assert get_default_model("codex") == "o3"
    assert get_default_model("nonexistent") == ""


def test_build_agent_command_claude() -> None:
    cmd = build_agent_command("claude", "sonnet")
    assert "claude" in cmd
    assert "sonnet" in cmd


def test_build_agent_command_codex() -> None:
    cmd = build_agent_command("codex", "o3")
    assert "codex" in cmd
    assert "o3" in cmd


def test_build_agent_command_custom() -> None:
    cmd = build_agent_command("custom", custom_command="my-agent --stdin")
    assert cmd == "my-agent --stdin"


def test_build_agent_command_default_model() -> None:
    cmd = build_agent_command("claude")
    assert "sonnet" in cmd  # default model


def test_build_agent_command_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown agent"):
        build_agent_command("nonexistent")


def test_agent_display_name() -> None:
    assert agent_display_name("claude", "sonnet") == "Claude (sonnet)"
    assert agent_display_name("codex", "o3") == "Codex (o3)"
    assert agent_display_name("custom") == "Custom command"
    assert agent_display_name("claude") == "Claude (sonnet)"  # uses default
