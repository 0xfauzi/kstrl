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
and ``kstrl.git`` functions are looked up on the module at call time
for the same reason.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import event_catalog, git
from kstrl.agents.base import (
    collect_usage,
)
from kstrl.agents.prompt_record import recording_prompts
from kstrl.context import IterationContext, IterationRecord
from kstrl.findings import (
    POLICY_CATEGORY_PREFIX,
    Finding,
)
from kstrl.inbox import (
    ItemKind,
)
from kstrl.manifest import (
    Component,
    ComponentStatus,
)
from kstrl.pipeline_checks import (
    MechanicalChecks,
    VerifyPhaseResult,
    _verify_routing,
)
from kstrl.pipeline_delivery import Delivery, PipelineOutcome
from kstrl.pipeline_knowledge import KnowledgePhase
from kstrl.pipeline_merge import PR_CLOSED_CHECK
from kstrl.pipeline_security import SecurityGate
from kstrl.pipeline_state import _iso_now
from kstrl.pipeline_transitions import FailureAction, PhaseFailure
from kstrl.pipeline_verdicts import ReviewPhaseResult
from kstrl.prd import PRD
from kstrl.review import (
    ReviewMode,
    ReviewResult,
)
from kstrl.review_claims import (
    claim_disagreements,
    claim_retry_context,
    revert_unconfirmed_stories,
)
from kstrl.rung import label_of
from kstrl.statedir import pre_run_prd_path
from kstrl.timeout import limit_seconds
from kstrl.verify import (
    LAYER0_NOT_MEASURED,
    VerificationResult,
)
from kstrl.waivers import (
    ApprovalSnapshot,
    approvals_on,
    overrides_on,
)
from kstrl.worktree_sweep import sweep_findings

if TYPE_CHECKING:
    from kstrl.factory import (
        ComponentResult,
    )


class ComponentPipeline(Delivery, KnowledgePhase, SecurityGate, MechanicalChecks):
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

    def repoll_merge_pending(self) -> None:
        """R0.2 crash recovery: MERGE_PENDING is re-pollable, not failed.

        A prior run initiated the merge but could not confirm it; check
        the PR state again before scheduling so confirmed merges unblock
        their dependents (MERGE_PENDING -> COMPLETED, or -> FAILED when
        the PR was closed without merging)."""
        merge_pending_comps = [
            c for c in self.manifest.components if c.status == ComponentStatus.MERGE_PENDING.value
        ]
        if not merge_pending_comps:
            return
        from kstrl.pr import (
            is_gh_available,
            pr_number_from_url,
            wait_for_merge,
        )

        if not self.factory_config.create_prs or not is_gh_available():
            self.ui.warn(
                f"  {len(merge_pending_comps)} component(s) are "
                f"merge-pending but PR polling is unavailable (create_prs "
                f"off or gh missing); their dependents stay blocked"
            )
        else:
            for comp in merge_pending_comps:
                pr_number = comp.pr_number or pr_number_from_url(comp.pr_url)
                if not pr_number:
                    self.ui.warn(f"  Cannot re-poll '{comp.id}': no PR number recorded")
                    continue
                self.ui.info(f"  Re-polling merge state for '{comp.id}' (PR #{pr_number})...")
                merge_state = wait_for_merge(
                    pr_number,
                    self.root_dir,
                    timeout=self.factory_config.merge_timeout,
                )
                if merge_state.state == "merged":
                    git.fetch_base_branch(
                        self.manifest.base_branch,
                        self.root_dir,
                    )
                    self._record_merge(comp, merge_state.merge_sha, merge_state.head_sha)
                    comp.status = ComponentStatus.COMPLETED.value
                    comp.error = ""
                    self.component_failure_signatures.pop(comp.id, None)
                    comp.completed_at = _iso_now()
                    self.factory_result.completed.append(comp.id)
                    self.bus.emit(
                        event_catalog.ComponentCompleted(
                            component=comp.id,
                            duration_seconds=comp.duration_seconds,
                            iterations=comp.iteration_count,
                        )
                    )
                    self.ui.ok(f"  PR #{pr_number} merged; '{comp.id}' completed")
                    # The gate that parked this component is answered by
                    # reality; leaving it open would hold a cap slot
                    # forever and ask a human to decide something already
                    # decided. So is every other item naming it (#438).
                    self._inbox_resolve_component(comp.id, f"PR #{pr_number} merged")
                elif merge_state.state == "closed":
                    comp.status = ComponentStatus.FAILED.value
                    comp.error = f"PR #{pr_number} closed without merge"
                    comp.completed_at = _iso_now()
                    # Not a merge decision any more - it is a halt.
                    self._inbox_resolve(
                        f"merge:{comp.id}",
                        f"PR #{pr_number} closed without merging",
                    )
                    self._inbox_add(
                        ItemKind.HALTED_RUN,
                        f"{comp.id}: PR closed without merging",
                        detail=comp.error,
                        component=comp.id,
                        dedupe_key=f"halted:{comp.id}:pr-closed",
                        evidence={"pr_number": pr_number},
                    )
                    comp.failed_phase = "pr"
                    comp.failed_check = PR_CLOSED_CHECK
                    self.component_failure_signatures[comp.id] = [
                        "pr:closed-without-merge",
                    ]
                    self._cascade_skip(comp.id)
                    self.factory_result.failed.append(comp.id)
                    self.bus.emit(
                        event_catalog.ComponentFailed(
                            component=comp.id,
                            error=comp.error,
                        )
                    )
                    self.notify.fire_first_failure(comp.id, comp.error)
                    self.ui.err(f"  Failed: {comp.id}: {comp.error}")
                else:
                    self.ui.warn(
                        f"  '{comp.id}' still awaiting merge of "
                        f"PR #{pr_number}; dependents stay blocked"
                    )
        self.manifest.save(self.manifest_path)

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
        from kstrl.factory import ComponentResult

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

    def _phase_verify(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
    ) -> VerifyPhaseResult:
        """Phase 1: mechanical verification (tests / typecheck / lint /
        PRD stories / diff scope / bad patterns)."""
        early = self._before_gates(comp, comp_result, wt_path)
        if early is not None:
            return early

        verify_config = self.factory_config.resolved_verify_config()
        self.ui.info(f"  Phase 1: mechanical verification for {comp.id}...")
        verify_start = time.monotonic()
        # #269: the run's plan-time snapshot, and no file read. Phase 1
        # used to load the component PRD from the WORKTREE and take
        # `allowedPaths` off it, while the in-loop guard deliberately
        # read the copy at root_dir - so the two guards could enforce
        # different allowlists on the same component in the same run,
        # and Phase 1's was the one the agent could edit. Both now read
        # RunScope, resolved once before the first engineer call.
        scope = self.run_scope.for_component(comp.id)
        # #646: the change this attempt is judged on, read once. An
        # approval covers only the diff it was taken on, and an item filed
        # below records the same pair.
        change = self._judged_change(comp, wt_path)
        # #192: the same rule as ``run_scope`` one line up, for the four
        # config values below. They came off disk per component until
        # the run's envelope was resolved once and injected, so a
        # mid-run edit to kstrl.toml changed what a later component was
        # held to without changing the hash the manifest records. The
        # level is the CLAMPED one the factory resolved, not the raw
        # stored level this used to read.
        verification = self.hooks.run_mechanical_verification(
            wt_path,
            wt_path / comp.prd_path,
            self.component_base(comp.id),
            scope.allowed_paths,
            verify_config,
            allowed_paths_error=scope.error,
            harness_paths=scope.harness_paths,
            # The copy the run started with, for the defence in depth
            # the snapshot does NOT provide: the stories and criteria
            # Phase 1 still has to read from the live file
            # (#269). Outside every worktree, so not agent-writable.
            pre_run_prd_path=pre_run_prd_path(
                self.root_dir, comp.id, comp.prd_path, plan_id=comp.plan_id
            ),
            policy_config=self.run_envelope.policy,
            autonomy_level=self.run_envelope.autonomy_level,
            # #595: the approvals snapshotted when the run started.
            waivers=self._waivers_for(comp, change[1]),
        )
        verify_duration = time.monotonic() - verify_start
        comp.verification_passed = verification.passed
        # R8.1: mechanical checks that produce typed findings (today the
        # policy envelope) get them into the component's finding stream, so
        # a machine-made gate decision reaches the audit trail - PR body,
        # journal, evolution - and not just the retry context. Recorded for
        # passing checks too: a non-blocking advisory is still evidence.
        check_findings = [finding for check in verification.checks for finding in check.findings]
        if check_findings:
            self._add_findings(comp, check_findings)
            # R8.3: an envelope breach is the archetypal exception - a
            # machine decision a human may want to approve once, or
            # convert into a widened policy. Advisories stay out: they
            # are recorded, not blocking, and the inbox is for decisions.
            for finding in check_findings:
                if (
                    finding.category.startswith(POLICY_CATEGORY_PREFIX)
                    and finding.severity != "advisory"
                ):
                    # #595 B2: the waiver_key is part of the dedupe key, not
                    # just category, so a same-category repeat with
                    # different evidence opens a second item instead of
                    # overwriting the one the operator is about to read.
                    # #646: so is the diff_sha, because the same text on a
                    # different change is a change the operator never saw.
                    evidence = self._waivable_evidence(comp, finding, change)
                    self._inbox_add(
                        ItemKind.POLICY_EXCEPTION,
                        f"{comp.id}: {finding.category}",
                        detail=finding.explanation,
                        component=comp.id,
                        dedupe_key=(
                            f"policy:{comp.id}:{finding.category}:{evidence['waiver_key']}:"
                            f"{evidence['diff_sha']}"
                        ),
                        evidence=evidence,
                    )
        # #696 decision 7: where Layer 0 ran, the pull request says nothing
        # measured it, through R1.2's phase-skip callout (pr.py). Only on a
        # pass, which is the only verification a pull request is built
        # from: on a failure the event's not_measured carries the gap, and a
        # finding would turn a failure with no evidence into one serve
        # reports as judged on its merits (serve._merits_outcome).
        if verification.passed and LAYER0_NOT_MEASURED in verification.not_measured:
            self._record_phase_skip(comp, "adequacy", LAYER0_NOT_MEASURED.detail)
        self.bus.emit(
            event_catalog.VerificationResultEvent(
                component=comp.id,
                passed=verification.passed,
                checks=tuple(c.name for c in verification.checks),
                failures=tuple(c.message for c in verification.checks if not c.passed),
                duration_seconds=round(verify_duration, 2),
                not_measured=tuple(g.as_token() for g in verification.not_measured),
                gate_logs=self._write_gate_logs(comp, verification),
                isolation=label_of(verify_config.rung),
            )
        )

        self._warn_not_measured(comp, verification)

        if not verification.passed:
            failing = [c for c in verification.checks if not c.passed]
            self.ui.warn(f"  Phase 1 FAILED for {comp.id}: {', '.join(c.name for c in failing)}")
            ctx = IterationContext.from_json(
                comp_result.context_json or "{}",
            )
            ctx.add_verification_failure(
                verification.as_context(),
                attempt=comp.retries + 1,
            )
            # R6.1: one signature per failed check (#696 decision 5)
            # into the journal instead of the flattened string.
            from kstrl.evolution import signatures_from_verification

            action, error = _verify_routing(failing)
            return VerifyPhaseResult(
                ran=True,
                verification=verification,
                failure=PhaseFailure(
                    action=action,
                    error=error,
                    phase="verify",
                    check=", ".join(c.name for c in failing),
                    context_json=ctx.to_json(),
                    signatures=signatures_from_verification(
                        verification.checks,
                    ),
                    failure_count=verification.failure_count,
                ),
            )

        self.ui.ok(f"  Phase 1 passed for {comp.id}")
        return VerifyPhaseResult(ran=True, verification=verification)

    def _phase_review(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
        verification: VerificationResult,
    ) -> ReviewPhaseResult:
        """Phase 2: second-opinion review against the PRD."""
        review_mode = ReviewMode(self.factory_config.review_mode)
        review_skip_reason: str | None = None
        # R10.3: "the operator turned the reviewer off" and "the
        # reviewer ran out of budget" are both SKIP, and the claim
        # gate has to tell them apart. The first is a choice, warned
        # about at startup and then honoured. The second is the reviewer
        # failing to run, which in blocking mode must not be spent as a
        # confirmation.
        budget_downgraded = False
        if review_mode == ReviewMode.SKIP:
            review_skip_reason = "review disabled (mode=skip)"
        elif not self.adversarial_budget_ok():
            # R10.5 (#226): hard mode refuses to merge unreviewed. The
            # reviewer is the check doing most of the catching, so an
            # exhausted budget must not shed it and let the component
            # through on mechanical checks alone. Nothing is skipped
            # and no event is invented for it (doctrine 6): the
            # Finding and the PhaseFailure are the record.
            #
            # The mode split is INSIDE the budget check, not a second
            # condition beside it, so the check stays closed over
            # ReviewMode and a mode added later cannot fall past both
            # branches and out of the budget check entirely. It keys on
            # ADVISORY rather than on HARD so that the arm it closes
            # ONTO is the refusal: keyed the other way, a mode added
            # later would merge unreviewed by default, which is the
            # fail-open direction and the exact outcome #226 exists to
            # remove. SKIP returned above, so the two arms reachable
            # today are exactly HARD and ADVISORY and this is the same
            # behaviour ``== HARD`` had, with the suite green either way.
            # Phase 2.5 has the same shape for the same reason.
            if review_mode != ReviewMode.ADVISORY:
                comp.review_passed = False
                return ReviewPhaseResult(
                    ran=False,
                    failure=self._budget_refusal(
                        comp,
                        phase="review",
                        banner="Phase 2",
                        role="Review",
                    ),
                )
            # Advisory downgrades to a recorded skip instead (R1.2
            # trace). That is not the same as "advisory cannot fail the
            # component": under claim_agreement = "block" the R10.3
            # gate in _review_did_not_run fails it a few lines below,
            # because a reviewer that never ran cannot confirm a story
            # the engineer marked passes=true.
            self.ui.warn(
                f"  Phase 2 SKIPPED for {comp.id}: "
                f"adversarial LLM budget "
                f"({self.factory_config.max_adversarial_calls}) exhausted"
            )
            review_skip_reason = (
                f"adversarial LLM budget ({self.factory_config.max_adversarial_calls}) exhausted"
            )
            review_mode = ReviewMode.SKIP
            budget_downgraded = True
        if review_mode == ReviewMode.SKIP:
            return self._review_did_not_run(
                comp,
                wt_path,
                review_skip_reason,
                budget_downgraded,
            )

        from kstrl.agents import get_agent

        self.adversarial_budget_consume()
        self.ui.info(f"  Phase 2: review ({review_mode.value}) for {comp.id}...")

        # Forensic home for full raw reviewer output on parse failures
        # (R1.2; mirrors knowledge.py's _debug/<run_id>/ layout).
        adversarial_debug_dir = self._debug_dir_for(comp.id)

        # R1.2: wrap the agent-driven work like Phase 2.5 does. A
        # reviewer crash degrades to a per-component infrastructure
        # failure; it must never abort the whole factory run.
        review_agent: Any = None
        try:
            # R7.1: the run-level selection (explicit config, or the
            # cross-family default, or the warned same-family
            # fallback) decides who reviews.
            review_agent = get_agent(
                self.review_selection.agent_cmd,
                self.review_selection.model,
                self.review_selection.reasoning,
                self.review_selection.agent_type,
                # #266: the reviewer reads the tree it is judging, so it
                # must not be able to write it. Not an operator knob:
                # measured, an unsandboxed reviewer left __pycache__/
                # behind in the worktree it was judging. The operator's
                # OS-level intent rides ALONGSIDE it - the two are
                # layered, not alternatives.
                #
                # One carve-out, and it is not silent: a custom
                # ``agent_cmd`` resolves to CustomAgent, which has no
                # generic sandbox surface and drops BOTH settings.
                # run_factory refuses to start under [sandbox] (#701) and
                # warns per reviewer role otherwise, rather than letting an
                # operator believe in a boundary that is not there.
                sandbox=self.sandbox_config,
                read_only=True,
                root_dir=self.root_dir,
            )
            with (
                self._phase_transcript(comp.id, "review") as on_line,
                recording_prompts(self._agent_call(comp, "review")),
            ):
                review_result = self.hooks.run_review(
                    review_agent,
                    wt_path / comp.prd_path,
                    wt_path,
                    self.component_base(comp.id),
                    verification,
                    review_mode,
                    self.ui,
                    timeout=limit_seconds(self.factory_config.review_timeout_seconds),
                    debug_dir=adversarial_debug_dir,
                    on_line=on_line,
                )
        except Exception as exc:  # noqa: BLE001
            self.ui.warn(f"  Review crashed: {exc}")
            review_result = ReviewResult(
                passed=review_mode != ReviewMode.HARD,
                mode=review_mode.value,
                overall_notes=f"Review agent crashed: {exc}",
                infrastructure_error=True,
                # R7.1: a crash before/inside the run is still
                # attributed to the selected reviewer identity.
                reviewer_model=self.review_selection.identity,
            )
        # R3.1: the instance is fresh per phase, so its accumulated
        # records are exactly this review's spend. Recorded before
        # pass/fail handling so a failed or crashed review still
        # counts.
        if review_agent is not None:
            self._record_usage(comp.id, "review", collect_usage(review_agent))
        breached = self.breached_ceiling()
        if breached is not None:
            return ReviewPhaseResult(
                ran=True,
                result=review_result,
                failure=PhaseFailure(
                    action=FailureAction.TOKEN_BUDGET,
                    error=f"budget exceeded ({breached})",
                    phase="review",
                ),
            )
        comp.review_passed = review_result.passed
        # E3: typed findings are the source of truth; the rendered
        # string is a derived view kept for backward-compat consumers.
        self._add_findings(comp, review_result.as_findings())
        comp.review_findings = review_result.as_pr_body_section()
        # #450: the two properties the "Review ..." log line prints and
        # the divergence reading's blocking_count reads. Criteria and
        # concerns together, one per finding row recorded just above.
        self.bus.emit(
            event_catalog.ReviewResultEvent(
                component=comp.id,
                passed=review_result.passed,
                mode=review_mode.value,
                fail_count=review_result.fail_count,
                advisory_count=review_result.advisory_count,
                duration_seconds=round(review_result.duration_seconds, 2),
            )
        )

        # R10.3 claim agreement. The engineer agent is the only
        # writer of the PRD's `passes` flag, so a story marked done is a
        # claim by the thing that did the work, not a measurement of it.
        # The reviewer's per-story verdicts are a second and independent
        # reading of the same question. Require them to agree.
        #
        # Ordering, which a reviewer will want to check: this block runs
        # BEFORE the hard-mode failure return below, so a criterion
        # failure and a claim disagreement in the same attempt both
        # reach the findings stream. The existing failure path then
        # returns exactly as it always did, carrying its own criterion
        # text in the retry context. The new failure path further down
        # fires only when the review PASSED and a story it did not
        # confirm is still marked done.
        blocking, severity = self._claim_blocking()
        prd_path = wt_path / comp.prd_path
        claim_prd: PRD | None = None
        disagreements: list[Finding] = []
        try:
            claim_prd = PRD.load(prd_path)
        except (OSError, ValueError) as exc:
            # The check could not run. E9/E3-infra: record that, so
            # len(findings) == 0 keeps meaning "every check ran and
            # found nothing" rather than "one of them was silent".
            #
            # Recorded, but never blocking, even in blocking mode. An
            # unreadable PRD holds no claim to disagree with, and it is
            # already caught twice over: check_prd_stories fails Phase 1
            # on it, and run_review loads the same file to build its
            # coverage gate, so a review that PASSED is itself evidence
            # the file parsed moments earlier. A third gate here would
            # guard a state the second one rules out.
            self._add_findings(
                comp,
                [
                    Finding.infrastructure_error(
                        phase="review",
                        explanation=(
                            "Claim agreement not measured: the PRD at "
                            f"{comp.prd_path} could not be read: {exc}"
                        ),
                    )
                ],
            )
        else:
            disagreements = claim_disagreements(
                claim_prd,
                review_result,
                severity=severity,
            )
            self._add_findings(comp, disagreements)

        if not review_result.passed:
            return self._review_failure(comp, comp_result, wt_path, review_result)

        # R10.3: the review itself passed. It can still have declined to
        # confirm a story the engineer marked done, and in blocking mode
        # that is a failure of the component, not a footnote on a pass.
        if (
            blocking
            and review_result.infrastructure_error
            and claim_prd is not None
            and any(st.passes for st in claim_prd.user_stories)
        ):
            # Halt over heroics. In ADVISORY review mode a crashed or
            # unparseable reviewer yields passed=True with
            # infrastructure_error=True (the crash handler above sets
            # `passed=review_mode != HARD`), so the failure path above
            # does not fire. claim_disagreements correctly returns
            # nothing - absence of a reading is not disagreement - but
            # in BLOCKING mode "the second check never reported" must
            # not be spent as "the second check confirmed". A story
            # still claims done and nothing independent has checked it.
            #
            # Nothing is reverted here: no evidence points at any
            # particular story, and retrying is what a reviewer outage
            # calls for. The outage itself is already in the findings
            # via ReviewResult.as_findings.
            self.ui.warn(
                f"  Phase 2 FAILED for {comp.id}: claim agreement "
                "cannot be confirmed, the reviewer did not report"
            )
            return self._claim_failure(
                comp,
                comp_result,
                review_result,
                error=(
                    "Claim disagreement: the reviewer produced no "
                    "usable verdict, so no story claimed done is confirmed"
                ),
                retry_text=(
                    "The reviewer did not produce a usable verdict this "
                    "attempt, so no story you marked done has been "
                    "independently confirmed. Nothing in the PRD was "
                    "changed. Re-run and make sure the work still stands "
                    "on its own evidence."
                ),
            )
        if blocking and disagreements and claim_prd is not None:
            reverted = revert_unconfirmed_stories(
                claim_prd,
                review_result,
                disagreements,
                attempt=comp.retries + 1,
            )
            saved = True
            try:
                claim_prd.save(prd_path)
            except OSError as exc:
                # process_result is not wrapped by its caller
                # (factory.py, the scheduler loop), so an exception
                # escaping here would abort the whole run and take every
                # other component's work with it. Degrade to a
                # per-component infrastructure finding, the same way a
                # reviewer crash does. The component still fails: the
                # disagreement is real whether or not the file could be
                # rewritten. What changes is the retry text, which must
                # not claim a revert that did not happen.
                saved = False
                self._add_findings(
                    comp,
                    [
                        Finding.infrastructure_error(
                            phase="review",
                            explanation=(
                                f"Claim revert could not be written to {comp.prd_path}: {exc}"
                            ),
                        )
                    ],
                )
            self.ui.warn(
                f"  Phase 2 FAILED for {comp.id}: claim disagreement "
                f"on {len(reverted)} story(ies)"
                + ("; passes reverted in the PRD" if saved else "; PRD could not be rewritten")
            )
            return self._claim_failure(
                comp,
                comp_result,
                review_result,
                error=(
                    f"Claim disagreement: {len(reverted)} story(ies) "
                    "claimed done but not confirmed by review"
                ),
                retry_text=claim_retry_context(
                    disagreements,
                    review_result,
                    reverted=saved,
                ),
            )

        self.ui.ok(f"  Phase 2 passed for {comp.id}")
        return ReviewPhaseResult(ran=True, result=review_result)
