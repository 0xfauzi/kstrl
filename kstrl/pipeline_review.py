"""Phase 2 of one attempt: the second-opinion review against the PRD, and the
claim agreement that follows it (R10.3).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.agents import recording_prompts
from kstrl.events import collect_usage, event_catalog
from kstrl.pipeline_ledger import UsageLedger
from kstrl.pipeline_transitions import FailureAction, PhaseFailure
from kstrl.pipeline_verdicts import ReviewPhaseResult, ReviewVerdicts
from kstrl.plan import PRD, Component, Finding
from kstrl.proc import limit_seconds
from kstrl.review import (
    ReviewMode,
    ReviewResult,
    claim_disagreements,
    claim_retry_context,
    revert_unconfirmed_stories,
)
from kstrl.verify import VerificationResult

if TYPE_CHECKING:
    from kstrl.factory_result import ComponentResult


class ReviewPhase(ReviewVerdicts, UsageLedger):
    """Phase 2: the reviewer run and the claim agreement gate."""

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
