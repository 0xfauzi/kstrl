"""Timeout utilities for subprocess execution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from kstrl import envcompat

# Field name -> environment variable, shared by from_env and load so the
# two surfaces cannot drift.
_ENV_VARS: dict[str, str] = {
    "git_operation": "KSTRL_TIMEOUT_GIT",
    "agent_iteration": "KSTRL_TIMEOUT_AGENT_ITERATION",
    "component_total": "KSTRL_TIMEOUT_COMPONENT",
    "verification_check": "KSTRL_TIMEOUT_VERIFY",
    "review_agent": "KSTRL_TIMEOUT_REVIEW",
    "contract_test": "KSTRL_TIMEOUT_CONTRACT",
    "subprocess_default": "KSTRL_TIMEOUT_DEFAULT",
    "scheduler_backstop_margin": "KSTRL_TIMEOUT_BACKSTOP_MARGIN",
}


@dataclass
class TimeoutConfig:
    """Timeout configuration for various operations.

    Single source of truth for the agent-iteration and component wall-clock
    limits enforced by loop.py, the agent adapters, and the factory
    scheduler (R0.1). A value of 0 or less disables that limit.
    """

    git_operation: float = 30.0
    agent_iteration: float = 1800.0
    component_total: float = 7200.0
    verification_check: float = 300.0
    review_agent: float = 600.0
    contract_test: float = 600.0
    subprocess_default: float = 60.0
    # Extra slack the factory scheduler grants a worker past
    # component_total before declaring the component dead: workers need
    # time for worktree setup, phase hand-offs, and the SIGTERM->SIGKILL
    # grace inside the adapters.
    scheduler_backstop_margin: float = 60.0

    @classmethod
    def from_env(cls) -> TimeoutConfig:
        """Load timeout config from environment variables."""
        config = cls()
        _apply_env_overrides(config)
        return config

    @classmethod
    def load(cls, root_dir: Path | None = None) -> TimeoutConfig:
        """Load timeout config with precedence: env > toml > defaults.

        Reads the ``[timeout]`` section from ``<root_dir>/ralph.toml`` if
        present, then overlays any explicitly-set env vars on top.
        """
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "timeout")
        for f in fields(cls):
            if f.name in section:
                setattr(config, f.name, float(section[f.name]))
        _apply_env_overrides(config)
        return config


def _apply_env_overrides(config: TimeoutConfig) -> None:
    """Overlay env vars that are explicitly set; unset vars leave the
    existing value untouched (so toml values survive the overlay)."""
    for field_name, env_var in _ENV_VARS.items():
        if envcompat.contains(env_var):
            setattr(config, field_name, float(envcompat.require(env_var)))


def run_with_timeout(
    cmd: list[str] | str,
    timeout: float,
    cwd: Path | None = None,
    shell: bool = False,
    input_text: str | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with a timeout.

    On timeout, subprocess.TimeoutExpired is raised.
    """
    return subprocess.run(
        cmd,
        cwd=cwd,
        shell=shell,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        **kwargs,
    )
