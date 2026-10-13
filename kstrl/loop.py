"""Main agentic loop for kstrl."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import git, git_tree, guards, statedir
from kstrl.agents.base import UsageTotals, collect_usage
from kstrl.agents.proc import TIMEOUT_MESSAGE_PREFIX
from kstrl.breaker import BreakerConfig, NoProgressBreaker
from kstrl.event_catalog import IterationCompleted, IterationStarted
from kstrl.events import EventBus
from kstrl.interaction import (
    InteractionChannel,
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from kstrl.iteration_prompt import (
    assemble_prompt,
    build_project_context,
    failed_fast_checks,
    fast_iteration_reading,
    measurement_block,
)
from kstrl.loop_budget import LoopBudget
from kstrl.loop_setup import capture_guard_baseline, print_startup
from kstrl.prd import PRD
from kstrl.timeout import TimeoutConfig
from kstrl.verify_model import VerifyConfig

if TYPE_CHECKING:
    from kstrl.agents.base import Agent
    from kstrl.config import KstrlConfig
    from kstrl.ui.base import UI

logger = logging.getLogger(__name__)

COMPLETION_MARKER = "<promise>COMPLETE</promise>"

#: The exit code a loop returns when ``stop_check`` asked it to stop:
#: the shell's 128 + SIGINT, so an operator's Ctrl-C and a stop
#: request read the same downstream. Named because a caller has to be
#: able to tell "the operator stopped this" from "it failed" - #288's
#: verification report refuses to run a test suite on the way out of a
#: stop.
STOP_EXIT_CODE = 130


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
    # R8: non-empty when a run-level ceiling (max_total_tokens or
    # max_cost_usd) halted the loop between iterations. The string is the
    # human-readable reason (see LoopBudget.halt_reason) and becomes the
    # component's error, so the audit trail records WHICH ceiling and
    # WHICH condition fired.
    budget_halt_reason: str = ""
    # R8 review (#180): the identity half of the same halt. Carried
    # structurally so the parent never has to re-derive which ceiling
    # fired from its own totals - which named the wrong one whenever a
    # breach and a dead ceiling coexisted - or from the prose above.
    budget_halt_condition: str = ""
    budget_halt_ceilings: tuple[str, ...] = ()
    # R8 review: files the in-loop ALLOWED_PATHS guard rejected. Carried
    # because the guard used to discard them (``ok, _ =``) and the
    # factory then reported the halt as "Did not complete" - the retry
    # agent was told nothing about what it had touched, so its cheapest
    # strategy was to repeat the edit.
    guard_violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class _LoopRun:
    """The values of one run that do not change between iterations. Each
    field is a reference to the object that ``run_loop`` made or received,
    never a copy, so a later write through one is seen through the other."""

    config: KstrlConfig
    ui: UI
    agent: Agent
    cwd: Path
    bus: EventBus | None
    timeouts: TimeoutConfig
    is_repo: bool
    channel: InteractionChannel
    guard_ignored: list[str]
    guard_baseline: git_tree.WorkspaceBaseline | None
    budget: LoopBudget | None
    breaker: NoProgressBreaker
    component_budget: float
    loop_start: float
    iteration_durations: list[float]
    context_prefix: str | None
    body: str
    verify_config: VerifyConfig | None
    on_iteration_usage: Callable[[UsageTotals], None] | None


def _resolve_iteration_pause(
    ui: UI,
    channel: InteractionChannel,
    config: KstrlConfig,
    iteration: int,
    agent: Agent,
) -> LoopResult | None:
    """The interactive pause between iterations (PR A: through the
    interaction seam). Returns a :class:`LoopResult` when the run should
    stop - Quit, or nobody answered - and ``None`` to continue (mutating
    ``config.interactive`` on 'Skip interactive').

    #594 D1: a channel that COULD prompt (``can_prompt()`` true) but came
    back with nobody answering is not consent to keep spending on another
    paid iteration. It stops the run the same way Quit does, with one log
    line saying so. A channel that cannot prompt never reaches the
    request below, so the non-interactive path is unchanged.
    """
    if not (config.interactive and channel.can_prompt()):
        return None
    response = channel.request(
        PromptRequest(
            kind=PromptKind.ITERATION,
            header="Iteration complete. What next?",
            options=("Continue", "Skip interactive", "Quit"),
            default=0,
        )
    )
    if response.choice == 1:
        # Disable interactive for remaining iterations
        config.interactive = False
        return None
    if response.choice == 2:
        return LoopResult(
            completed=False,
            iterations=iteration,
            exit_code=0,
            usage=collect_usage(agent),
        )
    if response.choice is None:
        ui.warn("Iteration pause was interrupted; stopping the run")
        return LoopResult(
            completed=False,
            iterations=iteration,
            exit_code=0,
            usage=collect_usage(agent),
        )
    return None


def _with_defaults(
    cwd: Path | None,
    timeouts: TimeoutConfig | None,
    breaker_config: BreakerConfig | None,
) -> tuple[Path, TimeoutConfig, BreakerConfig]:
    """The working directory, timeouts and breaker limits of a run, each
    taken from the caller or else from the environment."""
    if cwd is None:
        cwd = Path.cwd()
    if timeouts is None:
        timeouts = TimeoutConfig.from_env()
    if breaker_config is None:
        breaker_config = BreakerConfig.from_env()
    return cwd, timeouts, breaker_config


def _prompt_body(
    config: KstrlConfig,
    ui: UI,
    cwd: Path,
    context_prefix: str | None,
    verify_config: VerifyConfig | None,
    context_root: Path | None,
) -> tuple[str, str]:
    """The prompt body (template plus project context) and the first
    iteration's prompt, which is the body behind the retry context."""
    # Resolve the prompt template. If the explicit prompt file does not
    # exist, fall back to the H3-protected DEFAULT_PROMPT from
    # init_cmd.py. This makes ``ks factory`` runnable on a project
    # that has not been ``ks init``'d -- the engineer prompt is part
    # of the harness contract and should not require user setup.
    #
    # The fallback is announced explicitly so the operator can tell
    # "we used the default" from "we used your customized prompt at
    # scripts/kstrl/prompt.md", which matters when reading the
    # iteration log later.
    from string import Template

    if config.prompt_file.exists():
        raw_prompt = config.prompt_file.read_text(encoding="utf-8")
    else:
        from kstrl.scaffold_templates import DEFAULT_PROMPT

        ui.warn(
            f"Prompt file not found at {config.prompt_file}; "
            "falling back to harness DEFAULT_PROMPT (run `ks init` "
            "to scaffold a customizable copy)."
        )
        raw_prompt = DEFAULT_PROMPT
    # $progress_path must be CONCRETE: the agent is told to append to it.
    # config.progress_file is None until someone configures one (R8
    # review finding 2), so the standalone loop materializes the
    # historical repo-root default here. A factory worker arrives with an
    # already-resolved per-component path and gets it back untouched.
    prompt = Template(raw_prompt).safe_substitute(
        prd_path=str(config.prd_file),
        progress_path=str(config.resolved_progress_file(cwd)),
        codebase_map_path=str(config.codebase_map_file),
    )

    project_context = build_project_context(cwd, ui, verify_config, context_root=context_root)
    if project_context:
        prompt = project_context + "\n\n---\n\n" + prompt

    # The retry context goes in front of the body; from iteration 2 on the
    # last iteration's measurement goes between them (#233).
    body = prompt
    prompt = assemble_prompt(context_prefix, "", body)
    return body, prompt


def _checkout_branch(config: KstrlConfig, ui: UI, cwd: Path, is_repo: bool) -> LoopResult | None:
    """Check out the branch of the run, or return the refusal result when
    the checkout fails. None means that the run goes on."""
    if not is_repo:
        ui.warn("Not a git repository")
    elif not config.auto_checkout:
        ui.info("Branch: auto_checkout disabled; using current branch")
    else:
        branch, source = determine_branch(config)
        if branch:
            if not git.checkout_branch(branch, ui, cwd, source):
                ui.err(f"Failed to checkout branch: {branch}")
                # A refusal before iteration 1 exits 2 (#452, #531).
                return LoopResult(completed=False, iterations=0, exit_code=2)
        elif branch == "":
            ui.info("Branch: KSTRL_BRANCH is set but empty; skipping branch checkout")
        else:
            ui.info("Branch: no branch configured")
    return None


def _guard_setup(
    config: KstrlConfig,
    ui: UI,
    cwd: Path,
    is_repo: bool,
    guard_ignored_paths: list[str] | None,
    guard_state_root: Path | None,
    guard_base_ref: str | None,
) -> tuple[git_tree.WorkspaceBaseline | None, list[str]] | LoopResult:
    """The guard baseline and the ignored paths of the run, or the refusal
    result when the baseline could not be taken."""
    guard_baseline: git_tree.WorkspaceBaseline | None = None
    # The complete set the guard must not count: the caller's
    # per-invocation harness files (#264) and kstrl's own state
    # directory (#274). Both are loop-invariant, so this is assembled
    # once here rather than rebuilt every iteration.
    #
    # The state carve-out supersedes the `.kstrl/runs/<run_id>/` entry
    # this loop used to derive from the bus. `.kstrl/runs/` covers it
    # wherever kstrl actually writes it, and it is now absent inside a
    # component worktree, which is a TIGHTENING: the run journal is
    # never written there, so that entry could only ever have hidden a
    # `.kstrl/runs/<run_id>/` path the AGENT wrote.
    guard_ignored: list[str] = []
    if config.allowed_paths and is_repo:
        guard_ignored = [
            *(guard_ignored_paths or ()),
            *statedir.state_dir_carve_out(cwd, guard_state_root),
        ]
        ui.info(f"Enforcing ALLOWED_PATHS={','.join(config.allowed_paths)}")
        # R8 review finding 4: the guard's question is "what did THIS
        # agent change", and only a before-picture can answer it. Taken
        # once, here, BEFORE the first iteration and after the harness
        # has finished staging its own files into the worktree: what is
        # already dirty now belongs to the operator or the harness, and
        # everything the agent does from here - committed or not - is
        # attributable to the agent.
        guard_baseline = capture_guard_baseline(cwd, guard_base_ref, ui)
        if guard_baseline is None:
            # A refusal before iteration 1 exits 2 (#452, #531).
            return LoopResult(completed=False, iterations=0, exit_code=2)
    else:
        ui.info("ALLOWED_PATHS is empty; enforcement disabled")
    return guard_baseline, guard_ignored


def _iteration_timeout(
    timeouts: TimeoutConfig, component_budget: float, iter_start: float, loop_start: float
) -> float | None:
    """The time limit of one agent run."""
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
            min(iteration_timeout, remaining) if iteration_timeout is not None else remaining
        )
    return iteration_timeout


def _publish_usage(
    agent: Agent, on_iteration_usage: Callable[[UsageTotals], None] | None
) -> UsageTotals:
    """The usage snapshot of one iteration, handed to the caller's callback."""
    # One usage snapshot per iteration, taken before any early
    # return below so a timed-out or guard-failing iteration is
    # accounted for too. Published first (durability), then used for
    # the budget decision.
    loop_usage = collect_usage(agent)
    if on_iteration_usage is not None:
        try:
            on_iteration_usage(loop_usage)
        except Exception as exc:  # noqa: BLE001 - accounting never gates
            logger.warning("Failed to publish iteration usage: %s", exc)
    return loop_usage


def _run_one_iteration(
    run: _LoopRun, iteration: int, prompt: str, timed_out_iterations: int
) -> tuple[str, bool, UsageTotals, int]:
    """One agent run: the next prompt, whether the completion marker was
    seen, the usage so far and the count of timed-out iterations."""
    run.ui.section(f"Iteration {iteration} / {run.config.max_iterations}")
    iter_start = time.monotonic()
    if run.bus is not None:
        run.bus.emit(
            IterationStarted(
                iteration=iteration,
                max_iterations=run.config.max_iterations,
            )
        )

    iteration_timeout = _iteration_timeout(
        run.timeouts, run.component_budget, iter_start, run.loop_start
    )

    # Run agent
    completion_seen = False
    iteration_timed_out = False
    fast_checks_failed: tuple[str, ...] = ()
    try:
        for line in run.agent.run(prompt, run.cwd, timeout=iteration_timeout):
            if line.strip() == COMPLETION_MARKER:
                completion_seen = True
            if line.startswith(TIMEOUT_MESSAGE_PREFIX):
                iteration_timed_out = True
            run.ui.stream_line("AI", line)

        final_message = run.agent.final_message
        if not completion_seen and final_message:
            completion_seen = any(
                line.strip() == COMPLETION_MARKER for line in final_message.splitlines()
            )
        # #233: measure the tree this iteration left and rebuild the
        # next prompt around the reading. Inside the timed block, so
        # the checks' cost is part of the iteration's wall clock.
        reading = fast_iteration_reading(
            run.verify_config,
            run.cwd,
            run.ui,
            completed=completion_seen,
            timed_out=iteration_timed_out,
        )
        fast_checks_failed = failed_fast_checks(reading)
        prompt = assemble_prompt(run.context_prefix, measurement_block(reading), run.body)
    finally:
        iter_duration = time.monotonic() - iter_start
        run.iteration_durations.append(iter_duration)
        if run.bus is not None:
            run.bus.emit(
                IterationCompleted(
                    iteration=iteration,
                    duration_seconds=round(iter_duration, 2),
                    completed=completion_seen,
                    timed_out=iteration_timed_out,
                    fast_checks_failed=fast_checks_failed,
                )
            )

    loop_usage = _publish_usage(run.agent, run.on_iteration_usage)

    if iteration_timed_out:
        timed_out_iterations += 1
        run.ui.warn(
            f"Iteration {iteration} hit the agent iteration timeout "
            f"({iteration_timeout}s); the agent process group was killed"
        )
    return prompt, completion_seen, loop_usage, timed_out_iterations


def _guard_verdict(run: _LoopRun, iteration: int, timed_out_iterations: int) -> LoopResult | None:
    """The result that ends the run when the allowed paths guard fails."""
    # Enforce ALLOWED_PATHS BEFORE honoring the completion marker
    # (R0.4): an agent that edits out-of-scope files and emits
    # COMPLETE in the same iteration must not bypass enforcement.
    # When enforcement fails the iteration is treated as failed even
    # if the marker was seen.
    if run.config.allowed_paths and run.is_repo:
        ok, violations = guards.enforce_allowed_paths(
            run.config,
            run.ui,
            run.cwd,
            interaction=run.channel,
            ignored_paths=run.guard_ignored,
            baseline=run.guard_baseline,
        )
        if not ok:
            return LoopResult(
                completed=False,
                iterations=iteration,
                exit_code=1,
                duration_seconds=time.monotonic() - run.loop_start,
                iteration_durations=run.iteration_durations,
                timed_out_iterations=timed_out_iterations,
                usage=collect_usage(run.agent),
                guard_violations=tuple(violations),
            )
    return None


def _budget_verdict(
    run: _LoopRun, iteration: int, timed_out_iterations: int, loop_usage: UsageTotals
) -> LoopResult | None:
    """The result that ends the run when the run-level budget is spent."""
    # R8 run-level budget: the ONLY in-loop enforcement point for
    # [factory] max_total_tokens and [factory] max_cost_usd. Checked
    # here - after the completion return, before the breaker's stall
    # probe - so a blown ceiling never pays for another agent call or
    # another breaker test command. This bounds overshoot to the work
    # already in flight (about one iteration per running worker); it
    # cannot interrupt a call mid-flight. See LoopBudget.
    if run.budget is not None:
        halt = run.budget.halt_verdict(loop_usage)
        if halt is not None:
            run.ui.err(halt.reason)
            return LoopResult(
                completed=False,
                iterations=iteration,
                exit_code=1,
                duration_seconds=time.monotonic() - run.loop_start,
                iteration_durations=run.iteration_durations,
                timed_out_iterations=timed_out_iterations,
                usage=loop_usage,
                budget_halt_reason=halt.reason,
                budget_halt_condition=halt.condition,
                budget_halt_ceilings=halt.ceilings,
            )
    return None


def _breaker_verdict(run: _LoopRun, iteration: int, timed_out_iterations: int) -> LoopResult | None:
    """The result that ends the run when the no-progress breaker trips."""
    # R7.5 no-progress circuit breaker: the iteration finished
    # without completing AND without changing the tree or the test
    # outcome. After N consecutive such iterations, halt loudly -
    # every further iteration would re-run the same prompt against
    # the same state.
    if run.breaker.record_iteration():
        halt_message = run.breaker.halt_message()
        run.ui.err(halt_message)
        return LoopResult(
            completed=False,
            iterations=iteration,
            exit_code=1,
            duration_seconds=time.monotonic() - run.loop_start,
            iteration_durations=run.iteration_durations,
            timed_out_iterations=timed_out_iterations,
            usage=collect_usage(run.agent),
            no_progress=True,
        )
    return None


def _wall_clock_verdict(
    run: _LoopRun, iteration: int, timed_out_iterations: int
) -> LoopResult | None:
    """The result that ends the run when the component wall clock is spent."""
    # Component wall clock: abort cleanly rather than start work that
    # is already past its budget. This is the "which limit fired"
    # signal for the factory (timeout_limit="component").
    elapsed = time.monotonic() - run.loop_start
    if run.component_budget > 0 and elapsed >= run.component_budget:
        run.ui.err(
            f"Component timeout: {run.component_budget}s wall clock exceeded "
            f"after {iteration} iteration(s); aborting loop"
        )
        return LoopResult(
            completed=False,
            iterations=iteration,
            exit_code=1,
            duration_seconds=elapsed,
            iteration_durations=run.iteration_durations,
            timeout_limit="component",
            timed_out_iterations=timed_out_iterations,
            usage=collect_usage(run.agent),
        )
    return None


def _iteration_verdict(
    run: _LoopRun,
    iteration: int,
    timed_out_iterations: int,
    completion_seen: bool,
    loop_usage: UsageTotals,
) -> LoopResult | None:
    """The result that ends the run after one iteration, or None when the
    run goes on to the next iteration."""
    if (guard_result := _guard_verdict(run, iteration, timed_out_iterations)) is not None:
        return guard_result

    # Check for completion
    if completion_seen:
        run.ui.ok("Done")
        total_duration = time.monotonic() - run.loop_start
        return LoopResult(
            completed=True,
            iterations=iteration,
            exit_code=0,
            duration_seconds=total_duration,
            iteration_durations=run.iteration_durations,
            timed_out_iterations=timed_out_iterations,
            usage=collect_usage(run.agent),
        )

    if (
        budget_result := _budget_verdict(run, iteration, timed_out_iterations, loop_usage)
    ) is not None:
        return budget_result

    if (breaker_result := _breaker_verdict(run, iteration, timed_out_iterations)) is not None:
        return breaker_result

    if (clock_result := _wall_clock_verdict(run, iteration, timed_out_iterations)) is not None:
        return clock_result

    # Interactive pause (PR A: through the interaction seam)
    pause_result = _resolve_iteration_pause(run.ui, run.channel, run.config, iteration, run.agent)
    if pause_result is not None:
        return pause_result
    return None


def _max_iterations_result(run: _LoopRun, timed_out_iterations: int) -> LoopResult:
    """The result of a run that used all its iterations."""
    # Max iterations reached
    if timed_out_iterations:
        run.ui.warn(
            f"Max iterations reached (no {COMPLETION_MARKER} seen; "
            f"{timed_out_iterations} iteration(s) hit the agent timeout)"
        )
    else:
        run.ui.warn(f"Max iterations reached (no {COMPLETION_MARKER} seen)")
    total_duration = time.monotonic() - run.loop_start
    return LoopResult(
        completed=False,
        iterations=run.config.max_iterations,
        exit_code=1,
        duration_seconds=total_duration,
        iteration_durations=run.iteration_durations,
        timed_out_iterations=timed_out_iterations,
        usage=collect_usage(run.agent),
    )


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
    guard_ignored_paths: list[str] | None = None,
    # The project root whose `.kstrl/` this run writes (#274). Only the
    # caller knows it: `cwd` is the component worktree in a factory run
    # and the project root everywhere else, and the two are the same
    # directory only under --no-worktrees. Left None the loop carves
    # nothing out, which is the pre-#274 behaviour.
    guard_state_root: Path | None = None,
    # The component's base branch. The in-loop scope guard measures from
    # here so it asks the same question check_diff_scope does; None (the
    # standalone `ks run` case) falls back to the starting HEAD.
    guard_base_ref: str | None = None,
    budget: LoopBudget | None = None,
    on_iteration_usage: Callable[[UsageTotals], None] | None = None,
    verify_config: VerifyConfig | None = None,
    context_root: Path | None = None,
) -> LoopResult:
    """Run the main agentic loop.

    Args:
        config: kstrl configuration
        ui: UI implementation for output
        agent: Agent to run
        cwd: Working directory (defaults to current)
        context_prefix: Optional context prepended to the prompt
        timeouts: Timeout limits (agent_iteration is passed into every
            agent.run call; component_total is enforced as a wall clock
            across iterations). Defaults to TimeoutConfig.from_env().
        breaker_config: No-progress circuit breaker limits (R7.5).
            Defaults to BreakerConfig.from_env().
        budget: Run-level token ceiling (R8), checked between
            iterations. None (the default, and every non-factory
            caller) means no in-loop token limit.
        verify_config: The config the Phase 1 gate will run with, or
            None (the default) when no gate runs. See
            ``build_project_context`` (#261).
        context_root: The directory whose CLAUDE.md is the project
            context, or None (the default) for ``cwd``. A factory worker
            passes the checkout kstrl ran from, because kstrl no longer
            copies CLAUDE.md into a component worktree (#569).
        on_iteration_usage: Called with this loop's usage-so-far at
            every iteration boundary. The factory uses it to persist a
            durable copy, so a worker killed by a shutdown does not
            take its spend to the grave. Accounting only: an exception
            here is logged, never raised into the loop.

    Returns:
        LoopResult with completion status and exit code
    """
    cwd, timeouts, breaker_config = _with_defaults(cwd, timeouts, breaker_config)

    print_startup(config, ui, agent, cwd, timeouts, breaker_config)

    body, prompt = _prompt_body(config, ui, cwd, context_prefix, verify_config, context_root)

    # Preflight
    ui.section("Preflight")

    # Git/Branch handling
    ui.subsection("Git / Branch")
    is_repo = git.is_git_repo(cwd)

    if (refusal := _checkout_branch(config, ui, cwd, is_repo)) is not None:
        return refusal

    # Guardrails info
    ui.subsection("Guardrails")
    if isinstance(
        guard_setup := _guard_setup(
            config, ui, cwd, is_repo, guard_ignored_paths, guard_state_root, guard_base_ref
        ),
        LoopResult,
    ):
        return guard_setup
    guard_baseline, guard_ignored = guard_setup

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
    run = _LoopRun(
        config=config,
        ui=ui,
        agent=agent,
        cwd=cwd,
        bus=bus,
        timeouts=timeouts,
        is_repo=is_repo,
        channel=channel,
        guard_ignored=guard_ignored,
        guard_baseline=guard_baseline,
        budget=budget,
        breaker=breaker,
        component_budget=component_budget,
        loop_start=loop_start,
        iteration_durations=iteration_durations,
        context_prefix=context_prefix,
        body=body,
        verify_config=verify_config,
        on_iteration_usage=on_iteration_usage,
    )
    if breaker_config.no_progress_iterations > 0 and not breaker.enabled:
        ui.warn("No-progress breaker disabled: working directory is not a usable git repository")

    for iteration in range(1, config.max_iterations + 1):
        if stop_check is not None and stop_check():
            ui.warn("Stop requested; ending loop before next iteration")
            return LoopResult(
                completed=False,
                iterations=iteration - 1,
                exit_code=STOP_EXIT_CODE,
                duration_seconds=time.monotonic() - loop_start,
                iteration_durations=iteration_durations,
                timed_out_iterations=timed_out_iterations,
                usage=collect_usage(agent),
            )
        prompt, completion_seen, loop_usage, timed_out_iterations = _run_one_iteration(
            run, iteration, prompt, timed_out_iterations
        )

        if (
            verdict := _iteration_verdict(
                run, iteration, timed_out_iterations, completion_seen, loop_usage
            )
        ) is not None:
            return verdict

        # Sleep before next iteration (except on last)
        if iteration < config.max_iterations:
            time.sleep(config.sleep_seconds)

    return _max_iterations_result(run, timed_out_iterations)


def determine_branch(config: KstrlConfig) -> tuple[str | None, str | None]:
    """Determine which branch to use.

    Returns:
        Tuple of (branch_name, source) where:
        - branch_name: Branch to checkout, "" to skip, None if not configured
        - source: Source description (e.g. "from KSTRL_BRANCH", "from PRD")

    Public since #288 review round 2: ``feature_verify.baseline_skip_reason``
    has to know which branch this loop will check out BEFORE the loop
    runs, and a second copy of the precedence rule is a second thing that
    can disagree with the checkout that actually happens.
    """
    # If a branch is configured directly on the config, prefer it.
    # `kstrl_branch_explicit` is used to indicate whether it came from KSTRL_BRANCH/--branch.
    if config.kstrl_branch is not None:
        if config.kstrl_branch_explicit:
            return config.kstrl_branch, "from KSTRL_BRANCH"
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
