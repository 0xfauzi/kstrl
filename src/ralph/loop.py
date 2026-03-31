"""Core agentic loop - the heart of Ralph.

Runs an AI agent in a loop, feeding it a prompt on each iteration.
The agent works through user stories until all pass or max iterations is reached.

This module is framework-agnostic: it communicates via callbacks that both the
CLI (Rich console) and TUI (Textual widgets) layers can implement.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ralph.agent import AgentOutput, detect_completion, run_agent_async
from ralph.config import RalphConfig
from ralph.git_ops import (
    checkout_branch,
    current_branch,
    find_disallowed_files,
    is_git_repo,
    revert_files,
)
from ralph.prd import PRD, load_prd


class LoopCallbacks(Protocol):
    """Interface for receiving loop events.

    Both the CLI and TUI layers implement this protocol.
    """

    def on_loop_start(self, config: RalphConfig, prd: PRD | None) -> None: ...
    def on_branch_status(self, message: str) -> None: ...
    def on_iteration_start(self, iteration: int, max_iterations: int) -> None: ...
    def on_agent_line(self, output: AgentOutput) -> None: ...
    def on_iteration_end(self, iteration: int, elapsed_seconds: float) -> None: ...
    def on_guard_violation(self, disallowed: list[str]) -> None: ...
    def on_guard_reverted(self, messages: list[str]) -> None: ...
    def on_complete(self, success: bool, iterations_used: int) -> None: ...
    def on_info(self, message: str) -> None: ...
    def on_error(self, message: str) -> None: ...


@dataclass
class LoopControl:
    """Control signals for the loop. Shared between loop and UI."""

    stop_requested: bool = False
    pause_requested: bool = False

    def request_stop(self) -> None:
        self.stop_requested = True

    def request_pause(self) -> None:
        self.pause_requested = True

    def resume(self) -> None:
        self.pause_requested = False


@dataclass
class LoopResult:
    """Result of a loop run."""

    success: bool
    iterations_used: int
    exit_code: int  # 0=complete, 1=max iterations, 2=error


async def run_loop(
    config: RalphConfig,
    cwd: Path,
    callbacks: LoopCallbacks,
    control: LoopControl | None = None,
) -> LoopResult:
    """Run the main agentic loop.

    This is the async core that powers both CLI and TUI modes.
    """
    if control is None:
        control = LoopControl()

    prompt_path = cwd / config.paths.prompt
    prd_path = cwd / config.paths.prd

    # Validate prompt exists
    if not prompt_path.exists():
        callbacks.on_error(f"Missing prompt file: {prompt_path}")
        return LoopResult(success=False, iterations_used=0, exit_code=2)

    # Load PRD (optional - understanding mode may not have one)
    prd: PRD | None = None
    try:
        prd = load_prd(prd_path)
    except (FileNotFoundError, ValueError) as e:
        callbacks.on_info(f"PRD: {e}")

    callbacks.on_loop_start(config, prd)

    # Git branch management
    if is_git_repo(cwd) and config.git.auto_checkout:
        branch = config.git.branch
        if not branch and prd:
            branch = prd.branch_name
        if branch:
            msg = checkout_branch(branch, create=True, cwd=cwd)
            callbacks.on_branch_status(msg)
        else:
            cur = current_branch(cwd)
            callbacks.on_branch_status(f"On branch: {cur}" if cur else "No branch detected")
    elif not is_git_repo(cwd):
        callbacks.on_branch_status("Not a git repo, branch management disabled")

    # Main iteration loop
    max_iter = config.run.max_iterations
    consecutive_errors = 0
    max_consecutive_errors = 3

    for i in range(1, max_iter + 1):
        if control.stop_requested:
            callbacks.on_info("Stopped by user")
            return LoopResult(success=False, iterations_used=i - 1, exit_code=0)

        # Wait while paused
        while control.pause_requested:
            await asyncio.sleep(0.5)
            if control.stop_requested:
                callbacks.on_info("Stopped by user")
                return LoopResult(success=False, iterations_used=i - 1, exit_code=0)

        callbacks.on_iteration_start(i, max_iter)
        iter_start = time.monotonic()
        completed = False
        had_error = False

        # Run agent and stream output
        try:
            async for output in run_agent_async(
                agent_type=config.agent.type,
                model=config.agent.model,
                custom_command=config.agent.command,
                prompt_path=prompt_path,
                cwd=cwd,
                reasoning_effort=config.agent.reasoning_effort,
            ):
                callbacks.on_agent_line(output)
                if detect_completion(output.line):
                    completed = True
        except Exception as e:
            callbacks.on_error(f"Agent error: {e}")
            had_error = True

        elapsed = time.monotonic() - iter_start
        callbacks.on_iteration_end(i, elapsed)

        # Track consecutive errors and bail if they keep repeating
        if had_error:
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                callbacks.on_error(
                    f"Stopping: {max_consecutive_errors} consecutive errors. "
                    "Check your configuration and try again."
                )
                callbacks.on_complete(False, i)
                return LoopResult(success=False, iterations_used=i, exit_code=2)
        else:
            consecutive_errors = 0

        if completed:
            callbacks.on_complete(True, i)
            return LoopResult(success=True, iterations_used=i, exit_code=0)

        # Enforce allowed paths if configured
        if config.paths.allowed and is_git_repo(cwd):
            disallowed = find_disallowed_files(config.paths.allowed, cwd)
            if disallowed:
                callbacks.on_guard_violation(disallowed)
                # In non-interactive mode, revert automatically
                if not config.run.interactive:
                    msgs = revert_files(disallowed, cwd)
                    callbacks.on_guard_reverted(msgs)

        # Sleep between iterations
        if i < max_iter and config.run.sleep_seconds > 0:
            await asyncio.sleep(config.run.sleep_seconds)

    # Max iterations reached
    callbacks.on_complete(False, max_iter)
    return LoopResult(success=False, iterations_used=max_iter, exit_code=1)


def run_loop_sync(
    config: RalphConfig,
    cwd: Path,
    callbacks: LoopCallbacks,
    control: LoopControl | None = None,
) -> LoopResult:
    """Synchronous wrapper for run_loop. Used by CLI headless mode."""
    return asyncio.run(run_loop(config, cwd, callbacks, control))
