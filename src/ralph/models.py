"""Agent and model registry for Ralph.

Defines known agents (Claude, Codex) with their CLI commands, model lists,
and auto-detection logic. Custom agents are also supported.

Supports runtime model discovery when the agent CLI is available.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

# Session-level cache for discovered models
_discovery_cache: dict[str, list[ModelInfo] | None] = {}


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
    ModelInfo("sonnet", "Sonnet 4.6", "Fast and capable, 200K context"),
    ModelInfo("opus", "Opus 4.6", "Strongest reasoning, 1M context"),
    ModelInfo("haiku", "Haiku 4.5", "Fastest and cheapest, 200K context"),
)

CODEX_MODELS = (
    ModelInfo("o3", "o3", "Strong reasoning, 200K context, default"),
    ModelInfo("o4-mini", "o4-mini", "Fast reasoning, 200K context"),
    ModelInfo("codex-mini-latest", "Codex Mini", "Optimized for Codex CLI"),
    ModelInfo("gpt-4.1", "GPT-4.1", "General purpose, 1M context"),
    ModelInfo("gpt-4.1-mini", "GPT-4.1 Mini", "Fast general purpose, 1M context"),
    ModelInfo("gpt-4.1-nano", "GPT-4.1 Nano", "Fastest, 1M context"),
)

KNOWN_AGENTS: dict[str, AgentInfo] = {
    "claude": AgentInfo(
        id="claude",
        name="Claude Code",
        detect_command="claude",
        command_template="claude --print --model {model} --permission-mode auto",
        models=CLAUDE_MODELS,
        default_model="sonnet",
    ),
    "codex": AgentInfo(
        id="codex",
        name="Codex CLI",
        detect_command="codex",
        command_template="codex exec --full-auto -m {model}",
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


def discover_models(agent_id: str) -> list[ModelInfo] | None:
    """Try to discover available models by querying the agent CLI.

    Returns a list of ModelInfo on success, or None if discovery fails
    (CLI not installed, timeout, parse error, etc.).

    Results are cached for the session to avoid repeated subprocess calls.
    """
    if agent_id in _discovery_cache:
        return _discovery_cache[agent_id]

    result = _discover_models_impl(agent_id)
    _discovery_cache[agent_id] = result
    return result


def _discover_models_impl(agent_id: str) -> list[ModelInfo] | None:
    """Implementation of model discovery. Not cached."""
    if not is_agent_installed(agent_id):
        return None

    if agent_id == "claude":
        return _discover_claude_models()
    elif agent_id == "codex":
        return _discover_codex_models()

    return None


def _discover_claude_models() -> list[ModelInfo] | None:
    """Discover Claude models by querying the CLI."""
    try:
        result = subprocess.run(
            ["claude", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # The help output mentions model aliases. We can parse or just
        # use our known list with version info from the CLI version.
        version_result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        version = version_result.stdout.strip() if version_result.returncode == 0 else ""

        # Build models with version context
        models = [
            ModelInfo("opus", "Opus 4.6", f"Strongest reasoning, 1M context ({version})"),
            ModelInfo("sonnet", "Sonnet 4.6", f"Fast and capable, 200K context ({version})"),
            ModelInfo("haiku", "Haiku 4.5", f"Fastest and cheapest, 200K context ({version})"),
        ]
        return models if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _discover_codex_models() -> list[ModelInfo] | None:
    """Discover Codex models by querying the CLI."""
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        version = result.stdout.strip()

        # Codex doesn't expose a model list command, so we return
        # the known models with the CLI version appended for context
        return [
            ModelInfo("o3", "o3", f"Strong reasoning, 200K context ({version})"),
            ModelInfo("o4-mini", "o4-mini", f"Fast reasoning, 200K context ({version})"),
            ModelInfo(
                "codex-mini-latest", "Codex Mini",
                f"Optimized for Codex CLI ({version})",
            ),
            ModelInfo("gpt-4.1", "GPT-4.1", f"General purpose, 1M context ({version})"),
            ModelInfo(
                "gpt-4.1-mini", "GPT-4.1 Mini",
                f"Fast general purpose, 1M context ({version})",
            ),
            ModelInfo(
                "gpt-4.1-nano", "GPT-4.1 Nano",
                f"Fastest, 1M context ({version})",
            ),
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def get_models_for_agent(agent_id: str) -> list[ModelInfo]:
    """Return available models for a given agent type.

    Tries runtime discovery first, falls back to hardcoded list.
    """
    discovered = discover_models(agent_id)
    if discovered is not None:
        return discovered

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


def build_agent_command(
    agent_type: str, model: str = "", custom_command: str = "",
) -> str:
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
