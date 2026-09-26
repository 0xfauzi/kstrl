"""Timeout utilities for subprocess execution."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from kstrl.config_numbers import check_numbers

# Field name -> environment variable, shared by from_env and load so the
# two surfaces cannot drift.
_ENV_VARS: dict[str, str] = {
    "agent_iteration": "KSTRL_TIMEOUT_AGENT_ITERATION",
    "component_total": "KSTRL_TIMEOUT_COMPONENT",
    "scheduler_backstop_margin": "KSTRL_TIMEOUT_BACKSTOP_MARGIN",
}


#: What every surface prints for a limit that is not set (#467).
NO_LIMIT = "no limit"


def limit_seconds(value: float) -> float | None:
    """A configured time limit as a wait deadline: None when it is not set.

    A work limit in kstrl.toml is 0 (the default) when the operator set
    none, and that means no limit (#467); a negative one is refused at load
    (#571). A wait needs ``None`` for that:
    ``timeout=0`` means "already expired" to ``subprocess`` and ``Popen``.
    """
    return value if value > 0 else None


def describe_limit_seconds(value: float) -> str:
    """A time limit as a run header prints it: ``"1800.0s"`` or ``"no limit"``."""
    return f"{value}s" if value > 0 else NO_LIMIT


@dataclass
class TimeoutConfig:
    """Timeout configuration for various operations.

    Single source of truth for the agent-iteration and component wall-clock
    limits enforced by loop.py, the agent adapters, and the factory
    scheduler (R0.1). A value of 0 disables that limit, and the
    work limits default to 0: a limit the operator did not set does not
    end a run (#467). ``load`` refuses a value that is negative or not
    finite (#571).

    Every field here has a reader. #525 removed five that had none
    (``git_operation``, ``verification_check``, ``review_agent``,
    ``contract_test``, ``subprocess_default``): ``ks init`` scaffolded
    them and ``ks config show`` printed them as limits nothing enforced.
    """

    agent_iteration: float = 0.0
    component_total: float = 0.0
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

        Reads the ``[timeout]`` section from ``<root_dir>/kstrl.toml`` if
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
        return check_numbers(config)


def _apply_env_overrides(config: TimeoutConfig) -> None:
    """Overlay env vars that are explicitly set; unset vars leave the
    existing value untouched (so toml values survive the overlay)."""
    for field_name, env_var in _ENV_VARS.items():
        if env_var in os.environ:
            setattr(config, field_name, float(os.environ[env_var]))


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
        encoding="utf-8",
        timeout=timeout,
        **kwargs,
    )
