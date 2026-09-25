"""The Phase 3 outer loop's second reason to continue (#483; design #480 3.4, 3.5).

``_run_factory_locked`` iterates :meth:`IntegrationLoop.rounds`. Each round
is one scheduling pass plus Phase 3. After it, the loop re-enters
scheduling when a contract breaker was reset, runs the integration review
otherwise, and, only when ``[factory] integration_blocking`` is true, turns
the round's open code findings into one fix component and re-enters
scheduling again. The rules live here so the factory's own function does
not grow.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from kstrl.integration import STATUS_CLOSED, STATUS_OPEN
from kstrl.integration_fix import (
    FindingScope,
    append_fix_component,
    build_fix_prd,
    finding_scope,
    fix_id,
    fix_prd_rel,
    fix_subtree,
    tooling_criteria,
    write_fix_prd,
)
from kstrl.integration_phase import (
    IntegrationRecord,
    IntegrationRun,
    Phase3Round,
    run_integration_review,
)
from kstrl.integration_state import (
    OUTCOME_CLEAN,
    OUTCOME_NOT_RUN,
    OUTCOME_OPEN_FINDINGS,
    OUTCOME_RED,
    add_fix,
    add_stop,
    fix_components,
    hand_off,
    write_state,
)
from kstrl.manifest import Component, ComponentStatus
from kstrl.scope import RunScope

if TYPE_CHECKING:
    from kstrl.shutdown import StopController


class IntegrationLoop:
    """The rounds of the factory's outer loop, and the review after each."""

    def __init__(self, run: IntegrationRun, stop: StopController | None) -> None:
        self._run = run
        self._stop = stop
        self.record = IntegrationRecord(OUTCOME_NOT_RUN, "the integration review was not reached")

    def rounds(self) -> Iterator[Phase3Round]:
        """Yield one Phase3Round per scheduling pass until the loop stops.

        The factory fills the yielded round and must not replace it: this
        generator reads the same object back after the ``yield``.
        """
        while True:
            phase3 = Phase3Round(
                failures_before=len(self._run.pipeline.factory_result.contract_failures)
            )
            yield phase3
            if phase3.breaker_reset:
                continue
            self.record = run_integration_review(self._run, phase3, self._stop)
            if not self._run.pipeline.factory_config.integration_blocking:
                return
            self.record = decide_round(self._run, phase3, self.record)
            if not self.record.fix:
                project_stop(self._run, self.record)
                return


def decide_round(
    run: IntegrationRun, phase3: Phase3Round, record: IntegrationRecord
) -> IntegrationRecord:
    """Blocking only: build a fix from the round's open code findings, or
    stop on the first rule of design 3.5 that applies. A round that was
    clean, red or not run is its own stop, already recorded."""
    if record.state is None or record.outcome != OUTCOME_OPEN_FINDINGS:
        return record
    findings = _loop_findings(record.state, record)
    scopes = _scope_findings(run, record, findings)
    stop = _stop_reason(run, record, findings)
    if stop is not None:
        return _stop(run, record, *stop)
    return _build_fix(run, phase3, record, findings, scopes)


def _loop_findings(state: dict[str, Any], record: IntegrationRecord) -> list[dict[str, Any]]:
    """This round's opened findings and the carried ones still open."""
    wanted = {*record.opened, *record.still_open}
    return [f for f in state["findings"] if f["id"] in wanted]


def _scope_findings(
    run: IntegrationRun, record: IntegrationRecord, findings: Sequence[dict[str, Any]]
) -> dict[str, FindingScope]:
    """Scope every open finding; hand off, in the state, one that cannot be."""
    scopes: dict[str, FindingScope] = {}
    for finding in findings:
        if finding["status"] != STATUS_OPEN:
            continue
        scoped = finding_scope(finding["locations"], run.manifest, run.pipeline.run_scope)
        if scoped.handoff:
            assert record.state is not None
            hand_off(record.state, finding["id"], scoped.handoff, run.run_id, record.sha)
        else:
            scopes[finding["id"]] = scoped
    return scopes


def _stop_reason(
    run: IntegrationRun, record: IntegrationRecord, findings: Sequence[dict[str, Any]]
) -> tuple[str, str] | None:
    """The first stop rule that applies, as (outcome, reason), or None."""
    handed = [f["id"] for f in findings if f["status"] != STATUS_OPEN]
    if handed:
        return OUTCOME_RED, (
            f"{len(handed)} findings cannot become a fix story (register or unscoped): "
            f"{', '.join(handed)}"
        )
    if record.still_open and not record.closed:
        return OUTCOME_OPEN_FINDINGS, (
            f"no progress: no carried finding closed ({', '.join(record.still_open)} still open)"
        )
    built = len(fix_components(run.manifest))
    bound = run.pipeline.factory_config.integration_max_rounds
    if built >= bound:
        return OUTCOME_OPEN_FINDINGS, f"bound reached: {built} of {bound} fix components built"
    unfinished = [
        c.id for c in run.manifest.components if c.status != ComponentStatus.COMPLETED.value
    ]
    if unfinished:
        return OUTCOME_RED, (
            f"the feature is not complete ({', '.join(unfinished)}), so a fix "
            "that depends on it would never be scheduled"
        )
    return None


def _stop(
    run: IntegrationRun, record: IntegrationRecord, outcome: str, reason: str
) -> IntegrationRecord:
    """Record a loop stop in the state before anything projects it."""
    assert record.state is not None
    add_stop(record.state, run.run_id, record.sha, outcome, reason, record.evidence, gates=True)
    error = write_state(run.root_dir, record.state)
    if error:
        run.ui.err(f"  Integration state write failed: {error}")
    run.ui.warn(f"  Integration loop stopped: {outcome}: {reason}")
    return dataclasses.replace(record, outcome=outcome, reason=reason)


def _build_fix(
    run: IntegrationRun,
    phase3: Phase3Round,
    record: IntegrationRecord,
    findings: Sequence[dict[str, Any]],
    scopes: dict[str, FindingScope],
) -> IntegrationRecord:
    """Three stages in this order (design 3.4): state entry, PRD, manifest.
    A crash between two leaves a state the next run's reconcile names."""
    assert record.state is not None
    component_id = fix_id(len(fix_components(run.manifest)) + 1)
    ids = [f["id"] for f in findings]
    scope = [*dict.fromkeys(p for f in findings for p in scopes[f["id"]].paths)]
    scope.append(fix_subtree(component_id))
    try:
        tooling = tooling_criteria(run.manifest, run.root_dir)
    except (OSError, ValueError) as exc:
        return _stop(run, record, OUTCOME_RED, f"a feature PRD could not be read: {exc}")
    prd = build_fix_prd(component_id, findings, scopes, scope, tooling, run.decisions)
    add_fix(
        record.state, component_id, ids, scope, fix_prd_rel(component_id), run.run_id, record.sha
    )
    error = write_state(run.root_dir, record.state)
    if error:
        return _stop(run, record, OUTCOME_RED, f"the fix's state entry was not written: {error}")
    try:
        write_fix_prd(run.root_dir, component_id, prd)
    except OSError as exc:
        return _stop(run, record, OUTCOME_RED, f"the fix PRD was not written: {exc}")
    comp = append_fix_component(run.manifest, run.manifest_path, component_id, ids)
    refusal = _make_visible(run, comp)
    if refusal:
        return _stop(run, record, OUTCOME_RED, refusal)
    # The round's integrated test failure is now finding IF-n inside the fix,
    # so its "no blame attributed" line no longer describes the run.
    del run.pipeline.factory_result.contract_failures[phase3.failures_before :]
    run.ui.info(f"  Integration fix {component_id} built for {', '.join(ids)}; scheduling it")
    return dataclasses.replace(record, fix=component_id)


def _make_visible(run: IntegrationRun, comp: Component) -> str:
    """Make the appended fix visible to what the run resolved at start
    (design 3.4). Returns a refusal, or "" when the fix may launch."""
    from kstrl.factory import (
        _preflight_component_branches,
        _preflight_component_scope,
        _record_run_scope,
        _run_plan_event,
    )

    errors = run.manifest.validate_dag()
    if errors:
        return f"the manifest extended with {comp.id} fails the DAG check: {'; '.join(errors)}"
    pipeline = run.pipeline
    # The one reader of the snapshot: the pre-launch gate, _submit_args and
    # Phase 1 all read pipeline.run_scope. No existing id is re-resolved.
    pipeline.run_scope = pipeline.run_scope.with_component(comp, run.root_dir, pipeline.base_config)
    # Every other component is COMPLETED (_stop_reason), so the fix is the
    # only PENDING component these two preflights look at.
    errors = _preflight_component_scope(run.manifest, pipeline.run_scope)
    if not errors and pipeline.factory_config.use_worktrees:
        errors = _preflight_component_branches(run.manifest, run.root_dir, run.ui, {})
    if errors:
        return f"{comp.id} was refused before launch: {'; '.join(errors)}"
    pipeline.bus.emit(_run_plan_event(run.manifest, pipeline.factory_config))
    _record_run_scope(
        RunScope(MappingProxyType({comp.id: pipeline.run_scope.for_component(comp.id)})),
        pipeline.bus,
        run.ui,
        manifest=run.manifest,
    )
    return ""


def project_stop(run: IntegrationRun, record: IntegrationRecord) -> None:
    """Blocking only: a stop that is not clean fails the run through
    contract_failures, which the exit code already reads, and leaves one
    HALTED_RUN inbox item. The state entry was written first."""
    if record.outcome == OUTCOME_CLEAN:
        return
    open_ids = _open_ids(record)
    lines = [f"integration {fid}: {_summary(record, fid)}" for fid in open_ids]
    if not lines:
        lines = [f"integration {record.outcome}: {record.reason}"]
    run.pipeline.factory_result.contract_failures.extend(lines)
    run.pipeline.record_integration_halt(
        f"{record.outcome}: {record.reason}", open_ids, str(record.evidence or "")
    )


def _open_ids(record: IntegrationRecord) -> list[str]:
    """This round's findings and carried findings not closed at the stop."""
    if record.state is None:
        return []
    wanted = {*record.opened, *record.still_open}
    return [
        f["id"]
        for f in record.state["findings"]
        if f["id"] in wanted and f["status"] != STATUS_CLOSED
    ]


def _summary(record: IntegrationRecord, finding_id: str) -> str:
    assert record.state is not None
    finding = next(f for f in record.state["findings"] if f["id"] == finding_id)
    first = next((line for line in str(finding["text"]).splitlines() if line.strip()), "")
    return f"{finding['status']}: {first.strip()[:200]}"
