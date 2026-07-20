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
    COMPLETED      merge confirmed (or PR flow not configured)

Each phase returns an explicit typed result; ``process_result`` is the
single place that routes a phase failure into a transition. LLM- and
subprocess-heavy phase functions are injected via ``PipelineHooks`` (the
factory resolves them from its own module globals at run start, so the
historical ``patch("ralph_py.factory.run_review")`` seam keeps working),
and ``ralph_py.git`` functions are looked up on the module at call time
for the same reason.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ralph_py import events as ev
from ralph_py import git
from ralph_py.agents.base import UsageTotals, collect_usage
from ralph_py.context import IterationContext, IterationRecord
from ralph_py.findings import Finding, tag_finding_with_attempt
from ralph_py.fixtures import FixturesConfig
from ralph_py.interaction import (
    CheckpointContext,
    InteractionChannel,
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.observability import NotifyHooks
from ralph_py.prd import PRD
from ralph_py.review import ReviewMode, ReviewResult
from ralph_py.security import SecurityMode, SecurityResult
from ralph_py.verify import VerificationResult, VerifyConfig

if TYPE_CHECKING:
    from ralph_py.config import RalphConfig
    from ralph_py.factory import (
        AdversarialAgentSelection,
        ComponentResult,
        FactoryConfig,
        FactoryResult,
    )
    from ralph_py.knowledge import KnowledgeConfig
    from ralph_py.ui.base import UI


# PR A: the E6 checkpoint shows a real diff excerpt, not just the
# review summary string. Bounded so a huge diff cannot flood the modal.
CHECKPOINT_DIFF_CHAR_LIMIT = 20_000


def _iso_now() -> str:
    """Current UTC time as ISO 8601, matching the manifest timestamps."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Transition(Enum):
    """Terminal disposition of one ``process_result`` pass."""

    RETRYING = "retrying"
    FAILED = "failed"
    MERGE_PENDING = "merge_pending"
    COMPLETED = "completed"


class FailureAction(Enum):
    """How a phase failure must be routed (the single transition point)."""

    # Normal gate failure: retry with context, or FAILED once exhausted.
    RETRY_OR_FAIL = "retry_or_fail"
    # A wall retrying can never fix (R1.4 chunk-budget insufficiency):
    # fail directly without burning engineer iterations.
    FAIL = "fail"
    # R3.1 token budget: fail loudly via the budget path (synthetic
    # finding + budget_exceeded event), never silently degrade.
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


@dataclass(frozen=True)
class VerifyPhaseResult:
    """Phase 1 outcome. ``ran=False`` means --no-verify skipped it."""

    ran: bool
    verification: VerificationResult
    failure: PhaseFailure | None = None


@dataclass(frozen=True)
class DiffPhaseResult:
    """Shared diff fetch + R1.4 hard-mode chunking decision."""

    diff: str = ""
    review_diff: str = ""
    chunks: list[str] | None = None
    failure: PhaseFailure | None = None


@dataclass(frozen=True)
class ReviewPhaseResult:
    """Phase 2 outcome. ``ran=False`` records the skip reason."""

    ran: bool
    skip_reason: str | None = None
    result: ReviewResult | None = None
    failure: PhaseFailure | None = None


@dataclass(frozen=True)
class SecurityPhaseResult:
    """Phase 2.5 outcome. ``ran=False`` records the skip reason."""

    ran: bool
    skip_reason: str | None = None
    result: SecurityResult | None = None
    failure: PhaseFailure | None = None


@dataclass(frozen=True)
class DistillPhaseResult:
    """Pre-PR knowledge distillation outcome. Never fails the component."""

    ran: bool
    skip_reason: str | None = None


class CheckpointDecision(Enum):
    """E6 human-in-the-loop checkpoint outcome."""

    NOT_PROMPTED = "not_prompted"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETRY = "retry"


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


@dataclass(frozen=True)
class PipelineHooks:
    """Injected phase functions (LLM / subprocess seams).

    The factory resolves these from its module globals when the run
    starts, so tests patching ``ralph_py.factory.run_review`` (and
    friends) keep intercepting them; pipeline unit tests inject stubs
    directly.
    """

    run_mechanical_verification: Callable[..., VerificationResult]
    run_review: Callable[..., ReviewResult]
    run_chunked_review: Callable[..., ReviewResult]
    run_security_review: Callable[..., SecurityResult]
    run_chunked_security_review: Callable[..., SecurityResult]
    distill_facts: Callable[..., tuple[int, str]]
    build_knowledge_context: Callable[..., str]
    measure_fact_utilization: Callable[..., dict[str, int]]
    cleanup_worktree: Callable[[str, Path, str], None]


class ComponentPipeline:
    """Drives one component result through the phase chain and owns every
    component state transition (R7.3).

    Shared mutable structures (``worktree_paths``, ``component_contexts``,
    ``fresh_base_retry_ids``, ``component_failure_signatures``,
    ``factory_result``) are passed in by the factory and shared with its
    scheduler; the pipeline is the only writer for transition-related
    fields, the scheduler for provisioning-related ones.
    """

    def __init__(
        self,
        *,
        manifest: Manifest,
        manifest_path: Path,
        factory_config: FactoryConfig,
        base_config: RalphConfig,
        ui: UI,
        root_dir: Path,
        run_id: str,
        bus: ev.EventBus,
        journal_path: Path | None,
        run_paths: ev.RunPaths | None = None,
        interaction: InteractionChannel | None = None,
        notify: NotifyHooks,
        review_selection: AdversarialAgentSelection,
        security_selection: AdversarialAgentSelection | None,
        knowledge_config: KnowledgeConfig,
        factory_result: FactoryResult,
        hooks: PipelineHooks,
        worktree_paths: dict[str, Path],
        component_contexts: dict[str, str],
        fresh_base_retry_ids: set[str],
        component_failure_signatures: dict[str, list[str]],
    ) -> None:
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.factory_config = factory_config
        self.base_config = base_config
        self.ui = ui
        self.root_dir = root_dir
        self.run_id = run_id
        self.bus = bus
        self.journal_path = journal_path
        self.run_paths = run_paths
        # PR A: the interaction seam. Defaults to today's terminal
        # behavior; embedded mode (PR F) injects a QueueInteractionChannel.
        self.interaction: InteractionChannel = (
            interaction if interaction is not None else UiInteractionChannel(ui)
        )
        self.notify = notify
        self.review_selection = review_selection
        self.security_selection = security_selection
        self.knowledge_config = knowledge_config
        self.factory_result = factory_result
        self.hooks = hooks
        self.worktree_paths = worktree_paths
        self.component_contexts = component_contexts
        self.fresh_base_retry_ids = fresh_base_retry_ids
        self.component_failure_signatures = component_failure_signatures

        # R3.1 cost meter: per-component, per-phase usage rollup plus a
        # run-level total. Phases: "engineer" (loop iterations, reported
        # by the worker), "review", "security", "distill" (fresh agent
        # instance per phase, so an instance's accumulated usage_records
        # ARE that phase's spend - chunked reviews reuse one instance for
        # N calls and land here as N records). Retried attempts
        # accumulate: every attempt cost real tokens, so the meter never
        # forgets a failed attempt.
        self.usage_meter: dict[str, dict[str, UsageTotals]] = {}
        self.run_usage = UsageTotals()

        # E4: adversarial-call counter shared across review / security /
        # knowledge phases. When max_adversarial_calls is 0 the budget is
        # unbounded; otherwise the LLM phase is skipped once the budget
        # is exhausted, with an informational log line. R1.4 exception:
        # hard-mode chunked reviews never skip-on-exhausted; they either
        # cover every chunk (one call each) or fail the component as an
        # infrastructure error.
        self._adversarial_calls = 0

        # R6.4: monotonic start of each component's current attempt, so
        # the recorded duration covers the whole attempt (engineer loop +
        # verify + review + security + PR flow), not just the engineer
        # loop, and backstop-timeout failures stop recording 0.0.
        self._attempt_started_monotonic: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Budget + usage accounting
    # ------------------------------------------------------------------

    def adversarial_budget_ok(self) -> bool:
        cap = self.factory_config.max_adversarial_calls
        if cap <= 0:
            return True
        return self._adversarial_calls < cap

    def adversarial_budget_consume(self) -> None:
        self._adversarial_calls += 1

    def adversarial_budget_remaining(self) -> int | None:
        """Calls left in the budget, or None when unbounded (R1.4:
        chunked reviews need one call per chunk and must know whether
        the whole diff can be covered before starting)."""
        cap = self.factory_config.max_adversarial_calls
        if cap <= 0:
            return None
        return max(0, cap - self._adversarial_calls)

    def _record_usage(
        self, comp_id: str, phase: str, totals: UsageTotals,
    ) -> None:
        if totals.calls == 0:
            return
        slot = self.usage_meter.setdefault(comp_id, {}).setdefault(
            phase, UsageTotals(),
        )
        slot.merge(totals)
        self.run_usage.merge(totals)
        self.bus.emit(ev.ComponentUsage(
            component=comp_id, phase=phase, **totals.to_dict(),
        ))

    def usage_totals_for(self, comp_id: str) -> UsageTotals:
        """One component's spend across all phases (PR A: shown at the
        E6 checkpoint so the human sees what the attempt cost)."""
        totals = UsageTotals()
        for phase_totals in self.usage_meter.get(comp_id, {}).values():
            totals.merge(phase_totals)
        return totals

    def token_budget_exceeded(self) -> bool:
        cap = self.factory_config.max_total_tokens
        return cap > 0 and self.run_usage.total_tokens >= cap

    # ------------------------------------------------------------------
    # Attempt lifecycle + evidence pointers (R3.3)
    # ------------------------------------------------------------------

    def _journal_offset(self) -> int:
        """Current byte size of the v1 progress log; used to bracket one
        attempt's slice of events (R3.3). -1 when no real progress log
        is configured for this run. Deliberately pegged to the v1 compat
        file, NOT events.jsonl - the manifest's journal_offset_start/end
        semantics must not silently repoint (plan: explicit future
        schema decision)."""
        if self.journal_path is None:
            return -1
        try:
            return (
                self.journal_path.stat().st_size
                if self.journal_path.exists() else 0
            )
        except OSError:
            return -1

    @contextmanager
    def _phase_transcript(
        self, comp_id: str, phase: str,
    ) -> Iterator[Callable[[str], None] | None]:
        """Line writer onto RunPaths.phase_log for one phase invocation.

        Yields None when no run dir is configured (progress logging
        disabled) or the file cannot be opened - transcripts are
        observability and must never gate a phase (chunk 4). Repeated
        invocations (retries, chunked passes) append.
        """
        if self.run_paths is None:
            yield None
            return
        path = self.run_paths.phase_log(comp_id, phase)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a", buffering=1, encoding="utf-8")
        except OSError:
            yield None
            return
        def _write_line(line: str) -> None:
            fh.write(line + "\n")

        try:
            yield _write_line
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def _phase_started(self, comp: Component, phase: str) -> float:
        """Emit the authoritative phase bracket opener; returns the
        monotonic start for the matching _phase_completed."""
        self.bus.emit(ev.PhaseStarted(
            component=comp.id, phase=phase, attempt=comp.retries + 1,
        ))
        return time.monotonic()

    def _phase_completed(
        self, comp: Component, phase: str, started: float,
        passed: bool, detail: str = "",
    ) -> None:
        self.bus.emit(ev.PhaseCompleted(
            component=comp.id, phase=phase, passed=passed, detail=detail,
            duration_seconds=round(time.monotonic() - started, 2),
        ))

    def _debug_dir_for(self, comp_id: str) -> Path:
        """Forensic raw-output dir for this run's component (R1.2)."""
        return self.root_dir / ".ralph" / "debug" / self.run_id / comp_id

    def _add_findings(
        self, comp: Component, new_findings: list[Finding],
    ) -> None:
        """Append findings tagged ``attempt:<n>`` for the attempt in
        flight (R3.3), so the journal can attribute every finding to the
        attempt that produced it."""
        attempt = comp.retries + 1
        comp.findings.extend(
            tag_finding_with_attempt(f, attempt) for f in new_findings
        )
        # Chunk 4: stream each finding as a typed event the moment it is
        # recorded (the manifest only carries them at transition time).
        for finding in new_findings:
            self.bus.emit(ev.FindingRecorded(
                component=comp.id,
                phase=finding.phase,
                category=finding.category,
                severity=finding.severity,
                location=finding.location,
                explanation=finding.explanation,
                attempt=attempt,
            ))

    def begin_attempt(self, comp: Component) -> None:
        """PENDING -> RUNNING transition for one attempt (R3.3).

        The prior attempt's findings were journaled when its retry was
        scheduled (or by record_run when a previous run ended), so the
        manifest carries only the current attempt's stream; the failure
        and evidence pointers likewise describe only the attempt in
        flight."""
        comp.findings = []
        comp.review_findings = ""
        comp.failed_phase = ""
        comp.failed_check = ""
        comp.completed_at = ""
        comp.evidence_worktree = ""
        comp.evidence_debug_dir = ""
        comp.journal_offset_start = self._journal_offset()
        comp.journal_offset_end = -1
        comp.status = ComponentStatus.RUNNING.value
        comp.started_at = _iso_now()
        self.component_failure_signatures.pop(comp.id, None)
        self._attempt_started_monotonic[comp.id] = time.monotonic()

    def _end_attempt(self, comp: Component) -> None:
        """Stamp the attempt's evidence pointers when it stops running:
        the progress-log slice end, and the debug dir when any phase
        dumped raw output there (R3.3). Also stamp the attempt's full
        wall-clock duration (R6.4): every terminal transition (retry,
        fail, merge-pending, completed, scheduler backstop) routes
        through here, so duration_seconds covers engineer + verify +
        review + security + PR instead of the engineer loop only."""
        comp.journal_offset_end = self._journal_offset()
        started = self._attempt_started_monotonic.get(comp.id)
        if started is not None:
            comp.duration_seconds = time.monotonic() - started
        debug_dir = self._debug_dir_for(comp.id)
        if debug_dir.exists():
            comp.evidence_debug_dir = str(debug_dir)

    def journal_superseded_findings(self, comp: Component) -> None:
        """A scheduled retry supersedes the current attempt's findings.
        Record them in the evolution journal (attempt-tagged) before the
        next attempt clears the manifest stream, so superseded and
        shipped findings stay distinguishable (R3.3). The final
        attempt's findings reach the journal via record_run instead.
        Non-fatal on I/O errors, matching _record_contract_event."""
        if not comp.findings:
            return
        from ralph_py.evolution import JOURNAL_SCHEMA_VERSION, EvolutionConfig

        evo_config = EvolutionConfig.load(self.root_dir)
        if not evo_config.enabled:
            return
        entry = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "timestamp": _iso_now(),
            "run_id": self.run_id,
            "project": self.manifest.project_name,
            "component_id": comp.id,
            "event_type": "findings_superseded",
            "attempt": comp.retries + 1,
            "failure_signatures": self.component_failure_signatures.get(
                comp.id, [],
            ),
            "findings": [f.to_dict() for f in comp.findings],
        }
        try:
            evo_config.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(evo_config.journal_path, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as exc:
            # Evolution recording is non-fatal, but never silent (R6.1).
            self.ui.warn(
                f"  Evolution journal write failed (non-fatal): {exc}"
            )

    # ------------------------------------------------------------------
    # Transitions (the single place component state moves)
    # ------------------------------------------------------------------

    def _record_failure_signatures(
        self,
        comp: Component,
        phase: str,
        error: str,
        signatures: list[str] | None,
    ) -> None:
        """R6.1: remember the structured signatures for this failure so
        record_run journals real "<check>:<code>" identifiers instead of
        re-deriving a degenerate slug from the flattened error string.
        Sites without parser-level codes fall back to a slug of the
        error text under the failing phase."""
        from ralph_py.evolution import signature_for_error

        if signatures:
            self.component_failure_signatures[comp.id] = list(signatures)
        else:
            self.component_failure_signatures[comp.id] = [
                signature_for_error(phase or "unknown", error),
            ]

    def retry_or_fail(
        self,
        comp: Component,
        error: str,
        context_json: str | None,
        phase: str = "",
        check: str = "",
        signatures: list[str] | None = None,
        fresh_base: bool = False,
    ) -> Transition:
        """Retry a component or mark it as failed. ``phase``/``check``
        name the gate that fired (R3.3); on a retry they describe the
        superseded attempt until the next attempt clears them.
        ``signatures`` are the structured failure signatures (R6.1).
        ``fresh_base=True`` (R7.5 merge-conflict doctrine) forces the
        retry to recreate the worktree AND branch from the freshly
        merged base instead of resuming the attempt's commits."""
        self._record_failure_signatures(comp, phase, error, signatures)
        if comp.retries < self.factory_config.max_retries:
            if fresh_base and self.factory_config.use_worktrees:
                self.fresh_base_retry_ids.add(comp.id)
                error = (
                    error
                    + " [conflict retry: component re-run against the "
                    "freshly merged base; agent output is not rebased]"
                )
            # A timeout failure means the agent was killed mid-flight: the
            # worktree/branch state cannot be trusted. Note the hygiene
            # behavior in the error string so the audit trail explains why
            # the retry does not resume from the killed attempt's commits.
            elif "timeout" in error.lower() and self.factory_config.use_worktrees:
                self.fresh_base_retry_ids.add(comp.id)
                error = (
                    error
                    + " [timeout retry: worktree recreated from base; "
                    "stale index.lock removed]"
                )
            # R3.3: journal this attempt's findings as superseded BEFORE
            # the retry counter moves (the tag and the journal entry
            # must agree on the attempt number), then stamp the
            # attempt's evidence pointers.
            self.journal_superseded_findings(comp)
            self._end_attempt(comp)
            comp.failed_phase = phase
            comp.failed_check = check
            comp.retries += 1
            comp.status = ComponentStatus.PENDING.value
            comp.error = error
            if context_json:
                self.component_contexts[comp.id] = context_json
            self.bus.emit(ev.ComponentRetrying(
                component=comp.id, attempt=comp.retries, reason=error,
            ))
            self.ui.info(
                f"  Retrying '{comp.id}' "
                f"(attempt {comp.retries}/{self.factory_config.max_retries}): "
                f"{error[:80]}"
            )
            time.sleep(self.factory_config.retry_delay)
            self.manifest.save(self.manifest_path)
            return Transition.RETRYING
        return self.fail(
            comp, error, phase=phase, check=check, signatures=signatures,
        )

    def fail_aborted(self, comp_id: str, reason: str) -> None:
        """PR B: a shutdown aborted this component's in-flight attempt.
        Recorded as a plain FAILED with phase="aborted" so a resume can
        retry it; distinct from every organic failure signature."""
        comp = self.manifest.get_component(comp_id)
        if comp is None:
            return
        self.fail(
            comp, f"aborted: {reason}",
            phase="aborted", check="shutdown",
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
        conditions a retry can never fix (R1.4: chunked-review budget
        insufficiency - the adversarial budget only shrinks, so
        re-running the engineer would burn LLM calls to hit the same
        wall); retry_or_fail routes here once retries are exhausted."""
        self._record_failure_signatures(comp, phase, error, signatures)
        comp.status = ComponentStatus.FAILED.value
        comp.error = error
        comp.completed_at = _iso_now()
        comp.failed_phase = phase
        comp.failed_check = check
        self._end_attempt(comp)
        skipped = self.manifest.cascade_skip(comp.id)
        self.factory_result.failed.append(comp.id)
        self.factory_result.skipped.extend(skipped)
        self.bus.emit(ev.ComponentFailed(component=comp.id, error=error))
        self.notify.fire_first_failure(comp.id, error)
        self.ui.err(f"  Failed: {comp.id}: {error[:80]}")
        self.manifest.save(self.manifest_path)
        return Transition.FAILED

    def fail_for_budget(self, comp: Component, phase: str) -> Transition:
        """R3.1: halt LOUDLY on a blown token budget. Mirrors the R1.2/
        R1.4 synthetic-finding pattern (chunk_budget_insufficient): a
        typed Finding in the stream, a progress-log event, and a FAILED
        component - never a silent degrade. Retrying cannot un-spend
        tokens, so this fails directly instead of burning retries."""
        error = (
            f"token budget exceeded: {self.run_usage.total_tokens} total "
            f"tokens recorded >= max_total_tokens "
            f"({self.factory_config.max_total_tokens}); halting instead of "
            "spending further (R3.1)"
        )
        self.ui.err(f"  TOKEN BUDGET EXCEEDED for {comp.id}: {error}")
        self._add_findings(comp, [Finding.infrastructure_error(
            phase=phase, explanation=error,
        )])
        self.bus.emit(ev.BudgetExceeded(
            component=comp.id,
            total_tokens=self.run_usage.total_tokens,
            max_total_tokens=self.factory_config.max_total_tokens,
        ))
        return self.fail(
            comp, error, phase=phase, check="token_budget",
            signatures=["token_budget:exceeded"],
        )

    def complete(
        self, comp: Component, duration_seconds: float, iterations: int,
    ) -> Transition:
        """VERIFYING -> COMPLETED: every gate passed (and the PR merge,
        when configured, was confirmed)."""
        comp.status = ComponentStatus.COMPLETED.value
        comp.error = ""
        self.component_failure_signatures.pop(comp.id, None)
        comp.completed_at = _iso_now()
        self._end_attempt(comp)
        self.factory_result.completed.append(comp.id)
        self.bus.emit(ev.ComponentCompleted(
            component=comp.id, duration_seconds=duration_seconds,
            iterations=iterations,
        ))
        self.ui.ok(
            f"  COMPLETED: {comp.id} "
            f"({iterations} iterations, "
            f"{duration_seconds:.0f}s)"
        )
        self.manifest.save(self.manifest_path)
        return Transition.COMPLETED

    def _park_merge_pending(
        self, comp: Component, error: str,
    ) -> Transition:
        """VERIFYING -> MERGE_PENDING: the PR merge was initiated but not
        confirmed (R0.2). Parked, not terminal: no completed_at, but the
        attempt's journal slice is closed (R3.3) - after the
        merge_pending event so the slice includes it."""
        comp.status = ComponentStatus.MERGE_PENDING.value
        comp.error = error
        # Richer v2 event first; the v1-parity twin keeps progress.jsonl
        # unchanged (the reducer prefers the v2 event, chunk 2).
        self.bus.emit(ev.PrMergePending(
            component=comp.id, pr_url=comp.pr_url, error=comp.error,
        ))
        self.bus.emit(ev.MergePendingV1(
            component=comp.id, pr_url=comp.pr_url, error=comp.error,
        ))
        self.notify.fire_merge_pending(comp.id, comp.error)
        self._end_attempt(comp)
        self.ui.warn(
            f"  MERGE PENDING: {comp.id}: {comp.error}; "
            f"dependents stay blocked; a factory re-run "
            f"re-polls the PR"
        )
        self.manifest.save(self.manifest_path)
        return Transition.MERGE_PENDING

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
        from ralph_py.pr import close_pr_for_rerun, pr_number_from_url

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
                pr_number, comp.branch_name, self.root_dir,
            )
            if close_error:
                # Non-fatal: the re-run's own push fails loudly if the
                # remote branch is still in the way.
                self.ui.warn(
                    f"  Conflicting-PR cleanup incomplete (non-fatal): "
                    f"{close_error}"
                )
        comp.pr_number = None
        comp.pr_url = ""
        ctx = IterationContext.from_json(comp_result.context_json or "{}")
        ctx.add_iteration(IterationRecord(
            iteration=comp_result.iterations,
            success=False,
            error=(
                "The previous attempt's PR hit a merge conflict with the "
                "base branch; this attempt starts from the freshly merged "
                "base, which already contains the sibling changes"
            ),
        ))
        return self.retry_or_fail(
            comp, error, ctx.to_json(),
            phase="pr", check="merge_conflict",
            signatures=["pr:merge-conflict"],
            fresh_base=True,
        )

    def _fail_pr_flow(self, comp: Component, error: str) -> Transition:
        """VERIFYING -> FAILED on a push/create/merge failure (R0.2:
        COMPLETED requires a CONFIRMED merge)."""
        comp.status = ComponentStatus.FAILED.value
        comp.error = error
        comp.completed_at = _iso_now()
        comp.failed_phase = "pr"
        comp.failed_check = "pr_flow"
        self._record_failure_signatures(comp, "pr", comp.error, None)
        self._end_attempt(comp)
        skipped = self.manifest.cascade_skip(comp.id)
        self.factory_result.failed.append(comp.id)
        self.factory_result.skipped.extend(skipped)
        self.bus.emit(ev.ComponentFailed(component=comp.id, error=comp.error))
        self.notify.fire_first_failure(comp.id, comp.error)
        self.ui.err(f"  Failed: {comp.id}: {comp.error[:120]}")
        self.manifest.save(self.manifest_path)
        return Transition.FAILED

    def fail_scheduler_backstop(
        self, comp_id: str, backstop_seconds: float,
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
                timed_out_comp.evidence_worktree = str(
                    self.worktree_paths[comp_id]
                )
            skipped = self.manifest.cascade_skip(comp_id)
            self.factory_result.failed.append(comp_id)
            self.factory_result.skipped.extend(skipped)
            self.bus.emit(ev.ComponentFailed(
                component=comp_id, error="component timeout",
            ))
            self.notify.fire_first_failure(comp_id, "component timeout")
        self.ui.err(
            f"  Failed: {comp_id}: component timeout "
            f"(scheduler backstop after {backstop_seconds:.0f}s)"
        )
        self.ui.warn(
            f"  A worker process for '{comp_id}' may be leaked; "
            f"its worktree is left in place"
        )
        self.manifest.save(self.manifest_path)

    def repoll_merge_pending(self) -> None:
        """R0.2 crash recovery: MERGE_PENDING is re-pollable, not failed.

        A prior run initiated the merge but could not confirm it; check
        the PR state again before scheduling so confirmed merges unblock
        their dependents (MERGE_PENDING -> COMPLETED, or -> FAILED when
        the PR was closed without merging)."""
        merge_pending_comps = [
            c for c in self.manifest.components
            if c.status == ComponentStatus.MERGE_PENDING.value
        ]
        if not merge_pending_comps:
            return
        from ralph_py.pr import (
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
                    self.ui.warn(
                        f"  Cannot re-poll '{comp.id}': no PR number recorded"
                    )
                    continue
                self.ui.info(
                    f"  Re-polling merge state for '{comp.id}' "
                    f"(PR #{pr_number})..."
                )
                merge_state = wait_for_merge(
                    pr_number, self.root_dir,
                    timeout=self.factory_config.merge_timeout,
                )
                if merge_state == "merged":
                    git.fetch_base_branch(
                        self.manifest.base_branch, self.root_dir,
                    )
                    comp.status = ComponentStatus.COMPLETED.value
                    comp.error = ""
                    self.component_failure_signatures.pop(comp.id, None)
                    comp.completed_at = _iso_now()
                    self.factory_result.completed.append(comp.id)
                    self.bus.emit(ev.ComponentCompleted(
                        component=comp.id,
                        duration_seconds=comp.duration_seconds,
                        iterations=comp.iteration_count,
                    ))
                    self.ui.ok(
                        f"  PR #{pr_number} merged; '{comp.id}' completed"
                    )
                elif merge_state == "closed":
                    comp.status = ComponentStatus.FAILED.value
                    comp.error = f"PR #{pr_number} closed without merge"
                    comp.completed_at = _iso_now()
                    comp.failed_phase = "pr"
                    comp.failed_check = "pr_closed"
                    self.component_failure_signatures[comp.id] = [
                        "pr:closed-without-merge",
                    ]
                    skipped = self.manifest.cascade_skip(comp.id)
                    self.factory_result.failed.append(comp.id)
                    self.factory_result.skipped.extend(skipped)
                    self.bus.emit(ev.ComponentFailed(
                        component=comp.id, error=comp.error,
                    ))
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

    def _record_phase_skip(
        self, comp: Component, phase: str, reason: str,
    ) -> None:
        """R1.2: a phase that never ran must leave a trace in both
        the findings stream and the journal, so "ran clean" and
        "never ran" are distinguishable downstream."""
        self._add_findings(comp, [Finding.phase_skipped(phase, reason)])
        self.bus.emit(ev.PhaseSkipped(
            component=comp.id, phase=phase, reason=reason,
        ))

    def process_result(
        self, comp_id: str, comp_result: ComponentResult,
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

        # Record timing
        comp.duration_seconds = comp_result.duration_seconds
        comp.iteration_count = comp_result.iterations

        # Engineer bracket closer: PhaseStarted(engineer) was emitted by
        # the scheduler at submit time; the worker's exit lands here.
        self.bus.emit(ev.PhaseCompleted(
            component=comp_id, phase="engineer",
            passed=comp_result.success,
            detail=comp_result.error or "",
            duration_seconds=round(comp_result.duration_seconds, 2),
        ))

        # R3.1: engineer-loop spend counts BEFORE the success branch -
        # failed attempts cost real tokens too.
        if comp_result.usage is not None:
            self._record_usage(comp_id, "engineer", comp_result.usage)

        # R3.1 budget checkpoint: the engineer loop just reported the
        # dominant spend; halt before starting adversarial phases (or a
        # retry) when the run-level cap is blown.
        if self.token_budget_exceeded():
            return PipelineOutcome(
                transition=self.fail_for_budget(comp, "engineer"),
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
                self.bus.emit(ev.CircuitBreakerTripped(
                    component=comp_id, iterations=comp_result.iterations,
                    error=error,
                ))
                return PipelineOutcome(
                    transition=self.fail(
                        comp, error,
                        phase="engineer", check="no_progress_breaker",
                        signatures=["engineer:no-progress-stall"],
                    ),
                )
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_iteration(IterationRecord(
                iteration=comp_result.iterations,
                success=False,
                error=comp_result.error,
            ))
            return PipelineOutcome(
                transition=self.retry_or_fail(
                    comp, comp_result.error or "Unknown error", ctx.to_json(),
                    phase="engineer", check="loop",
                ),
            )

        wt_path = self.worktree_paths.get(comp_id, self.root_dir)

        # PHASE 1: Mechanical verification
        comp.status = ComponentStatus.VERIFYING.value
        self.manifest.save(self.manifest_path)

        t0 = self._phase_started(comp, "verify")
        verify = self._phase_verify(comp, comp_result, wt_path)
        self._phase_completed(
            comp, "verify", t0, verify.failure is None,
            verify.failure.error if verify.failure else "",
        )
        if verify.failure is not None:
            return PipelineOutcome(
                transition=self._route_failure(comp, verify.failure),
                verify=verify,
            )

        t0 = self._phase_started(comp, "diff")
        diff = self._phase_diff(comp, comp_result, wt_path)
        self._phase_completed(
            comp, "diff", t0, diff.failure is None,
            diff.failure.error if diff.failure else "",
        )
        if diff.failure is not None:
            return PipelineOutcome(
                transition=self._route_failure(comp, diff.failure),
                verify=verify, diff=diff,
            )

        # PHASE 2: Second-opinion review
        t0 = self._phase_started(comp, "review")
        review = self._phase_review(
            comp, comp_result, wt_path, verify.verification,
            diff.review_diff, diff.chunks,
        )
        self._phase_completed(
            comp, "review", t0, review.failure is None,
            review.failure.error if review.failure else "",
        )
        if review.failure is not None:
            return PipelineOutcome(
                transition=self._route_failure(comp, review.failure),
                verify=verify, diff=diff, review=review,
            )

        # PHASE 2.5: Security review
        t0 = self._phase_started(comp, "security")
        security = self._phase_security(
            comp, comp_result, wt_path, diff.review_diff, diff.chunks,
        )
        self._phase_completed(
            comp, "security", t0, security.failure is None,
            security.failure.error if security.failure else "",
        )
        if security.failure is not None:
            return PipelineOutcome(
                transition=self._route_failure(comp, security.failure),
                verify=verify, diff=diff, review=review, security=security,
            )

        # Knowledge distillation: a NAMED PRE-PR step (R7.3 decision).
        t0 = self._phase_started(comp, "distill")
        distill = self._phase_distill(comp, comp_result, wt_path, diff.diff)
        self._phase_completed(
            comp, "distill", t0, True, distill.skip_reason or "",
        )

        # HITL checkpoint + PR create/merge (per-component PR mode only).
        checkpoint = CheckpointDecision.NOT_PROMPTED
        pr = PrPhaseResult(disposition=PrDisposition.SKIPPED)
        if self.factory_config.create_prs and not self.factory_config.single_pr:
            checkpoint = self._phase_checkpoint(
                comp, diff_text=diff.review_diff,
            )
            if checkpoint == CheckpointDecision.REJECTED:
                return PipelineOutcome(
                    transition=self.fail(
                        comp, "Rejected at HITL checkpoint",
                        phase="pr", check="hitl_reject",
                    ),
                    verify=verify, diff=diff, review=review,
                    security=security, distill=distill,
                    checkpoint=checkpoint,
                )
            if checkpoint == CheckpointDecision.RETRY:
                ctx = IterationContext.from_json(
                    comp_result.context_json or "{}",
                )
                ctx.add_review_finding(
                    "Human reviewer requested changes at PR checkpoint",
                )
                return PipelineOutcome(
                    transition=self.retry_or_fail(
                        comp,
                        "Retry requested at HITL checkpoint",
                        ctx.to_json(), phase="pr", check="hitl_retry",
                    ),
                    verify=verify, diff=diff, review=review,
                    security=security, distill=distill,
                    checkpoint=checkpoint,
                )

            t0 = self._phase_started(comp, "pr")
            pr = self._phase_pr(comp)
            self._phase_completed(
                comp, "pr", t0,
                pr.disposition in (
                    PrDisposition.MERGED, PrDisposition.NO_GH,
                    PrDisposition.SKIPPED,
                ),
                pr.error,
            )
            if pr.disposition == PrDisposition.CONFLICT:
                return PipelineOutcome(
                    transition=self._retry_after_merge_conflict(
                        comp, comp_result, pr,
                    ),
                    verify=verify, diff=diff, review=review,
                    security=security, distill=distill,
                    checkpoint=checkpoint, pr=pr,
                )
            if pr.disposition == PrDisposition.MERGE_PENDING:
                return PipelineOutcome(
                    transition=self._park_merge_pending(comp, pr.error),
                    verify=verify, diff=diff, review=review,
                    security=security, distill=distill,
                    checkpoint=checkpoint, pr=pr,
                )
            if pr.disposition == PrDisposition.FAILED:
                return PipelineOutcome(
                    transition=self._fail_pr_flow(comp, pr.error),
                    verify=verify, diff=diff, review=review,
                    security=security, distill=distill,
                    checkpoint=checkpoint, pr=pr,
                )

        # Clean up worktree now that code is merged
        if self.factory_config.use_worktrees and comp_id in self.worktree_paths:
            self.hooks.cleanup_worktree(comp_id, self.root_dir, self.run_id)
            del self.worktree_paths[comp_id]

        return PipelineOutcome(
            transition=self.complete(
                comp, comp_result.duration_seconds, comp_result.iterations,
            ),
            verify=verify, diff=diff, review=review,
            security=security, distill=distill,
            checkpoint=checkpoint, pr=pr,
        )

    def _route_failure(
        self, comp: Component, failure: PhaseFailure,
    ) -> Transition:
        """The single dispatch from a phase's typed failure into a
        component state transition."""
        if failure.action == FailureAction.TOKEN_BUDGET:
            return self.fail_for_budget(comp, failure.phase)
        if failure.action == FailureAction.FAIL:
            return self.fail(
                comp, failure.error, phase=failure.phase,
                check=failure.check, signatures=failure.signatures,
            )
        return self.retry_or_fail(
            comp, failure.error, failure.context_json,
            phase=failure.phase, check=failure.check,
            signatures=failure.signatures,
        )

    def _phase_verify(
        self, comp: Component, comp_result: ComponentResult, wt_path: Path,
    ) -> VerifyPhaseResult:
        """Phase 1: mechanical verification (tests / typecheck / lint /
        PRD stories / diff scope / bad patterns / fixtures)."""
        if self.factory_config.skip_verification:
            # R2.3: --no-verify. Previously verify_config=None fell
            # through to VerifyConfig() defaults here and Phase 1 ran
            # anyway - on a non-Python repo that burned every retry
            # against checks that could never pass. The empty
            # VerificationResult below is what downstream reviewers see:
            # no checks ran, none are claimed.
            self.ui.info(
                f"  Phase 1 SKIPPED for {comp.id}: mechanical "
                f"verification disabled (--no-verify)"
            )
            comp.verification_passed = None
            self._record_phase_skip(
                comp, "verify",
                "mechanical verification disabled (--no-verify)",
            )
            return VerifyPhaseResult(
                ran=False,
                verification=VerificationResult(passed=True, checks=[]),
            )

        verify_config = self.factory_config.verify_config or VerifyConfig()
        self.ui.info(f"  Phase 1: mechanical verification for {comp.id}...")
        verify_start = time.monotonic()
        # Per-component allowed_paths comes from the PRD (architect-
        # emitted via DECOMPOSE_PROMPT v1.1.0+, REQUIRED for v1.2.0+).
        # Without this, the diff-scope check silently passes and a
        # rogue agent can touch anything in the worktree -- the
        # end-to-end validation run on 2026-05-27 caught an agent
        # editing factory internals because allowed_paths was always
        # None here. Legacy PRDs without the field load with
        # allowed_paths=None which preserves the prior "no constraint"
        # behavior; v1.2.0+ architect outputs are gated upstream in
        # decompose._validate_decompose_output.
        #
        # R1.5: a PRD that fails to LOAD is not the same as a PRD with
        # no allowedPaths. Swallowing the load error into
        # allowed_paths=None silently disabled the scope check -- an
        # agent that corrupts or deletes its own PRD would unbind its
        # write scope. Load failure now flows into check_diff_scope as
        # allowed_paths_error, which fails the check closed.
        component_allowed_paths: list[str] | None = None
        allowed_paths_error: str | None = None
        try:
            prd_for_scope = PRD.load(wt_path / comp.prd_path)
            component_allowed_paths = prd_for_scope.allowed_paths
        except FileNotFoundError as exc:
            allowed_paths_error = f"PRD not found: {exc}"
        except ValueError as exc:
            allowed_paths_error = f"PRD failed to parse: {exc}"
        # R7.2: fixtures config resolves from toml/env when the
        # caller did not inject one; enabled=false (the default)
        # makes run_mechanical_verification skip the check entirely.
        fixtures_cfg = (
            self.factory_config.fixtures_config
            or FixturesConfig.load(self.root_dir)
        )
        verification = self.hooks.run_mechanical_verification(
            wt_path,
            wt_path / comp.prd_path,
            self.manifest.base_branch,
            component_allowed_paths,
            verify_config,
            allowed_paths_error=allowed_paths_error,
            fixtures_config=fixtures_cfg,
            component_id=comp.id,
        )
        verify_duration = time.monotonic() - verify_start
        comp.verification_passed = verification.passed
        self.bus.emit(ev.VerificationResultEvent(
            component=comp.id, passed=verification.passed,
            checks=tuple(c.name for c in verification.checks),
            failures=tuple(
                c.message for c in verification.checks if not c.passed
            ),
            duration_seconds=round(verify_duration, 2),
        ))

        if not verification.passed:
            failing = [c for c in verification.checks if not c.passed]
            self.ui.warn(
                f"  Phase 1 FAILED for {comp.id}: "
                f"{', '.join(c.name for c in failing)}"
            )
            ctx = IterationContext.from_json(
                comp_result.context_json or "{}",
            )
            ctx.add_verification_failure(verification.as_context())
            # R6.1: carry the parser's structured codes (ruff rule,
            # mypy error code, pytest exception type) into the
            # journal instead of the flattened string.
            from ralph_py.evolution import signatures_from_verification
            return VerifyPhaseResult(
                ran=True,
                verification=verification,
                failure=PhaseFailure(
                    action=FailureAction.RETRY_OR_FAIL,
                    error="Mechanical verification failed",
                    phase="verify",
                    check=", ".join(c.name for c in failing),
                    context_json=ctx.to_json(),
                    signatures=signatures_from_verification(
                        verification.checks,
                    ),
                ),
            )

        self.ui.ok(f"  Phase 1 passed for {comp.id}")
        return VerifyPhaseResult(ran=True, verification=verification)

    def _phase_diff(
        self, comp: Component, comp_result: ComponentResult, wt_path: Path,
    ) -> DiffPhaseResult:
        """Fetch the component diff once and share it across Phase 2,
        Phase 2.5, and knowledge distillation. Without this each phase
        would shell out to `git diff` independently, redundantly
        rebuilding the same patch on every component.

        R1.3 (H-14): a git failure here used to yield "" and all three
        consumers silently reviewed an empty diff and passed. Now it
        is an infrastructure failure for the component: record the
        infra finding, journal it, and retry/fail closed.
        """
        try:
            shared_diff = git.get_diff_content(
                self.manifest.base_branch, wt_path,
            )
        except git.GitDiffError as exc:
            self.ui.err(f"  Diff fetch FAILED for {comp.id}: {exc}")
            self._add_findings(comp, [Finding.infrastructure_error(
                phase="diff",
                explanation=(
                    f"git diff against {self.manifest.base_branch} failed; "
                    f"review/security/knowledge cannot run: {exc}"
                ),
            )])
            self.bus.emit(ev.DiffFetchFailed(component=comp.id, error=str(exc)))
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_verification_failure(
                f"git diff against {self.manifest.base_branch} failed: {exc}"
            )
            return DiffPhaseResult(failure=PhaseFailure(
                action=FailureAction.RETRY_OR_FAIL,
                error=f"Diff fetch failed (infrastructure): {exc}",
                phase="diff", check="git_diff",
                context_json=ctx.to_json(),
                signatures=["diff:fetch-failed"],
            ))

        # R1.4: strip the engineer's Self-Critique block ONCE and share
        # the stripped diff with BOTH reviewers (E2 anti-anchoring now
        # applies to Phase 2.5 too, not just Phase 2). The knowledge
        # distiller keeps the unstripped shared_diff: the self-critique
        # is the engineer's own account, and anti-anchoring targets
        # adversarial reviewers, not the distiller.
        review_diff = git.strip_self_critique_from_diff(shared_diff)

        # R1.4 (H-16): a diff over the prompt cap must never be
        # partially reviewed in hard mode - a rogue engineer could pad
        # the first 50KB with benign churn and land a malicious hunk
        # after the cut. Split once on file boundaries; both hard-mode
        # phases reuse the same chunks (one budget call per chunk).
        # Advisory phases keep the single truncated pass, which
        # run_review/run_security_review annotate as PARTIAL.
        review_mode = ReviewMode(self.factory_config.review_mode)
        sec_config = self.factory_config.security_config
        oversized = (
            len(review_diff) > git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT
        )
        needs_chunks = oversized and (
            review_mode == ReviewMode.HARD
            or (
                sec_config is not None
                and sec_config.mode == SecurityMode.HARD.value
            )
        )
        review_chunks: list[str] | None = None
        if needs_chunks:
            try:
                review_chunks = git.split_diff_for_prompt(review_diff)
            except git.DiffUnsplittableError as exc:
                # Fail closed via the retry path: unlike budget
                # exhaustion, the engineer CAN fix this by producing a
                # smaller diff, so the retry context carries the signal.
                self.ui.err(f"  Diff unsplittable for {comp.id}: {exc}")
                self._add_findings(comp, [Finding.infrastructure_error(
                    phase="review",
                    explanation=(
                        "Hard-mode review requires chunking the "
                        f"oversized diff, but it cannot be split: {exc} "
                        "(R1.4: an unreviewable diff must not merge)"
                    ),
                )])
                self.bus.emit(ev.DiffUnsplittable(
                    component=comp.id, error=str(exc),
                    diff_chars=len(review_diff),
                ))
                ctx = IterationContext.from_json(
                    comp_result.context_json or "{}",
                )
                ctx.add_review_finding(
                    f"The diff is too large to review ({exc}). Reduce "
                    "the change so each file's diff fits the "
                    f"{git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT // 1000}"
                    "KB review cap."
                )
                return DiffPhaseResult(
                    diff=shared_diff,
                    review_diff=review_diff,
                    failure=PhaseFailure(
                        action=FailureAction.RETRY_OR_FAIL,
                        error=(
                            "Review diff unsplittable at the prompt cap: "
                            f"{exc}"
                        ),
                        phase="review", check="diff_chunking",
                        context_json=ctx.to_json(),
                        signatures=["review:diff-unsplittable"],
                    ),
                )
            self.bus.emit(ev.DiffChunked(
                component=comp.id, chunks=len(review_chunks),
                diff_chars=len(review_diff),
            ))

        return DiffPhaseResult(
            diff=shared_diff, review_diff=review_diff, chunks=review_chunks,
        )

    def _phase_review(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
        verification: VerificationResult,
        review_diff: str,
        review_chunks: list[str] | None,
    ) -> ReviewPhaseResult:
        """Phase 2: second-opinion review against the PRD."""
        review_mode = ReviewMode(self.factory_config.review_mode)
        chunked_review = (
            review_mode == ReviewMode.HARD and review_chunks is not None
        )
        review_skip_reason: str | None = None
        if review_mode == ReviewMode.SKIP:
            review_skip_reason = "review disabled (mode=skip)"
        elif not chunked_review and not self.adversarial_budget_ok():
            # Chunked hard-mode reviews never downgrade to SKIP on an
            # exhausted budget: their budget rule is "cover every chunk
            # or fail as infrastructure" (handled below).
            self.ui.warn(
                f"  Phase 2 SKIPPED for {comp.id}: "
                f"adversarial LLM budget "
                f"({self.factory_config.max_adversarial_calls}) exhausted"
            )
            review_skip_reason = (
                f"adversarial LLM budget "
                f"({self.factory_config.max_adversarial_calls}) exhausted"
            )
            review_mode = ReviewMode.SKIP
        if review_mode == ReviewMode.HARD and review_chunks is not None:
            remaining = self.adversarial_budget_remaining()
            if remaining is not None and remaining < len(review_chunks):
                # R1.4: the budget cannot cover the chunks. Retrying
                # cannot recover budget, so fail directly instead of
                # burning engineer iterations on a deterministic wall.
                error = (
                    f"Chunked review needs {len(review_chunks)} "
                    f"adversarial calls but only {remaining} remain in "
                    f"max_adversarial_calls "
                    f"({self.factory_config.max_adversarial_calls}); "
                    "refusing a partial hard-mode review (R1.4)"
                )
                self.ui.err(f"  Phase 2 FAILED for {comp.id}: {error}")
                comp.review_passed = False
                self._add_findings(comp, [Finding.infrastructure_error(
                    phase="review", explanation=error,
                )])
                self.bus.emit(ev.ChunkBudgetInsufficient(
                    component=comp.id, phase="review",
                    chunks=len(review_chunks), remaining=remaining,
                ))
                return ReviewPhaseResult(
                    ran=False,
                    failure=PhaseFailure(
                        action=FailureAction.FAIL,
                        error=f"Review infrastructure error: {error}",
                        phase="review", check="adversarial_budget",
                        signatures=["review:chunk-budget-insufficient"],
                    ),
                )
        if review_mode == ReviewMode.SKIP:
            comp.review_passed = None
            self._record_phase_skip(
                comp, "review", review_skip_reason or "review skipped",
            )
            return ReviewPhaseResult(ran=False, skip_reason=review_skip_reason)

        from ralph_py.agents import get_agent

        if not chunked_review:
            self.adversarial_budget_consume()
        chunk_note = (
            f", {len(review_chunks)} chunks"
            if chunked_review and review_chunks is not None else ""
        )
        self.ui.info(
            f"  Phase 2: review ({review_mode.value}{chunk_note}) "
            f"for {comp.id}..."
        )

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
            )
            if review_mode == ReviewMode.HARD and review_chunks is not None:
                # R1.4: one pass per chunk, each consuming budget;
                # any chunk failure fails the merged result.
                with self._phase_transcript(comp.id, "review") as on_line:
                    review_result = self.hooks.run_chunked_review(
                        review_agent,
                        wt_path / comp.prd_path,
                        wt_path,
                        self.manifest.base_branch,
                        verification,
                        review_mode,
                        self.ui,
                        diff_chunks=review_chunks,
                        budget_remaining=self.adversarial_budget_remaining(),
                        consume_budget=self.adversarial_budget_consume,
                        debug_dir=adversarial_debug_dir,
                        on_line=on_line,
                    )
            else:
                with self._phase_transcript(comp.id, "review") as on_line:
                    review_result = self.hooks.run_review(
                        review_agent,
                        wt_path / comp.prd_path,
                        wt_path,
                        self.manifest.base_branch,
                        verification,
                        review_mode,
                        self.ui,
                        diff_content=review_diff,
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
        # records are exactly this review's spend (N records for a
        # chunked review). Recorded before pass/fail handling so a
        # failed or crashed review still counts.
        if review_agent is not None:
            self._record_usage(comp.id, "review", collect_usage(review_agent))
        if self.token_budget_exceeded():
            return ReviewPhaseResult(
                ran=True,
                result=review_result,
                failure=PhaseFailure(
                    action=FailureAction.TOKEN_BUDGET,
                    error="token budget exceeded",
                    phase="review",
                ),
            )
        comp.review_passed = review_result.passed
        # E3: typed findings are the source of truth; the rendered
        # string is a derived view kept for backward-compat consumers.
        self._add_findings(comp, review_result.as_findings())
        comp.review_findings = review_result.as_pr_body_section()
        # Observability gets criterion-only counts to preserve the
        # historical meaning of fail_count = "failed PRD criteria".
        # Concern counts ride along separately via fail_concerns /
        # advisory_concerns so dashboards can distinguish.
        self.bus.emit(ev.ReviewResultEvent(
            component=comp.id, passed=review_result.passed,
            mode=review_mode.value,
            fail_count=review_result.criterion_fail_count,
            advisory_count=review_result.criterion_advisory_count,
            duration_seconds=round(review_result.duration_seconds, 2),
        ))

        if not review_result.passed:
            reason = (
                "Review infrastructure error"
                if review_result.infrastructure_error
                else "Review failed"
            )
            self.ui.warn(
                f"  Phase 2 FAILED for {comp.id}: "
                f"{review_result.fail_count} failures"
            )
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_review_finding(review_result.as_retry_context())
            # R6.1: journal the finding categories that failed the
            # gate ("review:scope_creep", "review:prd_criterion",
            # "review:infrastructure"), not the flattened reason.
            from ralph_py.evolution import signatures_from_findings
            return ReviewPhaseResult(
                ran=True,
                result=review_result,
                failure=PhaseFailure(
                    action=FailureAction.RETRY_OR_FAIL,
                    error=reason,
                    phase="review",
                    check=(
                        "infrastructure"
                        if review_result.infrastructure_error else "criteria"
                    ),
                    context_json=ctx.to_json(),
                    signatures=signatures_from_findings(
                        "review", review_result.as_findings(),
                    ),
                ),
            )

        self.ui.ok(f"  Phase 2 passed for {comp.id}")
        return ReviewPhaseResult(ran=True, result=review_result)

    def _phase_security(
        self,
        comp: Component,
        comp_result: ComponentResult,
        wt_path: Path,
        review_diff: str,
        review_chunks: list[str] | None,
    ) -> SecurityPhaseResult:
        """Phase 2.5: security review (adversarial pass focused on
        vulns). Runs as a separate LLM call with its own threat-model
        framing so it catches what the correctness reviewer misses.
        Hard-mode fails the component on findings at or above
        SecurityConfig.fail_threshold OR on infrastructure errors."""
        sec_config = self.factory_config.security_config
        chunked_security = (
            sec_config is not None
            and sec_config.mode == SecurityMode.HARD.value
            and review_chunks is not None
        )
        if sec_config is None:
            self._record_phase_skip(
                comp, "security", "security review not configured",
            )
            return SecurityPhaseResult(
                ran=False, skip_reason="security review not configured",
            )
        if sec_config.mode == SecurityMode.SKIP.value:
            self._record_phase_skip(
                comp, "security", "security review disabled (mode=skip)",
            )
            return SecurityPhaseResult(
                ran=False,
                skip_reason="security review disabled (mode=skip)",
            )
        if not chunked_security and not self.adversarial_budget_ok():
            # As with Phase 2: chunked hard-mode security never
            # downgrades to SKIP on an exhausted budget - it covers
            # every chunk or fails as infrastructure below.
            self.ui.warn(
                f"  Phase 2.5 SKIPPED for {comp.id}: "
                f"adversarial LLM budget exhausted"
            )
            self._record_phase_skip(
                comp, "security", "adversarial LLM budget exhausted",
            )
            return SecurityPhaseResult(
                ran=False, skip_reason="adversarial LLM budget exhausted",
            )
        if (
            sec_config.mode == SecurityMode.HARD.value
            and review_chunks is not None
        ):
            remaining = self.adversarial_budget_remaining()
            if remaining is not None and remaining < len(review_chunks):
                # R1.4: same rule as Phase 2 - budget cannot cover the
                # chunks, and retrying cannot recover budget.
                error = (
                    f"Chunked security review needs {len(review_chunks)} "
                    f"adversarial calls but only {remaining} remain in "
                    f"max_adversarial_calls "
                    f"({self.factory_config.max_adversarial_calls}); "
                    "refusing a partial hard-mode security review (R1.4)"
                )
                self.ui.err(f"  Phase 2.5 FAILED for {comp.id}: {error}")
                self._add_findings(comp, [Finding.infrastructure_error(
                    phase="security", explanation=error,
                )])
                self.bus.emit(ev.ChunkBudgetInsufficient(
                    component=comp.id, phase="security",
                    chunks=len(review_chunks), remaining=remaining,
                ))
                return SecurityPhaseResult(
                    ran=False,
                    failure=PhaseFailure(
                        action=FailureAction.FAIL,
                        error=(
                            f"Security review infrastructure error: {error}"
                        ),
                        phase="security", check="adversarial_budget",
                        signatures=["security:chunk-budget-insufficient"],
                    ),
                )

        if not chunked_security:
            # Chunked passes consume per-chunk inside
            # run_chunked_security_review instead.
            self.adversarial_budget_consume()
        from ralph_py.agents import get_agent as _get_sec_agent

        chunk_note = (
            f", {len(review_chunks)} chunks"
            if chunked_security and review_chunks is not None else ""
        )
        self.ui.info(
            f"  Phase 2.5: security review "
            f"({sec_config.mode}{chunk_note}) for {comp.id}..."
        )
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
            )
            if (
                sec_config.mode == SecurityMode.HARD.value
                and review_chunks is not None
            ):
                # R1.4: one pass per chunk, each consuming budget
                # via consume_budget; any chunk failure fails the
                # merged result.
                with self._phase_transcript(comp.id, "security") as on_line:
                    sec_result = self.hooks.run_chunked_security_review(
                        sec_agent,
                        wt_path / comp.prd_path,
                        wt_path,
                        self.manifest.base_branch,
                        sec_config,
                        self.ui,
                        diff_chunks=review_chunks,
                        budget_remaining=self.adversarial_budget_remaining(),
                        consume_budget=self.adversarial_budget_consume,
                        debug_dir=adversarial_debug_dir,
                        on_line=on_line,
                    )
            else:
                with self._phase_transcript(comp.id, "security") as on_line:
                    sec_result = self.hooks.run_security_review(
                        sec_agent,
                        wt_path / comp.prd_path,
                        wt_path,
                        self.manifest.base_branch,
                        sec_config,
                        self.ui,
                        diff_content=review_diff,
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
                overall_notes=(
                    f"Security review agent failed before "
                    f"completion: {exc}"
                ),
                infrastructure_error=True,
                # R7.1: a crash before/inside the run is still
                # attributed to the selected reviewer identity.
                reviewer_model=self.security_selection.identity,
            )

        # R3.1: record security spend before pass/fail handling so
        # failed and crashed passes still count toward the meter.
        if sec_agent is not None:
            self._record_usage(comp.id, "security", collect_usage(sec_agent))
        if self.token_budget_exceeded():
            return SecurityPhaseResult(
                ran=True,
                result=sec_result,
                failure=PhaseFailure(
                    action=FailureAction.TOKEN_BUDGET,
                    error="token budget exceeded",
                    phase="security",
                ),
            )

        if sec_result is not None:
            self.bus.emit(ev.ReviewResultEvent(
                component=comp.id, passed=sec_result.passed,
                mode=f"security-{sec_config.mode}",
                fail_count=sec_result.critical_count + sec_result.high_count,
                advisory_count=len(sec_result.findings),
                duration_seconds=round(sec_result.duration_seconds, 2),
            ))

            # E3: source-of-truth typed findings list, plus the
            # legacy rendered string for PR body / manifest readers.
            self._add_findings(comp, sec_result.as_findings())
            if sec_result.findings:
                if comp.review_findings:
                    comp.review_findings = (
                        comp.review_findings + "\n\n"
                        + sec_result.as_pr_body_section()
                    )
                else:
                    comp.review_findings = sec_result.as_pr_body_section()

            if not sec_result.passed:
                reason = (
                    "Security review crashed"
                    if sec_result.infrastructure_error
                    else "Security review failed"
                )
                self.ui.warn(
                    f"  Phase 2.5 FAILED for {comp.id}: "
                    f"{sec_result.critical_count} critical, "
                    f"{sec_result.high_count} high"
                )
                ctx = IterationContext.from_json(
                    comp_result.context_json or "{}",
                )
                # as_retry_context is empty for infra results (no
                # findings list); fall back to the notes so the
                # retry prompt still says what went wrong.
                ctx.add_review_finding(
                    sec_result.as_retry_context()
                    or "Security review infrastructure error: "
                    + sec_result.overall_notes
                )
                # R6.1: journal the vuln categories that failed the
                # gate ("security:injection", ...), not the reason.
                from ralph_py.evolution import signatures_from_findings
                return SecurityPhaseResult(
                    ran=True,
                    result=sec_result,
                    failure=PhaseFailure(
                        action=FailureAction.RETRY_OR_FAIL,
                        error=reason,
                        phase="security",
                        check=(
                            "infrastructure"
                            if sec_result.infrastructure_error
                            else "findings"
                        ),
                        context_json=ctx.to_json(),
                        signatures=signatures_from_findings(
                            "security", sec_result.as_findings(),
                        ),
                    ),
                )

        return SecurityPhaseResult(ran=True, result=sec_result)

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
        """
        knowledge_config = self.knowledge_config
        if not knowledge_config.enabled:
            return DistillPhaseResult(
                ran=False, skip_reason="knowledge disabled",
            )
        if self.manifest.single_pr:
            self.ui.info(
                f"  Knowledge: skipped for {comp.id} "
                f"(single_pr mode produces a polluted per-component diff)"
            )
            self._record_phase_skip(
                comp, "knowledge",
                "single_pr mode produces a polluted per-component diff",
            )
            return DistillPhaseResult(
                ran=False,
                skip_reason=(
                    "single_pr mode produces a polluted per-component diff"
                ),
            )
        if not self.adversarial_budget_ok():
            self.ui.info(
                f"  Knowledge: skipped for {comp.id} "
                f"(adversarial budget exhausted)"
            )
            self._record_phase_skip(
                comp, "knowledge", "adversarial LLM budget exhausted",
            )
            return DistillPhaseResult(
                ran=False, skip_reason="adversarial LLM budget exhausted",
            )
        if self.token_budget_exceeded():
            # R3.1: the gates all passed before the cap tripped, so the
            # component proceeds to PR - but no further LLM spend. The
            # skip is recorded, and the scheduling gate stops any
            # remaining components loudly.
            self.ui.warn(
                f"  Knowledge: skipped for {comp.id} "
                f"(token budget exceeded: {self.run_usage.total_tokens} >= "
                f"{self.factory_config.max_total_tokens})"
            )
            self._record_phase_skip(
                comp, "knowledge", "token budget (max_total_tokens) exceeded",
            )
            return DistillPhaseResult(
                ran=False,
                skip_reason="token budget (max_total_tokens) exceeded",
            )

        self.adversarial_budget_consume()
        distill_agent: Any = None
        try:
            from ralph_py.agents import get_agent as _get_agent

            # Reuse the diff already fetched by the diff phase - the
            # worktree state hasn't changed between Phase 1 and here.
            diff_content = shared_diff
            distill_model = (
                knowledge_config.distill_model or self.base_config.model
            )
            distill_agent = _get_agent(
                self.base_config.agent_cmd,
                distill_model,
                self.base_config.model_reasoning_effort,
                self.base_config.agent_type,
            )
            distill_start = time.monotonic()
            with self._phase_transcript(comp.id, "distill") as on_line:
                written, status = self.hooks.distill_facts(
                    distill_agent,
                    comp,
                    diff_content,
                    wt_path / comp.prd_path,
                    comp_result.iterations,
                    self.run_id,
                    knowledge_config.knowledge_root,
                    knowledge_config,
                    wt_path,
                    comp.review_passed,
                    on_line=on_line,
                )
            self.bus.emit(ev.DistillResult(
                component=comp.id, facts_written=written,
                duration_seconds=round(
                    time.monotonic() - distill_start, 2,
                ),
            ))
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
                comp.id, "distill", collect_usage(distill_agent),
            )

        # Fact-utilization metric: did the agent reference any of
        # the facts we injected at the top of the worker prompt?
        # Crude substring match against the post-iteration diff and
        # progress.txt; under-counts when the LLM paraphrases.
        try:
            prefix = self.hooks.build_knowledge_context(
                self.manifest, comp,
                knowledge_config.knowledge_root, knowledge_config,
            )
            if prefix:
                progress_text = ""
                progress_path = (
                    wt_path / "scripts" / "ralph" / "progress.txt"
                )
                try:
                    progress_text = progress_path.read_text(encoding="utf-8")
                except OSError:
                    pass
                util = self.hooks.measure_fact_utilization(
                    prefix, shared_diff, progress_text,
                )
                if util["injected"] > 0:
                    self.ui.info(
                        f"  Knowledge utilization: "
                        f"{util['referenced']}/{util['injected']} "
                        f"facts referenced in diff or progress.txt"
                    )
        except Exception:  # noqa: BLE001
            pass

        return DistillPhaseResult(ran=True)

    def _phase_checkpoint(
        self, comp: Component, *, diff_text: str = "",
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
        retry like any other failure. Skip the prompt when no UI
        is interactive - automation should fail loudly rather
        than block indefinitely."""
        if not self.factory_config.pause_before_pr_merge:
            return CheckpointDecision.NOT_PROMPTED
        question = f"Approve PR creation and merge for {comp.id}?"
        self.bus.emit(ev.CheckpointRequested(
            component=comp.id, kind="pr_merge", question=question,
        ))
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
                    diff_text, CHECKPOINT_DIFF_CHAR_LIMIT,
                ) if diff_text else "",
                review_findings=tuple(
                    f for f in comp.findings if f.phase == "review"
                ),
                security_findings=tuple(
                    f for f in comp.findings if f.phase == "security"
                ),
                usage=self.usage_totals_for(comp.id),
                branch=comp.branch_name,
            ),
        )
        if not self.interaction.can_prompt():
            self.ui.warn(
                f"  pause_before_pr_merge requested but UI is "
                f"non-interactive; proceeding without prompt for {comp.id}"
            )
            self.bus.emit(ev.CheckpointResolved(
                component=comp.id, kind="pr_merge",
                decision="not_prompted", decided_by="auto",
            ))
            return CheckpointDecision.NOT_PROMPTED
        self.ui.section(f"Human checkpoint: {comp.id}")
        self.ui.info(comp.review_findings or "(no review findings)")
        response = self.interaction.request(request)
        if not response.answered:
            # The channel lost its resolver between the guard and the
            # answer (detached TUI): same semantics as non-interactive.
            self.bus.emit(ev.CheckpointResolved(
                component=comp.id, kind="pr_merge",
                decision="not_prompted", decided_by="auto",
            ))
            return CheckpointDecision.NOT_PROMPTED
        decision = {
            1: CheckpointDecision.REJECTED,
            2: CheckpointDecision.RETRY,
        }.get(response.choice, CheckpointDecision.APPROVED)
        self.bus.emit(ev.CheckpointResolved(
            component=comp.id, kind="pr_merge",
            decision=decision.name.lower(), decided_by="operator",
        ))
        if decision == CheckpointDecision.REJECTED:
            self.ui.warn(
                f"  Human rejected {comp.id} at PR checkpoint"
            )
        elif decision == CheckpointDecision.RETRY:
            self.ui.warn(
                f"  Human requested retry for {comp.id} "
                f"at PR checkpoint"
            )
        return decision

    def _phase_pr(self, comp: Component) -> PrPhaseResult:
        """Per-component PR create+merge. single_pr mode is exempt
        (handled by the caller): every component shares one branch, a
        single PR is created at end-of-run, and squash-merging the
        shared branch per component would destroy the history the
        remaining components build on."""
        from ralph_py.pr import is_gh_available, push_create_and_merge_pr

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
            comp, self.manifest, self.root_dir, self.ui,
            merge_method="squash",
            merge_timeout=self.factory_config.merge_timeout,
        )
        if outcome.pr_url:
            self.factory_result.pr_urls.append(outcome.pr_url)
            self.bus.emit(ev.PrCreated(
                component=comp.id, pr_number=comp.pr_number or 0,
                pr_url=outcome.pr_url,
            ))
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
            )
        self.bus.emit(ev.PrMerged(
            component=comp.id, pr_number=comp.pr_number or 0,
            pr_url=outcome.pr_url,
        ))
        return PrPhaseResult(
            disposition=PrDisposition.MERGED, pr_url=outcome.pr_url,
        )
