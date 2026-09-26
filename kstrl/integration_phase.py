"""The integration review, factory side (#482, #483)."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import git
from kstrl.agents.base import INTEGRATION_COMPONENT, INTEGRATION_ROLE, collect_usage
from kstrl.agents.prompt_record import AgentCall, recording_prompts
from kstrl.contract import (
    ContractCleanupError,
    ContractMode,
    ContractResult,
    _create_temp_worktree,
    _remove_temp_worktree,
)
from kstrl.integration import (
    STATUS_HANDOFF,
    ExpectedStory,
    IntegrationOutcome,
    carried_story,
    integration_outcome,
    integration_stories,
    write_integration_prd,
)
from kstrl.integration_fix import reconcile_fixes
from kstrl.integration_state import (
    OUTCOME_CLEAN,
    OUTCOME_NOT_RUN,
    OUTCOME_OPEN_FINDINGS,
    OUTCOME_RED,
    add_findings,
    add_stop,
    bind_state,
    carried_findings,
    close_findings,
    evidence_dir,
    fix_components,
    has_fix_component,
    next_finding_ids,
    next_review_number,
    read_state,
    state_binding,
    write_evidence,
    write_state,
)
from kstrl.manifest import ComponentStatus
from kstrl.review import ReviewMode, ReviewResult
from kstrl.verify import VerificationResult
from kstrl.version import kstrl_version

if TYPE_CHECKING:
    from kstrl.decisions import SpecDecision
    from kstrl.factory import FactoryConfig
    from kstrl.manifest import Manifest
    from kstrl.pipeline import ComponentPipeline
    from kstrl.shutdown import StopController
    from kstrl.ui.base import UI

GATE_NOTE = (
    "Record only: the integration verdict does not gate this run, so it cannot "
    "report the feature as integrated (#482)."
)
BLOCKING_NOTE = (
    "Blocking: a stop without a clean integration verdict fails this run, and "
    "open code findings become a fix component (#483)."
)


@dataclass
class Phase3Round:
    base_sha: str = ""
    ran: bool = False
    results: list[ContractResult] = field(default_factory=list)
    #: Set by the factory when a contract breaker was sent back for retry:
    #: the round re-enters scheduling without an integration review.
    breaker_reset: bool = False
    #: len(contract_failures) when the round began, so the lines this round
    #: added can be told apart (#483).
    failures_before: int = 0

    def record(self, results: list[ContractResult]) -> None:
        self.ran = True
        self.results = list(results)


@dataclass(frozen=True)
class IntegrationRun:
    manifest: Manifest
    manifest_path: Path
    root_dir: Path
    run_id: str
    pipeline: ComponentPipeline
    ui: UI
    #: The architect decisions this run bound (#260), for a fix PRD's notes.
    decisions: tuple[SpecDecision, ...] = ()


@dataclass(frozen=True)
class IntegrationRecord:
    outcome: str
    reason: str
    opened: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    evidence: Path | None = None
    #: The state this round wrote, for the loop's decision (#483). None
    #: when no state could be bound.
    state: dict[str, Any] | None = None
    sha: str = ""
    closed: tuple[str, ...] = ()
    still_open: tuple[str, ...] = ()
    gates: bool = False
    #: The fix component the loop built after this round; "" when none.
    fix: str = ""


@dataclass(frozen=True)
class _Pin:
    sha: str = ""
    test_result: ContractResult | None = None
    refusal: str = ""


def run_integration_review(
    run: IntegrationRun, phase3: Phase3Round, stop: StopController | None
) -> IntegrationRecord:
    reason = _unreachable_reason(run.pipeline.factory_config, stop)
    if reason:
        return IntegrationRecord(OUTCOME_NOT_RUN, reason, gates=_gates(run))
    run.ui.section(f"Integration review ({'blocking' if _gates(run) else 'record only'})")
    if not run.manifest.feature_base_sha:
        return _not_run(
            run,
            "no feature base: featureBaseSha is empty, so the feature's change has no known start",
            None,
            "",
        )
    bound = bind_state(
        read_state(run.root_dir),
        state_binding(run.manifest, run.manifest_path),
        has_fix_component(run.manifest),
    )
    if bound.state is None:
        return _not_run(run, bound.refusal, None, "")
    if "replacedBinding" in bound.state:
        run.ui.warn(
            f"  Integration state belonged to another feature "
            f"({bound.state['replacedBinding']}); starting a new record"
        )
    reason = _fix_refusal(run, bound.state)
    if reason:
        return _not_run(run, reason, bound.state, "")
    pin = _pin_round(run, phase3)
    if pin.refusal:
        return _not_run(run, pin.refusal, bound.state, pin.sha)
    reason = _spend_refusal(run.pipeline)
    if reason:
        return _not_run(run, reason, bound.state, pin.sha)
    return _review_round(run, bound.state, pin)


def _gates(run: IntegrationRun) -> bool:
    return run.pipeline.factory_config.integration_blocking


def _fix_refusal(run: IntegrationRun, state: dict[str, Any]) -> str:
    """Why no round may be reviewed: a fix whose three creation stages
    disagree, or a last fix that did not end COMPLETED and merged (design
    3.5 rule 3). "" when the round may proceed."""
    errors = reconcile_fixes(state, run.manifest, run.root_dir)
    if errors:
        return "; ".join(errors)
    fixes = fix_components(run.manifest)
    if not fixes:
        return ""
    last = fixes[-1]
    if last.status == ComponentStatus.COMPLETED.value and (last.merge_sha or last.pr_url):
        return ""
    return f"the last fix {last.id} ended {last.status}, not merged, so no tree holds it"


def _unreachable_reason(config: FactoryConfig, stop: StopController | None) -> str:
    from kstrl.factory import review_enabled

    if not config.integration_review:
        return "[factory] integration_review = false"
    if config.single_pr:
        return (
            "single_pr mode opens its one PR after Phase 3, so the base branch "
            "is not the merged feature"
        )
    if not config.create_prs:
        return "create_prs is off, so no component merged into the base branch"
    if not review_enabled(config):
        return "review_mode = skip, so no reviewer was resolved to dispatch"
    if stop is not None and stop.is_set():
        return f"the run was stopped ({stop.reason})"
    return ""


def _pin_round(run: IntegrationRun, phase3: Phase3Round) -> _Pin:
    contract = run.pipeline.factory_config.contract_config
    if contract is None or contract.mode == ContractMode.SKIP.value:
        return _pin_without_tests(run)
    if not phase3.ran:
        return _Pin(refusal="Phase 3 did not finish, so there is no tested commit to review")
    if len(phase3.results) != 1 or not phase3.base_sha:
        return _Pin(
            refusal=f"Phase 3 did not test one pinned commit ({len(phase3.results)} results, "
            f"base {phase3.base_sha or 'unresolved'})"
        )
    return _nothing_merged(run, _Pin(sha=phase3.base_sha, test_result=phase3.results[0]))


def _pin_without_tests(run: IntegrationRun) -> _Pin:
    try:
        sha = git.resolve_base_sha(run.manifest.base_branch, run.root_dir)
    except git.GitDiffError as exc:
        return _Pin(refusal=f"the base branch did not resolve: {exc}")
    return _nothing_merged(run, _Pin(sha=sha))


def _nothing_merged(run: IntegrationRun, pin: _Pin) -> _Pin:
    if pin.sha == run.manifest.feature_base_sha:
        return dataclasses.replace(
            pin, refusal=f"nothing merged since the feature base {pin.sha[:12]}"
        )
    return pin


def _spend_refusal(pipeline: ComponentPipeline) -> str:
    if not pipeline.adversarial_budget_ok():
        cap = pipeline.factory_config.max_adversarial_calls
        return f"the adversarial call budget ({cap}) is exhausted"
    breached = pipeline.breached_ceiling()
    if breached is not None:
        return f"the run reached {breached}"
    return ""


def _review_round(run: IntegrationRun, state: dict[str, Any], pin: _Pin) -> IntegrationRecord:
    try:
        tracked = git.tracked_files_at(pin.sha, run.root_dir)
    except git.GitDiffError as exc:
        return _not_run(
            run, f"the files at {pin.sha[:12]} could not be listed: {exc}", state, pin.sha
        )
    directory = evidence_dir(run.root_dir, run.run_id)
    number = next_review_number(directory)
    stories = (
        *integration_stories(run.manifest.feature_base_sha),
        *(carried_story(f["id"], f["text"], f["locations"]) for f in carried_findings(state)),
    )
    run.pipeline.adversarial_budget_consume()
    worktree, error = _create_temp_worktree(pin.sha, run.root_dir, "integration")
    if worktree is None:
        result = _infra(f"the integration worktree could not be created: {error}")
        outcome = integration_outcome(pin.test_result, result, stories, tracked=tracked)
        return _record_round(run, state, pin, number, stories, result, outcome, "")
    try:
        with recording_prompts(
            AgentCall(
                run_root=run.pipeline.usage_paths.root,
                run_id=run.run_id,
                component=INTEGRATION_COMPONENT,
                role=INTEGRATION_ROLE,
                attempt=number,
            )
        ):
            result = _run_reviewer(run, worktree, stories, directory / f"prd-{number}.json")
        outcome = integration_outcome(pin.test_result, result, stories, tracked=tracked)
    finally:
        cleanup_error = _remove(worktree, run.root_dir, run.ui)
    return _record_round(run, state, pin, number, stories, result, outcome, cleanup_error)


def _run_reviewer(
    run: IntegrationRun,
    worktree: Path,
    stories: Sequence[ExpectedStory],
    prd_path: Path,
) -> ReviewResult:
    from kstrl.agents import get_agent

    agent: object | None = None
    try:
        selection = run.pipeline.review_selection
        agent = get_agent(
            selection.agent_cmd,
            selection.model,
            selection.reasoning,
            selection.agent_type,
            sandbox=run.pipeline.sandbox_config,
            read_only=True,
        )
        return review_commit(
            agent,
            run.pipeline.hooks.run_review,
            worktree,
            run.manifest.feature_base_sha,
            stories,
            prd_path,
            run.ui,
        )
    except Exception as exc:  # noqa: BLE001 - as Phase 2
        return _infra(f"the integration reviewer crashed: {exc}")
    finally:
        if agent is not None:
            run.pipeline.record_integration_usage(collect_usage(agent))


def review_commit(
    agent: object,
    run_review: Callable[..., ReviewResult],
    worktree: Path,
    feature_base_sha: str,
    stories: Sequence[ExpectedStory],
    prd_path: Path,
    ui: UI,
) -> ReviewResult:
    """The integration review call: write the PRD at ``prd_path``, then run
    ``run_review`` in HARD mode over ``feature_base_sha...HEAD`` in ``worktree``.

    The factory and the ``integration`` calibration role both call this, so
    the call a calibration run measures is the call the factory makes (#482).
    """
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    write_integration_prd(prd_path, stories)
    return run_review(
        agent,
        prd_path,
        worktree,
        feature_base_sha,
        VerificationResult(passed=True, checks=[]),
        ReviewMode.HARD,
        ui,
        debug_dir=prd_path.parent,
    )


def _infra(notes: str) -> ReviewResult:
    return ReviewResult(
        passed=False, mode=ReviewMode.HARD.value, overall_notes=notes, infrastructure_error=True
    )


def _remove(worktree: Path, root: Path, ui: UI) -> str:
    try:
        _remove_temp_worktree(worktree, root, ui, "integration")
    except ContractCleanupError as exc:
        return str(exc)
    return ""


def _stop_outcome(outcome: IntegrationOutcome) -> str:
    if outcome.errors:
        return OUTCOME_RED
    if outcome.opened or outcome.still_open:
        return OUTCOME_OPEN_FINDINGS
    return OUTCOME_CLEAN


def _round_reason(stop_outcome: str, outcome: IntegrationOutcome, gates: bool) -> str:
    if stop_outcome == OUTCOME_RED:
        return f"the review output failed validation ({len(outcome.errors)} errors); nothing opened"
    if stop_outcome == OUTCOME_OPEN_FINDINGS:
        handoffs = sum(1 for f in outcome.opened if f.status == STATUS_HANDOFF)
        return (
            f"{len(outcome.opened)} findings opened, {handoffs} handed off, "
            f"{len(outcome.closed)} carried closed, {len(outcome.still_open)} carried "
            f"still open; {'blocking' if gates else 'recorded only'}"
        )
    return f"no finding open; {len(outcome.closed)} carried closed"


def _base_moved_to(run: IntegrationRun, sha: str) -> str:
    try:
        moved, current = git.base_moved(run.manifest.base_branch, sha, run.root_dir)
    except git.GitDiffError as exc:
        return f"unresolved: {exc}"
    return current if moved else ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _record_round(
    run: IntegrationRun,
    state: dict[str, Any],
    pin: _Pin,
    number: int,
    stories: Sequence[ExpectedStory],
    result: ReviewResult,
    outcome: IntegrationOutcome,
    cleanup_error: str,
) -> IntegrationRecord:
    ids = next_finding_ids(state, len(outcome.opened))
    stop_outcome = _stop_outcome(outcome)
    reason = _round_reason(stop_outcome, outcome, _gates(run))
    payload = _review_evidence(
        run, pin, stories, result, outcome, ids, stop_outcome, reason, cleanup_error
    )
    evidence, evidence_error = write_evidence(
        evidence_dir(run.root_dir, run.run_id), number, payload
    )
    add_findings(state, run.run_id, pin.sha, list(zip(ids, outcome.opened, strict=True)))
    close_findings(state, outcome.closed, run.run_id, pin.sha)
    add_stop(state, run.run_id, pin.sha, stop_outcome, reason, evidence, gates=_gates(run))
    state_error = write_state(run.root_dir, state)
    run.pipeline.journal_integration_result(
        stop_outcome, reason, pin.sha, ids, outcome.errors, gates=_gates(run)
    )
    for label, error in (
        ("worktree cleanup", cleanup_error),
        ("evidence write", evidence_error),
        ("state write", state_error),
    ):
        if error:
            run.ui.err(f"  Integration {label} failed: {error}")
    run.ui.info(f"  Integration review: {stop_outcome}: {reason}")
    return IntegrationRecord(
        stop_outcome,
        reason,
        tuple(ids),
        outcome.errors,
        evidence,
        state=state,
        sha=pin.sha,
        closed=outcome.closed,
        still_open=outcome.still_open,
        gates=_gates(run),
    )


def _review_evidence(
    run: IntegrationRun,
    pin: _Pin,
    stories: Sequence[ExpectedStory],
    result: ReviewResult,
    outcome: IntegrationOutcome,
    ids: list[str],
    stop_outcome: str,
    reason: str,
    cleanup_error: str,
) -> dict[str, Any]:
    test = pin.test_result
    phase3: dict[str, Any] = (
        {"ran": False}
        if test is None
        else {
            "ran": True,
            "passed": test.passed,
            "testedSha": test.tested_sha,
            "output": test.test_output[:2000],
        }
    )
    return {
        "schemaVersion": 1,
        "runId": run.run_id,
        "kstrlVersion": kstrl_version(),
        "at": _now(),
        "outcome": stop_outcome,
        "reason": reason,
        "gates": _gates(run),
        "featureBaseSha": run.manifest.feature_base_sha,
        "reviewedSha": pin.sha,
        "baseMovedTo": _base_moved_to(run, pin.sha),
        "phase3": phase3,
        "stories": [
            {"id": s.story_id, "title": s.title, "criterion": s.criterion} for s in stories
        ],
        "review": {
            "mode": result.mode,
            "passed": result.passed,
            "infrastructureError": result.infrastructure_error,
            "coverage": result.diffstat_disagreement,
            "reviewerModel": result.reviewer_model,
            "overallNotes": result.overall_notes,
            "droppedConcerns": result.dropped_concerns,
            "concernsNotList": result.concerns_not_list,
            "criteria": [
                {
                    "storyId": c.story_id,
                    "criterion": c.criterion,
                    "verdict": c.verdict,
                    "explanation": c.explanation,
                    "suggestion": c.suggestion,
                }
                for c in result.criteria
            ],
            "concerns": [
                {
                    "category": c.category,
                    "severity": c.severity,
                    "location": c.location,
                    "explanation": c.explanation,
                    "suggestion": c.suggestion,
                }
                for c in result.concerns
            ],
        },
        "errors": list(outcome.errors),
        "opened": [
            {"id": fid, **dataclasses.asdict(finding)}
            for fid, finding in zip(ids, outcome.opened, strict=True)
        ],
        "recorded": [dataclasses.asdict(r) for r in outcome.recorded],
        "closed": list(outcome.closed),
        "stillOpen": list(outcome.still_open),
        "cleanupError": cleanup_error,
    }


def _not_run(
    run: IntegrationRun, reason: str, state: dict[str, Any] | None, sha: str
) -> IntegrationRecord:
    directory = evidence_dir(run.root_dir, run.run_id)
    payload = {
        "schemaVersion": 1,
        "runId": run.run_id,
        "kstrlVersion": kstrl_version(),
        "at": _now(),
        "outcome": OUTCOME_NOT_RUN,
        "reason": reason,
        "gates": _gates(run),
        "featureBaseSha": run.manifest.feature_base_sha,
        "reviewedSha": sha,
    }
    evidence, evidence_error = write_evidence(directory, next_review_number(directory), payload)
    state_error = ""
    if state is not None:
        add_stop(state, run.run_id, sha, OUTCOME_NOT_RUN, reason, evidence, gates=_gates(run))
        state_error = write_state(run.root_dir, state)
    run.pipeline.journal_integration_result(OUTCOME_NOT_RUN, reason, sha, [], (), gates=_gates(run))
    run.ui.warn(f"  Integration review not run: {reason}")
    for label, error in (("evidence write", evidence_error), ("state write", state_error)):
        if error:
            run.ui.err(f"  Integration {label} failed: {error}")
    return IntegrationRecord(
        OUTCOME_NOT_RUN, reason, evidence=evidence, state=state, sha=sha, gates=_gates(run)
    )


def report_integration(record: IntegrationRecord, ui: UI) -> None:
    ui.kv("Integration review", f"{record.outcome}: {record.reason}")
    if record.opened:
        ui.info(f"  open findings: {', '.join(record.opened)}")
    for error in record.errors:
        ui.err(f"  {error}")
    if record.evidence:
        ui.info(f"  evidence: {record.evidence}")
    ui.info(f"  {BLOCKING_NOTE if record.gates else GATE_NOTE}")
