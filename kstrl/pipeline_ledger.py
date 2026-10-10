"""The usage meter and the run ceilings of one pipeline.

``UsageLedger`` holds the usage meter, the run ceilings and their coverage,
the adversarial-call budget, and the takeover of the spend of an
interrupted run (#463).
"""

from __future__ import annotations

from collections.abc import Sequence

from kstrl import event_catalog
from kstrl import events as ev
from kstrl.agents.base import (
    ARCHITECT_COMPONENT,
    ARCHITECT_ROLE,
    CEILING_AXES,
    DESIGNER_ROLE,
    INTEGRATION_COMPONENT,
    INTEGRATION_ROLE,
    CeilingCoverage,
    UsageTotals,
    usage_coverage,
)
from kstrl.loop import UNENFORCEABLE_CALLS
from kstrl.manifest import Component, ComponentStatus
from kstrl.pipeline_state import PipelineState

#: How a retry this run carried from an interrupted run is marked (#463).
CARRIED_REASON_PREFIX = "carried from run "


def _carried_reason(prior_run_id: str, reason: str) -> str:
    """``reason`` marked as carried from ``prior_run_id``, once (#463)."""
    if reason.startswith(CARRIED_REASON_PREFIX):
        return reason
    return f"{CARRIED_REASON_PREFIX}{prior_run_id}: {reason}"


def _usage_by_component_phase(
    events: Sequence[event_catalog.Event],
) -> dict[tuple[str, str], UsageTotals]:
    """Every ``component_usage`` in ``events``, summed per (component, phase) (#463)."""
    spent: dict[tuple[str, str], UsageTotals] = {}
    for event in events:
        if not isinstance(event, event_catalog.ComponentUsage):
            continue
        spent.setdefault((event.component, event.phase), UsageTotals()).merge(
            UsageTotals(
                calls=event.calls,
                known_calls=event.known_calls,
                token_calls=event.token_calls,
                cost_calls=event.cost_calls,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cache_read_tokens=event.cache_read_tokens,
                cache_creation_tokens=event.cache_creation_tokens,
                total_tokens=event.total_tokens,
                cost_usd=event.cost_usd,
                duration_seconds=event.duration_seconds,
            )
        )
    return spent


def _take_over_attempts(components: Sequence[Component], *, carrying: bool) -> dict[str, range]:
    """Set the first attempt each component answers for in this run, and
    return the earlier attempts this run takes over, per component (#463).

    Only a PENDING component of a run that stopped before its summary
    (``carrying``) is taken over: it keeps ``first_attempt`` and this run
    owes attempts ``first_attempt..retries``. Every other component starts
    at ``retries + 1``, including one the killed run left MERGE_PENDING or
    AWAITING_APPROVAL, whose earlier attempts this run does not write again.
    """
    owed = {
        comp.id: range(comp.first_attempt, comp.retries + 1)
        for comp in components
        if carrying and comp.status == ComponentStatus.PENDING.value
    }
    for comp in components:
        if comp.id not in owed:
            comp.first_attempt = comp.retries + 1
    return owed


class UsageLedger(PipelineState):
    """The usage meter, the ceilings and the budget of one run."""

    def adversarial_budget_ok(self) -> bool:
        cap = self.factory_config.max_adversarial_calls
        if cap <= 0:
            return True
        return self._adversarial_calls < cap

    def adversarial_budget_consume(self) -> None:
        self._adversarial_calls += 1

    def adversarial_budget_remaining(self) -> int | None:
        """Calls left in the budget, or None when unbounded."""
        cap = self.factory_config.max_adversarial_calls
        if cap <= 0:
            return None
        return max(0, cap - self._adversarial_calls)

    def _record_usage(
        self,
        comp_id: str,
        phase: str,
        totals: UsageTotals,
    ) -> None:
        if totals.calls == 0:
            return
        slot = self.usage_meter.setdefault(comp_id, {}).setdefault(
            phase,
            UsageTotals(),
        )
        slot.merge(totals)
        self.run_usage.merge(totals)
        self.bus.emit(
            event_catalog.ComponentUsage(
                component=comp_id,
                phase=phase,
                **totals.to_dict(),
            )
        )
        self._announce_coverage_gaps()

    def _announce_coverage_gaps(self) -> None:
        """Report the FIRST time a configured ceiling stops covering
        every metered call (R8, measured).

        Evaluated on every usage capture rather than at the halt,
        because a coverage fact delivered with the halt arrives after
        the money is spent. The earliest honest moment is the phase that
        first reports nothing on that axis - typically the first
        cross-family review, a few minutes into a run.

        Adds no failure mode to ``_record_usage``: the work is integer
        arithmetic over the meter plus one ``bus.emit``, and
        ``EventBus.emit`` already isolates sink exceptions. The meter
        must never gate correctness (R3.1 requirement 4).
        """
        for ceiling in CEILING_AXES:
            if ceiling in self._coverage_announced:
                continue
            coverage = self.ceiling_coverage(ceiling)
            if coverage is None or coverage.calls == 0 or coverage.complete:
                continue
            self._coverage_announced.add(ceiling)
            detail = coverage.note()
            self.ui.warn(f"  BUDGET COVERAGE: {detail}")
            self.bus.emit(
                event_catalog.BudgetCoverage(
                    ceiling=coverage.ceiling,
                    axis=coverage.axis,
                    calls=coverage.calls,
                    covered_calls=coverage.covered_calls,
                    uncovered_calls=coverage.uncovered_calls,
                    uncovered_tokens=coverage.uncovered_tokens,
                    uncovered_roles=coverage.uncovered_roles,
                    detail=detail,
                )
            )

    def record_engineer_usage(
        self,
        comp_id: str,
        totals: UsageTotals,
    ) -> None:
        """Record engineer spend that never came back through
        process_result (R8 abort path). The worker was killed, so its
        records reach the meter here or not at all; the caller
        guarantees the matching future produced no result, so this can
        never double count with the normal path."""
        self._record_usage(comp_id, "engineer", totals)

    def record_architect_usage(self, totals: UsageTotals | None) -> None:
        """Fold the architect's spend into this run before it starts (#257).

        Every other role is metered by a phase this pipeline drives. The
        architect is not: `ks factory` decomposes the spec in the command
        itself, before any run id or run directory exists, and only then
        builds this pipeline. Its spend therefore has to be handed in
        rather than captured, which is what ``architect_usage`` on
        ``run_factory`` carries.

        It goes through the ordinary ``_record_usage`` path, and that is
        the entire point of the seat. ``run_usage`` is what
        :meth:`cost_budget_exceeded` reads, so an operator's
        ``--max-cost-usd`` now bounds the architect too instead of
        bounding the four roles that follow it; the meter gains a fifth
        row; and ``_announce_coverage_gaps`` counts the architect as a
        metered call rather than leaving it invisible to the coverage
        accounting.

        The component id is ``ARCHITECT_COMPONENT``, the namespaced key
        `ks decompose` already writes and the one
        ``serve.read_run_spend`` reads; the phase is the bare
        ``ARCHITECT_ROLE``. Pairing them HERE is why the constants exist
        rather than a literal per surface.

        They stopped being the same string in #281. A bare component key
        shared a keyspace with LLM-emitted component ids, so a component
        genuinely named `architect` merged with this row - folding its
        spend into the architect's, and clearing the honesty flag on
        ``serve.RunSpend`` for a run whose architect never reported.

        ``None`` or zero calls records nothing: a run resumed from a
        manifest never ran an architect, and an agent that reported no
        usage must not become a phantom row claiming it cost nothing.
        """
        if totals is None:
            return
        self._record_usage(ARCHITECT_COMPONENT, ARCHITECT_ROLE, totals)

    def record_designer_usage(self, comp_id: str, totals: UsageTotals) -> None:
        """Meter the verification designer (#700 slice 7) under its own role
        row of the component it designed checks for."""
        self._record_usage(comp_id, DESIGNER_ROLE, totals)

    def record_integration_usage(self, totals: UsageTotals) -> None:
        """Meter the integration review (#482) under its own role row.

        Through ``_record_usage`` like every role, so the run total, the
        ceilings and the coverage accounting count it. Zero calls record
        nothing, as for the architect.
        """
        self._record_usage(INTEGRATION_COMPONENT, INTEGRATION_ROLE, totals)

    def carry_interrupted_run(self) -> None:
        """Take over what an interrupted run recorded (#463).

        The run the manifest names, when ``completed_at`` is empty, stopped
        before its summary: it was killed, or its process died, so it wrote
        no journal result, no experiments.tsv row, and its spend is in no
        run total. Its retries are already on the manifest. This run records
        the rest under its own id, so every per-run surface counts what the
        manifest counts:

        - every ``component_usage`` in that run's stream enters this run's
          meter, so the run total, the cost ceiling, the journal and
          experiments.tsv include it;
        - for each component this run will run again (PENDING after the
          crash-recovery reset), that run's ``component_retrying`` events and
          ``findings_superseded`` journal rows are written again under this
          run's id, so progress.jsonl and the #233 reading see every attempt
          the manifest counts.

        A chain of interrupted runs carries through, because each resume
        writes what it took over under its own id and the next resume reads
        it from there. Must run before the manifest is saved with this
        run's id.
        """
        prior = self.manifest.run_id
        carrying = bool(prior) and not self.manifest.completed_at
        owed = _take_over_attempts(self.manifest.components, carrying=carrying)
        if not carrying:
            return
        from kstrl.evolution import EvolutionJournal
        from kstrl.reducer import read_run_dir

        events = read_run_dir(ev.RunPaths.for_run(self.root_dir, prior).root)
        spent = _usage_by_component_phase(events)
        for (comp_id, phase), totals in spent.items():
            self._record_usage(comp_id, phase, totals)
        retried = [
            e
            for e in events
            if isinstance(e, event_catalog.ComponentRetrying)
            and e.attempt in owed.get(e.component, range(0))
        ]
        for event in retried:
            self.bus.emit(
                event_catalog.ComponentRetrying(
                    component=event.component,
                    attempt=event.attempt,
                    reason=_carried_reason(prior, event.reason),
                )
            )
        readings = 0
        journal = EvolutionJournal.open(self.root_dir, warn=self.ui.warn)
        if journal is not None:
            try:
                readings = journal.carry_superseded(prior, self.run_id, owed)
            except OSError as exc:
                self.ui.warn(f"  Evolution journal write failed (non-fatal): {exc}")
        self.ui.info(
            f"  Carried from interrupted run {prior}: {len(retried)} retried attempt(s), "
            f"{readings} attempt reading(s), "
            f"{sum(t.calls for t in spent.values())} call(s) costing "
            f"${sum(t.cost_usd for t in spent.values()):.4f}"
        )

    def mark_usage_salvage_safe(self, comp_id: str) -> None:
        """The attempt's usage snapshot slot is provably clean."""
        self._usage_salvage_unsafe.discard(comp_id)

    def mark_usage_salvage_unsafe(self, comp_id: str) -> None:
        """A stale snapshot may survive for this attempt, so disk
        salvage must not run for it (R8 review: deletion IS the
        attempt-scoping invariant; when it fails, what is on disk may
        already have been counted by ``process_result``)."""
        self._usage_salvage_unsafe.add(comp_id)

    def usage_salvage_is_safe(self, comp_id: str) -> bool:
        return comp_id not in self._usage_salvage_unsafe

    def engineer_usage_totals(self) -> UsageTotals:
        """Engineer-loop spend across every component and attempt.

        Feeds the loop-side budget's tokenless-call threshold (R8). That
        threshold asks "does the ENGINEER's adapter report tokens?", so
        it must not be answered with run-wide totals: a timed-out
        architect or reviewer call is tokenless too, and counting those
        let two unrelated timeouts condemn an engineer adapter that had
        been reporting perfectly well - while the halt message asserted
        the cap "can never trip on this adapter". Engineer-scoped, the
        counter still survives the case it exists for
        (``max_iterations = 1`` and retries, where a per-loop counter
        resets before it can conclude anything).

        The OVERRUN half stays run-wide: that one asks what the RUN has
        spent against the cap, which is every phase's business.
        """
        totals = UsageTotals()
        for phases in self.usage_meter.values():
            engineer = phases.get("engineer")
            if engineer is not None:
                totals.merge(engineer)
        return totals

    def usage_totals_for(self, comp_id: str) -> UsageTotals:
        """One component's spend across all phases (PR A: shown at the
        E6 checkpoint so the human sees what the attempt cost)."""
        totals = UsageTotals()
        for phase_totals in self.usage_meter.get(comp_id, {}).values():
            totals.merge(phase_totals)
        return totals

    def token_budget_exceeded(self) -> bool:
        cap = self.factory_config.max_total_tokens
        return cap > 0 and self.run_usage.total_tokens >= cap

    def cost_budget_exceeded(self) -> bool:
        """R8: the run's reported USD spend has reached ``max_cost_usd``.

        Separate from :meth:`token_budget_exceeded` because the two
        ceilings measure genuinely different things. Measured on a real
        run: 1,864,081 total tokens (95.6% of them cache reads, which
        ``total_tokens`` counts at par) cost $1.22, so a token ceiling is
        a poor proxy for spend.
        """
        cap = self.factory_config.max_cost_usd
        return cap > 0 and self.run_usage.cost_usd >= cap

    def breached_ceiling(self) -> str | None:
        """Which configured ceiling the run has reached, or None.

        Returns the config key name (``"max_total_tokens"`` /
        ``"max_cost_usd"``) so every audit surface can NAME the ceiling
        that tripped instead of asserting "token budget" for both. When
        both are over at the same evaluation the token one is named
        first - an arbitrary but fixed order; over time whichever is
        reached first halts, because the gates run continuously.
        """
        if self.token_budget_exceeded():
            return "max_total_tokens"
        if self.cost_budget_exceeded():
            return "max_cost_usd"
        return None

    def budget_exceeded(self) -> bool:
        """Any configured run-level ceiling has been reached."""
        return self.breached_ceiling() is not None

    def token_budget_unenforceable(self) -> str | None:
        """Why the TOKEN cap can no longer fire at all, or None.

        The parent-side twin of :meth:`LoopBudget.halt_reason`'s
        unenforceable branch, and it exists because the in-loop check has
        a blind spot the loop cannot cover itself: a loop that emits
        COMPLETE returns BEFORE evaluating its budget, so an adapter that
        finishes on its first tokenless call never reaches the halt.
        That is the ordinary success path for a custom ``agent_cmd``, not
        an artificial case - each component completes, the engineer's
        tokenless count climbs, and the cap never fires (review finding
        on 22e99b4; the previous docstring's "the halt lands on the next
        loop" was simply false when the next loop also completes).

        Per-ceiling by construction: a dead TOKEN cap is not on its own a
        reason to stop when a live COST cap is also configured. The
        scheduling gate consults :meth:`budget_unenforceable`, which
        halts only when EVERY configured ceiling is dead.

        Checked at the scheduling gate, so the run stops handing out NEW
        work under a dead cap. Deliberately does not retroactively fail
        components that already completed: their work is valid, and the
        cap's job is to stop spending, not to destroy what was bought.
        Bound: at most the component in flight when the determination
        lands.
        """
        cap = self.factory_config.max_total_tokens
        if cap <= 0:
            return None
        engineer = self.engineer_usage_totals()
        if engineer.token_calls > 0:
            return None
        if engineer.tokenless_calls < UNENFORCEABLE_CALLS:
            return None
        return (
            f"token budget unenforceable: the engineer has made "
            f"{engineer.tokenless_calls} agent call(s) this run and none "
            f"reported a token count, so max_total_tokens ({cap}) cannot "
            "advance; refusing to schedule further components rather than "
            "spending under a cap that cannot fire (R8)"
        )

    def cost_budget_unenforceable(self) -> str | None:
        """Why the COST cap can no longer fire at all, or None.

        The cost mirror of :meth:`token_budget_unenforceable`, reading
        ``cost_calls`` rather than ``token_calls``. The two answers are
        independent in both directions: the codex adapter reports a token
        total and no cost (token ceiling alive, cost ceiling dead), the
        claude adapter can report ``total_cost_usd`` with no ``usage``
        dict (cost ceiling alive, token ceiling dead).
        """
        cap = self.factory_config.max_cost_usd
        if cap <= 0:
            return None
        engineer = self.engineer_usage_totals()
        if engineer.cost_calls > 0:
            return None
        if engineer.costless_calls < UNENFORCEABLE_CALLS:
            return None
        return (
            f"cost budget unenforceable: the engineer has made "
            f"{engineer.costless_calls} agent call(s) this run and none "
            f"reported a cost, so max_cost_usd (${cap}) cannot advance; "
            "refusing to schedule further components rather than spending "
            "under a cap that cannot fire (R8)"
        )

    def ceiling_coverage(self, ceiling: str) -> CeilingCoverage | None:
        """What fraction of the run's metered calls ``ceiling`` counts.

        None when that ceiling is not configured (nothing to qualify) or
        the key is not a ceiling.

        The middle term the R8 ceilings were missing. ``*_budget_exceeded``
        answers "was it breached", ``*_budget_unenforceable`` answers "can
        it ever fire"; both were true-or-false over the WHOLE run, and a
        ceiling that counts some roles and not others is neither. Measured
        on a real run: the engineer reported cost on every call, the
        cross-family reviewer reported tokens and no cost on 5, and the
        run's cost total equalled the engineer's exactly - a $25 ceiling
        that bounded one role while every existing surface reported it as
        healthy.

        Deliberately does NOT change what the ceiling counts. Converting
        the uncovered calls' tokens to dollars would need a price table
        this repo does not have and must not invent (a fabricated cost in
        an audit trail is worse than a missing one). What changes is that
        the gap is now stated instead of implied.
        """
        axis = CEILING_AXES.get(ceiling)
        if axis is None:
            return None
        if ceiling == "max_cost_usd" and self.factory_config.max_cost_usd <= 0:
            return None
        if ceiling == "max_total_tokens" and self.factory_config.max_total_tokens <= 0:
            return None
        return usage_coverage(self.usage_meter, axis=axis, ceiling=ceiling)

    def coverage_notes(self, ceilings: Sequence[str]) -> list[str]:
        """Operator sentences for the named ceilings that fall short.

        Empty when every named ceiling counted every call, so a
        fully-covered halt reads exactly as it did before.
        """
        notes: list[str] = []
        for ceiling in ceilings:
            coverage = self.ceiling_coverage(ceiling)
            if coverage is None:
                continue
            note = coverage.note()
            if note:
                notes.append(note)
        return notes

    def unenforceable_ceilings(self) -> list[str]:
        """Configured ceilings that can no longer fire, by config key.

        The identity half of :meth:`budget_unenforceable`. An
        unenforceable halt crosses no numeric threshold, so
        :meth:`breached_ceiling` correctly returns None for it - and
        every audit surface downstream then rendered the empty string as
        "token budget", naming a knob that may not even be configured
        (review finding on #180: a cost-only run on a costless adapter
        reported "run token budget exceeded (200/0)"). The ceiling that
        FAILED still has a name even when nothing was breached.
        """
        dead: list[str] = []
        if (
            self.factory_config.max_total_tokens > 0
            and self.token_budget_unenforceable() is not None
        ):
            dead.append("max_total_tokens")
        if self.factory_config.max_cost_usd > 0 and self.cost_budget_unenforceable() is not None:
            dead.append("max_cost_usd")
        return dead

    def budget_halt_identity(self) -> tuple[str, tuple[str, ...]]:
        """``(condition, ceilings)`` for a halt derived from run totals.

        The precedence rule lives HERE, once, because both call sites got
        it wrong when they each spelled it out: a numeric breach and a
        dead ceiling can coexist (a run whose token cap trips while its
        cost cap never received a figure), and joining the dead list
        first named ``max_cost_usd`` for a halt the TOKEN cap caused -
        which then rendered as ``cost budget exceeded: $0 >= $100``, a
        sentence that is both false and arithmetically impossible
        (review finding on #180).

        A breach is the stronger fact: it is a threshold that was
        actually crossed, with numbers behind it. Dead ceilings are only
        consulted when nothing was breached.
        """
        breached = self.breached_ceiling()
        if breached is not None:
            return ("breached", (breached,))
        dead = self.unenforceable_ceilings()
        if dead:
            return ("unenforceable", tuple(dead))
        return ("", ())

    def budget_unenforceable(self) -> str | None:
        """Why NO configured ceiling can fire any more, or None.

        The scheduling gate's question. Halts only when EVERY configured
        ceiling is dead: an adapter that reports cost but not tokens can
        still enforce ``max_cost_usd``, and stopping the run because the
        token ceiling died would discard a ceiling that still works.
        With no ceiling configured there is nothing to enforce and this
        is always None.
        """
        reasons = [
            reason
            for reason in (
                self.token_budget_unenforceable(),
                self.cost_budget_unenforceable(),
            )
            if reason is not None
        ]
        configured = sum(
            (
                self.factory_config.max_total_tokens > 0,
                self.factory_config.max_cost_usd > 0,
            )
        )
        if not reasons or len(reasons) < configured:
            return None
        return "; ".join(reasons)
