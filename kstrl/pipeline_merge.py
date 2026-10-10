"""The merge gate: the PR step, the merge record, the parks, the merge-conflict
retry and the inbox merge decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from kstrl import event_catalog, git
from kstrl.acceptance_lines import pr_isolation
from kstrl.context import IterationContext, IterationRecord
from kstrl.inbox import (
    InboxItem,
    ItemKind,
    ItemStatus,
)
from kstrl.manifest import (
    Component,
    ComponentStatus,
)
from kstrl.pipeline_state import _iso_now
from kstrl.pipeline_transitions import Transition, Transitions

if TYPE_CHECKING:
    from kstrl.factory import (
        ComponentResult,
    )

#: The two ``Component.failed_check`` values that say a human turned a
#: merge candidate down: rejected at the merge gate (interactively or
#: with ``ks inbox reject``), or its PR closed without merging while
#: kstrl waited (#601). One owner: ``kstrl.factory``'s
#: ``HUMAN_REJECTION_CHECKS`` reads these rather than re-spelling them,
#: so renaming either check here cannot silently switch off the L3
#: rejection demotion (a second spelling was exactly how that failed
#: open before).
HITL_REJECT_CHECK = "hitl_reject"

PR_CLOSED_CHECK = "pr_closed"


class PrDisposition(Enum):
    """Outcome of the per-component PR create+merge step."""

    SKIPPED = "skipped"  # create_prs off, or single_pr defers to end-of-run
    MERGED = "merged"
    MERGE_PENDING = "merge_pending"
    # R7.5: the PR conflicts with base; routed to the re-run doctrine
    # (re-run the component against the freshly merged base) instead of
    # a terminal failure.
    CONFLICT = "conflict"
    FAILED = "failed"
    NO_GH = "no_gh"  # completes without a PR; code stays on its branch


@dataclass(frozen=True)
class PrPhaseResult:
    """PR step outcome; pending/failed dispositions carry the error."""

    disposition: PrDisposition
    pr_url: str = ""
    error: str = ""
    #: The failed_check a FAILED disposition records (#601: a PR closed
    #: without merging is ``pr_closed``, which the ladder reads).
    check: str = "pr_flow"


class MergeGate(Transitions):
    """The PR step, the merge record and the parks."""

    def _park_merge_pending(
        self,
        comp: Component,
        error: str,
    ) -> Transition:
        """VERIFYING -> MERGE_PENDING: the PR merge was initiated but not
        confirmed (R0.2). Parked, not terminal: no completed_at, but the
        attempt's journal slice is closed (R3.3) - after the
        merge_pending event so the slice includes it."""
        comp.status = ComponentStatus.MERGE_PENDING.value
        comp.error = error
        # Richer v2 event first; the v1-parity twin keeps progress.jsonl
        # unchanged (the reducer prefers the v2 event, chunk 2).
        self.bus.emit(
            event_catalog.PrMergePending(
                component=comp.id,
                pr_url=comp.pr_url,
                error=comp.error,
            )
        )
        self.bus.emit(
            event_catalog.MergePendingV1(
                component=comp.id,
                pr_url=comp.pr_url,
                error=comp.error,
            )
        )
        self.notify.fire_merge_pending(comp.id, comp.error)
        self._inbox_add(
            ItemKind.MERGE_GATE,
            f"{comp.id} merge unconfirmed",
            detail=comp.error,
            component=comp.id,
            dedupe_key=f"merge:{comp.id}",
            evidence={"pr_url": comp.pr_url},
        )
        self._end_attempt(comp)
        self.ui.warn(
            f"  MERGE PENDING: {comp.id}: {comp.error}; "
            f"dependents stay blocked; a factory re-run "
            f"re-polls the PR"
        )
        self.manifest.save(self.manifest_path)
        return Transition.MERGE_PENDING

    def _park_awaiting_approval(self, comp: Component) -> Transition:
        """VERIFYING -> AWAITING_APPROVAL: the merge gate had nobody to ask (#465).

        Every gate passed, so nothing here is a failure: no failure
        signature, no cascade skip, no completed_at, no halted_run item.
        The branch keeps the reviewed commits and the dependents stay
        PENDING. The attempt's journal slice is closed like any other
        terminal transition of this run (R3.3).

        A park is only a park when its merge_gate item is open: that item
        is the one thing `ks inbox approve` and `ks inbox reject` can act
        on. With the inbox disabled or unwritable nothing could ever move
        the component, so it fails at the gate as it did before #465 and
        `ks retry` can rebuild it.
        """
        item = self._park_decision(comp.id)
        if item is None or item.status is not ItemStatus.OPEN:
            return self.fail(
                comp,
                "Stopped at the merge gate with nothing to approve it: no open "
                "merge_gate inbox item ([inbox] disabled or unwritable), so "
                f"nothing was pushed; `ks retry {comp.id}` rebuilds it",
                phase="pr",
                check="merge_gate",
            )
        comp.status = ComponentStatus.AWAITING_APPROVAL.value
        comp.error = (
            "Parked awaiting merge approval (pause_before_pr_merge, nobody "
            "answered the gate); `ks inbox approve <id>` merges it"
        )
        self._end_attempt(comp)
        self.ui.warn(
            f"  AWAITING APPROVAL: {comp.id} passed every gate and nothing was "
            f"pushed; its dependents wait. `ks inbox ls` lists the merge_gate "
            f"item; `ks inbox approve <id>` merges {comp.branch_name} and "
            f"continues the run"
        )
        self.manifest.save(self.manifest_path)
        return Transition.AWAITING_APPROVAL

    def _retry_after_merge_conflict(
        self,
        comp: Component,
        comp_result: ComponentResult,
        pr: PrPhaseResult,
    ) -> Transition:
        """R7.5 merge-conflict doctrine: re-run, don't rebase.

        A conflicting PR means the base moved under this component
        (usually a sibling merged first). Rebasing agent output would
        hand the conflict back to a model with no context on the other
        side of it; re-running the component against the freshly merged
        base lets the engineer implement WITH the sibling's code in
        view. Mechanics: close the conflicting PR (audit comment) and
        delete its remote branch, clear the manifest's PR pointers so
        the retry creates a fresh PR instead of re-polling the closed
        one, then route through the fresh-base retry path (worktree AND
        branch recreated from origin/<base>).
        """
        from kstrl.pr import close_pr_for_rerun, pr_number_from_url

        error = pr.error or "PR conflicts with base"
        self.ui.warn(
            f"  MERGE CONFLICT: {comp.id}: {error[:120]}; re-running "
            f"the component against the freshly merged base"
        )
        pr_number = comp.pr_number or pr_number_from_url(
            comp.pr_url or pr.pr_url,
        )
        if pr_number:
            close_error = close_pr_for_rerun(
                pr_number,
                comp.branch_name,
                self.root_dir,
            )
            if close_error:
                # Non-fatal: the re-run's own push fails loudly if the
                # remote branch is still in the way.
                self.ui.warn(f"  Conflicting-PR cleanup incomplete (non-fatal): {close_error}")
        comp.pr_number = None
        comp.pr_url = ""
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        ctx.add_iteration(
            IterationRecord(
                iteration=comp_result.iterations,
                success=False,
                attempt=comp.retries + 1,
                error=(
                    "The previous attempt's PR hit a merge conflict with the "
                    "base branch; this attempt starts from the freshly merged "
                    "base, which already contains the sibling changes"
                ),
            )
        )
        return self.retry_or_fail(
            comp,
            error,
            ctx.to_json(),
            phase="pr",
            check="merge_conflict",
            signatures=["pr:merge-conflict"],
            fresh_base=True,
        )

    def _fail_pr_flow(self, comp: Component, error: str, *, check: str) -> Transition:
        """VERIFYING -> FAILED on a push/create/merge failure, or a merge-pending
        PR GitHub reports closed unmerged (R0.2: COMPLETED requires a CONFIRMED
        merge). ``check`` names which: ``"pr_flow"`` or ``PR_CLOSED_CHECK``
        (#601) - no default, every caller states which it observed."""
        comp.status = ComponentStatus.FAILED.value
        comp.error = error
        comp.completed_at = _iso_now()
        comp.failed_phase = "pr"
        comp.failed_check = check
        # Spelled out with keywords, and read that way. #339: this
        # is the third caller of the funnel and the only one that is
        # not `fail` / `retry_or_fail`, so no `signatures=` is ever
        # handed to it and the phase becomes the check name. It was
        # invisible to every layer of
        # `tests/test_check_name_enrolment.py`: not a fail call, no
        # keyword, no colon in "pr" for the spellings net to see.
        # Mutating "pr" to "bogus_flow" left 4737 tests green.
        self._record_failure_signatures(
            comp,
            phase="pr",
            error=comp.error,
            signatures=None,
        )
        self._end_attempt(comp)
        self._cascade_skip(comp.id)
        self.factory_result.failed.append(comp.id)
        self.bus.emit(event_catalog.ComponentFailed(component=comp.id, error=comp.error))
        self.notify.fire_first_failure(comp.id, comp.error)
        self._inbox_add(
            ItemKind.HALTED_RUN,
            f"{comp.id} halted in the PR flow",
            detail=comp.error,
            component=comp.id,
            dedupe_key=f"halted:{comp.id}:pr",
            evidence={"phase": "pr", "pr_url": comp.pr_url},
        )
        self.ui.err(f"  Failed: {comp.id}: {comp.error[:120]}")
        self.manifest.save(self.manifest_path)
        return Transition.FAILED

    def _record_merge(self, comp: Component, merge_sha: str, head_sha: str) -> None:
        """Record a confirmed merge of ``comp``'s PR: the one place (#584).

        Both paths that confirm a merge call this: ``_phase_pr`` when this
        run merged the PR, and ``repoll_merge_pending`` when a restarted
        run finds a parked PR merged. The manifest gets the merge commit
        and is saved, then the run's stream gets ``pr_merged`` with the
        PR number, URL and commit the manifest now holds, so every reader
        of either record sees the same merge. ``merge_sha`` is "" when
        GitHub published no commit, and then both records say "": a
        commit an earlier merge of this component recorded is not this
        merge's commit.

        #601: ``head_sha`` is the PR head GitHub merged ("" when it
        reported none). It goes on ``factory_result.merged``, the one dict
        (component id -> head sha) the autonomy ladder counts merges from.
        """
        from kstrl.pr import pr_number_from_url

        comp.merge_sha = merge_sha
        self.factory_result.merged[comp.id] = head_sha
        self.manifest.save(self.manifest_path)
        self.bus.emit(
            event_catalog.PrMerged(
                component=comp.id,
                pr_number=comp.pr_number or pr_number_from_url(comp.pr_url),
                pr_url=comp.pr_url,
                merge_sha=comp.merge_sha,
            )
        )

    def apply_merge_decisions(self) -> None:
        """#465: act on the decision each merge-gate park was waiting for.

        Run after every pre-spend refusal in ``_run_factory_locked`` (a
        refused run must not push or merge anything) and before anything
        is scheduled, so an approved component is merged before its
        dependents are cut from the base branch. The decision is the
        park's merge_gate inbox item: APPROVED merges the branch the gate
        parked, REJECTED fails the component and skips its dependents, and
        anything else (open, snoozed, no item, inbox off) leaves it parked.
        """
        parked = [
            c
            for c in self.manifest.components
            if c.status == ComponentStatus.AWAITING_APPROVAL.value
        ]
        for comp in parked:
            decision = self._park_decision(comp.id)
            if decision is not None and decision.status is ItemStatus.APPROVED:
                self._merge_approved(comp, decision)
            elif decision is not None and decision.status is ItemStatus.REJECTED:
                # The rejection IS the human decision; a halted_run item
                # on top would ask for it again.
                self._inbox_suppress_generic(comp.id)
                self.fail(
                    comp,
                    f"Rejected at the merge gate: {decision.decision_comment}",
                    phase="pr",
                    check=HITL_REJECT_CHECK,
                    signatures=["review:hitl-rejected"],
                )
            else:
                self.ui.warn(
                    f"  '{comp.id}' is still awaiting merge approval; its "
                    f"dependents wait (`ks inbox ls`, then `ks inbox approve <id>`)"
                )

    def _merge_approved(self, comp: Component, decision: InboxItem) -> None:
        """Push, open and merge the branch exactly as the gate parked it.

        The approval covers the commit recorded when the gate parked the
        component. A branch that has moved since carries work nobody
        reviewed, so it is refused and nothing is pushed.
        """
        approved = str(decision.evidence.get("head_sha") or "")
        head = git.branch_sha(comp.branch_name, self.root_dir) or ""
        if not approved or head != approved:
            self.fail(
                comp,
                f"merge approval does not match the branch: approved "
                f"{approved or 'no recorded commit'}, '{comp.branch_name}' is "
                f"now at {head or 'nothing'}; nothing was pushed",
                phase="pr",
                check="merge_gate",
            )
            return
        pr = self._phase_pr(comp)
        if pr.disposition in (PrDisposition.MERGED, PrDisposition.NO_GH):
            self.complete(comp)
        elif pr.disposition == PrDisposition.MERGE_PENDING:
            self._park_merge_pending(comp, pr.error)
        else:
            # A conflict too: the re-run doctrine re-runs the engineer,
            # and an approval is not a request for that. `ks retry` is.
            self._fail_pr_flow(comp, pr.error or "PR flow failed", check=pr.check)

    def _phase_pr(self, comp: Component) -> PrPhaseResult:
        """Per-component PR create+merge. single_pr mode is exempt
        (handled by the caller): every component shares one branch, a
        single PR is created at end-of-run, and squash-merging the
        shared branch per component would destroy the history the
        remaining components build on."""
        from kstrl.pr import is_gh_available, push_create_and_merge_pr

        if not is_gh_available():
            # No gh: the PR/merge gate cannot run. Completing anyway
            # preserves local-only workflows, but say so loudly -
            # this component's code exists only on its local branch.
            self.ui.warn(
                f"  gh CLI not available: {comp.id} completes without "
                f"a PR; its code stays on branch {comp.branch_name}"
            )
            return PrPhaseResult(disposition=PrDisposition.NO_GH)

        self.ui.info(f"  Creating and merging PR for {comp.id}...")
        outcome = push_create_and_merge_pr(
            comp,
            self.manifest,
            self.root_dir,
            self.ui,
            merge_method="squash",
            merge_timeout=self.factory_config.merge_timeout,
            isolation=pr_isolation(self.factory_config),
            requirements=self.factory_config.requirements,
        )
        if outcome.pr_url:
            self.factory_result.pr_urls.append(outcome.pr_url)
            self.bus.emit(
                event_catalog.PrCreated(
                    component=comp.id,
                    pr_number=comp.pr_number or 0,
                    pr_url=outcome.pr_url,
                )
            )
        self.manifest.save(self.manifest_path)

        # R0.2 (CRIT-2): COMPLETED requires a CONFIRMED merge.
        # Anything less and dependents would cut worktrees from
        # a base that lacks this component's code.
        if not outcome.merged:
            if outcome.merge_conflict:
                # R7.5: conflicts route to the re-run doctrine.
                return PrPhaseResult(
                    disposition=PrDisposition.CONFLICT,
                    pr_url=outcome.pr_url,
                    error=outcome.error or "PR conflicts with base",
                )
            if outcome.merge_pending:
                return PrPhaseResult(
                    disposition=PrDisposition.MERGE_PENDING,
                    pr_url=outcome.pr_url,
                    error=outcome.error or "PR merge not confirmed",
                )
            return PrPhaseResult(
                disposition=PrDisposition.FAILED,
                pr_url=outcome.pr_url,
                error=outcome.error or "PR flow failed",
                check=PR_CLOSED_CHECK if outcome.closed else "pr_flow",
            )
        self._record_merge(comp, outcome.merge_sha, outcome.head_sha)
        return PrPhaseResult(
            disposition=PrDisposition.MERGED,
            pr_url=outcome.pr_url,
        )
