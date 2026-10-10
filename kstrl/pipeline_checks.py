"""The mechanical checks of one attempt: the gate worktree, the gate logs, the
acceptance check and the diff.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import event_catalog, git
from kstrl.atomicio import atomic_write_text
from kstrl.context import ACCEPTANCE_PHASE, IterationContext
from kstrl.findings import Finding
from kstrl.manifest import Component
from kstrl.pipeline_attempt import AttemptRecorder
from kstrl.pipeline_inbox import InboxDesk
from kstrl.pipeline_transitions import FailureAction, PhaseFailure
from kstrl.verify import (
    SCOPE_UNREADABLE_CHECK,
    CheckResult,
    VerificationResult,
    scope_unreadable_error,
)
from kstrl.waivers import ApprovalSnapshot

if TYPE_CHECKING:
    from kstrl.factory import ComponentResult


@dataclass(frozen=True)
class VerifyPhaseResult:
    """Phase 1 outcome. ``ran=False`` means --no-verify skipped it."""

    ran: bool
    verification: VerificationResult
    failure: PhaseFailure | None = None


@dataclass(frozen=True)
class DiffPhaseResult:
    """The component's diff, fetched once and shared by the phases that
    still take one as text: fact-utilization measurement, the knowledge
    distiller, the HITL checkpoint excerpt and the PR body.

    #266: the review and security phases are NOT among them any more.
    They run inside the worktree and read git themselves, so there is no
    prompt-sized diff to prepare for them, no chunking decision to make,
    and no way for a diff to be too large to review."""

    diff: str = ""
    failure: PhaseFailure | None = None


def _verify_routing(failing: list[CheckResult]) -> tuple[FailureAction, str]:
    """How a failed Phase 1 transitions, and what the record says.

    #294: a scope that could not be READ is a wall, not a gate. Every
    other Phase 1 check measures the engineer's work, so a retry
    re-measures something that changed. This one measures the HARNESS's
    own input, resolved once at plan time from the pre-run checkout and
    frozen for the life of the run (``scope.RunScope``), so attempt two
    runs the identical prompt against the identical snapshot into the
    identical refusal. ``FailureAction.FAIL`` is written for exactly
    that: "a wall retrying can never fix... fail directly without
    burning engineer iterations".

    Reaching here at all means the component got past
    ``factory``'s two pre-engineer refusals, which is the cheap place to
    catch this and where the cost is actually saved. This is the
    backstop for a caller that has neither.

    Any other failing check alongside it still fails rather than
    retries: a readable scope is a precondition for judging the rest, so
    an unreadable one is decisive whatever else also failed. A check
    that is NOT it retries exactly as before.

    Its own function so ``_phase_verify`` spends no cognitive complexity
    on the choice: that method is already over the repo's gate and is
    judged against its own previous value, so ternaries inline there are
    a refusal at commit time.
    """
    for check in failing:
        if check.name == SCOPE_UNREADABLE_CHECK:
            cause = check.details[0] if check.details else check.message
            return FailureAction.FAIL, scope_unreadable_error(cause)
    return FailureAction.RETRY_OR_FAIL, "Mechanical verification failed"


class MechanicalChecks(InboxDesk, AttemptRecorder):
    """Phase 1 support, the acceptance check and the diff of one attempt."""

    def _warn_not_measured(self, comp: Component, verification: VerificationResult) -> None:
        """Say out loud what Phase 1 was asked to measure and could not.

        #306. Not a gate failure: a gap does not reach
        ``verification.passed``, and it is deliberately kept out of
        ``as_context`` too, because no engineer iteration can install a
        missing binary and a retry would burn ``repair_max_runs``
        proving it. Not nothing either: before #306 the row said PASS,
        and omitting the row alone said nothing at all, so a
        permanently broken mutation gate looked exactly like a working
        one. This is the third option, and it is the only place the
        pipeline says it.
        """
        for gap in verification.not_measured:
            self.ui.warn(f"  {comp.id}: {gap.check} not measured ({gap.reason}) - {gap.detail}")

    def _write_gate_logs(
        self,
        comp: Component,
        verification: VerificationResult,
    ) -> tuple[str, ...]:
        """Write each failed gate's output to disk; return the paths (#462).

        One file per failed test / typecheck / lint gate, including one
        that timed out or printed bytes that are not utf-8 (#527), at
        ``.kstrl/debug/<run>/<component>/attempt-<n>/<check>.log``: the
        directory the failure summary, ``ks status`` and the TUI retry
        screen already name as the component's raw outputs, split by
        attempt so a retry does not overwrite the evidence of the attempt
        before it. A write that fails is said out loud and left out of
        the returned paths, so the event never names a file that is not
        there, and it does not change Phase 1's verdict.
        """
        attempt_dir = self._debug_dir_for(comp.id) / f"attempt-{comp.retries + 1}"
        written: list[str] = []
        for check in verification.checks:
            if check.passed or check.output is None:
                continue
            path = attempt_dir / f"{check.name}.log"
            try:
                attempt_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, check.output)
            except OSError as exc:
                self.ui.warn(
                    f"  {comp.id}: could not write the {check.name} output to {path}: {exc}"
                )
                continue
            written.append(str(path))
        return tuple(written)

    def _judged_change(self, comp: Component, wt_path: Path) -> tuple[str, str]:
        """The commit Phase 1 judged and the sha256 of its diff (#646).

        The diff is the one Phase 1 read: ``component_base...HEAD`` in the
        worktree, not ``manifest.base_branch``, so a dependent's item
        describes its own change and not its dependencies'. Either half is
        "" when git cannot answer, and an unreadable diff is said out loud.

        It reads the stored diff (``as_stored=True``, #695): every changed
        byte, with no git setting able to rewrite or empty it, so two
        different changes cannot share a diff_sha because a setting
        printed them alike (``diff.external`` empties every diff).
        """
        head = git.branch_sha(comp.branch_name, self.root_dir) or ""
        try:
            diff = git.get_diff_content(self.component_base(comp.id), wt_path, as_stored=True)
        except git.GitDiffError as exc:
            self.ui.warn(
                f"  {comp.id}: the judged diff could not be read, so no approval applies "
                f"and an item filed now records no diff_sha: {exc}"
            )
            return head, ""
        return head, hashlib.sha256(diff.encode("utf-8", "surrogateescape")).hexdigest()

    def _waivable_evidence(
        self, comp: Component, finding: Finding, change: tuple[str, str]
    ) -> dict[str, Any]:
        """The evidence of a policy_exception item (#595).

        ``waiver_key`` is what an approval of the item covers: exactly
        this finding, in this plan, for this component. ``head_sha`` and
        ``diff_sha`` are the change the operator is shown (#646), from
        :meth:`_judged_change`.
        """
        head_sha, diff_sha = change
        return {
            "category": finding.category,
            "severity": finding.severity,
            "location": finding.location,
            "suggestion": finding.suggestion,
            "explanation": finding.explanation,
            "plan_id": comp.plan_id,
            "waiver_key": self._waiver_scope(comp).key(finding),
            "head_sha": head_sha,
            "diff_sha": diff_sha,
        }

    def _phase_acceptance(
        self, comp: Component, comp_result: ComponentResult, wt_path: Path
    ) -> PhaseFailure | None:
        """The acceptance checks on this head (#700 slices 4, 6 and 7).

        The verdict is written, printed and emitted, then gates the head,
        whether an operator or the verification designer wrote the checks
        (owner decision of 2026-10-07). A failed held-out check halts the component
        with no retry, naming the check by its id alone (decision 3), and
        so does a head nothing a retry could fix was measured on, and so
        does a dispute of a visible check that kstrl accepted (decision 13,
        slice 10a); any other check that did not pass goes to the
        engineer's retry. An approved halt on this head that names every
        failing check passes it (decision 14,
        :func:`kstrl.waivers.covering_override`). The stop lines of a
        designed plan go into the text of the halt and of the last
        attempt's failure only: a retry's reason stays one short line.
        """
        from kstrl.acceptance import judge_head
        from kstrl.acceptance_lines import halt_text

        head = git.get_head_sha(wt_path) or ""
        approvals = self._approvals or ApprovalSnapshot()
        progress = self.base_config.component_progress_file(comp.prd_path, self.root_dir)
        outcome = judge_head(
            self.root_dir,
            self.factory_config,
            comp.id,
            head,
            run_id=self.run_id,
            attempt=comp.retries + 1,
            ui=self.ui,
            overrides=[item for item in approvals.overrides if item.component == comp.id],
            progress=wt_path / progress,
        )
        if outcome is None:
            return None
        for line in outcome.lines:
            self.ui.info(line)
        self.bus.emit(
            event_catalog.VerificationResultEvent(
                component=comp.id,
                passed=outcome.passed,
                checks=outcome.checks,
                failures=outcome.failures,
                phase=ACCEPTANCE_PHASE,
                isolation=outcome.isolation,
            )
        )
        if outcome.passed:
            return None
        failing = ", ".join(outcome.failing)
        if outcome.disputed or outcome.held_out or not outcome.told:
            return PhaseFailure(
                action=FailureAction.FAIL,
                error=halt_text(outcome, head, comp.id),
                phase=ACCEPTANCE_PHASE,
                check=failing,
            )
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        ctx.add_acceptance_failure(outcome.told, attempt=comp.retries + 1)
        # retry_or_fail reads "timeout" in a retry's reason as a killed
        # engineer, and a check's output can say it: the stop lines go only
        # into the failure of the last attempt, which retries nothing.
        last = comp.retries >= self.factory_config.max_retries
        stopped = "".join(f"\n{line}" for line in outcome.stopped) if last else ""
        return PhaseFailure(
            action=FailureAction.RETRY_OR_FAIL,
            error=f"the acceptance checks {failing} did not pass on {head[:12]}{stopped}",
            phase=ACCEPTANCE_PHASE,
            check=failing,
            context_json=ctx.to_json(),
        )

    def _before_gates(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
    ) -> VerifyPhaseResult | None:
        """What Phase 1 returns before any gate runs: the --no-verify skip,
        or a failed worktree setup (#624). None means the gates run."""
        if self.factory_config.skip_verification:
            # R2.3: --no-verify. Previously verify_config=None fell
            # through to VerifyConfig() defaults here and Phase 1 ran
            # anyway - on a non-Python repo that burned every retry
            # against checks that could never pass. The empty
            # VerificationResult below is what downstream reviewers see:
            # no checks ran, none are claimed.
            self.ui.info(
                f"  Phase 1 SKIPPED for {comp.id}: mechanical verification disabled (--no-verify)"
            )
            comp.verification_passed = None
            self._record_phase_skip(
                comp,
                "verify",
                "mechanical verification disabled (--no-verify)",
            )
            return VerifyPhaseResult(
                ran=False,
                verification=VerificationResult(passed=True, checks=[]),
            )
        return self._set_up_gate_worktree(comp, comp_result, wt_path)

    def _set_up_gate_worktree(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
    ) -> VerifyPhaseResult | None:
        """Run the worktree setup before Phase 1 reads the tree (#624).

        Again, after the engineer: its commits may have changed the lockfile
        the setup installs from, and a gate must measure the branch's
        dependencies, never the root checkout's. A failure is an
        infrastructure failure and no gate runs; the retry context carries
        the setup's output, because a lockfile the engineer broke is the
        engineer's to repair. Nothing runs under ``use_worktrees=False``,
        where the tree is the operator's own checkout.
        """
        if not self.factory_config.use_worktrees:
            return None
        error = self.factory_config.worktree_setup(comp.scaffold).prepare(wt_path)
        if not error:
            return None
        headline = error.splitlines()[0]
        self.ui.err(f"  Worktree setup FAILED for {comp.id}, so no Phase 1 gate ran: {headline}")
        self._add_findings(
            comp, [Finding.infrastructure_error(phase="provisioning", explanation=error)]
        )
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        ctx.add_verification_failure(error, attempt=comp.retries + 1, infrastructure=True)
        return VerifyPhaseResult(
            ran=False,
            verification=VerificationResult(passed=False, checks=[]),
            failure=PhaseFailure(
                action=FailureAction.RETRY_OR_FAIL,
                error=f"Worktree setup failed (infrastructure): {headline}",
                phase="provisioning",
                check="worktree_setup",
                context_json=ctx.to_json(),
            ),
        )

    def _phase_diff(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
    ) -> DiffPhaseResult:
        """Fetch the component diff once and share it with every phase
        that consumes one as text: fact-utilization measurement, the
        knowledge distiller, the HITL checkpoint and the PR body.
        Without this each would shell out to `git diff` independently,
        redundantly rebuilding the same patch on every component.

        #266: the review and security phases are no longer consumers.
        They read the worktree they already run in, so this diff is not
        on their path at all, and neither is the size cap that used to
        govern it.

        R1.3 (H-14): a git failure here used to yield "" and every
        consumer silently worked from an empty diff and passed. Now it
        is an infrastructure failure for the component: record the
        infra finding, journal it, and retry/fail closed.
        """
        base = self.component_base(comp.id)
        try:
            shared_diff = git.get_diff_content(
                base,
                wt_path,
            )
        except git.GitDiffError as exc:
            self.ui.err(f"  Diff fetch FAILED for {comp.id}: {exc}")
            self._add_findings(
                comp,
                [
                    Finding.infrastructure_error(
                        phase="diff",
                        explanation=(
                            f"git diff against {base} failed; "
                            f"knowledge distillation and the PR body cannot "
                            f"be built: {exc}"
                        ),
                    )
                ],
            )
            self.bus.emit(event_catalog.DiffFetchFailed(component=comp.id, error=str(exc)))
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_verification_failure(
                f"git diff against {base} failed: {exc}",
                attempt=comp.retries + 1,
                phase="diff",
                infrastructure=True,
            )
            return DiffPhaseResult(
                failure=PhaseFailure(
                    action=FailureAction.RETRY_OR_FAIL,
                    error=f"Diff fetch failed (infrastructure): {exc}",
                    phase="diff",
                    check="git_diff",
                    context_json=ctx.to_json(),
                    signatures=["diff:fetch-failed"],
                )
            )

        # #601: the commit every later gate judges. The ladder counts a
        # merge clean only when GitHub merged exactly this commit.
        comp.judged_sha = git.branch_sha(comp.branch_name, self.root_dir) or ""
        return DiffPhaseResult(diff=shared_diff)
