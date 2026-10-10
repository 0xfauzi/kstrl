"""How the review result becomes a phase failure: coverage, divergence, the
budget refusal, the claim gate and the review that did not run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import event_catalog, git
from kstrl.context import IterationContext
from kstrl.divergence import (
    AttemptReading,
    detect_divergence,
    review_finding_keys,
)
from kstrl.findings import (
    CLAIM_DISAGREEMENT_CATEGORY,
    Finding,
)
from kstrl.manifest import (
    ADVERSARIAL_BUDGET_CHECK,
    Component,
)
from kstrl.pipeline_attempt import AttemptRecorder
from kstrl.pipeline_transitions import FailureAction, PhaseFailure
from kstrl.policy import count_diff_size
from kstrl.prd import PRD
from kstrl.review import (
    ReviewResult,
)
from kstrl.review_claims import (
    claim_blocks,
)
from kstrl.security import SecurityResult

if TYPE_CHECKING:
    from kstrl.factory import (
        ComponentResult,
    )


def _gate_failure_count(result: ReviewResult | SecurityResult) -> int | None:
    """A reviewer's blocking-finding count, or None when it did not run."""
    return None if result.infrastructure_error else result.fail_count


def _produced_a_reading(ran: bool, result: ReviewResult | SecurityResult | None) -> bool:
    """Whether a skippable phase actually measured the change (#247).

    Narrower than ``ran`` on purpose, and the narrowness is the point.
    The only consumer is the record ``_buckets`` uses to decide whether
    an OLDER finding may be dropped, so the predicate has to be the one
    that makes dropping safe.

    ``ran`` is not that predicate. In ADVISORY mode a reviewer that
    CRASHES yields ``passed = review_mode != HARD`` = True with
    ``infrastructure_error=True``: it records no failure entry and the
    phase returns ``ran=True``. Keying on ``ran`` would let a crashed
    reviewer in attempt N retire attempt N-1's real finding, and if the
    budget then ran out the finding would never be seen again. That is
    the fail-open ``FailureEntry.infrastructure`` and
    ``Finding.infrastructure_error`` (E9) both exist to prevent.

    A review that ran and FAILED does count. Its own entry lands in
    ``current`` and an older entry at the same rank is retired by the
    ``rank == q`` branch already, so recording it changes no bucket; it
    keeps the record a truthful census of the attempt rather than a
    special case.

    A BLIND reviewer does not count either, and it is the same class as
    the crashed one. ``apply_coverage_check`` (``review.py``,
    ``security.py``) sets ``diffstat_disagreement`` in EVERY mode when
    the numstat the reviewer reported disagrees with git's, but only
    HARD mode turns that into ``infrastructure_error``; ADVISORY returns
    early and ``run_review`` then forces ``passed = True``, which also
    makes ``coverage_refused`` False so #266's wall does not fire. The
    result therefore arrives here as ran, present and not an
    infrastructure error, while #266's own words for the state are "the
    verdict was reached without the whole change in hand". Retiring an
    earlier finding on it would drop a live finding on the strength of a
    reading that did not cover the change.

    ``result is not None`` cannot be False on a ``ran=True`` path today.
    It is here so the answer stays the safe one if a future return site
    makes it possible.

    Review and security answer this the same way, and one definition is
    the point: two copies drift into two meanings of "the reviewer
    looked", and the looser one is the one that retires a finding. A
    shared frozen base is not available, because narrowing ``result``
    per subclass is an invariant-attribute override ``mypy --strict``
    rejects; both concrete results declare ``infrastructure_error``, so
    a function over the union is.
    """
    return (
        ran
        and result is not None
        and not result.infrastructure_error
        and not result.diffstat_disagreement
    )


@dataclass(frozen=True)
class ReviewPhaseResult:
    """Phase 2 outcome. ``ran=False`` carries a skip reason when the
    phase was SKIPPED; the R10.5 budget refusal leaves it None, because
    nothing was skipped and the ``failure`` is the record."""

    ran: bool
    skip_reason: str | None = None
    result: ReviewResult | None = None
    failure: PhaseFailure | None = None

    @property
    def produced_a_reading(self) -> bool:
        return _produced_a_reading(self.ran, self.result)


class ReviewVerdicts(AttemptRecorder):
    """The review result as a phase failure."""

    def _divergence_failure(
        self,
        comp: Component,
        wt_path: Path,
        review_result: ReviewResult,
    ) -> str | None:
        """#265: record this attempt's reading and ask whether the retry
        loop is diverging. Returns the message that should FAIL the
        component, or ``None`` when nothing should.

        A trip is reported here whatever the mode - the line, the
        finding and the event all go out before this returns - so the
        return value carries the routing decision only, and ``None``
        covering both "not diverging" and "recorded, keep retrying" is
        not a lost fact: an advisory trip is already durable in the
        event stream by then.

        Every "cannot be told" path declines to record a reading rather
        than recording a guessed one. The predicate needs CONSECUTIVE
        attempts, so a missing reading breaks the streak by itself, which
        is the fail-open direction: the loop keeps its retries.
        """
        config = self.divergence_config
        if not config.measures:
            return None
        if review_result.infrastructure_error:
            # A crashed reviewer produced no verdict, so there is nothing
            # to compare. Same rule as FailureEntry.infrastructure.
            return None
        keys = review_finding_keys(review_result)
        if not keys:
            # The review failed on something this predicate cannot key
            # (an empty-location concern family, a hand-built result).
            return None
        try:
            numstat = git.get_diff_numstat(
                self.component_base(comp.id),
                wt_path,
                strict=True,
            )
        except git.GitDiffError as exc:
            self.ui.warn(f"  Divergence detector could not measure {comp.id}: {exc}")
            return None
        # R8.1's size caps and this detector must agree about how large a
        # change is, so both count through the same helper. The result is
        # lines ADDED PLUS REMOVED, so it is churn rather than file growth;
        # see the module docstring for why that is what the predicate wants.
        files_changed, lines_changed = count_diff_size(numstat)
        readings = self.review_readings.setdefault(comp.id, [])
        readings.append(
            AttemptReading(
                attempt=comp.retries + 1,
                lines_changed=lines_changed,
                files_changed=files_changed,
                finding_keys=keys,
                # The reviewer's own count, NOT len(keys): keys
                # deduplicate and the operator's line above this one
                # ("Phase 2 FAILED: N failures") does not.
                blocking_count=review_result.fail_count,
            )
        )
        verdict = detect_divergence(readings, config)
        if verdict is None:
            return None
        message = verdict.message
        self._add_findings(
            comp,
            [Finding.divergence(message, severity="fail" if config.blocks else "advisory")],
        )
        self.bus.emit(
            event_catalog.ReviewDivergence(
                component=comp.id,
                attempts=tuple(r.attempt for r in verdict.readings),
                lines_changed=tuple(r.lines_changed for r in verdict.readings),
                files_changed=tuple(r.files_changed for r in verdict.readings),
                blocking_findings=tuple(r.blocking_count for r in verdict.readings),
                blocked=config.blocks,
            )
        )
        # One event, one print site. Severity of the LINE follows the
        # severity of the decision.
        if config.blocks:
            self.ui.err(f"  {message}")
            return message
        self.ui.warn(f"  {message}")
        return None

    #: The operator-facing account of a refused review, shared by both
    #: phases so the remedy is worded once. ``noun`` is what cannot be
    #: trusted (a verdict, or findings) and ``agent`` names the config
    #: knob to look at.
    _COVERAGE_UNVERIFIED = (
        "{role} coverage unverified: {disagreement}. The reviewer could not "
        "show it read the whole change, so its {noun} cannot be trusted and "
        "the engineer cannot fix it. Check that the {agent} can run git in "
        "the worktree, or select a model that honours the observedDiffstat "
        "field (#266)."
    )

    def _coverage_failure(
        self,
        comp: Component,
        *,
        phase: str,
        banner: str,
        role: str,
        noun: str,
        agent: str,
        disagreement: str,
        reported_a_stat: bool,
    ) -> PhaseFailure:
        """#266: the wall a review that cannot prove its coverage hits.

        FAIL, never RETRY_OR_FAIL. The refusal is a HARNESS-side fault -
        the reviewer could not show it read the change - and the
        engineer cannot fix it by writing code. Routed through the
        ordinary failure path it re-ran the engineer and handed it the
        coverage complaint as retry context.

        That is worse than useless, because unlike a crash or a
        truncated JSON reply this trigger is DETERMINISTIC: a reviewer
        model that does not emit ``observedDiffstat``, or counts it
        differently, produces the identical failure on every attempt.
        The component would burn its whole retry budget on engineer runs
        that cannot move the outcome and then fail anyway - precisely
        the #265 economics this issue exists to remove, reintroduced by
        its own fix. Same rule as the budget walls: retrying cannot
        change the input, so do not pay for the attempt.
        """
        error = self._COVERAGE_UNVERIFIED.format(
            role=role,
            disagreement=disagreement,
            noun=noun,
            agent=agent,
        )
        self.ui.err(f"  {banner} FAILED for {comp.id}: {error}")
        # The two refusals have different remedies and the journal
        # should not have to read prose to tell them apart. "no-diffstat"
        # is a model that never emits the field - swap the reviewer.
        # "range-mismatch" is a model that emitted the wrong numbers -
        # look at whether it can reach the repository. Both are
        # harness-side and neither is fixable by the engineer, which is
        # why they route the same way.
        reason = "range-mismatch" if reported_a_stat else "no-diffstat"
        return PhaseFailure(
            action=FailureAction.FAIL,
            error=error,
            phase=phase,
            check="coverage",
            signatures=[f"{phase}:coverage-unverified:{reason}"],
        )

    def _budget_refusal(
        self,
        comp: Component,
        *,
        phase: str,
        banner: str,
        role: str,
    ) -> PhaseFailure:
        """R10.5 (#226): how a hard-mode adversarial phase refuses when
        ``max_adversarial_calls`` is spent before it runs.

        FAIL, never RETRY_OR_FAIL: the budget only shrinks, so a retry
        would burn engineer iterations against the same exhausted cap.
        The infrastructure Finding is the record in the findings stream
        and the PR body; ``check=ADVERSARIAL_BUDGET_CHECK`` and the
        ``adversarial_budget:<phase>`` signature are the record in the
        journal, and ``ks serve`` reads the first of those to make the
        run terminal rather than retrying it (``serve.
        _budget_halt_outcome``). Advisory mode never reaches here: it
        keeps the recorded skip.

        THE SIGNATURE LEADS WITH THE CHECK, not with the phase, and that
        is what makes the journal and the replay agree about this run.
        ``evolution.split_signature`` takes everything before the last
        colon as the check name and ``_CATEGORY_BY_CHECK`` categorises
        it, which ``autonomy_replay.INFRA_FAILURE_PREFIXES`` is derived
        from. A ``review:`` prefix would file a reviewer that never ran
        under the reviewer's own category, and the replay would then
        count the run as a verdict about the factory's judgement while
        ``factory``'s live accounting, which asks the FINDING question,
        counts it as an infrastructure casualty and does not. #315's
        rule is that a taxonomy answering a question twice will
        eventually answer it two ways; ``adversarial_budget`` is
        enrolled as infrastructure, so both consumers read the same
        answer out of one table.
        """
        cap = self.factory_config.max_adversarial_calls
        # One sentence, used by the banner and by the Finding, so the two
        # cannot drift. The Finding carries ``phase`` as a field, so the
        # text does not name it.
        reason = (
            f"adversarial LLM budget ({cap}) exhausted before the phase ran; "
            "hard mode refuses to merge unreviewed"
        )
        error = f"{role} infrastructure error: {reason}"
        self.ui.err(f"  {banner} FAILED for {comp.id}: {error}")
        self._add_findings(
            comp,
            [Finding.infrastructure_error(phase=phase, explanation=reason)],
        )
        return PhaseFailure(
            action=FailureAction.FAIL,
            error=error,
            phase=phase,
            check=ADVERSARIAL_BUDGET_CHECK,
            signatures=[f"{ADVERSARIAL_BUDGET_CHECK}:{phase}"],
        )

    def _review_did_not_run(
        self,
        comp: Component,
        wt_path: Path,
        skip_reason: str | None,
        budget_downgraded: bool,
    ) -> ReviewPhaseResult:
        """Phase 2's tail for a review that was not executed: record the
        skip, then let the R10.3 claim gate decide whether the
        component may proceed without one.

        Lifted out of ``_phase_review`` unchanged by #226, which added a
        branch above it and would otherwise have grown that method's
        branching past the ratchet.
        """
        comp.review_passed = None
        self._record_phase_skip(
            comp,
            "review",
            skip_reason or "review skipped",
        )
        # R10.3: this return is BEFORE the claim gate, so a
        # component whose reviewer never ran would otherwise
        # complete with a story still claiming done and nothing
        # having checked it - the gate failing open, silently, at
        # exactly the moment the budget ran out. Only the budget
        # downgrade fails here: an explicit review_mode = "skip"
        # is the operator's decision, and run_factory already warns
        # at startup that the gate cannot fire under it.
        #
        # FAIL, not RETRY_OR_FAIL: retrying cannot recover budget,
        # so a retry would burn engineer iterations against a
        # deterministic refusal.
        #
        # #226 narrowed who reaches here: hard mode now refuses at the
        # exhausted budget instead of downgrading, so
        # ``budget_downgraded`` is only ever true for an advisory
        # reviewer. An advisory reviewer that never ran can still FAIL
        # the component here, so "advisory never blocks" is false as a
        # statement about this branch, whatever else it may describe.
        if (
            budget_downgraded
            and self._claim_blocking()[0]
            and self._has_unconfirmed_claim(comp, wt_path)
        ):
            error = (
                "Claim agreement cannot be confirmed: the "
                "reviewer never ran (adversarial LLM budget "
                f"({self.factory_config.max_adversarial_calls}) "
                "exhausted) and a story is still marked passes=true"
            )
            self.ui.err(f"  Phase 2 FAILED for {comp.id}: {error}")
            return ReviewPhaseResult(
                ran=False,
                skip_reason=skip_reason,
                failure=PhaseFailure(
                    action=FailureAction.FAIL,
                    error=error,
                    phase="review",
                    check="claim",
                    # Same class as _budget_refusal's signature and swept
                    # with it (#226 round 2): the reviewer did not run, so
                    # a ``review:`` prefix would tell the replay this run
                    # produced a verdict about the factory's judgement.
                    # ``check`` stays "claim" - the field ``ks serve``
                    # reads is Component.failed_check, and this refusal is
                    # the claim gate's, not the budget branch's.
                    signatures=[f"{ADVERSARIAL_BUDGET_CHECK}:claim"],
                ),
            )
        return ReviewPhaseResult(ran=False, skip_reason=skip_reason)

    def _review_failure(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
        review_result: ReviewResult,
    ) -> ReviewPhaseResult:
        """Phase 2's failure branch: build the retry context, then decide
        between retrying and the #265 divergence wall."""
        if review_result.coverage_refused:
            # BEFORE the "N failures" banner below: the coverage concern
            # is advisory, so a reviewer that returned passing criteria
            # would otherwise print "Phase 2 FAILED: 0 failures"
            # immediately above the line saying what actually went wrong.
            return ReviewPhaseResult(
                ran=True,
                result=review_result,
                failure=self._coverage_failure(
                    comp,
                    phase="review",
                    banner="Phase 2",
                    role="Review",
                    noun="verdict",
                    agent="review agent",
                    disagreement=review_result.diffstat_disagreement,
                    reported_a_stat=review_result.observed_diffstat is not None,
                ),
            )
        self.ui.warn(f"  Phase 2 FAILED for {comp.id}: {review_result.fail_count} failures")
        # #265: before spending another engineer run, ask whether the
        # last few have been spent moving away from a pass. Advisory by
        # default: this records the trip and keeps retrying, and returns
        # a message only under `[divergence] mode = "block"`.
        #
        # When it DOES block, it is FAIL rather than RETRY_OR_FAIL,
        # because the whole value is not paying for the attempt it
        # forecloses and an outcome that only reported would save
        # nothing. Stated plainly, because the other FAIL sites do not
        # work this way: budget exhaustion is a PROOF that retrying
        # cannot help (a budget only shrinks), while this is a FORECAST
        # from the loop's own trajectory. That gap is exactly why the
        # default is advisory, and why the forecast is built to fail
        # open - see the module docstring on which way its identity
        # heuristic errs. Do not read the precedent the other way round
        # and route a weaker forecast here.
        divergence = self._divergence_failure(comp, wt_path, review_result)
        if divergence is not None:
            return ReviewPhaseResult(
                ran=True,
                result=review_result,
                failure=PhaseFailure(
                    action=FailureAction.FAIL,
                    error=divergence,
                    phase="review",
                    check="divergence",
                    signatures=["review:divergence"],
                ),
            )
        # The retry context is built only on the branch that uses it: the
        # divergence wall above ends the component, so parsing and
        # re-rendering the accumulated context there would be work thrown
        # straight away.
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        ctx.add_review_finding(
            review_result.as_retry_context(),
            attempt=comp.retries + 1,
            phase="review",
            infrastructure=review_result.infrastructure_error,
        )
        # R6.1: journal the finding categories that failed the gate
        # ("review:scope_creep", "review:prd_criterion",
        # "review:infrastructure"), not the flattened reason.
        from kstrl.evolution import signatures_from_findings

        return ReviewPhaseResult(
            ran=True,
            result=review_result,
            failure=PhaseFailure(
                action=FailureAction.RETRY_OR_FAIL,
                error=(
                    "Review infrastructure error"
                    if review_result.infrastructure_error
                    else "Review failed"
                ),
                phase="review",
                check=("infrastructure" if review_result.infrastructure_error else "criteria"),
                context_json=ctx.to_json(),
                signatures=signatures_from_findings("review", review_result.as_findings()),
                failure_count=_gate_failure_count(review_result),
            ),
        )

    def _has_unconfirmed_claim(self, comp: Component, wt_path: Path) -> bool:
        """R10.3: whether the PRD still claims a story is done.

        Used on the skip path, where the reviewer never ran and there is
        no ReviewResult to compare against. A PRD that cannot be read
        answers False, for the same reason the main gate records but
        does not block on one: an unreadable PRD holds no claim to
        disagree with, and Phase 1's check_prd_stories fails on it
        first.
        """
        try:
            prd = PRD.load(wt_path / comp.prd_path)
        except (OSError, ValueError):
            return False
        return any(story.passes for story in prd.user_stories)

    def _claim_blocking(self) -> tuple[bool, str]:
        """R10.3: whether a claim disagreement fails the component,
        and the severity its findings carry.

        The autonomy level is the one ``_phase_verify`` hands Phase 1,
        and since #192 that is true by construction
        rather than by two copies of the same expression: both read the
        run's envelope, resolved once at run start.
        """
        level = self.run_envelope.autonomy_level
        blocking = claim_blocks(self.factory_config, level)
        return blocking, "fail" if blocking else "advisory"

    def _claim_failure(
        self,
        comp: Component,
        comp_result: ComponentResult,
        review_result: ReviewResult,
        *,
        error: str,
        retry_text: str,
    ) -> ReviewPhaseResult:
        """R10.3: the typed failure a blocked claim check returns.

        Note for anyone tracing the retry context: this is the first
        site that builds an ``IterationContext`` on a review that
        PASSED. Every other site builds one only on a failure path, and
        that asymmetry is why #247 keeps the record of which phases ran
        on the pipeline rather than in the context: a phase that passes
        has no failure path to write on. The entry is filed under phase
        "review" so a later review reading retires it.

        Two different failures route through here and they must not be
        recorded as the same thing. A DISAGREEMENT is a measurement: the
        reviewer ran, reported, and declined to confirm the claim. An
        OUTAGE is the absence of one. R10.2 draws that line explicitly
        (``FailureEntry.infrastructure``): only a measured entry retires
        its own phase, because a crashed check that retired a real
        earlier finding would silently drop it. Journalling an outage as
        ``review:claim_disagreement`` would also tell the evolution
        data that a reviewer disagreed when none reported.
        """
        infrastructure = review_result.infrastructure_error
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        ctx.add_review_finding(
            retry_text,
            attempt=comp.retries + 1,
            phase="review",
            infrastructure=infrastructure,
        )
        return ReviewPhaseResult(
            ran=True,
            result=review_result,
            failure=PhaseFailure(
                action=FailureAction.RETRY_OR_FAIL,
                error=error,
                phase="review",
                check="infrastructure" if infrastructure else "claim",
                context_json=ctx.to_json(),
                # R6.1: the journal signature. Not derived via
                # signatures_from_findings, which only emits for
                # severity fail/critical/high - true for a disagreement,
                # but the signature must not silently depend on that.
                signatures=[
                    "review:infrastructure"
                    if infrastructure
                    else f"review:{CLAIM_DISAGREEMENT_CATEGORY}"
                ],
            ),
        )
