"""Main agentic loop for kstrl."""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

COMPLETION_MARKER = "<promise>COMPLETE</promise>"

# How many TOKENLESS agent calls the RUN must accumulate before an
# enabled token cap is declared unenforceable (see
# LoopBudget.halt_reason). A judgment call, not a measurement: one
# tokenless call is an incident (the claude adapter records
# source="timeout" / "parse-error" with no counts, and a cost-only
# result event is tokenless too), two in one run is a property of the
# adapter. The cost of the extra call is one iteration of overshoot on
# an adapter that reports no tokens; the cost of setting it to 1 is
# killing a capped run over a single timed-out first iteration.
#
# Counted RUN-WIDE (prior_calls/prior_token_calls carry the parent's
# figures down), because a per-loop counter resets on every attempt and
# every component: with max_iterations = 1, or a retry that dies after
# one call, no single loop ever reached the threshold and the rule was
# decorative. Review finding P1-a reproduced exactly that.
_UNENFORCEABLE_CALLS = 2


@dataclass(frozen=True)
class LoopBudget:
    """Run-level token ceiling pushed DOWN into the engineer loop (R8).

    ``[factory] max_total_tokens`` was, until R8, consulted only at
    parent-process phase boundaries (pipeline.process_result, the review
    / security / distill gates, the scheduling gate). Those checks can
    stop the NEXT phase; they can never stop the iteration already
    running, so a single component could spend for ``max_iterations x
    agent_iteration`` seconds before the cap was consulted once.

    This object is the loop-side half. It is evaluated BETWEEN
    iterations, which is the tightest bound available without a
    streaming token feed from the adapter.

    WHAT IT GUARANTEES: the engineer loop starts no NEW iteration once
    the run's reported spend has reached the cap. Overshoot is bounded
    by the work already in flight - roughly one iteration per running
    worker - not by zero.

    WHAT STAYS UNBOUNDED:
    - A single runaway iteration. Nothing here interrupts an agent call
      mid-flight; only ``[timeout] agent_iteration`` bounds that, and it
      bounds wall clock, not tokens.
    - Concurrent siblings. ``prior_total_tokens`` is the run total as of
      THIS worker's launch; spend by workers running in parallel is
      invisible until they report back to the parent. With
      ``max_parallel = N`` the overshoot scales with N.
    - Unreported spend. Every figure here is a CLI self-report; see
      :meth:`halt_reason` for how unknown usage is treated.
    """

    # Copies of the parent's FactoryConfig.max_total_tokens and the run
    # usage accumulated before this worker launched. Plain ints so the
    # whole object pickles cleanly to a pool worker.
    max_total_tokens: int = 0
    prior_total_tokens: int = 0
    #: Reporting calls (token OR cost) the run had made before this
    #: worker launched. Provenance only - it says whether the run's
    #: prior total is a real figure or an artefact of a silent adapter -
    #: and NOT part of any decision; see :meth:`halt_reason`.
    prior_known_calls: int = 0
    #: Agent calls the run had made before this worker launched, and how
    #: many of them reported a TOKEN figure. Their difference is the
    #: run's tokenless-call count, which is what the unenforceable rule
    #: counts (R8 review P1-a: a per-loop counter resets on every
    #: attempt/component, so short loops never reached the threshold).
    prior_calls: int = 0
    prior_token_calls: int = 0

    @property
    def enabled(self) -> bool:
        return self.max_total_tokens > 0

    def halt_reason(self, loop_usage: UsageTotals) -> str | None:
        """Why this loop must stop now, or None to keep iterating.

        Two conditions, both computed from CLI self-reported usage:

        1. OVERRUN - the run total (spend before this worker launched
           plus what this loop has reported) has reached the cap.
        2. UNENFORCEABLE - the cap provably cannot trip. Two facts have
           to hold together, and they are deliberately measured over
           different scopes:

           (a) TOKEN EVIDENCE, judged on THIS loop: not one of this
               loop's calls reported a token figure
               (``loop_usage.token_calls == 0``). That is what makes the
               cap dead rather than merely slow, because
               ``prior_total_tokens`` is frozen at this worker's launch:
               if this loop never reports tokens, the run total stays
               fixed below the ceiling forever no matter how long the
               engineer runs.

               Judged on the loop ALONE, never on the run. Summing the
               run's reported calls in looked stricter but was weaker: a
               reporting architect (or a previous component) suppressed
               the halt for a silent engineer and handed back the
               decorative cap in exactly the configuration where it is
               easiest to hit - ``[agent] command`` sets a custom
               engineer command while the adversarial roles keep a
               reporting adapter.

               Reads ``token_calls``, not ``known_calls``: a record
               carrying only ``cost_usd`` sets ``known_calls`` while
               contributing nothing to ``total_tokens``, so
               ``known_calls`` reported perfect coverage for a cap that
               could never advance (review finding P1-b).

           (b) CALL THRESHOLD, counted RUN-WIDE: the run has now seen
               ``_UNENFORCEABLE_CALLS`` tokenless calls in total
               (``prior_calls - prior_token_calls`` plus this loop's
               own). This is the "have we seen enough to conclude?"
               half, and it must not reset per attempt or per component
               - with ``max_iterations = 1``, or a retry that dies after
               one call, a per-loop counter never reached 2 and the rule
               was decorative (review finding P1-a).

               Tokenless calls, not all calls: a call that DID report
               tokens is evidence the adapter works, so counting it
               toward "this adapter never reports" would be backwards
               and would make a single unparseable result fatal in an
               otherwise healthy run.

        CONSEQUENCE, stated plainly: in a run where every prior call
        reported tokens, a lone unparseable engineer call still does not
        halt - the run-wide tokenless count is 1. But once a run has
        accumulated one tokenless call ANYWHERE (including in a
        different role's adapter), the engineer's first tokenless
        iteration does trip the halt. That is the deliberate trade: two
        independent tokenless calls in one run is adapter behavior, not
        an incident, and the failure is loud, recorded, and recoverable
        (raise or clear ``max_total_tokens``), whereas the alternative
        is unbounded spend under a ceiling that cannot fire.

        A loop that has made no calls at all can never halt here: with
        ``loop_usage.calls == 0`` there is no token evidence either way,
        so a worker launched into a run with a high ``prior_calls`` must
        still get to run its engineer.

        An iteration that reports nothing WHILE other calls in the same
        loop do report counts as zero tokens: the running total stays a
        lower bound (see ``UsageTotals.unreported_calls``) that still
        grows toward the cap, so the halt arrives late rather than
        never. Charging unknown iterations a guessed amount would invent
        numbers the CLI never gave us.
        """
        if not self.enabled:
            return None
        total = self.prior_total_tokens + loop_usage.total_tokens
        if total >= self.max_total_tokens:
            return (
                f"token budget exceeded: {total} total tokens recorded >= "
                f"max_total_tokens ({self.max_total_tokens}); halting the "
                "engineer loop instead of starting another iteration (R8)"
            )
        prior_tokenless = max(0, self.prior_calls - self.prior_token_calls)
        run_tokenless = prior_tokenless + loop_usage.tokenless_calls
        if (
            loop_usage.calls > 0
            and loop_usage.token_calls == 0
            and run_tokenless >= _UNENFORCEABLE_CALLS
        ):
            return (
                f"token budget unenforceable: none of this loop's "
                f"{loop_usage.calls} agent call(s) reported a token count "
                f"and {run_tokenless} call(s) in this run have now reported "
                f"none, so max_total_tokens ({self.max_total_tokens}) can "
                "never trip on this adapter; halting rather than spending "
                "under a cap that cannot fire (R8)"
            )
        return None


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
    # R8: non-empty when the run-level token budget halted the loop
    # between iterations. The string is the human-readable reason (see
    # LoopBudget.halt_reason) and becomes the component's error, so the
    # audit trail records WHICH budget condition fired.
    budget_halt_reason: str = ""


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
    budget: LoopBudget | None = None,
    on_iteration_usage: Callable[[UsageTotals], None] | None = None,
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
        on_iteration_usage: Called with this loop's usage-so-far at
            every iteration boundary. The factory uses it to persist a
            durable copy, so a worker killed by a shutdown does not
            take its spend to the grave. Accounting only: an exception
            here is logged, never raised into the loop.

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
        raw_prompt = config.prompt_file.read_text()
    else:
        from kstrl.init_cmd import DEFAULT_PROMPT
        ui.warn(
            f"Prompt file not found at {config.prompt_file}; "
            "falling back to harness DEFAULT_PROMPT (run `ks init` "
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
            ui.info("Branch: KSTRL_BRANCH is set but empty; skipping branch checkout")
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
            ignored_paths = list(guard_ignored_paths or ())
            if bus is not None and bus.run_id:
                ignored_paths.append(f".kstrl/runs/{bus.run_id}/")
            ok, _ = guards.enforce_allowed_paths(
                config, ui, cwd, interaction=channel,
                ignored_paths=ignored_paths,
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

        # R8 run-level token budget: the ONLY in-loop enforcement point
        # for [factory] max_total_tokens. Checked here - after the
        # completion return, before the breaker's stall probe - so a
        # blown budget never pays for another agent call or another
        # breaker test command. This bounds overshoot to the work
        # already in flight (about one iteration per running worker); it
        # cannot interrupt a call mid-flight. See LoopBudget.
        if budget is not None:
            halt_reason = budget.halt_reason(loop_usage)
            if halt_reason is not None:
                ui.err(halt_reason)
                return LoopResult(
                    completed=False,
                    iterations=iteration,
                    exit_code=1,
                    duration_seconds=time.monotonic() - loop_start,
                    iteration_durations=iteration_durations,
                    timed_out_iterations=timed_out_iterations,
                    usage=loop_usage,
                    budget_halt_reason=halt_reason,
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
        - source: Source description (e.g. "from KSTRL_BRANCH", "from PRD")
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
