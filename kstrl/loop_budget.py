"""Run-level spend ceilings for the engineer loop."""

from __future__ import annotations

from dataclasses import dataclass

from kstrl.agents.base import UsageTotals

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
               the engineer has now made ``UNENFORCEABLE_CALLS`` calls
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
            if loop_usage.token_calls == 0 and run_tokenless >= UNENFORCEABLE_CALLS:
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
            if loop_usage.cost_calls == 0 and run_costless >= UNENFORCEABLE_CALLS:
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
