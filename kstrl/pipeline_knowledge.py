"""Pre-PR knowledge distillation and the fact-utilization measurement (#191)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import event_catalog, git
from kstrl.agents.base import collect_usage
from kstrl.agents.prompt_record import recording_prompts
from kstrl.manifest import Component
from kstrl.pipeline_attempt import AttemptRecorder
from kstrl.pipeline_ledger import UsageLedger

if TYPE_CHECKING:
    from kstrl.factory import ComponentResult


@dataclass(frozen=True)
class FactUtilization:
    """One component's knowledge fact-utilization measurement.

    ``measured=False`` means WE COULD NOT MEASURE. It does not mean the
    engineer referenced nothing. A recorded ``referenced=0`` alongside
    ``measured=True`` is real evidence that injected facts went unused;
    an unmeasured entry is no evidence at all. Collapsing the two is
    what let a broken recorder read as a legitimate zero and left the
    L2+ fact-utilization gate un-evidenceable (#191).

    ``injected`` is a lower bound: the metric is a 30-char
    case-insensitive substring match
    (``knowledge.measure_fact_utilization``).

    The totals are also biased upward in the denominator, which is what
    ``by_tier`` exists to expose: the sibling tier carries
    first-sentence summaries of every OTHER component's facts, and those
    are the claims this component is least likely to echo.
    ``core_referenced / core_injected`` is the sharper ratio - did the
    engineer use what was known about the component it was building?
    """

    measured: bool = False
    injected: int = 0
    referenced: int = 0
    # Per-tier split of the same measurement. These can sum to less than
    # the totals when the prefix carries a section the knowledge module
    # did not write; an unrecognized section is not folded into a real
    # tier.
    core_injected: int = 0
    core_referenced: int = 0
    dependency_injected: int = 0
    dependency_referenced: int = 0
    sibling_injected: int = 0
    sibling_referenced: int = 0
    # Why unmeasured; "" when measured. Journal-only: the event stream
    # carries the top-line numbers, the journal carries the diagnosis
    # and the tier breakdown.
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "measured": self.measured,
            "injected": self.injected,
            "referenced": self.referenced,
            "reason": self.reason,
            "by_tier": {
                "core": {
                    "injected": self.core_injected,
                    "referenced": self.core_referenced,
                },
                "dependency": {
                    "injected": self.dependency_injected,
                    "referenced": self.dependency_referenced,
                },
                "sibling": {
                    "injected": self.sibling_injected,
                    "referenced": self.sibling_referenced,
                },
            },
        }


@dataclass(frozen=True)
class DistillPhaseResult:
    """Pre-PR knowledge distillation outcome. Never fails the component."""

    ran: bool
    skip_reason: str | None = None
    # Deliberately independent of `ran`: fact utilization costs zero
    # tokens, so it is measured and recorded even when distillation was
    # budget-skipped or raised.
    utilization: FactUtilization | None = None


class KnowledgePhase(AttemptRecorder, UsageLedger):
    """The pre-PR distillation and the measurement of the use of injected facts."""

    def record_injected_knowledge(
        self,
        comp_id: str,
        prefix: str | None,
    ) -> None:
        """Record the knowledge prefix handed to ``comp_id``'s engineer.

        Called by the factory at submit time (#191), unconditionally -
        including with ``None`` when retrieval failed or knowledge is
        off. Unconditional is the point: ``_submit_args`` runs once per
        ATTEMPT, so a retry whose capture fails must not silently
        inherit the previous attempt's prefix and measure this
        attempt's diff against it.

        Freezing the prefix here is also what makes the measurement
        honest. The distill phase used to rebuild it, by which time
        ``distill_facts`` had written this run's facts into the store
        and the core tier read them straight back - counting facts the
        engineer never saw, and (since those facts are distilled FROM
        the diff) matching them against it. A parallel sibling writing
        mid-run corrupted the dependency and sibling tiers the same
        way. Neither is reachable from a submit-time snapshot.
        """
        self.injected_knowledge[comp_id] = prefix

    def record_fact_utilization(
        self,
        comp: Component,
        wt_path: Path,
        diff_text: str | None = None,
        *,
        unavailable: str = "",
    ) -> None:
        """Measure, store and emit this attempt's fact utilization (#191).

        Called from ``process_result`` as soon as a diff is obtainable,
        NOT from the distill phase. Distillation runs only after
        verification, review, and security all pass, so measuring there
        sampled successful components exclusively - a component that
        failed any gate had facts injected and may well have used them,
        and the gate never saw it.

        Pass ``diff_text`` when the diff phase already fetched it. Pass
        nothing to have the diff fetched here: a mechanical-verification
        failure means ``_phase_diff`` has not run YET, not that no diff
        exists - the engineer's change is committed in the worktree and
        is exactly what verification just inspected. Excluding those
        components would leave every test, typecheck, and lint failure
        out of the sample, which is most of the failure population. The
        extra ``git diff`` only runs on that path and is cheap next to
        the component run that preceded it.

        ``unavailable`` records a component as unmeasured with a stated
        reason rather than letting it go silently absent; it is used
        when the diff phase itself already failed, so we do not re-run a
        fetch that is known to fail.

        The measurement is free (a substring scan, no LLM call), so it
        sits above every budget guard. Overwrites per attempt, matching
        ``record_injected_knowledge``.
        """
        if not self.knowledge_config.enabled:
            return
        if self.manifest.single_pr:
            # That mode's shared diff carries sibling components'
            # changes, which would inflate `referenced` - the same
            # reason distillation skips it.
            return
        if unavailable:
            self._store_fact_utilization(
                comp,
                FactUtilization(
                    reason=unavailable,
                ),
            )
            return
        if diff_text is None:
            try:
                diff_text = git.get_diff_content(
                    self.component_base(comp.id),
                    wt_path,
                )
            except git.GitDiffError as exc:
                self._store_fact_utilization(
                    comp,
                    FactUtilization(
                        reason=f"diff unavailable: {exc}",
                    ),
                )
                return
        self._store_fact_utilization(
            comp,
            self._measure_utilization(comp, wt_path, diff_text),
        )

    def _store_fact_utilization(
        self,
        comp: Component,
        util: FactUtilization,
    ) -> None:
        """Persist one measurement and put it on the event stream.

        The event is emitted HERE, not from the distill phase, so that
        `events.jsonl` carries the same population `evolution.jsonl`
        does. Emitting it from the distill phase meant a component that
        failed review, or whose distill was budget-skipped or raised,
        landed in the journal with evidence the event stream never saw -
        and #191 requires the ratio in both.
        """
        self.fact_utilization[comp.id] = util
        self.bus.emit(
            event_catalog.FactUtilizationMeasured(
                component=comp.id,
                measured=util.measured,
                injected=util.injected,
                referenced=util.referenced,
                reason=util.reason,
                core_injected=util.core_injected,
                core_referenced=util.core_referenced,
                dependency_injected=util.dependency_injected,
                dependency_referenced=util.dependency_referenced,
                sibling_injected=util.sibling_injected,
                sibling_referenced=util.sibling_referenced,
            )
        )
        if util.measured and util.injected > 0:
            self.ui.info(
                f"  Knowledge utilization: "
                f"{util.referenced}/{util.injected} "
                f"facts referenced in added lines or progress.txt"
                + (
                    f" (core {util.core_referenced}/{util.core_injected})"
                    if util.core_injected
                    else ""
                )
            )

    def _phase_distill(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
        shared_diff: str,
    ) -> DistillPhaseResult:
        """Knowledge distillation: the PRE-PR step (R7.3 decision).

        Voyager-style post-gate write: runs after Phase 2/2.5 succeed
        (or are skipped) but BEFORE the PR merge step pulls main into
        the worktree, so the distilled diff is the component's true
        delta. Placement is deliberate - moving it post-merge would
        hand the distiller a diff polluted by the merge commit and
        break the "true delta" invariant. Non-fatal on any failure.

        In single_pr mode every component shares one branch, which
        means `git diff base...HEAD` for component B also includes
        A's changes - distillation would write facts for B citing
        A's code as evidence. Skip the phase entirely until A2's
        follow-up wires up per-component diff isolation.

        Fact utilization is NOT measured here. It is measured as soon as
        the diff phase produces a diff, so that components which fail
        review or security are still sampled - see
        ``record_fact_utilization``. This phase only reads the stored
        result to put it on the ``DistillResult`` event.
        """
        knowledge_config = self.knowledge_config
        if not knowledge_config.enabled:
            return DistillPhaseResult(
                ran=False,
                skip_reason="knowledge disabled",
            )
        if self.manifest.single_pr:
            self.ui.info(
                f"  Knowledge: skipped for {comp.id} "
                f"(single_pr mode produces a polluted per-component diff)"
            )
            self._record_phase_skip(
                comp,
                "knowledge",
                "single_pr mode produces a polluted per-component diff",
            )
            return DistillPhaseResult(
                ran=False,
                skip_reason=("single_pr mode produces a polluted per-component diff"),
            )
        # Already measured at the diff phase, for every component that
        # got that far - including the ones that then failed review or
        # security and never reach this line. Read, do not re-measure:
        # measuring again here would score the same component twice and
        # re-introduce the ordering hazard that put the measurement
        # after distillation's writes in the first place.
        util = self.fact_utilization.get(comp.id) or FactUtilization()

        if not self.adversarial_budget_ok():
            self.ui.info(f"  Knowledge: skipped for {comp.id} (adversarial budget exhausted)")
            self._record_phase_skip(
                comp,
                "knowledge",
                "adversarial LLM budget exhausted",
            )
            return DistillPhaseResult(
                ran=False,
                skip_reason="adversarial LLM budget exhausted",
                utilization=util,
            )
        breached = self.breached_ceiling()
        if breached is not None:
            # R3.1: the gates all passed before the ceiling tripped, so
            # the component proceeds to PR - but no further LLM spend.
            # The skip is recorded, and the scheduling gate stops any
            # remaining components loudly.
            detail = (
                f"{self.run_usage.total_tokens} >= {self.factory_config.max_total_tokens}"
                if breached == "max_total_tokens"
                else (f"${self.run_usage.cost_usd:.6f} >= ${self.factory_config.max_cost_usd}")
            )
            self.ui.warn(
                f"  Knowledge: skipped for {comp.id} (budget exceeded, {breached}: {detail})"
            )
            self._record_phase_skip(
                comp,
                "knowledge",
                f"budget ({breached}) exceeded",
            )
            return DistillPhaseResult(
                ran=False,
                skip_reason=f"budget ({breached}) exceeded",
                utilization=util,
            )

        self.adversarial_budget_consume()
        distill_agent: Any = None
        try:
            from kstrl.agents import get_agent as _get_agent

            # Reuse the diff already fetched by the diff phase - the
            # worktree state hasn't changed between Phase 1 and here.
            diff_content = shared_diff
            distill_model = knowledge_config.distill_model or self.base_config.model
            distill_agent = _get_agent(
                self.base_config.agent_cmd,
                distill_model,
                self.base_config.model_reasoning_effort,
                self.base_config.agent_type,
                root_dir=self.root_dir,
            )
            distill_start = time.monotonic()
            with (
                self._phase_transcript(comp.id, "distill") as on_line,
                recording_prompts(self._agent_call(comp, "distill")),
            ):
                written, status, parse_failed = self.hooks.distill_facts(
                    distill_agent,
                    comp,
                    diff_content,
                    wt_path / comp.prd_path,
                    comp_result.iterations,
                    self.run_id,
                    knowledge_config.knowledge_root,
                    config=knowledge_config,
                    worktree_path=wt_path,
                    review_passed=comp.review_passed,
                    on_line=on_line,
                )
            self.bus.emit(
                event_catalog.DistillResult(
                    component=comp.id,
                    facts_written=written,
                    parse_failed=parse_failed,
                    duration_seconds=round(
                        time.monotonic() - distill_start,
                        2,
                    ),
                )
            )
            if written > 0:
                self.ui.ok(f"  Knowledge: {status}")
            else:
                self.ui.info(f"  Knowledge: {status}")
        except Exception as exc:  # noqa: BLE001 - non-fatal
            self.ui.warn(f"  Knowledge distillation failed: {exc}")

        # R3.1: distillation spend (recorded even when the distill
        # failed - the call still cost tokens). No fail-the-component
        # checkpoint here: every gate already passed; the scheduling
        # gate halts the run before any FURTHER spend.
        if distill_agent is not None:
            self._record_usage(
                comp.id,
                "distill",
                collect_usage(distill_agent),
            )

        return DistillPhaseResult(ran=True, utilization=util)

    def _measure_utilization(
        self,
        comp: Component,
        wt_path: Path,
        shared_diff: str,
    ) -> FactUtilization:
        """Did the engineer reference any fact we injected? (#191)

        Measured against the prefix the factory recorded at SUBMIT time
        (``record_injected_knowledge``), never a rebuild - see that
        method for why a rebuild reports a corrupted number.

        Costs zero tokens: it is a substring scan, no LLM call. That is
        why the caller runs it above the adversarial-budget and
        cost-ceiling guards and outside the distillation try block. The
        answer to "did the engineer use what we gave it" is in the diff
        and does not depend on distillation succeeding, on the run
        having budget left, or on anything else downstream.

        Never raises. A failure returns ``measured=False`` with the
        cause in ``reason``, which is deliberately NOT the same value
        as a measured zero.
        """
        _MISSING = "\x00missing"
        prefix = self.injected_knowledge.get(comp.id, _MISSING)
        if prefix == _MISSING:
            return FactUtilization(
                reason="no injected prefix recorded for this attempt",
            )
        if prefix is None:
            return FactUtilization(reason="knowledge retrieval failed")
        if not prefix:
            # Knowledge was on and retrieval succeeded with nothing to
            # inject - a cold store. A measured zero, not a failure:
            # "the layer has not warmed up yet" is honest evidence.
            return FactUtilization(measured=True)
        try:
            progress_text = ""
            # Resolved exactly the way the engineer's worker resolved
            # it, so the metric reads the file this component actually
            # wrote. The hardcoded scripts/kstrl/progress.txt this
            # replaced was a second copy of the out-of-scope default
            # and read nothing for every decomposed component.
            progress_path = wt_path / self.base_config.component_progress_file(
                comp.prd_path,
                self.root_dir,
            )
            try:
                progress_text = progress_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # One clause: ``progress_text`` stays "" either way and
                # the metric below degrades to "no evidence", which is
                # the same answer for both causes and is reported as
                # such. #320's separate-remedy rule needs a message to
                # separate; this site emits none.
                pass
            # The diff MUST go in as `diff=`, never as a positional
            # artifact. Artifacts are searched raw; only `diff=` is
            # reduced to added lines. Passing it positionally silently
            # restores the false positive where deleting the code that
            # expressed a fact scores as referencing it - and it does so
            # invisibly, because the signature accepts *artifacts, so
            # neither mypy nor a permissive test stub can see it.
            # TestFactUtilizationUsesTheRealMatcher pins this call shape
            # against the real matcher for exactly that reason.
            util = self.hooks.measure_fact_utilization(
                prefix,
                progress_text,
                diff=shared_diff,
            )
            # .get for the per-tier keys: this is an injected seam, and
            # a hook that only reports the totals must degrade to "no
            # tier breakdown", not blow up the measurement.
            return FactUtilization(
                measured=True,
                injected=int(util["injected"]),
                referenced=int(util["referenced"]),
                core_injected=int(util.get("core_injected", 0)),
                core_referenced=int(util.get("core_referenced", 0)),
                dependency_injected=int(util.get("dependency_injected", 0)),
                dependency_referenced=int(
                    util.get("dependency_referenced", 0),
                ),
                sibling_injected=int(util.get("sibling_injected", 0)),
                sibling_referenced=int(util.get("sibling_referenced", 0)),
            )
        except Exception as exc:  # noqa: BLE001 - non-fatal, never silent
            # Was a bare `except: pass`. Silence made a broken recorder
            # indistinguishable from "the engineer referenced nothing",
            # so the L2+ gate could never accrue and never say why.
            self.ui.warn(f"  Knowledge utilization measurement failed: {exc}")
            return FactUtilization(reason=f"{type(exc).__name__}: {exc}")
