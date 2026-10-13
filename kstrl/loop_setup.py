"""The startup report and the guard baseline of ``kstrl.loop.run_loop``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.breaker import BreakerConfig
from kstrl.git import git, git_tree
from kstrl.proc import TimeoutConfig, describe_limit_seconds

if TYPE_CHECKING:
    from kstrl.agents.base import Agent
    from kstrl.config import KstrlConfig
    from kstrl.ui import UI


def capture_guard_baseline(
    cwd: Path | None,
    base_ref: str | None,
    ui: UI,
) -> git_tree.WorkspaceBaseline | None:
    """The before-picture the allowed-paths guard compares against, or None
    when it could not be read (the caller refuses before any spend)."""
    try:
        baseline = git_tree.capture_workspace_baseline(cwd, base_ref=base_ref)
    except git.GitDiffError as exc:
        ui.err(f"Guard baseline could not be taken: {exc}")
        return None
    if baseline.dirty:
        ui.info(
            f"Guard baseline: HEAD {baseline.head or '<unborn>'}, "
            f"{len(baseline.dirty)} pre-existing uncommitted "
            "file(s) excluded from enforcement"
        )
    return baseline


def print_startup(
    config: KstrlConfig,
    ui: UI,
    agent: Agent,
    cwd: Path,
    timeouts: TimeoutConfig,
    breaker_config: BreakerConfig,
) -> None:
    """Show the startup art, the title and the settings of the run."""
    ui.startup_art()

    # Display title
    ui.title("kstrl")

    # Display startup info
    ui.section("Startup")
    ui.kv("Root", str(cwd))
    ui.kv("Prompt", str(config.prompt_file))
    ui.kv("PRD", str(config.prd_file))
    ui.kv("Agent", agent.name)
    ui.kv("Max iterations", str(config.max_iterations))
    ui.kv("Sleep", f"{config.sleep_seconds}s")
    ui.kv("Interactive", "yes" if config.interactive else "no")
    allowed_paths = ", ".join(config.allowed_paths) if config.allowed_paths else "<disabled>"
    ui.kv("Allowed paths", allowed_paths)
    ui.kv("Reasoning", config.model_reasoning_effort or "<default>")
    ui.kv("UI", config.ui_mode)
    ui.kv("Agent timeout", describe_limit_seconds(timeouts.agent_iteration))
    ui.kv("Component timeout", describe_limit_seconds(timeouts.component_total))
    ui.kv(
        "No-progress breaker",
        f"{breaker_config.no_progress_iterations} iterations"
        if breaker_config.no_progress_iterations > 0
        else "<disabled>",
    )
