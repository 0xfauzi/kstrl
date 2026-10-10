"""Phase 2.5, the security review."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import event_catalog
from kstrl.agents.base import (
    collect_usage,
)
from kstrl.agents.prompt_record import recording_prompts
from kstrl.context import IterationContext
from kstrl.manifest import (
    Component,
)
from kstrl.pipeline_ledger import UsageLedger
from kstrl.pipeline_transitions import FailureAction, PhaseFailure
from kstrl.pipeline_verdicts import ReviewVerdicts, _gate_failure_count, _produced_a_reading
from kstrl.security import SecurityConfig, SecurityMode, SecurityResult

if TYPE_CHECKING:
    from kstrl.factory import (
        ComponentResult,
    )


@dataclass(frozen=True)
class SecurityPhaseResult:
    """Phase 2.5 outcome. Same rule as ``ReviewPhaseResult``: a skip
    carries a reason, the R10.5 budget refusal carries a ``failure``."""

    ran: bool
    skip_reason: str | None = None
    result: SecurityResult | None = None
    failure: PhaseFailure | None = None

    @property
    def produced_a_reading(self) -> bool:
        return _produced_a_reading(self.ran, self.result)


class SecurityGate(ReviewVerdicts, UsageLedger):
    """Phase 2.5, the security review."""

    def _security_budget_branch(
        self,
        comp: Component,
        sec_config: SecurityConfig,
    ) -> SecurityPhaseResult:
        """Phase 2.5's exhausted-budget branch, both modes.

        Same reason the review side keeps its mode split inside the
        budget check (see ``_phase_review``), and keyed the same way:
        the arm a mode added later would fall into is the REFUSAL, not
        the downgrade that merges a component no security reviewer
        looked at. ``SecurityConfig.__post_init__`` rejects any mode
        outside skip|advisory|hard, and skip already returned above, so
        the two arms are exactly hard and advisory today.
        """
        if sec_config.mode != SecurityMode.ADVISORY.value:
            # R10.5 (#226): same rule as Phase 2. Hard mode refuses to
            # merge a component no security reviewer looked at.
            return SecurityPhaseResult(
                ran=False,
                failure=self._budget_refusal(
                    comp,
                    phase="security",
                    banner="Phase 2.5",
                    role="Security review",
                ),
            )
        self.ui.warn(f"  Phase 2.5 SKIPPED for {comp.id}: adversarial LLM budget exhausted")
        self._record_phase_skip(
            comp,
            "security",
            "adversarial LLM budget exhausted",
        )
        return SecurityPhaseResult(
            ran=False,
            skip_reason="adversarial LLM budget exhausted",
        )

    def _phase_security(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
    ) -> SecurityPhaseResult:
        """Phase 2.5: security review (adversarial pass focused on
        vulns). Runs as a separate LLM call with its own threat-model
        framing so it catches what the correctness reviewer misses.
        Hard-mode fails the component on findings at or above
        SecurityConfig.fail_threshold OR on infrastructure errors."""
        sec_config = self.factory_config.security_config
        if sec_config.mode == SecurityMode.SKIP.value:
            self._record_phase_skip(
                comp,
                "security",
                "security review disabled (mode=skip)",
            )
            return SecurityPhaseResult(
                ran=False,
                skip_reason="security review disabled (mode=skip)",
            )
        if not self.adversarial_budget_ok():
            return self._security_budget_branch(comp, sec_config)
        self.adversarial_budget_consume()
        from kstrl.agents import get_agent as _get_sec_agent

        self.ui.info(f"  Phase 2.5: security review ({sec_config.mode}) for {comp.id}...")
        sec_result = None
        sec_agent: Any = None
        # R7.1: the run-level selection already folded in the
        # explicit sec_config fields and the engineer fallbacks (or
        # picked the cross-family default). sec_config is non-None
        # and non-skip here, so the selection was resolved at run
        # start.
        assert self.security_selection is not None
        adversarial_debug_dir = self._debug_dir_for(comp.id)
        # The try/except deliberately wraps ONLY the agent-driven
        # work (getting the agent + running the review). Errors in
        # the retry-or-fail path below must NOT be swallowed - if
        # they were, a hard-mode security failure could fall through
        # to PR creation as if it had passed.
        try:
            sec_agent = _get_sec_agent(
                self.security_selection.agent_cmd,
                self.security_selection.model,
                self.security_selection.reasoning,
                self.security_selection.agent_type,
                # #266: same rule as Phase 2 - a reviewer must not be
                # able to write the tree it is judging, and the
                # operator's sandbox intent rides alongside.
                sandbox=self.sandbox_config,
                read_only=True,
                root_dir=self.root_dir,
            )
            with (
                self._phase_transcript(comp.id, "security") as on_line,
                recording_prompts(self._agent_call(comp, "security")),
            ):
                sec_result = self.hooks.run_security_review(
                    sec_agent,
                    wt_path / comp.prd_path,
                    wt_path,
                    self.component_base(comp.id),
                    config=sec_config,
                    ui=self.ui,
                    debug_dir=adversarial_debug_dir,
                    on_line=on_line,
                )
        except Exception as exc:  # noqa: BLE001
            # Agent infrastructure failed before run_security_review
            # could classify the outcome. Synthesize an infra result
            # and fall through to the shared recording block below:
            # hard mode blocks via passed=False, advisory continues
            # but the infra finding stays in the findings stream and
            # the PR body instead of vanishing (R1.2, sec-pr-body).
            self.ui.warn(f"  Security review crashed: {exc}")
            sec_result = SecurityResult(
                passed=sec_config.mode != SecurityMode.HARD.value,
                mode=sec_config.mode,
                overall_notes=(f"Security review agent failed before completion: {exc}"),
                infrastructure_error=True,
                # R7.1: a crash before/inside the run is still
                # attributed to the selected reviewer identity.
                reviewer_model=self.security_selection.identity,
            )

        # R3.1: record security spend before pass/fail handling so
        # failed and crashed passes still count toward the meter.
        if sec_agent is not None:
            self._record_usage(comp.id, "security", collect_usage(sec_agent))
        breached = self.breached_ceiling()
        if breached is not None:
            return SecurityPhaseResult(
                ran=True,
                result=sec_result,
                failure=PhaseFailure(
                    action=FailureAction.TOKEN_BUDGET,
                    error=f"budget exceeded ({breached})",
                    phase="security",
                ),
            )

        if sec_result is not None:
            self.bus.emit(
                event_catalog.ReviewResultEvent(
                    component=comp.id,
                    passed=sec_result.passed,
                    mode=f"security-{sec_config.mode}",
                    fail_count=sec_result.fail_count,
                    advisory_count=sec_result.advisory_count,
                    duration_seconds=round(sec_result.duration_seconds, 2),
                )
            )

            # E3: source-of-truth typed findings list, plus the
            # legacy rendered string for PR body / manifest readers.
            self._add_findings(comp, sec_result.as_findings())
            if sec_result.findings:
                if comp.review_findings:
                    comp.review_findings = (
                        comp.review_findings + "\n\n" + sec_result.as_pr_body_section()
                    )
                else:
                    comp.review_findings = sec_result.as_pr_body_section()

            if not sec_result.passed:
                return self._security_failure(comp, comp_result, sec_result)

        return SecurityPhaseResult(ran=True, result=sec_result)

    def _security_failure(
        self,
        comp: Component,
        comp_result: ComponentResult,
        sec_result: SecurityResult,
    ) -> SecurityPhaseResult:
        """Phase 2.5's failure branch, mirroring ``_review_failure``.

        Extracted so the two phases route a failure the same way and in
        the same shape - the coverage wall first, the retry path after -
        rather than one of them being a method and the other twenty
        lines inline in the middle of the phase.
        """
        if sec_result.coverage_refused:
            return SecurityPhaseResult(
                ran=True,
                result=sec_result,
                failure=self._coverage_failure(
                    comp,
                    phase="security",
                    banner="Phase 2.5",
                    role="Security review",
                    noun="findings",
                    agent="security agent",
                    disagreement=sec_result.diffstat_disagreement,
                    reported_a_stat=sec_result.observed_diffstat is not None,
                ),
            )
        reason = (
            "Security review crashed"
            if sec_result.infrastructure_error
            else "Security review failed"
        )
        self.ui.warn(f"  Phase 2.5 FAILED for {comp.id}: {sec_result.fail_count} failures")
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        # as_retry_context is empty for infra results (no findings list);
        # fall back to the notes so the retry prompt still says what
        # went wrong.
        ctx.add_review_finding(
            sec_result.as_retry_context()
            or "Security review infrastructure error: " + sec_result.overall_notes,
            attempt=comp.retries + 1,
            phase="security",
            infrastructure=sec_result.infrastructure_error,
        )
        # R6.1: journal the vuln categories that failed the gate
        # ("security:injection", ...), not the reason.
        from kstrl.evolution import signatures_from_findings

        return SecurityPhaseResult(
            ran=True,
            result=sec_result,
            failure=PhaseFailure(
                action=FailureAction.RETRY_OR_FAIL,
                error=reason,
                phase="security",
                check=("infrastructure" if sec_result.infrastructure_error else "findings"),
                context_json=ctx.to_json(),
                signatures=signatures_from_findings(
                    "security", sec_result.as_findings(), sec_result.failing_severities
                ),
                failure_count=_gate_failure_count(sec_result),
            ),
        )
