"""Per-component pipeline: the factory's component state machine (R7.3).

Extracted from ``factory.run_factory``'s ``_handle_result`` closure so the
state machine is unit-testable in isolation. The pipeline owns one
component attempt's journey through the phase chain:

    engineer result -> verify -> diff -> review -> security
        -> knowledge distillation (PRE-PR, a named step: the distiller
           reads the component's true delta before the merge pulls main
           into the worktree)
        -> HITL checkpoint -> PR create+merge -> COMPLETED

and every transition out of it:

    RETRYING       retries remain; component back to PENDING with context
    FAILED         retries exhausted, budget wall, HITL reject, PR failure
                   (dependents cascade-skip)
    MERGE_PENDING  PR merge initiated but unconfirmed; re-polled next run
    AWAITING_APPROVAL
                   every gate passed and the merge gate had nobody to ask;
                   nothing pushed, dependents wait (#465). The next run
                   merges it once `ks inbox approve` has, or fails it once
                   `ks inbox reject` has (apply_merge_decisions).
    COMPLETED      merge confirmed (or PR flow not configured)

Each phase returns an explicit typed result; ``process_result`` is the
single place that routes a phase failure into a transition. LLM- and
subprocess-heavy phase functions are injected via ``PipelineHooks`` (the
factory resolves them from its own module globals at run start, so the
historical ``patch("kstrl.factory.run_review")`` seam keeps working),
and ``kstrl.git.git`` functions are looked up on the module at call time
for the same reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import event_catalog
from kstrl.context import IterationContext, IterationRecord
from kstrl.git import git
from kstrl.manifest import (
    Component,
    ComponentStatus,
)
from kstrl.pipeline_checks import (
    MechanicalChecks,
)
from kstrl.pipeline_delivery import Delivery, PipelineOutcome
from kstrl.pipeline_knowledge import KnowledgePhase
from kstrl.pipeline_review import ReviewPhase
from kstrl.pipeline_security import SecurityGate
from kstrl.waivers import (
    ApprovalSnapshot,
    approvals_on,
    overrides_on,
)
from kstrl.worktree_sweep import sweep_findings

if TYPE_CHECKING:
    from kstrl.factory_result import ComponentResult


class ComponentPipeline(Delivery, KnowledgePhase, SecurityGate, ReviewPhase, MechanicalChecks):
    """Drives one component result through the phase chain and owns every
    component state transition (R7.3).

    The five mutable structures this pipeline shares with the factory's
    scheduler arrive in one :class:`kstrl.runstate.RunState` and are
    exposed here as read-only properties, so no reader of
    ``pipeline.worktree_paths`` and friends changed on #193. The
    ownership rule, the per-operation writer split and the measured
    consequence of copying any of them instead of aliasing it are in
    ``kstrl/runstate.py``'s module docstring, which is where the rule
    now lives: this class is only one of the two writers.
    """

    # ------------------------------------------------------------------
    # Budget + usage accounting
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Attempt lifecycle + evidence pointers (R3.3)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Transitions (the single place component state moves)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Phase chain
    # ------------------------------------------------------------------

    def process_result(
        self,
        comp_id: str,
        comp_result: ComponentResult,
    ) -> PipelineOutcome | None:
        """Process one component result through the phase chain.

        Returns the typed outcome (None when the component id is unknown).
        Every side effect - manifest saves, progress events, notify hooks,
        retries - happens here or in the transition methods this routes
        into; the scheduler only launches workers and hands results in.
        """
        comp = self.manifest.get_component(comp_id)
        if comp is None:
            return None

        # The iteration count only: _end_attempt stamps duration_seconds (#450).
        comp.iteration_count = comp_result.iterations

        # Engineer bracket closer: PhaseStarted(engineer) was emitted by
        # the scheduler at submit time; the worker's exit lands here.
        self.bus.emit(
            event_catalog.PhaseCompleted(
                component=comp_id,
                phase="engineer",
                passed=comp_result.success,
                detail=comp_result.error or "",
                duration_seconds=round(comp_result.duration_seconds, 2),
            )
        )

        # R3.1: engineer-loop spend counts BEFORE the success branch -
        # failed attempts cost real tokens too.
        if comp_result.usage is not None:
            self._record_usage(comp_id, "engineer", comp_result.usage)
        # #461: what the attempt left running in its worktree.
        self._add_findings(comp, sweep_findings(comp_result.worktree_sweep, "engineer"))

        # R3.1 budget checkpoint: the engineer loop just reported the
        # dominant spend; halt before starting adversarial phases (or a
        # retry) when the run-level cap is blown.
        #
        # R8: the loop can also halt ITSELF between iterations, which is
        # the only enforcement that happens while the spend is being
        # incurred. Both routes land here, so the audit trail (typed
        # finding, BudgetExceeded event, single budget_overrun inbox
        # item, no duplicate halted_run) is identical either way. The
        # worker's reason is used only when the parent's own totals do
        # not show a breach - the unreportable-usage case, where the
        # derived "N >= cap" sentence would be false.
        if comp_result.budget_exceeded or self.budget_exceeded():
            reason = "" if self.budget_exceeded() else (comp_result.error or "")
            return PipelineOutcome(
                transition=self.fail_for_budget(
                    comp,
                    "engineer",
                    reason,
                    # The loop's own verdict when IT halted; empty when
                    # the parent's totals are what tripped, in which
                    # case fail_for_budget derives the identity under
                    # the single precedence rule.
                    condition=comp_result.budget_halt_condition,
                    ceilings=comp_result.budget_halt_ceilings,
                ),
            )

        if not comp_result.success:
            # R7.5: the no-progress circuit breaker is a direct FAILED
            # transition, never a retry - a fresh attempt would re-run
            # the same prompt against the same base state, which is the
            # exact spend the breaker exists to stop. Loud and distinct:
            # its own progress-log event plus a structured failure
            # signature for the evolution journal.
            if comp_result.no_progress:
                error = comp_result.error or "no-progress circuit breaker tripped"
                self.bus.emit(
                    event_catalog.CircuitBreakerTripped(
                        component=comp_id,
                        iterations=comp_result.iterations,
                        error=error,
                    )
                )
                return PipelineOutcome(
                    transition=self.fail(
                        comp,
                        error,
                        phase="engineer",
                        check="no_progress_breaker",
                        signatures=["engineer:no-progress-stall"],
                    ),
                )
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_iteration(
                IterationRecord(
                    iteration=comp_result.iterations,
                    success=False,
                    attempt=comp.retries + 1,
                    error=comp_result.error,
                )
            )
            if comp_result.guard_violations:
                # The in-loop guard runs the SAME check as Phase 1's
                # diff_scope, only earlier, so it must produce the same
                # audit record - otherwise moving the catch forward
                # silently changes the shape of the journal, and a
                # scope failure caught in-loop would leave no
                # verification_result behind at all.
                #
                # Exactly one check is reported. Listing the others
                # would claim tests and typecheck ran when the loop
                # halted before they could.
                detail = comp_result.error or "files outside allowed scope"
                self.bus.emit(
                    event_catalog.VerificationResultEvent(
                        component=comp.id,
                        passed=False,
                        checks=("diff_scope",),
                        failures=(detail,),
                        duration_seconds=0.0,
                    )
                )
                # Same "<check>: FAIL" token as
                # VerificationResult.as_context(), and the same retry
                # reason Phase 1 uses: consumers keying on either keep
                # working no matter which layer caught the violation. A
                # second wording for one failure is how a mislabel
                # becomes a divergence (R7.1 / #179).
                # R10.2: the engineer rank, not verification. The
                # failure text deliberately matches Phase 1's diff_scope
                # token (above), but the RANK answers a different
                # question: did Phase 1 produce a reading this attempt?
                # It did not, the guard fired inside the engineer loop
                # first. Ranking this as verification retired an earlier
                # attempt's real Phase 1 failure as re-measured.
                ctx.add_engineer_failure(
                    f"diff_scope: FAIL - {detail}",
                    attempt=comp.retries + 1,
                )
                return PipelineOutcome(
                    transition=self.retry_or_fail(
                        comp,
                        "Mechanical verification failed",
                        ctx.to_json(),
                        phase="verify",
                        check="diff_scope",
                    ),
                )
            # R10.2: without an entry this attempt would carry no
            # dated evidence, and the renderer would present an older
            # attempt's finding as the current one. The guard branch
            # above already records its own entry, so only the plain
            # loop failure needs this.
            error = comp_result.error or "Unknown error"
            ctx.add_engineer_failure(error, attempt=comp.retries + 1)
            return PipelineOutcome(
                transition=self.retry_or_fail(
                    comp,
                    error,
                    ctx.to_json(),
                    phase="engineer",
                    check="loop",
                ),
            )

        return self._judge_attempt(comp, comp_result)

    def engineer_worktree(self, comp: Component, wt_path: Path | None) -> Path | None:
        """The worktree an engineer runs in, or None when no engineer runs.

        None when provisioning failed (``wt_path`` is None), and when ``ks
        retry`` kept the approved head (#646): that commit is judged here by
        :meth:`rejudge_kept_head`, and the component has already transitioned.
        """
        if wt_path is None or not comp.rejudge_sha:
            return wt_path
        self.rejudge_kept_head(comp, wt_path)
        return None

    def rejudge_kept_head(self, comp: Component, wt_path: Path) -> PipelineOutcome:
        """Judge the head ``ks retry`` kept, with no engineer attempt (#646).

        ``ks retry`` keeps a failed branch when an approved policy_exception
        item was taken on its tip, and records that commit
        in ``comp.rejudge_sha``. It is read once and cleared here, so only
        this attempt skips the engineer: a retry this attempt buys runs the
        engineer on the kept commits, and its edit is a new diff, asked
        again. ``process_result`` is not called, because it closes an
        engineer phase that never started; the gates get a result that
        carries only the context they read.
        """
        from kstrl.factory_result import ComponentResult

        kept, comp.rejudge_sha = comp.rejudge_sha, ""
        head = git.get_head_sha(wt_path) or ""
        if head != kept:
            return PipelineOutcome(
                transition=self.fail(
                    comp,
                    f"ks retry kept commit {kept[:12]} for a re-judge, and the worktree "
                    f"{wt_path} is at {head[:12] or 'no commit'}; nothing was judged. Run "
                    f"ks retry {comp.id} again",
                    phase="provisioning",
                    check="worktree_setup",
                )
            )
        approvals = self._approvals or ApprovalSnapshot()
        ids = approvals_on(approvals, comp.id, kept) + overrides_on(approvals, comp.id, kept)
        named = ", ".join(i[:8] for i in ids) or "none"
        reason = (
            f"ks retry kept commit {kept[:12]}, which inbox approval {named} was taken on; "
            "Phase 1 judges it again"
        )
        self.ui.info(f"  Engineer SKIPPED for {comp.id}: {reason}")
        self._record_phase_skip(comp, "engineer", reason)
        context = self.component_contexts.get(comp.id)
        return self._judge_attempt(
            comp, ComponentResult(component_id=comp.id, success=True, context_json=context)
        )

    def _judge_attempt(self, comp: Component, comp_result: ComponentResult) -> PipelineOutcome:
        """Every gate after the engineer, on the commits in the component's worktree."""
        comp_id = comp.id
        wt_path = self.worktree_paths.get(comp_id, self.root_dir)

        # PHASE 1: Mechanical verification
        comp.status = ComponentStatus.VERIFYING.value
        self.manifest.save(self.manifest_path)

        t0 = self._phase_started(comp, "verify")
        verify = self._phase_verify(comp, comp_result, wt_path)
        self._phase_completed(
            comp,
            "verify",
            t0,
            verify.failure is None,
            verify.failure.error if verify.failure else "",
        )
        if verify.failure is not None:
            # No diff_text: the diff phase has not run, but the change
            # IS committed in the worktree - it is what verification
            # just inspected - so the diff is fetched here rather than
            # writing off every test/lint/typecheck failure as
            # unmeasurable.
            self.record_fact_utilization(comp, wt_path)
            return PipelineOutcome(
                transition=self._route_failure(comp, verify.failure),
                verify=verify,
            )

        # #700 slice 6: the operator's acceptance checks gate the head.
        acceptance = self._phase_acceptance(comp, comp_result, wt_path)
        if acceptance is not None:
            self.record_fact_utilization(comp, wt_path)
            return PipelineOutcome(
                transition=self._route_failure(comp, acceptance),
                verify=verify,
            )

        t0 = self._phase_started(comp, "diff")
        diff = self._phase_diff(comp, comp_result, wt_path)
        self._phase_completed(
            comp,
            "diff",
            t0,
            diff.failure is None,
            diff.failure.error if diff.failure else "",
        )
        if diff.failure is not None:
            self.record_fact_utilization(
                comp,
                wt_path,
                "",
                unavailable="diff unavailable",
            )
            return PipelineOutcome(
                transition=self._route_failure(comp, diff.failure),
                verify=verify,
                diff=diff,
            )

        # #191: measure fact utilization the moment a diff exists, so
        # the sample is every component that produced one - not only
        # those that survive the review and security gates below.
        self.record_fact_utilization(comp, wt_path, diff.diff)

        # PHASE 2: Second-opinion review
        t0 = self._phase_started(comp, "review")
        review = self._phase_review(
            comp,
            comp_result,
            wt_path,
            verify.verification,
        )
        self._phase_completed(
            comp,
            "review",
            t0,
            review.failure is None,
            review.failure.error if review.failure else "",
        )
        # Before the failure return on purpose: a review that ran and
        # failed produced a reading too (#247).
        self._note_phase_reading(comp, "review", review.produced_a_reading)
        if review.failure is not None:
            return PipelineOutcome(
                transition=self._route_failure(comp, review.failure),
                verify=verify,
                diff=diff,
                review=review,
            )

        # PHASE 2.5: Security review
        t0 = self._phase_started(comp, "security")
        security = self._phase_security(
            comp,
            comp_result,
            wt_path,
        )
        self._phase_completed(
            comp,
            "security",
            t0,
            security.failure is None,
            security.failure.error if security.failure else "",
        )
        self._note_phase_reading(comp, "security", security.produced_a_reading)
        if security.failure is not None:
            return PipelineOutcome(
                transition=self._route_failure(comp, security.failure),
                verify=verify,
                diff=diff,
                review=review,
                security=security,
            )

        # Knowledge distillation: a NAMED PRE-PR step (R7.3 decision).
        t0 = self._phase_started(comp, "distill")
        distill = self._phase_distill(comp, comp_result, wt_path, diff.diff)
        self._phase_completed(
            comp,
            "distill",
            t0,
            True,
            distill.skip_reason or "",
        )

        return self._deliver(
            comp,
            comp_result,
            verify=verify,
            diff=diff,
            review=review,
            security=security,
            distill=distill,
        )
