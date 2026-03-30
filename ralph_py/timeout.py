"""Timeout utilities for subprocess execution."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TimeoutConfig:
    """Timeout configuration for various operations."""

    git_operation: float = 30.0
    agent_iteration: float = 1800.0
    component_total: float = 7200.0
    verification_check: float = 300.0
    review_agent: float = 600.0
    contract_test: float = 600.0
    subprocess_default: float = 60.0

    @classmethod
    def from_env(cls) -> TimeoutConfig:
        """Load timeout config from environment variables."""
        return cls(
            git_operation=float(os.environ.get("RALPH_TIMEOUT_GIT", "30")),
            agent_iteration=float(os.environ.get("RALPH_TIMEOUT_AGENT_ITERATION", "1800")),
            component_total=float(os.environ.get("RALPH_TIMEOUT_COMPONENT", "7200")),
            verification_check=float(os.environ.get("RALPH_TIMEOUT_VERIFY", "300")),
            review_agent=float(os.environ.get("RALPH_TIMEOUT_REVIEW", "600")),
            contract_test=float(os.environ.get("RALPH_TIMEOUT_CONTRACT", "600")),
            subprocess_default=float(os.environ.get("RALPH_TIMEOUT_DEFAULT", "60")),
        )


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
