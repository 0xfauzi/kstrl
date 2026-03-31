"""Agent and model registry for Ralph.

Defines known agents (Claude, Codex) with their CLI commands, model lists,
and auto-detection logic. Custom agents are also supported.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class AgentInfo:
    id: str
    name: str
    detect_command: str  # binary to check in PATH
    command_template: str  # command with {model} placeholder
    models: tuple[ModelInfo, ...]
    default_model: str


CLAUDE_MODELS = (
    ModelInfo("sonnet", "Claude Sonnet", "Fast and capable, good default"),
    ModelInfo("opus", "Claude Opus", "Strongest reasoning, slower"),
    ModelInfo("haiku", "Claude Haiku", "Fastest and cheapest"),
)

CODEX_MODELS = (
    ModelInfo("o3", "o3", "Default codex model"),
    ModelInfo("o4-mini", "o4-mini", "Faster, cheaper"),
    ModelInfo("gpt-4.1", "GPT-4.1", "GPT-4 class model"),
)

KNOWN_AGENTS: dict[str, AgentInfo] = {
    "claude": AgentInfo(
        id="claude",
        name="Claude",
        detect_command="claude",
        command_template="claude --print --model {model}",
        models=CLAUDE_MODELS,
        default_model="sonnet",
    ),
    "codex": AgentInfo(
        id="codex",
        name="Codex",
        detect_command="codex",
        command_template="codex exec -m {model}",
        models=CODEX_MODELS,
        default_model="o3",
    ),
}


def is_agent_installed(agent_id: str) -> bool:
    """Check if an agent's CLI binary is available in PATH."""
    info = KNOWN_AGENTS.get(agent_id)
    if info is None:
        return False
    return shutil.which(info.detect_command) is not None


def detect_installed_agents() -> list[str]:
    """Return list of agent IDs that are installed on this system."""
    return [aid for aid in KNOWN_AGENTS if is_agent_installed(aid)]


def get_models_for_agent(agent_id: str) -> list[ModelInfo]:
    """Return available models for a given agent type."""
    info = KNOWN_AGENTS.get(agent_id)
    if info is None:
        return []
    return list(info.models)


def get_default_model(agent_id: str) -> str:
    """Return the default model for a given agent type."""
    info = KNOWN_AGENTS.get(agent_id)
    if info is None:
        return ""
    return info.default_model


def build_agent_command(agent_type: str, model: str = "", custom_command: str = "") -> str:
    """Build the full shell command to invoke an agent.

    For known agents, substitutes {model} into the command template.
    For custom agents, returns the custom command as-is.
    """
    if agent_type == "custom":
        return custom_command

    info = KNOWN_AGENTS.get(agent_type)
    if info is None:
        raise ValueError(f"Unknown agent type: {agent_type}")

    effective_model = model or info.default_model
    return info.command_template.format(model=effective_model)


def agent_display_name(agent_type: str, model: str = "") -> str:
    """Return a human-readable agent description."""
    if agent_type == "custom":
        return "Custom command"
    info = KNOWN_AGENTS.get(agent_type)
    if info is None:
        return agent_type
    model_str = model or info.default_model
    return f"{info.name} ({model_str})"
