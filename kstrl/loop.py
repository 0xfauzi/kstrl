"""Main agentic loop for kstrl."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import git, guards, statedir
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
from kstrl.verify import (
    VerifyConfig,
    resolve_verify_commands,
    scrub_project_claude_md,
)

if TYPE_CHECKING:
    from kstrl.agents.base import Agent
    from kstrl.config import KstrlConfig
    from kstrl.ui.base import UI

logger = logging.getLogger(__name__)

COMPLETION_MARKER = "<promise>COMPLETE</promise>"

#: The exit code a loop returns when ``stop_check`` asked it to stop:
#: the shell's 128 + SIGINT, so an operator's Ctrl-C and a TUI stop
#: request read the same downstream. Named because a caller has to be
#: able to tell "the operator stopped this" from "it failed" - #288's
#: verification report refuses to run a test suite on the way out of a
#: stop.
STOP_EXIT_CODE = 130

# How many NON-REPORTING agent calls the RUN must accumulate before an
# enabled ceiling is declared unenforceable (see LoopBudget.halt_reason).
# Applied PER CEILING: tokenless calls condemn the token ceiling,
# costless calls condemn the cost ceiling. A judgment call, not a
# measurement: one silent call is an incident (the claude adapter records
# source="timeout" / "parse-error" with no counts, and a cost-only
# result event is tokenless too), two in one run is a property of the
# adapter. The cost of the extra call is one iteration of overshoot on
# an adapter that reports nothing; the cost of setting it to 1 is
# killing a capped run over a single timed-out first iteration.
#
# Counted across the RUN'S ENGINEER LOOPS (prior_calls/prior_token_calls
# carry the parent's engineer-only figures down), because a per-loop
# counter resets on every attempt and every component: with
# max_iterations = 1, or a retry that dies after one call, no single loop
# ever reached the threshold and the rule was decorative (review finding
# P1-a).
#
# Engineer-scoped, NOT run-wide across roles: a timed-out architect or
# reviewer call is tokenless too, and counting those let two unrelated
# timeouts condemn an engineer adapter that had been reporting fine -
# while the halt message asserted the cap could "never trip". The
# question this threshold asks is whether the ENGINEER's adapter reports
# tokens, so only engineer calls are evidence about it.
UNENFORCEABLE_CALLS = 2
_UNENFORCEABLE_CALLS = UNENFORCEABLE_CALLS  # back-compat alias


@dataclass(frozen=True)
class BudgetHalt:
    """Why a run-level ceiling stopped the loop, carried structurally.

    R8 review (#180): the halt used to travel as a prose string only, so
    every consumer downstream had to re-derive WHICH ceiling and WHICH
    condition from either the sentence or the parent's own totals. Both
    re-derivations were wrong in ways that put false numbers in the audit
    trail - the parent picked a dead ceiling over a breached one, and the
    "N >= cap" wording was emitted for unenforceable halts where nothing
    was ever exceeded.

    The two facts are orthogonal and are now kept apart:

    ``condition`` - ``"breached"`` (a total reached a ceiling) or
    ``"unenforceable"`` (a configured ceiling provably cannot fire).
    Only ``"breached"`` licenses a comparison sentence.

    ``ceilings`` - the config key(s) involved. Exactly one when breached;
    one or more when unenforceable, because the loop halts on that
    condition only once EVERY configured ceiling is dead.
    """

    condition: str
    ceilings: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class LoopBudget:
    """Run-level spend ceilings pushed DOWN into the engineer loop (R8).

    Two independent ceilings, either or both configurable:
    ``[factory] max_total_tokens`` and ``[factory] max_cost_usd``.

    They are NOT interchangeable, and the reason the cost one exists is
    measured, not hypothetical. A real run halted on
    ``max_total_tokens = 500000`` at 1,864,081 total tokens of which
    1,781,669 (95.6%) were CACHE READS. Cache reads cost roughly an order
    of magnitude less than fresh input tokens, so the operator who set a
    500k "budget" expecting a spend ceiling was stopped at $1.22.
    ``UsageTotals.total_tokens`` counts cache reads at par, which makes
    it a real measurement of something that is nearly uncorrelated with
    money. ``max_cost_usd`` is the ceiling an operator usually means.

    Both ceilings were, until R8, consulted only at parent-process phase
    boundaries (pipeline.process_result, the review / security / distill
    gates, the scheduling gate). Those checks can stop the NEXT phase;
    they can never stop the iteration already running, so a single
    component could spend for ``max_iterations x agent_iteration``
    seconds before a ceiling was consulted once.

    This object is the loop-side half. It is evaluated BETWEEN
    iterations, which is the tightest bound available without a
    streaming usage feed from the adapter.

    WHAT IT GUARANTEES: the engineer loop starts no NEW iteration once
    the run's reported spend has reached a configured ceiling. Overshoot
    is bounded by the work already in flight - roughly one iteration per
    running worker - not by zero.

    WHAT STAYS UNBOUNDED (identical for BOTH ceilings - the cost ceiling
    is not a hard cap and must not be described as one):
    - A single runaway iteration. Nothing here interrupts an agent call
      mid-flight; only ``[timeout] agent_iteration`` bounds that, and it
      bounds wall clock, not tokens and not dollars. Measured: the run
      cited above overshot its entire 500k cap by 3.7x inside ONE
      engineer call of 376s.
    - Concurrent siblings. ``prior_total_tokens`` / ``prior_cost_usd``
      are the run totals as of THIS worker's launch; spend by workers
      running in parallel is invisible until they report back to the
      parent. With ``max_parallel = N`` the overshoot scales with N.
    - Unreported spend. Every figure here is a CLI self-report; see
      :meth:`halt_reason` for how unknown usage is treated.
    - Loops that COMPLETE. The completion return fires before this check
      is evaluated, so an adapter that finishes on its first silent call
      never halts itself - the ordinary success path for a custom
      ``agent_cmd``. That bypass is closed in the PARENT by
      ``ComponentPipeline.budget_unenforceable`` at the scheduling gate,
      which stops the run handing out new work; this object cannot see
      it, and pretending otherwise is how it went unnoticed.

    ``[agent] budget_usd`` is a different thing and must not be confused
    with ``max_cost_usd``: it is adapter-internal, enforced inside a
    single turn by the claude-sdk adapter only, and says nothing about
    the run.
    """

    # Copies of the parent's FactoryConfig ceilings and the run usage
    # accumulated before this worker launched. Plain ints/floats so the
    # whole object pickles cleanly to a pool worker.
    max_total_tokens: int = 0
    prior_total_tokens: int = 0
    #: Reporting calls (token OR cost) the run had made before this
    #: worker launched. Provenance only - it says whether the run's
    #: prior total is a real figure or an artefact of a silent adapter -
    #: and NOT part of any decision; see :meth:`halt_reason`.
    prior_known_calls: int = 0
    #: ENGINEER-loop calls the run had made before this worker launched,
    #: and how many reported a TOKEN figure. Their difference is the
    #: engineer's tokenless-call count, which is what the unenforceable
    #: rule counts (R8 review P1-a: a per-loop counter resets on every
    #: attempt/component, so short loops never reached the threshold).
    #: Engineer-scoped on purpose - another role's timeout is not
    #: evidence about the engineer's adapter.
    prior_calls: int = 0
    prior_token_calls: int = 0
    #: The cost ceiling and its priors. Separate from the token ones
    #: because the two axes have separate coverage: an adapter can report
    #: cost without tokens (claude with a missing ``usage`` dict) or
    #: tokens without cost (codex), so each ceiling has to judge its own
    #: enforceability from its own evidence.
    max_cost_usd: float = 0.0
    prior_cost_usd: float = 0.0
    prior_cost_calls: int = 0

    @property
    def token_enabled(self) -> bool:
        return self.max_total_tokens > 0

    @property
    def cost_enabled(self) -> bool:
        return self.max_cost_usd > 0

    @property
    def enabled(self) -> bool:
        """True when at least one ceiling is configured."""
        return self.token_enabled or self.cost_enabled

    def halt_verdict(self, loop_usage: UsageTotals) -> BudgetHalt | None:
        """Why this loop must stop now, or None to keep iterating.

        Two conditions, both computed from CLI self-reported usage, and
        both evaluated PER CEILING:

        1. OVERRUN - a configured ceiling's run total (spend before this
           worker launched plus what this loop has reported) has reached
           it. Checked for every configured ceiling; whichever is over
           halts. With both set, whichever is reached first in time wins,
           because the check runs between every iteration; when both are
           over at the same evaluation the token one is named first, an
           arbitrary but fixed order.

        2. UNENFORCEABLE - a ceiling provably cannot trip. Two facts have
           to hold together, and they are deliberately measured over
           different scopes:

           (a) REPORTING EVIDENCE, judged on THIS loop: not one of this
               loop's calls reported the figure that ceiling needs
               (``loop_usage.token_calls == 0`` for the token ceiling,
               ``loop_usage.cost_calls == 0`` for the cost one). That is
               what makes a ceiling dead rather than merely slow, because
               the priors are frozen at this worker's launch: if this
               loop never reports the figure, the run total stays fixed
               below that ceiling forever no matter how long the engineer
               runs.

               Judged on the loop ALONE, never on the run. Summing the
               run's reported calls in looked stricter but was weaker: a
               reporting architect (or a previous component) suppressed
               the halt for a silent engineer and handed back the
               decorative cap in exactly the configuration where it is
               easiest to hit - ``[agent] command`` sets a custom
               engineer command while the adversarial roles keep a
               reporting adapter.

               Reads ``token_calls`` / ``cost_calls``, never
               ``known_calls``: a record carrying only ``cost_usd`` sets
               ``known_calls`` while contributing nothing to
               ``total_tokens``, so ``known_calls`` reported perfect
               coverage for a token cap that could never advance (review
               finding P1-b). The converse is just as real, which is why
               the two axes are now tracked separately.

           (b) CALL THRESHOLD, counted across the run's ENGINEER loops:
               the engineer has now made ``_UNENFORCEABLE_CALLS`` calls
               that did not report that figure (``prior_calls -
               prior_token_calls`` / ``prior_calls - prior_cost_calls``
               plus this loop's own). This is the "have we seen enough to
               conclude?" half, and it must not reset per attempt or per
               component - with ``max_iterations = 1``, or a retry that
               dies after one call, a per-loop counter never reached 2
               and the rule was decorative (review finding P1-a).

               Non-reporting calls, not all calls: a call that DID report
               is evidence the adapter works, so counting it toward "this
               adapter never reports" would be backwards and would make a
               single unparseable result fatal in an otherwise healthy
               run.

           PER-CEILING, and the loop halts as unenforceable only when
           EVERY CONFIGURED ceiling is dead. An adapter that reports cost
           but not tokens can still enforce ``max_cost_usd`` while
           ``max_total_tokens`` is beyond saving, so halting on the dead
           token ceiling alone would throw away a ceiling that still
           works. If any configured ceiling can still fire, keep going.

        CONSEQUENCE, stated plainly: in a run whose engineer calls have
        all reported, a lone unparseable engineer call does not halt -
        the non-reporting count is 1. A second such ENGINEER call does,
        for whichever ceilings it left without evidence. Other roles'
        timeouts never contribute. That is the deliberate trade: two
        independent silent calls in one run is adapter behavior, not an
        incident, and the failure is loud, recorded, and recoverable
        (raise or clear the ceiling), whereas the alternative is
        unbounded spend under a ceiling that cannot fire.

        A loop that has made no calls at all can never halt here: with
        ``loop_usage.calls == 0`` there is no evidence either way, so a
        worker launched into a run with a high ``prior_calls`` must still
        get to run its engineer.

        An iteration that reports nothing WHILE other calls in the same
        loop do report counts as zero: the running total stays a lower
        bound (see ``UsageTotals.unreported_calls``) that still grows
        toward the ceiling, so the halt arrives late rather than never.
        Charging unknown iterations a guessed amount would invent numbers
        the CLI never gave us.
        """
        if not self.enabled:
            return None

        # OVERRUN, per ceiling. Fixed order (token, then cost) only
        # decides which is NAMED when both are over at the same
        # evaluation; over time whichever is reached first halts.
        total_tokens = self.prior_total_tokens + loop_usage.total_tokens
        if self.token_enabled and total_tokens >= self.max_total_tokens:
            return BudgetHalt(
                "breached",
                ("max_total_tokens",),
                (
                    f"token budget exceeded: {total_tokens} total tokens "
                    f"recorded >= max_total_tokens ({self.max_total_tokens}); "
                    "halting the engineer loop instead of starting another "
                    "iteration (R8)"
                ),
            )
        total_cost = self.prior_cost_usd + loop_usage.cost_usd
        if self.cost_enabled and total_cost >= self.max_cost_usd:
            return BudgetHalt(
                "breached",
                ("max_cost_usd",),
                (
                    f"cost budget exceeded: ${total_cost:.6f} recorded >= "
                    f"max_cost_usd (${self.max_cost_usd}); halting the engineer "
                    "loop instead of starting another iteration (R8)"
                ),
            )

        # UNENFORCEABLE, per ceiling. Only halts when every configured
        # ceiling is dead - a live one is worth continuing under.
        if loop_usage.calls == 0:
            return None
        clauses: list[str] = []
        dead: list[str] = []
        if self.token_enabled:
            prior_tokenless = max(0, self.prior_calls - self.prior_token_calls)
            run_tokenless = prior_tokenless + loop_usage.tokenless_calls
            if loop_usage.token_calls == 0 and run_tokenless >= _UNENFORCEABLE_CALLS:
                clauses.append(
                    f"token budget unenforceable: none of this loop's "
                    f"{loop_usage.calls} agent call(s) reported a token "
                    f"count, and the engineer has now made {run_tokenless} "
                    f"tokenless call(s) this run. Spend before this worker "
                    f"launched is frozen at {self.prior_total_tokens}, so "
                    f"max_total_tokens ({self.max_total_tokens}) cannot "
                    f"advance from this loop"
                )
                dead.append("max_total_tokens")
            else:
                return None
        if self.cost_enabled:
            prior_costless = max(0, self.prior_calls - self.prior_cost_calls)
            run_costless = prior_costless + loop_usage.costless_calls
            if loop_usage.cost_calls == 0 and run_costless >= _UNENFORCEABLE_CALLS:
                clauses.append(
                    f"cost budget unenforceable: none of this loop's "
                    f"{loop_usage.calls} agent call(s) reported a cost, and "
                    f"the engineer has now made {run_costless} costless "
                    f"call(s) this run. Spend before this worker launched is "
                    f"frozen at ${self.prior_cost_usd:.6f}, so max_cost_usd "
                    f"(${self.max_cost_usd}) cannot advance from this loop"
                )
                dead.append("max_cost_usd")
            else:
                return None
        if not clauses:
            return None
        return BudgetHalt(
            "unenforceable",
            tuple(dead),
            "; ".join(clauses)
            + ("; halting rather than spending under a cap that cannot fire (R8)"),
        )

    def halt_reason(self, loop_usage: UsageTotals) -> str | None:
        """The prose half of :meth:`halt_verdict`, or None.

        Kept because the sentence is what the operator reads; callers
        that need to ACT on the halt want the verdict instead, so the
        condition and the ceiling identities do not have to be recovered
        by parsing this string."""
        verdict = self.halt_verdict(loop_usage)
        return None if verdict is None else verdict.reason


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


def build_project_context(
    cwd: Path,
    ui: UI,
    verify_config: VerifyConfig | None = None,
) -> str:
    """Assemble the project-context prefix of the engineer prompt.

    Two sections: the project's CLAUDE.md, if it has one, and the
    verification commands the mechanical gate will run.

    #261: the commands come from ``verify.resolve_verify_commands``, the
    same resolver the gate itself calls, against the same directory the
    gate will run in. There is no second copy for the agent to read, so
    it cannot be told a command the gate will not run.

    ``verify_config`` is the config Phase 1 will run with, and ``None``
    means NO mechanical gate runs for this invocation, so no commands are
    stated. None is the default on purpose: `ks understand` and
    `ks feature` call ``run_loop`` directly and run no verification at
    all, so a default that assumed a gate told a read-only mapping run to
    execute the whole test suite on every pass. Only a caller that can
    name the gate it will run gets to make the claim, and the factory
    passes the exact object ``pipeline._phase_verify`` reads.
    """
    commands = resolve_verify_commands(verify_config, cwd) if verify_config is not None else None

    sections: list[str] = []
    claude_md_path = cwd / "CLAUDE.md"
    if claude_md_path.exists():
        claude_md = claude_md_path.read_text(encoding="utf-8")
        if commands is not None:
            # A CLAUDE.md scaffolded before #261 still carries verification
            # bullets that disagree with the gate. Drop the divergent ones
            # from the prompt copy (never from disk) and say so.
            scrubbed = scrub_project_claude_md(cwd, commands)
            if scrubbed is not None:
                for divergence in scrubbed.divergences:
                    ui.warn(divergence)
                claude_md = scrubbed.text
        sections.append("# Project Context (from CLAUDE.md)\n\n" + claude_md)

    if commands is not None:
        sections.append(commands.format_for_prompt())
    return "\n\n".join(sections)


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
    allowed_paths = ", ".join(config.allowed_paths) if config.allowed_paths else "<disabled>"
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
        if breaker_config.no_progress_iterations > 0
        else "<disabled>",
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
        raw_prompt = config.prompt_file.read_text(encoding="utf-8")
    else:
        from kstrl.init_cmd import DEFAULT_PROMPT

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

    project_context = build_project_context(cwd, ui, verify_config)
    if project_context:
        prompt = project_context + "\n\n---\n\n" + prompt

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
    guard_baseline: git.WorkspaceBaseline | None = None
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
        guard_baseline = git.capture_workspace_baseline(
            cwd,
            base_ref=guard_base_ref,
        )
        if guard_baseline.dirty:
            ui.info(
                f"Guard baseline: HEAD {guard_baseline.head or '<unborn>'}, "
                f"{len(guard_baseline.dirty)} pre-existing uncommitted "
                "file(s) excluded from enforcement"
            )
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
        ui.section(f"Iteration {iteration} / {config.max_iterations}")
        iter_start = time.monotonic()
        if bus is not None:
            bus.emit(
                IterationStarted(
                    iteration=iteration,
                    max_iterations=config.max_iterations,
                )
            )

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
                    line.strip() == COMPLETION_MARKER for line in final_message.splitlines()
                )
        finally:
            iter_duration = time.monotonic() - iter_start
            iteration_durations.append(iter_duration)
            if bus is not None:
                bus.emit(
                    IterationCompleted(
                        iteration=iteration,
                        duration_seconds=round(iter_duration, 2),
                        completed=completion_seen,
                        timed_out=iteration_timed_out,
                    )
                )

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
            ok, violations = guards.enforce_allowed_paths(
                config,
                ui,
                cwd,
                interaction=channel,
                ignored_paths=guard_ignored,
                baseline=guard_baseline,
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
                    guard_violations=tuple(violations),
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

        # R8 run-level budget: the ONLY in-loop enforcement point for
        # [factory] max_total_tokens and [factory] max_cost_usd. Checked
        # here - after the completion return, before the breaker's stall
        # probe - so a blown ceiling never pays for another agent call or
        # another breaker test command. This bounds overshoot to the work
        # already in flight (about one iteration per running worker); it
        # cannot interrupt a call mid-flight. See LoopBudget.
        if budget is not None:
            halt = budget.halt_verdict(loop_usage)
            if halt is not None:
                ui.err(halt.reason)
                return LoopResult(
                    completed=False,
                    iterations=iteration,
                    exit_code=1,
                    duration_seconds=time.monotonic() - loop_start,
                    iteration_durations=iteration_durations,
                    timed_out_iterations=timed_out_iterations,
                    usage=loop_usage,
                    budget_halt_reason=halt.reason,
                    budget_halt_condition=halt.condition,
                    budget_halt_ceilings=halt.ceilings,
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
            response = channel.request(
                PromptRequest(
                    kind=PromptKind.ITERATION,
                    header="Iteration complete. What next?",
                    options=("Continue", "Skip interactive", "Quit"),
                    default=0,
                )
            )
            if response.answered and response.choice == 1:
                # Disable interactive for remaining iterations
                config.interactive = False
            elif response.answered and response.choice == 2:
                return LoopResult(
                    completed=False,
                    iterations=iteration,
                    exit_code=0,
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
