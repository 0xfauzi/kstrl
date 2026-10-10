"""The human checkpoint (E6) and the route from a judged attempt to the PR."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from kstrl import event_catalog, git
from kstrl.context import IterationContext
from kstrl.inbox import (
    ItemKind,
)
from kstrl.interaction import (
    CheckpointContext,
    PromptKind,
    PromptRequest,
    PromptResponse,
)
from kstrl.manifest import (
    Component,
    park_dedupe_key,
)
from kstrl.pipeline_checks import (
    DiffPhaseResult,
    VerifyPhaseResult,
)
from kstrl.pipeline_knowledge import DistillPhaseResult
from kstrl.pipeline_merge import HITL_REJECT_CHECK, MergeGate, PrDisposition, PrPhaseResult
from kstrl.pipeline_security import SecurityPhaseResult
from kstrl.pipeline_transitions import Transition
from kstrl.pipeline_verdicts import ReviewPhaseResult
from kstrl.worktree_sweep import sweep_findings

if TYPE_CHECKING:
    from kstrl.factory import (
        ComponentResult,
    )

# PR A: the E6 checkpoint shows a real diff excerpt, not just the
# review summary string. Bounded so a huge diff cannot flood the modal.
CHECKPOINT_DIFF_CHAR_LIMIT = 20_000

#: The merge-gate park's inbox item text, which ``ks inbox show`` and the TUI
#: inbox print as it is; what the shell commands do is the shell's (#433 H7, K1).
PARK_DETAIL = (
    "Merge approval is required and no prompt was available to ask for it, so "
    "nothing was pushed and no PR was opened. The branch holds the reviewed "
    "work. From the shell, ks inbox approve <id> records approval and starts "
    "the ks factory run that pushes the branch, opens the PR, merges it and "
    "continues; ks inbox reject <id> --comment ... records rejection and "
    "starts the run that fails the component and skips its dependents."
)


class CheckpointDecision(Enum):
    """E6 human-in-the-loop checkpoint outcome."""

    # `_phase_checkpoint` itself never returns this while its own gate
    # (pause_before_pr_merge) is on - the guard at its top returns this
    # and nothing else runs. `PipelineOutcome.checkpoint`, the field
    # `process_result` sets it into, is a DIFFERENT claim: that field
    # keeps this value whenever `create_prs` is off or `single_pr` is on
    # (the `create_prs and not single_pr` guard in `process_result`),
    # because `_phase_checkpoint` is never even called then -
    # regardless of whether the gate itself is on (#594
    # simplify round, B2).
    NOT_PROMPTED = "not_prompted"
    APPROVED = "approved"
    # R8.3: nobody answered the gate (no interactive UI, or #594 a prompt
    # that went unanswered), so the component is parked for the inbox
    # rather than merged unreviewed.
    PARKED = "parked"
    REJECTED = "rejected"
    RETRY = "retry"


def _unanswered_choice_note(response: PromptResponse) -> str:
    """``, choice=N``, only when there is a real answer to report.

    An unanswered response carries ``choice=None`` (#647), and the
    ``answered=False`` beside it in the log line already says so. A free
    function rather than an inline conditional in the caller:
    `_phase_checkpoint` sits at the complexipy ceiling already, and a
    branch here does not add to it.
    """
    return f", choice={response.choice}" if response.choice is not None else ""


@dataclass(frozen=True)
class PipelineOutcome:
    """Everything one ``process_result`` pass decided, for callers/tests."""

    transition: Transition
    verify: VerifyPhaseResult | None = None
    diff: DiffPhaseResult | None = None
    review: ReviewPhaseResult | None = None
    security: SecurityPhaseResult | None = None
    distill: DistillPhaseResult | None = None
    checkpoint: CheckpointDecision | None = None
    pr: PrPhaseResult | None = None


class Delivery(MergeGate):
    """The human checkpoint and the route to the PR."""

    def _deliver(
        self,
        comp: Component,
        comp_result: ComponentResult,
        *,
        verify: VerifyPhaseResult,
        diff: DiffPhaseResult,
        review: ReviewPhaseResult,
        security: SecurityPhaseResult,
        distill: DistillPhaseResult,
    ) -> PipelineOutcome:
        """After every gate passed: the checkpoint, the PR, and completion."""
        comp_id = comp.id
        # HITL checkpoint + PR create/merge (per-component PR mode only).
        checkpoint = CheckpointDecision.NOT_PROMPTED
        pr = PrPhaseResult(disposition=PrDisposition.SKIPPED)
        if self.factory_config.create_prs and not self.factory_config.single_pr:
            checkpoint = self._phase_checkpoint(
                comp,
                diff_text=diff.diff,
                review=review,
            )
            refusal = self._checkpoint_refusal(
                comp,
                checkpoint,
                comp_result,
                verify=verify,
                diff=diff,
                review=review,
                security=security,
                distill=distill,
            )
            if refusal is not None:
                return refusal

            t0 = self._phase_started(comp, "pr")
            pr = self._phase_pr(comp)
            self._phase_completed(
                comp,
                "pr",
                t0,
                pr.disposition
                in (
                    PrDisposition.MERGED,
                    PrDisposition.NO_GH,
                    PrDisposition.SKIPPED,
                ),
                pr.error,
            )
            if pr.disposition == PrDisposition.CONFLICT:
                return PipelineOutcome(
                    transition=self._retry_after_merge_conflict(
                        comp,
                        comp_result,
                        pr,
                    ),
                    verify=verify,
                    diff=diff,
                    review=review,
                    security=security,
                    distill=distill,
                    checkpoint=checkpoint,
                    pr=pr,
                )
            if pr.disposition == PrDisposition.MERGE_PENDING:
                return PipelineOutcome(
                    transition=self._park_merge_pending(comp, pr.error),
                    verify=verify,
                    diff=diff,
                    review=review,
                    security=security,
                    distill=distill,
                    checkpoint=checkpoint,
                    pr=pr,
                )
            if pr.disposition == PrDisposition.FAILED:
                return PipelineOutcome(
                    transition=self._fail_pr_flow(comp, pr.error, check=pr.check),
                    verify=verify,
                    diff=diff,
                    review=review,
                    security=security,
                    distill=distill,
                    checkpoint=checkpoint,
                    pr=pr,
                )

        # Clean up worktree now that code is merged
        if self.factory_config.use_worktrees and comp_id in self.worktree_paths:
            self._add_findings(
                comp,
                sweep_findings(
                    self.hooks.cleanup_worktree(comp_id, self.root_dir, self.run_id),
                    "cleanup",
                ),
            )
            del self.worktree_paths[comp_id]

        return PipelineOutcome(
            transition=self.complete(comp),
            verify=verify,
            diff=diff,
            review=review,
            security=security,
            distill=distill,
            checkpoint=checkpoint,
            pr=pr,
        )

    def _checkpoint_refusal(
        self,
        comp: Component,
        checkpoint: CheckpointDecision,
        comp_result: ComponentResult,
        *,
        verify: VerifyPhaseResult,
        diff: DiffPhaseResult,
        review: ReviewPhaseResult,
        security: SecurityPhaseResult,
        distill: DistillPhaseResult,
    ) -> PipelineOutcome | None:
        """The checkpoint's non-PR routing, extracted from
        ``process_result`` so the allow-list guard below does not add to
        that function's already-over-ceiling complexity (#594 simplify
        round, A3). ``None`` means proceed to ``_phase_pr``; anything
        else is the terminal outcome for this pass.

        ALLOW-LIST, not the deny-list this replaces. ``process_result``
        used to special-case ``REJECTED``, ``PARKED`` and ``RETRY`` by
        name and let anything else - including a producer defect in
        ``_phase_checkpoint`` returning some other decision - fall
        through to ``_phase_pr`` unreviewed. Only an answered Approve
        (``APPROVED``) or the gate being off (``NOT_PROMPTED``) proceeds
        now; everything else parks or refuses, and an unrecognised
        decision is logged rather than silently merged.
        """
        outcome_fields: dict[str, Any] = {
            "verify": verify,
            "diff": diff,
            "review": review,
            "security": security,
            "distill": distill,
            "checkpoint": checkpoint,
        }
        if checkpoint == CheckpointDecision.REJECTED:
            return PipelineOutcome(
                transition=self.fail(
                    comp,
                    "Rejected at HITL checkpoint",
                    phase="pr",
                    check=HITL_REJECT_CHECK,
                    # THE RULE FOR ALL THREE CHECKPOINT BRANCHES,
                    # stated here and pointed at from the other two.
                    # Without an explicit `signatures=` the PHASE
                    # becomes the check name, and `pr` is the row
                    # that holds push, create and merge plumbing -
                    # infrastructure since #315, which throws the
                    # whole run out of the autonomy ladder's
                    # evidence. So: a branch that carries a VERDICT
                    # about the change must name it, and a branch
                    # that carries no verdict must not. A person
                    # looking at the change and refusing it is the
                    # most decisive verdict about the factory's
                    # judgement there is; it reached the journal as
                    # `pr:rejected-at-hitl-checkpoint`.
                    #
                    # The phase stays "pr" because that is the
                    # vocabulary these branches have always used and
                    # `failed_phase` is written to the manifest.
                    # Note it is not where this happens:
                    # `_phase_started(comp, "pr")` fires below all
                    # three branches, so by the module's own
                    # accounting the checkpoint precedes the phase
                    # it is filed under. Renaming it is a separate
                    # decision, and #339 review declined to make it
                    # here because the three branches need three
                    # different categories, so no one phase name can
                    # serve them. The mechanism that would remove
                    # the literals entirely is keying the fallback
                    # on `check` rather than `phase` - the branches
                    # already carry hitl_reject / merge_gate /
                    # hitl_retry - and that is blocked on
                    # factory.py.
                    signatures=["review:hitl-rejected"],
                ),
                **outcome_fields,
            )
        if checkpoint == CheckpointDecision.PARKED:
            # R8.3: the gate could not be answered, so the merge does
            # NOT happen. #465: and it is not a failure either. The
            # component waits with its reviewed branch, its
            # dependents wait with it, and the merge_gate item
            # _phase_checkpoint filed is the question.
            return PipelineOutcome(transition=self._park_awaiting_approval(comp), **outcome_fields)
        if checkpoint == CheckpointDecision.RETRY:
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_checkpoint_request(
                "Human reviewer requested changes at PR checkpoint",
                attempt=comp.retries + 1,
            )
            return PipelineOutcome(
                transition=self.retry_or_fail(
                    comp,
                    "Retry requested at HITL checkpoint",
                    ctx.to_json(),
                    phase="pr",
                    check="hitl_retry",
                    # #339: a human asking for changes is a verdict
                    # ON the change, so it names one. Same rule as
                    # hitl_reject above, and it had the same defect
                    # until this round.
                    signatures=["review:hitl-changes-requested"],
                ),
                **outcome_fields,
            )
        if checkpoint in (CheckpointDecision.APPROVED, CheckpointDecision.NOT_PROMPTED):
            return None
        # #594 A3: a decision none of the three branches above name, and
        # not an answered Approve or the gate off either - a producer
        # defect in `_phase_checkpoint`. No merge_gate inbox item was
        # filed for this path (only `_phase_checkpoint`'s park block
        # files one), so parking here would either fail with a
        # misleading "no open merge_gate inbox item" or park against a
        # stale item from an earlier run of this component. Refuse
        # instead, rather than falling through to `_phase_pr`
        # unreviewed, and say so.
        self.ui.warn(
            f"  the merge gate for {comp.id} returned an unrecognised "
            f"decision ({checkpoint!r}); refusing it (nothing was pushed)"
        )
        return PipelineOutcome(
            transition=self.fail(
                comp,
                f"Unrecognised merge-gate decision {checkpoint!r}; nothing was pushed",
                phase="pr",
                check="merge_gate",
            ),
            **outcome_fields,
        )

    def _phase_checkpoint(
        self,
        comp: Component,
        *,
        diff_text: str,
        review: ReviewPhaseResult,
    ) -> CheckpointDecision:
        """E6: human-in-the-loop checkpoint. When opt-in, prompt
        before pushing+merging so a human can inspect the diff,
        the review findings, and the security findings before
        the PR goes through. Reject is terminal (R2.6): it marks
        the component FAILED and cascade-skips dependents with no
        retry and no re-prompt - routing it through the retry
        loop would re-run the full agent+review cycle and ask the
        human again, once per remaining retry. A human who wants
        a re-run says so explicitly via Retry, which consumes a
        retry like any other failure. When no UI is interactive
        the prompt is skipped but the gate is NOT: R8.3 returns
        PARKED, which withholds the merge and files a merge_gate
        inbox item - automation fails loudly rather than blocking
        indefinitely OR merging something nobody approved. A prompt
        that was asked and not answered parks the same way (#594):
        an unanswered request or a choice outside the three options
        is not consent. NOT_PROMPTED means the gate is off, and
        nothing else."""
        if not self.factory_config.pause_before_pr_merge:
            return CheckpointDecision.NOT_PROMPTED
        question = f"Approve PR creation and merge for {comp.id}?"
        self.bus.emit(
            event_catalog.CheckpointRequested(
                component=comp.id,
                kind="pr_merge",
                question=question,
            )
        )
        request = PromptRequest(
            kind=PromptKind.CHECKPOINT,
            header=question,
            options=(
                "Approve",
                "Reject (fail component, skip dependents)",
                "Retry (consume a retry, re-run component)",
            ),
            default=0,
            component_id=comp.id,
            checkpoint=CheckpointContext(
                component_id=comp.id,
                diff_excerpt=git.truncate_diff_for_prompt(
                    diff_text,
                    CHECKPOINT_DIFF_CHAR_LIMIT,
                )
                if diff_text
                else "",
                review_findings=tuple(f for f in comp.findings if f.phase == "review"),
                security_findings=tuple(f for f in comp.findings if f.phase == "security"),
                usage=self.usage_totals_for(comp.id),
                branch=comp.branch_name,
            ),
        )
        if self.interaction.can_prompt():
            self.ui.section(f"Human checkpoint: {comp.id}")
            self.ui.info(comp.review_findings or "(no review findings)")
            response = self.interaction.request(request)
            # #594: only an answered Approve merges. No `.get` default: a
            # choice outside the three options is not an answer, and an
            # unanswered request (a TUI that detached, an interrupted
            # terminal prompt) is not consent. Both park below, through
            # the same code as the non-interactive gate.
            decision = (
                {
                    0: CheckpointDecision.APPROVED,
                    1: CheckpointDecision.REJECTED,
                    2: CheckpointDecision.RETRY,
                }.get(response.choice)
                if response.choice is not None
                else None
            )
            if decision is not None:
                self.bus.emit(
                    event_catalog.CheckpointResolved(
                        component=comp.id,
                        kind="pr_merge",
                        decision=decision.name.lower(),
                        decided_by="operator",
                    )
                )
                if decision == CheckpointDecision.REJECTED:
                    self.ui.warn(f"  Human rejected {comp.id} at PR checkpoint")
                elif decision == CheckpointDecision.RETRY:
                    self.ui.warn(f"  Human requested retry for {comp.id} at PR checkpoint")
                return decision
            self.ui.warn(
                f"  the merge gate for {comp.id} got no answer "
                f"(answered={response.answered}"
                f"{_unanswered_choice_note(response)}); "
                f"parking it for approval (see `ks inbox ls`)"
            )
        else:
            # R8.3: the merge gate is the whole point of
            # pause_before_pr_merge, and proceeding here silently merged
            # without the approval that was asked for - in exactly the
            # unattended case R8.2's L1/L2 forces the gate ON for. Park
            # the component instead and route the decision to the inbox.
            self.ui.warn(
                f"  pause_before_pr_merge requested but UI is "
                f"non-interactive; parking {comp.id} for approval "
                f"(see `ks inbox ls`)"
            )
        # #465: the item records the commit it parked, and
        # apply_merge_decisions merges exactly that commit once
        # `ks inbox approve` has answered.
        self.bus.emit(
            event_catalog.CheckpointResolved(
                component=comp.id,
                kind="pr_merge",
                decision="parked",
                decided_by="inbox",
            )
        )
        evidence: dict[str, Any] = {
            "branch": comp.branch_name,
            # #601: the diff phase already recorded this same commit onto
            # ``comp.judged_sha`` (``git.branch_sha`` again here would be a
            # second owner of "what the branch was at" for one park - kstrl
            # never commits to a component branch after the diff phase, so
            # the two reads can only differ if something outside it moved
            # the branch, which is exactly the drift this evidence exists
            # to catch in ``_merge_approved``).
            "head_sha": comp.judged_sha,
        }
        # #450: the review_result event's counts, read from the same
        # properties. Absent when the review produced no reading, so a
        # crashed or skipped review is never reported as zero findings.
        if review.produced_a_reading and review.result is not None:
            evidence["review_fail_count"] = review.result.fail_count
            evidence["review_advisory_count"] = review.result.advisory_count
        self._inbox_add(
            ItemKind.MERGE_GATE,
            f"{comp.id} awaiting merge approval",
            detail=PARK_DETAIL,
            component=comp.id,
            dedupe_key=park_dedupe_key(comp.id),
            evidence=evidence,
        )
        return CheckpointDecision.PARKED
