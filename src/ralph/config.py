"""Configuration loading and management for Ralph.

Loads settings from ralph.toml, with env var overrides and CLI flag support.
Resolution order: CLI flags > env vars > ralph.toml > defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentConfig:
    type: str = "claude"  # "claude" | "codex" | "custom"
    command: str = ""  # only used when type = "custom"
    model: str = ""  # e.g., "sonnet" for claude, "o3" for codex
    reasoning_effort: str = ""  # codex-specific: low|medium|high|xhigh


@dataclass
class RunConfig:
    max_iterations: int = 10
    sleep_seconds: int = 2
    interactive: bool = False


@dataclass
class PathsConfig:
    prompt: str = "scripts/ralph/prompt.md"
    prd: str = "scripts/ralph/prd.json"
    progress: str = "scripts/ralph/progress.txt"
    codebase_map: str = "scripts/ralph/codebase_map.md"
    allowed: list[str] = field(default_factory=list)


@dataclass
class GitConfig:
    branch: str = ""  # override branch (empty = use PRD branchName)
    auto_checkout: bool = True


@dataclass
class UIConfig:
    ascii: bool = False


@dataclass
class RalphConfig:
    agent: AgentConfig = field(default_factory=AgentConfig)
    run: RunConfig = field(default_factory=RunConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    git: GitConfig = field(default_factory=GitConfig)
    ui: UIConfig = field(default_factory=UIConfig)


ENV_VAR_MAP: dict[str, tuple[str, str, type]] = {
    # env var -> (config section, config key, type)
    "AGENT_CMD": ("agent", "command", str),
    "MODEL": ("agent", "model", str),
    "MODEL_REASONING_EFFORT": ("agent", "reasoning_effort", str),
    "SLEEP_SECONDS": ("run", "sleep_seconds", int),
    "INTERACTIVE": ("run", "interactive", bool),
    "PROMPT_FILE": ("paths", "prompt", str),
    "PRD_FILE": ("paths", "prd", str),
    "ALLOWED_PATHS": ("paths", "allowed", list),
    "RALPH_BRANCH": ("git", "branch", str),
    "RALPH_ASCII": ("ui", "ascii", bool),
}


def _parse_bool(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


def _parse_list(value: str) -> list[str]:
    if not value.strip():
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _apply_toml_section(config_obj: Any, data: dict[str, Any]) -> None:
    """Apply a dict of values to a dataclass instance."""
    for key, value in data.items():
        if hasattr(config_obj, key):
            setattr(config_obj, key, value)


def load_toml(path: Path) -> dict[str, Any]:
    """Load a ralph.toml file and return its contents."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(
    toml_path: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> RalphConfig:
    """Load configuration with full resolution: CLI > env vars > toml > defaults."""
    config = RalphConfig()

    # Layer 1: Load from ralph.toml
    if toml_path is None:
        toml_path = Path("ralph.toml")
    toml_data = load_toml(toml_path)
    if "agent" in toml_data:
        _apply_toml_section(config.agent, toml_data["agent"])
    if "run" in toml_data:
        _apply_toml_section(config.run, toml_data["run"])
    if "paths" in toml_data:
        _apply_toml_section(config.paths, toml_data["paths"])
    if "git" in toml_data:
        _apply_toml_section(config.git, toml_data["git"])
    if "ui" in toml_data:
        _apply_toml_section(config.ui, toml_data["ui"])

    # Layer 2: Apply env var overrides
    for env_var, (section, key, typ) in ENV_VAR_MAP.items():
        value = os.environ.get(env_var)
        if value is None:
            continue
        section_obj = getattr(config, section)
        if typ is bool:
            setattr(section_obj, key, _parse_bool(value))
        elif typ is int:
            setattr(section_obj, key, int(value))
        elif typ is list:
            setattr(section_obj, key, _parse_list(value))
        else:
            setattr(section_obj, key, value)

    # Special case: AGENT_CMD implies type = "custom"
    if os.environ.get("AGENT_CMD"):
        config.agent.type = "custom"

    # Layer 3: Apply CLI overrides
    if cli_overrides:
        for dotted_key, value in cli_overrides.items():
            parts = dotted_key.split(".", 1)
            if len(parts) == 2:
                section_obj = getattr(config, parts[0], None)
                if section_obj is not None and hasattr(section_obj, parts[1]):
                    setattr(section_obj, parts[1], value)

    return config


def save_config(config: RalphConfig, path: Path) -> None:
    """Save configuration to ralph.toml."""
    lines: list[str] = []

    lines.append("[agent]")
    lines.append(f'type = "{config.agent.type}"')
    lines.append(f'command = "{config.agent.command}"')
    lines.append(f'model = "{config.agent.model}"')
    lines.append(f'reasoning_effort = "{config.agent.reasoning_effort}"')
    lines.append("")

    lines.append("[run]")
    lines.append(f"max_iterations = {config.run.max_iterations}")
    lines.append(f"sleep_seconds = {config.run.sleep_seconds}")
    lines.append(f"interactive = {'true' if config.run.interactive else 'false'}")
    lines.append("")

    lines.append("[paths]")
    lines.append(f'prompt = "{config.paths.prompt}"')
    lines.append(f'prd = "{config.paths.prd}"')
    lines.append(f'progress = "{config.paths.progress}"')
    lines.append(f'codebase_map = "{config.paths.codebase_map}"')
    allowed_items = ", ".join(f'"{p}"' for p in config.paths.allowed)
    lines.append(f"allowed = [{allowed_items}]")
    lines.append("")

    lines.append("[git]")
    lines.append(f'branch = "{config.git.branch}"')
    lines.append(f"auto_checkout = {'true' if config.git.auto_checkout else 'false'}")
    lines.append("")

    lines.append("[ui]")
    lines.append(f"ascii = {'true' if config.ui.ascii else 'false'}")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def config_to_display(config: RalphConfig) -> dict[str, str]:
    """Return a flat dict for display purposes (key-value summary)."""
    agent_desc = config.agent.type
    if config.agent.type == "custom":
        agent_desc = f"custom ({config.agent.command})"
    elif config.agent.model:
        agent_desc = f"{config.agent.type} ({config.agent.model})"

    return {
        "Agent": agent_desc,
        "Max iterations": str(config.run.max_iterations),
        "Sleep": f"{config.run.sleep_seconds}s",
        "Interactive": "yes" if config.run.interactive else "no",
        "Prompt": config.paths.prompt,
        "PRD": config.paths.prd,
        "Allowed paths": ", ".join(config.paths.allowed) if config.paths.allowed else "<disabled>",
        "Branch": config.git.branch or "<from PRD>",
        "Auto checkout": "yes" if config.git.auto_checkout else "no",
    }
