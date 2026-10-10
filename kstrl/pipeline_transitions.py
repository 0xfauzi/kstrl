"""The transitions of one component: retry, fail and complete.

``Transitions`` holds the component transitions retry, fail and complete, the
budget failure, the backstop failure, and the two writers of the retry
context.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from kstrl import event_catalog, git
from kstrl.agents.base import CEILING_AXES
from kstrl.context import ACCEPTANCE_PHASE, IterationContext
from kstrl.divergence import convergence_message, failure_counts_not_converging
from kstrl.findings import Finding
from kstrl.inbox import ItemKind
from kstrl.manifest import Component, ComponentStatus
from kstrl.pipeline_attempt import AttemptRecorder
from kstrl.pipeline_inbox import InboxDesk
from kstrl.pipeline_ledger import UsageLedger
from kstrl.pipeline_state import _iso_now


class Transition(Enum):
    """Terminal disposition of one ``process_result`` pass."""

    RETRYING = "retrying"
    FAILED = "failed"
    MERGE_PENDING = "merge_pending"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"


class FailureAction(Enum):
    """How a phase failure must be routed (the single transition point)."""

    # Normal gate failure: retry with context, or FAILED once exhausted.
    RETRY_OR_FAIL = "retry_or_fail"
    # A wall retrying can never fix (the adversarial budget only
    # shrinks): fail directly without burning engineer iterations.
    FAIL = "fail"
    # R3.1/R8 run-level ceiling (max_total_tokens or max_cost_usd): fail
    # loudly via the budget path (synthetic finding + budget_exceeded
    # event), never silently degrade. The member name is unchanged
    # vocabulary; the ceiling that tripped travels in the message.
    TOKEN_BUDGET = "token_budget"


@dataclass(frozen=True)
class PhaseFailure:
    """A phase's terminal signal: what fired and how to transition."""

    action: FailureAction
    error: str
    phase: str
    check: str = ""
    context_json: str | None = None
    signatures: list[str] | None = None
    #: #233: how many failures the gate reported, for ``[factory]
    #: convergence_attempts``. None when the failure is not a gate's
    #: count (an engineer-loop failure, a crashed reviewer), which breaks
    #: the run of readings rather than counting as a reading.
    failure_count: int | None = None


class Transitions(InboxDesk, AttemptRecorder, UsageLedger):
    """The transitions that move a component to RETRYING, FAILED or COMPLETED."""

    def _cascade_skip(self, failed_id: str) -> list[str]:
        """Skip every transitive dependent of ``failed_id`` and say so on
        the run's own stream (#448/#457).

        ``manifest.cascade_skip`` only marks the manifest. Before this,
        the run wrote no event for a dependent it skipped, so the board
        (which folds only this run's events) showed it as pending while
        ``ks status --no-tui`` (which reads the manifest) said skipped.
        This is the one place in ``kstrl/`` that may call
        ``manifest.cascade_skip`` directly; a census in
        ``tests/test_cascade_skip_events.py`` requires that every other
        caller route through here instead of the manifest method.
        """
        skipped = self.manifest.cascade_skip(failed_id)
        self.factory_result.skipped.extend(skipped)
        for sid in skipped:
            self.bus.emit(
                event_catalog.ComponentSkipped(
                    component=sid, reason=f"dependency '{failed_id}' failed"
                )
            )
        return skipped

    def record_contract_failure(self, comp_id: str, attempt: int, test_output: str) -> None:
        """Add a contract-test failure to a component's retry context.

        Called by the factory's contract loop, which owns the rest of
        that reset (the manifest save, the completed-list removal, the
        signature). Only the CONTEXT write lives here, and it lives here
        because the retry context has exactly two writers - this one and
        ``retry_or_fail`` - and both must merge the phase readings or
        the contract path keeps the defect the retry path just lost: a
        contract failure in attempt N would re-raise a review finding
        the reviewer cleared in attempt N.

        ``attempt`` is the attempt whose contract test failed. The
        caller has already incremented ``retries``, so it passes that
        value directly rather than ``retries + 1``; the pipeline sites
        use ``+ 1`` because there the increment happens inside
        ``retry_or_fail`` AFTER the entry is recorded.
        """
        ctx = IterationContext.from_json(self.component_contexts.get(comp_id, "{}"))
        ctx.add_contract_failure(test_output, attempt=attempt)
        self._merge_phase_readings(comp_id, ctx)
        self.component_contexts[comp_id] = ctx.to_json()
        # #233: the attempt passed every gate before its contract test
        # failed, so it has no gate count, and the run of readings starts
        # again, as it does after any attempt that produced no count.
        self.failure_counts.pop(comp_id, None)

    def retry_or_fail(
        self,
        comp: Component,
        error: str,
        context_json: str | None,
        phase: str = "",
        check: str = "",
        signatures: list[str] | None = None,
        fresh_base: bool = False,
        failure_count: int | None = None,
    ) -> Transition:
        """Retry a component or mark it as failed. ``phase``/``check``
        name the gate that fired (R3.3); on a retry they describe the
        superseded attempt until the next attempt clears them.
        ``signatures`` are the structured failure signatures (R6.1).
        ``fresh_base=True`` (R7.5 merge-conflict doctrine) forces the
        retry to recreate the worktree AND branch from the freshly
        merged base instead of resuming the attempt's commits.
        ``failure_count`` is the gate's count for #233's convergence
        check; a retry it would buy is refused when the count has not
        fallen for ``[factory] convergence_attempts`` attempts."""
        self._record_failure_signatures(comp, phase, error, signatures)
        counts = self._record_failure_count(comp.id, phase, failure_count)
        if comp.retries < self.factory_config.max_retries:
            if failure_counts_not_converging(counts, self.factory_config.convergence_attempts):
                return self._fail_not_converging(comp, counts)
            if fresh_base and self.factory_config.use_worktrees:
                self.fresh_base_retry_ids.add(comp.id)
                error = (
                    error + " [conflict retry: component re-run against the "
                    "freshly merged base; agent output is not rebased]"
                )
            # A timeout failure means the agent was killed mid-flight: the
            # worktree/branch state cannot be trusted. Note the hygiene
            # behavior in the error string so the audit trail explains why
            # the retry does not resume from the killed attempt's commits.
            elif "timeout" in error.lower() and self.factory_config.use_worktrees:
                self.fresh_base_retry_ids.add(comp.id)
                error = (
                    error + " [timeout retry: worktree recreated from base; "
                    "stale index.lock removed]"
                )
            # R3.3: journal this attempt's findings as superseded BEFORE
            # the retry counter moves (the tag and the journal entry
            # must agree on the attempt number), then stamp the
            # attempt's evidence pointers.
            self.journal_superseded_findings(comp, failure_count)
            self._end_attempt(comp)
            comp.failed_phase = phase
            comp.failed_check = check
            comp.retries += 1
            comp.status = ComponentStatus.PENDING.value
            comp.error = error
            if context_json:
                # The chokepoint: every failing gate that carries a
                # context routes here, so this is the one place the
                # attempt's phase readings have to be merged in (#247).
                ctx = IterationContext.from_json(context_json)
                self._merge_phase_readings(comp.id, ctx)
                self.component_contexts[comp.id] = ctx.to_json()
            self.bus.emit(
                event_catalog.ComponentRetrying(
                    component=comp.id,
                    attempt=comp.retries,
                    reason=error,
                )
            )
            self.ui.info(
                f"  Retrying '{comp.id}' "
                f"(attempt {comp.retries}/{self.factory_config.max_retries}): "
                f"{error[:80]}"
            )
            time.sleep(self.factory_config.retry_delay)
            self.manifest.save(self.manifest_path)
            return Transition.RETRYING
        return self.fail(
            comp,
            error,
            phase=phase,
            check=check,
            signatures=signatures,
        )

    def _record_failure_count(self, comp_id: str, phase: str, count: int | None) -> list[int]:
        """Add this attempt's gate failure count to the component's run of
        readings and return the run's counts, oldest first (#233).

        The run starts again when the attempt produced no count, and when
        it failed in a different phase from the attempt before: two gates'
        counts measure different things, and reaching a later gate is
        progress the counts cannot show.
        """
        history = self.failure_counts.setdefault(comp_id, [])
        if count is None or (history and history[-1][0] != phase):
            history.clear()
        if count is not None:
            history.append((phase, count))
        return [reading for _, reading in history]

    def _fail_not_converging(self, comp: Component, counts: list[int]) -> Transition:
        """#233: end the component instead of buying a retry that the
        failure count says is not converging."""
        attempts = self.factory_config.convergence_attempts
        message = convergence_message(counts, attempts)
        self._add_findings(comp, [Finding.not_converging(message)])
        return self.fail(
            comp,
            message,
            phase="engineer",
            check="convergence",
            signatures=["engineer:divergence"],
        )

    def fail_aborted(self, comp_id: str, reason: str) -> None:
        """PR B: a shutdown aborted this component's in-flight attempt.
        Recorded as a plain FAILED with phase="aborted" so a resume can
        retry it; distinct from every organic failure signature."""
        comp = self.manifest.get_component(comp_id)
        if comp is None:
            return
        self.fail(
            comp,
            f"aborted: {reason}",
            phase="aborted",
            check="shutdown",
            signatures=["aborted:shutdown"],
        )

    def fail(
        self,
        comp: Component,
        error: str,
        phase: str = "",
        check: str = "",
        signatures: list[str] | None = None,
    ) -> Transition:
        """Mark a component FAILED with no retry. Direct callers are
        conditions a retry can never fix (the adversarial budget only
        shrinks, so re-running the engineer would burn LLM calls to hit
        the same wall); retry_or_fail routes here once retries are
        exhausted."""
        self._record_failure_signatures(comp, phase, error, signatures)
        comp.status = ComponentStatus.FAILED.value
        comp.error = error
        comp.completed_at = _iso_now()
        comp.failed_phase = phase
        comp.failed_check = check
        self._end_attempt(comp)
        self._cascade_skip(comp.id)
        self.factory_result.failed.append(comp.id)
        self.bus.emit(event_catalog.ComponentFailed(component=comp.id, error=error))
        self.notify.fire_first_failure(comp.id, error)
        if comp.id in self._inbox_typed:
            self._inbox_typed.discard(comp.id)
        else:
            evidence: dict[str, Any] = {"phase": phase, "check": check, "error": error}
            if phase == ACCEPTANCE_PHASE:
                # #700 decision 14: an approval of this halt covers these
                # checks on this commit only (waivers.covering_override).
                evidence["head_sha"] = git.branch_sha(comp.branch_name, self.root_dir) or ""
            self._inbox_add(
                ItemKind.HALTED_RUN,
                f"{comp.id} halted in {phase}",
                detail=error,
                component=comp.id,
                dedupe_key=f"halted:{comp.id}:{phase}:{check}",
                evidence=evidence,
            )
        self.ui.err(f"  Failed: {comp.id}: {error[:80]}")
        self.manifest.save(self.manifest_path)
        return Transition.FAILED

    def fail_for_budget(
        self,
        comp: Component,
        phase: str,
        reason: str = "",
        condition: str = "",
        ceilings: tuple[str, ...] = (),
    ) -> Transition:
        """R3.1/R8: halt LOUDLY on a blown run-level ceiling. Mirrors the
        R1.2 synthetic-finding pattern: a typed Finding in the
        stream, a progress-log event, and a FAILED
        component - never a silent degrade. Retrying cannot un-spend what
        was spent, so this fails directly instead of burning retries.

        The message and the finding NAME the ceiling that tripped
        (``max_total_tokens`` or ``max_cost_usd``). With two ceilings a
        message that always said "token budget" would send the operator
        to raise the wrong knob.

        ``reason`` (R8) overrides the derived message when the breach
        was detected somewhere the parent's own totals do not describe -
        specifically the engineer loop's in-loop halt on unreportable
        usage, where ``run_usage`` shows no breach and stating one would
        put a false number in the audit trail. That string is produced by
        :meth:`LoopBudget.halt_reason`, which names its own ceiling.
        Every other side effect is identical wherever the breach is
        caught.

        R8 (measured): the message and the event also state what the
        named ceilings COVER when they do not cover everything. A run
        whose engineer reported cost and whose cross-family reviewer
        reported tokens and no cost halted on a total that equalled the
        engineer's exactly - 193,633 reviewer tokens contributed $0 -
        while the sentence said only "cost budget exceeded". The number
        was true and the sentence was misleading, so the coverage is now
        attached where the ceiling is evaluated."""
        # An identity supplied by the caller wins: the engineer loop
        # detects its own halt against priors the parent's totals do not
        # describe, so only the loop knows what actually fired there.
        # Everything else derives from run totals under the single
        # precedence rule in budget_halt_identity().
        if not ceilings:
            condition, ceilings = self.budget_halt_identity()
        # Only a BREACH licenses a "N >= cap" sentence. An unenforceable
        # halt crossed nothing, and the derived comparison read
        # "token budget exceeded: 0 >= 500" for runs where the totals
        # never moved (review finding on #180).
        if reason:
            error = reason
        elif condition == "unenforceable":
            error = (
                f"budget ceiling unenforceable ({', '.join(ceilings)}): no "
                "configured ceiling can still fire, so the run cannot be "
                "bounded; halting rather than spending under a cap that "
                "cannot trip (R8)"
            )
        elif ceilings == ("max_cost_usd",):
            error = (
                f"cost budget exceeded: ${self.run_usage.cost_usd:.6f} "
                f"recorded >= max_cost_usd "
                f"(${self.factory_config.max_cost_usd}); halting instead of "
                "spending further (R8)"
            )
        else:
            error = (
                f"token budget exceeded: {self.run_usage.total_tokens} total "
                f"tokens recorded >= max_total_tokens "
                f"({self.factory_config.max_total_tokens}); halting instead "
                "of spending further (R3.1)"
            )
        # Coverage rides along with the halt, in the prose AND
        # structurally. Appended rather than folded into each branch so
        # it also qualifies a loop-supplied ``reason``: the loop knows
        # what fired, only the parent knows what the ceiling counted.
        #
        # EVERY CONFIGURED named ceiling is recorded, including ones that
        # covered every call and ones that did not cause this halt. An
        # empty or partial ``coverage`` would otherwise mean several
        # things at once - "no gap", "not the cause", "written before
        # this landed" - and a reader could not tell verified from
        # unknown (the distinction E9 added ``infrastructure_error``
        # for).
        #
        # Iterating CEILING_AXES rather than ``ceilings`` is R8 review
        # finding 3: ``ceilings`` is the CAUSAL identity, so with both
        # caps enabled a token breach yields ``("max_total_tokens",)``
        # and a simultaneously PARTIAL ``max_cost_usd`` was dropped from
        # the halt event and the inbox evidence - the operator deciding
        # which knob to raise never saw that the other cap was counting
        # a subset. ``ceiling_coverage()`` returns None for a ceiling
        # that is not configured, so an absent entry keeps exactly one
        # meaning: that cap was off.
        #
        # The PROSE stays quiet on full coverage, because ``note()``
        # returns "" there: the operator-facing sentence is unchanged
        # wherever there is nothing to disclose.
        coverage = [
            cov for cov in (self.ceiling_coverage(c) for c in CEILING_AXES) if cov is not None
        ]
        notes = [note for note in (cov.note() for cov in coverage) if note]
        if notes:
            error = f"{error} [{'; '.join(notes)}]"
        ceiling = ", ".join(ceilings)
        label = ceiling or "budget"
        self.ui.err(f"  BUDGET EXCEEDED ({label}) for {comp.id}: {error}")
        self._add_findings(
            comp,
            [
                Finding.infrastructure_error(
                    phase=phase,
                    explanation=error,
                )
            ],
        )
        self.bus.emit(
            event_catalog.BudgetExceeded(
                component=comp.id,
                total_tokens=self.run_usage.total_tokens,
                max_total_tokens=self.factory_config.max_total_tokens,
                cost_usd=round(self.run_usage.cost_usd, 6),
                max_cost_usd=self.factory_config.max_cost_usd,
                ceiling=ceiling,
                condition=condition,
                ceilings=ceilings,
                coverage=tuple(cov.to_dict() for cov in coverage),
            )
        )
        # Raised BEFORE delegating to fail(): a blown budget is its own
        # exception kind, and the generic halted_run item fail() adds
        # would bury why the run stopped.
        self._inbox_add(
            ItemKind.BUDGET_OVERRUN,
            f"{comp.id} exceeded the {label} budget",
            detail=error,
            component=comp.id,
            dedupe_key=f"budget:{comp.id}",
            evidence={
                "phase": phase,
                "ceiling": ceiling,
                "condition": condition,
                "total_tokens": self.run_usage.total_tokens,
                "max_total_tokens": self.factory_config.max_total_tokens,
                "cost_usd": round(self.run_usage.cost_usd, 6),
                "max_cost_usd": self.factory_config.max_cost_usd,
                # The operator triaging this item is deciding whether to
                # raise the ceiling; what it counted is part of that
                # decision, not a footnote.
                "coverage": [cov.to_dict() for cov in coverage],
            },
        )
        self._inbox_suppress_generic(comp.id)
        # The check name and failure signature stay "token_budget" for
        # both ceilings: they are the evolution journal's stable
        # vocabulary for "a run-level ceiling stopped this component",
        # and renaming them would orphan every historical record. The
        # ceiling that tripped is carried by the message, the finding,
        # the event and the inbox evidence instead.
        return self.fail(
            comp,
            error,
            phase=phase,
            check="token_budget",
            signatures=["token_budget:exceeded"],
        )

    def complete(self, comp: Component) -> Transition:
        """VERIFYING -> COMPLETED: every gate passed (and the PR merge,
        when configured, was confirmed).

        The event and the console line read ``comp.duration_seconds``
        after ``_end_attempt`` has stamped it, so they carry the value the
        manifest and the journal carry: the whole attempt, not the
        engineer loop (#450). The engineer loop's own duration is on the
        ``phase_completed`` event for the engineer phase."""
        comp.status = ComponentStatus.COMPLETED.value
        comp.error = ""
        self.component_failure_signatures.pop(comp.id, None)
        comp.completed_at = _iso_now()
        self._end_attempt(comp)
        self.factory_result.completed.append(comp.id)
        self.bus.emit(
            event_catalog.ComponentCompleted(
                component=comp.id,
                duration_seconds=comp.duration_seconds,
                iterations=comp.iteration_count,
            )
        )
        self.ui.ok(
            f"  COMPLETED: {comp.id} ({comp.iteration_count} iterations, "
            f"{comp.duration_seconds:.0f}s)"
        )
        self.manifest.save(self.manifest_path)
        # #438: after the save, so an item is never resolved for a
        # completion the manifest does not yet hold.
        self._inbox_resolve_component(comp.id)
        return Transition.COMPLETED

    def fail_scheduler_backstop(
        self,
        comp_id: str,
        backstop_seconds: float,
    ) -> None:
        """RUNNING -> FAILED when the scheduler backstop deadline passes
        (R0.1): the worker hung outside the adapter and loop timeout
        layers. The worker may still be alive, so its worktree is kept
        and pointed at as evidence (R3.3)."""
        timed_out_comp = self.manifest.get_component(comp_id)
        if timed_out_comp is not None:
            timed_out_comp.status = ComponentStatus.FAILED.value
            timed_out_comp.error = "component timeout"
            timed_out_comp.completed_at = _iso_now()
            timed_out_comp.failed_phase = "engineer"
            timed_out_comp.failed_check = "scheduler_backstop"
            self.component_failure_signatures[comp_id] = [
                "engineer:component-timeout",
            ]
            self._end_attempt(timed_out_comp)
            # The worktree stays (leaked worker may own it);
            # point the evidence at it (R3.3).
            if comp_id in self.worktree_paths:
                timed_out_comp.evidence_worktree = str(self.worktree_paths[comp_id])
            self._cascade_skip(comp_id)
            self.factory_result.failed.append(comp_id)
            started = self._attempt_started_monotonic.get(comp_id)
            duration = time.monotonic() - started if started is not None else 0.0
            self.bus.emit(
                event_catalog.PhaseCompleted(
                    component=comp_id,
                    phase="engineer",
                    passed=False,
                    detail="component timeout",
                    duration_seconds=round(duration, 2),
                )
            )
            self.bus.emit(
                event_catalog.ComponentFailed(
                    component=comp_id,
                    error="component timeout",
                )
            )
            self.notify.fire_first_failure(comp_id, "component timeout")
        self.ui.err(
            f"  Failed: {comp_id}: component timeout "
            f"(scheduler backstop after {backstop_seconds:.0f}s)"
        )
        self.ui.warn(
            f"  A worker process for '{comp_id}' may be leaked; its worktree is left in place"
        )
        self._inbox_add(
            ItemKind.HALTED_RUN,
            f"{comp_id} abandoned by the scheduler backstop",
            detail=(
                f"no result after {backstop_seconds}s; the worker may still "
                "be alive, so its worktree was left in place"
            ),
            component=comp_id,
            dedupe_key=f"halted:{comp_id}:backstop",
            evidence={"backstop_seconds": backstop_seconds},
        )
        self.manifest.save(self.manifest_path)

    def _route_failure(
        self,
        comp: Component,
        failure: PhaseFailure,
    ) -> Transition:
        """The single dispatch from a phase's typed failure into a
        component state transition."""
        if failure.action == FailureAction.TOKEN_BUDGET:
            return self.fail_for_budget(comp, failure.phase)
        if failure.action == FailureAction.FAIL:
            return self.fail(
                comp,
                failure.error,
                phase=failure.phase,
                check=failure.check,
                signatures=failure.signatures,
            )
        return self.retry_or_fail(
            comp,
            failure.error,
            failure.context_json,
            phase=failure.phase,
            check=failure.check,
            signatures=failure.signatures,
            failure_count=failure.failure_count,
        )
