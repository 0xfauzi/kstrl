"""The acceptance checks that hold a run.

They are the L1 plan gate, the carried checks and the Phase 3 base.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import git, git_base
from kstrl.acceptance import PinnedPlan, replay_base
from kstrl.acceptance_carried import carried_on_base, carry_passed
from kstrl.acceptance_design import (
    NOT_REPRODUCED,
    design_plan,
    unreproduced,
)
from kstrl.contract import (
    ContractResult,
)
from kstrl.decisions import (
    DecisionRegister,
)
from kstrl.event_catalog import ComponentFailed
from kstrl.factory_autonomy import _LadderOutcome
from kstrl.factory_config import (
    FactoryConfig,
)
from kstrl.factory_preflight import (
    _report_preflight,
)
from kstrl.factory_worktrees import (
    _has_merged,
)
from kstrl.manifest import (
    ComponentStatus,
    Manifest,
)
from kstrl.pipeline import ComponentPipeline
from kstrl.pipeline_state import _iso_now
from kstrl.plan_gate import run_plan_gate

if TYPE_CHECKING:
    from kstrl.events import EventBus
    from kstrl.factory_result import FactoryResult
    from kstrl.integration_phase import Phase3Round
    from kstrl.observability import NotifyHooks
    from kstrl.runstate import RunState
    from kstrl.ui.base import UI


def _plan_gated(
    register: DecisionRegister | None,
    pipeline: ComponentPipeline,
    ladder: _LadderOutcome | None,
) -> DecisionRegister | int:
    """The preflights' bound register, or the exit code that ends the run.

    2 when a preflight refused (``register is None``), and then the plan
    gate asks nothing. Otherwise the #602 plan gate runs on the CLAMPED
    bundle, after every pre-spend refusal and before the feature base is
    stamped and parked merges are applied, so a plan nobody approved
    pushes, merges and runs nothing: its park is 1 and a rejection 2.
    """
    if register is None or _refused_carried(pipeline) or _refused_acceptance_design(pipeline):
        return 2
    stop = run_plan_gate(pipeline, ladder.bundle if ladder is not None else None)
    if stop is not None:
        return stop
    return 2 if _refused_acceptance_base(pipeline) else register


def _refused_carried(pipeline: ComponentPipeline) -> bool:
    """Replay the checks of earlier features on the base (#466), after
    every pre-spend refusal and before the designer and the plan gate; True
    when one does not pass there, which files an inbox item for it."""
    config = pipeline.factory_config
    config.acceptance_carried, errors = carried_on_base(
        pipeline.root_dir,
        config,
        pipeline.manifest,
        pipeline.run_id,
        pipeline.ui,
        pipeline.record_carried_halt,
        pipeline.inbox_config,
    )
    return _report_preflight(
        pipeline.ui, "an earlier feature's acceptance check does not hold on the base", errors
    )


def _refused_acceptance_design(pipeline: ComponentPipeline) -> bool:
    """Ask the verification designer for the acceptance checks (#700 slice
    7) when the run asked for them and none were designed for this plan yet:
    after every pre-spend refusal and before the plan gate, so the one
    approval covers them. True when the run must not start."""
    config = pipeline.factory_config
    if not config.design_acceptance or config.acceptance_plan is not None:
        return False
    config.acceptance_plan, errors = design_plan(pipeline)
    return _report_preflight(pipeline.ui, "the verification designer wrote no usable plan", errors)


def _refused_acceptance_base(pipeline: ComponentPipeline) -> bool:
    """Replay the acceptance checks on the base, after the plan gate and
    before the first engineer (#700 slice 4, decision 2(a)); True when the
    base refuses the plan. A plan the base accepted is pinned on the
    manifest, which the run's next save writes."""
    config, manifest = pipeline.factory_config, pipeline.manifest
    plan, stack = config.acceptance_plan, config.project_stack
    if plan is None or stack is None:
        return False
    config.acceptance_base, errors = replay_base(
        pipeline.root_dir, stack, plan, manifest.base_branch, pipeline.run_id, config, pipeline.ui
    )
    if errors and plan.designed:
        errors.append(
            f"A model wrote these checks. Delete {plan.source} and run again to ask the "
            "verification designer for new ones, which the plan gate then asks about."
        )
    if _report_preflight(pipeline.ui, "the acceptance checks do not hold on the base", errors):
        return True
    if _report_preflight(
        pipeline.ui, NOT_REPRODUCED, unreproduced(pipeline, plan, config.acceptance_base)
    ):
        return True
    manifest.acceptance_digest = plan.digest
    return False


def _stamp_feature_base(manifest: Manifest, manifest_path: Path, root_dir: Path, ui: UI) -> None:
    """Record the commit this feature starts from, once (#481).

    Called before anything is scheduled or merged. An existing stamp is
    never rewritten, so a resume keeps the commit its first run recorded.
    When a component has already merged, the commit before the first
    merge can no longer be known: ``merge_sha`` may be empty for a merged
    PR, manifest order is not merge order, and a resumed run can hold
    merges from an earlier invocation. The stamp then stays empty and the
    run says so, as it does when the base does not resolve.
    """
    if manifest.feature_base_sha:
        return
    if any(_has_merged(c) for c in manifest.components):
        ui.warn(
            "  Feature base unknown: a component merged before any run recorded "
            "where this feature started"
        )
        return
    try:
        manifest.feature_base_sha = git_base.resolve_base_sha(manifest.base_branch, root_dir)
    except git.GitDiffError as exc:
        ui.warn(f"  Feature base unknown: {exc}")
        return
    manifest.save(manifest_path)
    ui.kv("Feature base", manifest.feature_base_sha)


def _plan_of_kept_checks(factory_config: FactoryConfig) -> PinnedPlan | None:
    """The pinned plan narrowed to the checks the heads ran. A designed check
    the base removed (#753) runs on no head, so Phase 3 must not replay it:
    a vacuous designed check cannot block a run (owner decision of 2026-10-07)."""
    plan = factory_config.acceptance_plan
    base = factory_config.acceptance_base
    if plan is None or base is None:
        return plan
    return replace(plan, components=dict(base.kept))


def _carry(pipeline: ComponentPipeline, results: Sequence[ContractResult]) -> None:
    """After a Phase 3 round passed on the base its components merged into,
    keep their checks for every later run to replay (#466). A failed write
    fails the run: the components are merged, and a later feature could
    break them unseen. Only the integrated check of a run with one PR per
    component tests the base itself (``tested_sha``), and only a component
    whose change reached the base is carried (``carry_passed``)."""
    error = carry_passed(
        pipeline.root_dir,
        pipeline.factory_config,
        pipeline.manifest,
        [comp for cr in results for comp in cr.components_tested],
        results[0].tested_sha if results else "",
        pipeline.run_id,
    )
    if error:
        pipeline.ui.err(f"  {error}")
        pipeline.factory_result.contract_failures.append(error)


def _halt_carried(pipeline: ComponentPipeline, failures: Sequence[ContractResult]) -> None:
    """File one inbox item for each earlier feature's check that failed a
    Phase 3 with nothing left to retry (#466). A run that merges its
    components only after Phase 3 never puts the break on the base, so the
    base replay never files one, and a check a change made obsolete could
    otherwise never retire."""
    for cr in failures:
        for entry in cr.carried_failed:
            pipeline.record_carried_halt(entry, f"Phase 3 tier {cr.tier}", "the run failed on it")


def _resolve_round_base(manifest: Manifest, root_dir: Path, ui: UI) -> str:
    """The commit the base branch names now, for one Phase 3 round (#481).

    Resolved once per round and handed to the integrated check, so the
    tree it tests is one fixed commit even if a merge moves the branch
    meanwhile. "" when the base does not resolve; the integrated check
    then fails having tested nothing.
    """
    try:
        return git_base.resolve_base_sha(manifest.base_branch, root_dir)
    except git.GitDiffError as exc:
        ui.warn(f"  Phase 3 base unresolved: {exc}")
        return ""


def _stop_reason(result: ContractResult) -> str:
    """Why a failed tier's breaker is not retried: a failed held-out check
    gets no retry (#700 decision 3); otherwise its retries are spent."""
    return "a held-out check failed, no retry" if result.held_out else "retries exhausted"


def _record_contract_event(
    cr: ContractResult, root_dir: Path, ui: UI, run_id: str, manifest: Manifest
) -> None:
    """Append a contract_result event to the evolution journal.

    Written for pass AND fail (R0.3): the journal is the audit trail
    for every contract phase outcome, including intermediate failures
    that a breaker retry later resolves. Non-fatal on I/O errors,
    matching EvolutionJournal.record_run.
    """
    from kstrl.evolution import EvolutionJournal
    from kstrl.journal_rows import JOURNAL_SCHEMA_VERSION

    # R2.1: honor [evolution] in kstrl.toml + env, resolved against
    # the factory root rather than whatever the process CWD is.
    journal = EvolutionJournal.open(root_dir, warn=ui.warn)
    if journal is None:
        return
    entry = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id,
        "project": manifest.project_name,
        "component_id": cr.breaker or "",
        "event_type": "contract_result",
        "tier": cr.tier,
        "passed": cr.passed,
        "breaker": cr.breaker,
        "components_tested": cr.components_tested,
        "test_output": cr.test_output[:2000],
        "duration_seconds": round(cr.duration_seconds, 2),
    }
    try:
        journal.append_entries([entry])
    except OSError as exc:
        # Evolution recording is non-fatal, but never silent (R6.1).
        ui.warn(f"  Evolution journal write failed (non-fatal): {exc}")


def _reset_contract_breakers(
    failures: list[ContractResult],
    phase3_round: Phase3Round,
    manifest: Manifest,
    manifest_path: Path,
    factory_config: FactoryConfig,
    ui: UI,
    pipeline: ComponentPipeline,
    run_state: RunState,
    factory_result: FactoryResult,
) -> None:
    """Send each retryable contract breaker back to PENDING (#776, B3)."""
    # Reset retryable breakers to PENDING; the outer loop then
    # re-enters scheduling so the promised retry actually runs.
    for cr in failures:
        if not cr.breaker:
            continue
        breaker = manifest.get_component(cr.breaker)
        if breaker and breaker.retries < factory_config.max_retries and not cr.held_out:
            # R3.3: the completed attempt's findings are superseded
            # by the contract-triggered re-run; journal them before
            # the retry increments the attempt counter.
            pipeline.journal_superseded_findings(breaker)
            breaker.retries += 1
            breaker.status = ComponentStatus.PENDING.value
            breaker.error = f"Contract test failed at tier {cr.tier}"
            run_state.component_failure_signatures[cr.breaker] = [
                f"contract:tier_{cr.tier}",
            ]
            # Remove from completed list
            if cr.breaker in factory_result.completed:
                factory_result.completed.remove(cr.breaker)
            # The context write lives on the pipeline (#247): it is
            # one of exactly two writers of the retry context and
            # both must merge the attempt's phase readings, or a
            # contract failure re-raises a review finding the
            # reviewer already cleared. breaker.retries was
            # incremented above and so names the attempt whose
            # contract test failed; see record_contract_failure.
            pipeline.record_contract_failure(
                cr.breaker,
                breaker.retries,
                cr.test_output[:500],
            )
            manifest.save(manifest_path)
            phase3_round.breaker_reset = True
            ui.warn(f"  Contract breaker '{cr.breaker}' sent back for retry")


def _record_contract_failures(
    failures: list[ContractResult],
    manifest: Manifest,
    manifest_path: Path,
    ui: UI,
    pipeline: ComponentPipeline,
    run_state: RunState,
    factory_result: FactoryResult,
    bus: EventBus,
    notify: NotifyHooks,
) -> None:
    """Record each contract failure that has no retry left (#776, B3)."""
    # Terminal contract failure: nothing left to retry. Record it in
    # the run result so the summary shows it and the exit code is
    # nonzero (previously this fell through silently and the run
    # exited 0 with broken integrated code).
    for cr in failures:
        detail = (cr.test_output or "").strip()
        summary_line = detail.splitlines()[-1][:200] if detail else ""
        if cr.breaker:
            breaker = manifest.get_component(cr.breaker)
            if breaker is not None:
                breaker.status = ComponentStatus.FAILED.value
                breaker.error = f"Contract test failed at tier {cr.tier} ({_stop_reason(cr)})"
                breaker.completed_at = _iso_now()
                breaker.failed_phase = "contract"
                breaker.failed_check = f"tier_{cr.tier}"
                run_state.component_failure_signatures[cr.breaker] = [
                    f"contract:tier_{cr.tier}",
                ]
            if cr.breaker in factory_result.completed:
                factory_result.completed.remove(cr.breaker)
            if cr.breaker not in factory_result.failed:
                factory_result.failed.append(cr.breaker)
            bus.emit(
                ComponentFailed(
                    component=cr.breaker,
                    error=(f"Contract test failed at tier {cr.tier} ({_stop_reason(cr)})"),
                )
            )
            notify.fire_first_failure(
                cr.breaker,
                f"Contract test failed at tier {cr.tier} ({_stop_reason(cr)})",
            )
            factory_result.contract_failures.append(
                f"tier {cr.tier}: breaker '{cr.breaker}' ({_stop_reason(cr)}): {summary_line}"
            )
        else:
            factory_result.contract_failures.append(
                f"tier {cr.tier}: contract tests failed, no blame "
                f"attributed (components: "
                f"{', '.join(cr.components_tested)}): {summary_line}"
            )
        ui.err(f"  Contract failure recorded for tier {cr.tier}; run will exit nonzero")
    _halt_carried(pipeline, failures)
    manifest.save(manifest_path)
