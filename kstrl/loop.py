"""Main agentic loop for Ralph."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import git, guards
from kstrl.agents.base import UsageTotals, collect_usage
from kstrl.agents.proc import TIMEOUT_MESSAGE_PREFIX
from kstrl.breaker import BreakerConfig, NoProgressBreaker
from kstrl.events import EventBus, IterationCompleted, IterationStarted
from kstrl.interaction import (
    InteractionChannel,
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from kstrl.prd import PRD
from kstrl.timeout import TimeoutConfig

if TYPE_CHECKING:
    from kstrl.agents.base import Agent
    from kstrl.config import KstrlConfig
    from kstrl.ui.base import UI

COMPLETION_MARKER = "<promise>COMPLETE</promise>"


@dataclass
class LoopResult:
    """Result of running the agentic loop."""

    completed: bool
    iterations: int
    exit_code: int
    duration_seconds: float = 0.0
    iteration_durations: list[float] = field(default_factory=list)
    # R0.1: which limit aborted the loop, if any. "component" means the
    # component_total wall clock was exceeded across iterations.
    timeout_limit: str | None = None
    # How many iterations were killed by the per-iteration agent timeout.
    # Derived from the adapters' timeout error line - a reporting hint,
    # never a control-flow gate.
    timed_out_iterations: int = 0
    # R3.1: aggregated engineer-loop usage (one record per agent.run
    # call, collected from the agent's usage_records). Token/cost fields
    # are CLI self-reports - lower bounds whenever unreported_calls > 0.
    usage: UsageTotals = field(default_factory=UsageTotals)
    # R7.5: True when the no-progress circuit breaker halted the loop
    # (N consecutive iterations with an unchanged diff hash and test
    # signature). Typed so the factory can route it distinctly instead
    # of string-matching the error.
    no_progress: bool = False


def run_loop(
    config: KstrlConfig,
    ui: UI,
    agent: Agent,
    cwd: Path | None = None,
    context_prefix: str | None = None,
    timeouts: TimeoutConfig | None = None,
    breaker_config: BreakerConfig | None = None,
    *,
    bus: EventBus | None = None,
    interaction: InteractionChannel | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> LoopResult:
    """Run the main agentic loop.

    Args:
        config: Ralph configuration
        ui: UI implementation for output
        agent: Agent to run
        cwd: Working directory (defaults to current)
        context_prefix: Optional context prepended to the prompt
        timeouts: Timeout limits (agent_iteration is passed into every
            agent.run call; component_total is enforced as a wall clock
            across iterations). Defaults to TimeoutConfig.from_env().
        breaker_config: No-progress circuit breaker limits (R7.5).
            Defaults to BreakerConfig.from_env().

    Returns:
        LoopResult with completion status and exit code
    """
    if cwd is None:
        cwd = Path.cwd()
    if timeouts is None:
        timeouts = TimeoutConfig.from_env()
    if breaker_config is None:
        breaker_config = BreakerConfig.from_env()

    ui.startup_art()

    # Display title
    ui.title("Ralph")

    # Display startup info
    ui.section("Startup")
    ui.kv("Root", str(cwd))
    ui.kv("Prompt", str(config.prompt_file))
    ui.kv("PRD", str(config.prd_file))
    ui.kv("Agent", agent.name)
    ui.kv("Max iterations", str(config.max_iterations))
    ui.kv("Sleep", f"{config.sleep_seconds}s")
    ui.kv("Interactive", "yes" if config.interactive else "no")
    allowed_paths = (
        ", ".join(config.allowed_paths) if config.allowed_paths else "<disabled>"
    )
    ui.kv("Allowed paths", allowed_paths)
    ui.kv("Reasoning", config.model_reasoning_effort or "<default>")
    ui.kv("UI", config.ui_mode)
    ui.kv(
        "Agent timeout",
        f"{timeouts.agent_iteration}s" if timeouts.agent_iteration > 0 else "<disabled>",
    )
    ui.kv(
        "Component timeout",
        f"{timeouts.component_total}s" if timeouts.component_total > 0 else "<disabled>",
    )
    ui.kv(
        "No-progress breaker",
        f"{breaker_config.no_progress_iterations} iterations"
        if breaker_config.no_progress_iterations > 0 else "<disabled>",
    )

    # Resolve the prompt template. If the explicit prompt file does not
    # exist, fall back to the H3-protected DEFAULT_PROMPT from
    # init_cmd.py. This makes ``ralph factory`` runnable on a project
    # that has not been ``ralph init``'d -- the engineer prompt is part
    # of the harness contract and should not require user setup.
    #
    # The fallback is announced explicitly so the operator can tell
    # "we used the default" from "we used your customized prompt at
    # scripts/kstrl/prompt.md", which matters when reading the
    # iteration log later.
    from string import Template
    if config.prompt_file.exists():
        raw_prompt = config.prompt_file.read_text()
    else:
        from kstrl.init_cmd import DEFAULT_PROMPT
        ui.warn(
            f"Prompt file not found at {config.prompt_file}; "
            "falling back to harness DEFAULT_PROMPT (run `ralph init` "
            "to scaffold a customizable copy)."
        )
        raw_prompt = DEFAULT_PROMPT
    prompt = Template(raw_prompt).safe_substitute(
        prd_path=str(config.prd_file),
        progress_path=str(config.progress_file),
        codebase_map_path=str(config.codebase_map_file),
    )

    # Prepend CLAUDE.md project context if it exists in the working directory
    claude_md_path = cwd / "CLAUDE.md"
    if claude_md_path.exists():
        claude_md_content = claude_md_path.read_text()
        prompt = (
            "# Project Context (from CLAUDE.md)\n\n"
            + claude_md_content
            + "\n\n---\n\n"
            + prompt
        )

    # Prepend context from previous retries if provided
    if context_prefix:
        prompt = context_prefix + "\n\n" + prompt

    # Preflight
    ui.section("Preflight")

    # Git/Branch handling
    ui.subsection("Git / Branch")
    is_repo = git.is_git_repo(cwd)

    if not is_repo:
        ui.warn("Not a git repository")
    elif not config.auto_checkout:
        ui.info("Branch: auto_checkout disabled; using current branch")
    else:
        branch, source = _determine_branch(config)
        if branch:
            if not git.checkout_branch(branch, ui, cwd, source):
                ui.err(f"Failed to checkout branch: {branch}")
                return LoopResult(completed=False, iterations=0, exit_code=1)
        elif branch == "":
            ui.info("Branch: RALPH_BRANCH is set but empty; skipping branch checkout")
        else:
            ui.info("Branch: no branch configured")

    # Guardrails info
    ui.subsection("Guardrails")
    if config.allowed_paths and is_repo:
        ui.info(f"Enforcing ALLOWED_PATHS={','.join(config.allowed_paths)}")
    else:
        ui.info("ALLOWED_PATHS is empty; enforcement disabled")

    # PR A: one interaction channel for the whole loop (guards + pause).
    channel: InteractionChannel = (
        interaction if interaction is not None else UiInteractionChannel(ui)
    )
    loop_start = time.monotonic()
    iteration_durations: list[float] = []
    timed_out_iterations = 0
    component_budget = timeouts.component_total

    # R7.5: baseline fingerprint captured before iteration 1 so an
    # agent that changes nothing in its first N iterations still trips.
    # Inert outside a git repo (nothing to fingerprint) - stated loudly
    # rather than silently.
    breaker = NoProgressBreaker(cwd, breaker_config)
    if breaker_config.no_progress_iterations > 0 and not breaker.enabled:
        ui.warn(
            "No-progress breaker disabled: working directory is not a "
            "usable git repository"
        )

    for iteration in range(1, config.max_iterations + 1):
        if stop_check is not None and stop_check():
            ui.warn("Stop requested; ending loop before next iteration")
            return LoopResult(
                completed=False, iterations=iteration - 1, exit_code=130,
                duration_seconds=time.monotonic() - loop_start,
                iteration_durations=iteration_durations,
                timed_out_iterations=timed_out_iterations,
                usage=collect_usage(agent),
            )
        ui.section(f"Iteration {iteration} / {config.max_iterations}")
        iter_start = time.monotonic()
        if bus is not None:
            bus.emit(IterationStarted(
                iteration=iteration, max_iterations=config.max_iterations,
            ))

        # Bound the iteration by the per-iteration limit AND the remaining
        # component budget, so one iteration cannot blow far past the
        # component wall clock (the adapters kill the agent's process
        # group when the passed timeout expires).
        iteration_timeout: float | None = (
            timeouts.agent_iteration if timeouts.agent_iteration > 0 else None
        )
        if component_budget > 0:
            remaining = component_budget - (iter_start - loop_start)
            iteration_timeout = (
                min(iteration_timeout, remaining)
                if iteration_timeout is not None else remaining
            )

        # Run agent
        completion_seen = False
        iteration_timed_out = False
        try:
            for line in agent.run(prompt, cwd, timeout=iteration_timeout):
                if line.strip() == COMPLETION_MARKER:
                    completion_seen = True
                if line.startswith(TIMEOUT_MESSAGE_PREFIX):
                    iteration_timed_out = True
                ui.stream_line("AI", line)

            final_message = agent.final_message
            if not completion_seen and final_message:
                completion_seen = any(
                    line.strip() == COMPLETION_MARKER
                    for line in final_message.splitlines()
                )
        finally:
            iter_duration = time.monotonic() - iter_start
            iteration_durations.append(iter_duration)
            if bus is not None:
                bus.emit(IterationCompleted(
                    iteration=iteration,
                    duration_seconds=round(iter_duration, 2),
                    completed=completion_seen,
                    timed_out=iteration_timed_out,
                ))

        if iteration_timed_out:
            timed_out_iterations += 1
            ui.warn(
                f"Iteration {iteration} hit the agent iteration timeout "
                f"({iteration_timeout}s); the agent process group was killed"
            )

        # Enforce ALLOWED_PATHS BEFORE honoring the completion marker
        # (R0.4): an agent that edits out-of-scope files and emits
        # COMPLETE in the same iteration must not bypass enforcement.
        # When enforcement fails the iteration is treated as failed even
        # if the marker was seen.
        if config.allowed_paths and is_repo:
            ok, _ = guards.enforce_allowed_paths(
                config, ui, cwd, interaction=channel,
            )
            if not ok:
                return LoopResult(
                    completed=False,
                    iterations=iteration,
                    exit_code=1,
                    duration_seconds=time.monotonic() - loop_start,
                    iteration_durations=iteration_durations,
                    timed_out_iterations=timed_out_iterations,
                    usage=collect_usage(agent),
                )

        # Check for completion
        if completion_seen:
            ui.ok("Done")
            total_duration = time.monotonic() - loop_start
            return LoopResult(
                completed=True,
                iterations=iteration,
                exit_code=0,
                duration_seconds=total_duration,
                iteration_durations=iteration_durations,
                timed_out_iterations=timed_out_iterations,
                usage=collect_usage(agent),
            )

        # R7.5 no-progress circuit breaker: the iteration finished
        # without completing AND without changing the tree or the test
        # outcome. After N consecutive such iterations, halt loudly -
        # every further iteration would re-run the same prompt against
        # the same state.
        if breaker.record_iteration():
            halt_message = breaker.halt_message()
            ui.err(halt_message)
            return LoopResult(
                completed=False,
                iterations=iteration,
                exit_code=1,
                duration_seconds=time.monotonic() - loop_start,
                iteration_durations=iteration_durations,
                timed_out_iterations=timed_out_iterations,
                usage=collect_usage(agent),
                no_progress=True,
            )

        # Component wall clock: abort cleanly rather than start work that
        # is already past its budget. This is the "which limit fired"
        # signal for the factory (timeout_limit="component").
        elapsed = time.monotonic() - loop_start
        if component_budget > 0 and elapsed >= component_budget:
            ui.err(
                f"Component timeout: {component_budget}s wall clock exceeded "
                f"after {iteration} iteration(s); aborting loop"
            )
            return LoopResult(
                completed=False,
                iterations=iteration,
                exit_code=1,
                duration_seconds=elapsed,
                iteration_durations=iteration_durations,
                timeout_limit="component",
                timed_out_iterations=timed_out_iterations,
                usage=collect_usage(agent),
            )

        # Interactive pause (PR A: through the interaction seam)
        if config.interactive and channel.can_prompt():
            response = channel.request(PromptRequest(
                kind=PromptKind.ITERATION,
                header="Iteration complete. What next?",
                options=("Continue", "Skip interactive", "Quit"),
                default=0,
            ))
            if response.answered and response.choice == 1:
                # Disable interactive for remaining iterations
                config.interactive = False
            elif response.answered and response.choice == 2:
                return LoopResult(
                    completed=False, iterations=iteration, exit_code=0,
                    usage=collect_usage(agent),
                )

        # Sleep before next iteration (except on last)
        if iteration < config.max_iterations:
            time.sleep(config.sleep_seconds)

    # Max iterations reached
    if timed_out_iterations:
        ui.warn(
            f"Max iterations reached (no {COMPLETION_MARKER} seen; "
            f"{timed_out_iterations} iteration(s) hit the agent timeout)"
        )
    else:
        ui.warn(f"Max iterations reached (no {COMPLETION_MARKER} seen)")
    total_duration = time.monotonic() - loop_start
    return LoopResult(
        completed=False,
        iterations=config.max_iterations,
        exit_code=1,
        duration_seconds=total_duration,
        iteration_durations=iteration_durations,
        timed_out_iterations=timed_out_iterations,
        usage=collect_usage(agent),
    )


def _determine_branch(config: KstrlConfig) -> tuple[str | None, str | None]:
    """Determine which branch to use.

    Returns:
        Tuple of (branch_name, source) where:
        - branch_name: Branch to checkout, "" to skip, None if not configured
        - source: Source description (e.g. "from RALPH_BRANCH", "from PRD")
    """
    # If a branch is configured directly on the config, prefer it.
    # `kstrl_branch_explicit` is used to indicate whether it came from RALPH_BRANCH/--branch.
    if config.kstrl_branch is not None:
        if config.kstrl_branch_explicit:
            return config.kstrl_branch, "from RALPH_BRANCH"
        return config.kstrl_branch, "default"

    # Try to get from PRD
    if config.prd_file.exists():
        try:
            prd = PRD.load(config.prd_file)
            if prd.branch_name:
                return prd.branch_name, "from PRD"
        except Exception:
            pass

    return None, None
