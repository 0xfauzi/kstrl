"""The finish of a factory run: PRs, release decision, summary and records (#776, B3)."""

from __future__ import annotations

import time

from kstrl.acceptance import pr_isolation
from kstrl.events import RunCompleted, print_usage_rollup
from kstrl.factory.factory_autonomy import _record_autonomy
from kstrl.factory.factory_run import RunContext
from kstrl.factory.integration_phase import report_integration
from kstrl.pipeline import (
    RELEASE_REF_RULE,
    FactoryResult,
    ReleaseInputs,
    create_prs_in_order,
    create_single_pr,
    ids_in_status,
    iso_now,
    release_ref_from,
    release_withheld,
    report_awaiting_approval,
    resolve_exit_code,
    run_is_clean,
)
from kstrl.plan import ComponentStatus


def _finish_run(ctx: RunContext) -> FactoryResult:
    """Create the last PRs, decide the release, print the summary and record the run."""
    # Create PRs for any remaining components that weren't handled per-component
    # (e.g. single-pr mode, or stragglers from parallel execution)
    _create_remaining_prs(ctx)

    # R0.2: collect components parked awaiting merge confirmation. Built
    # from the manifest (not accumulated during the run) so it reflects
    # the final state after any crash-recovery re-poll. Moved here, AHEAD
    # of the release block below, so run_is_clean reads a populated
    # factory_result.merge_pending rather than the permanent empty list
    # it was rebuilt into seventeen lines below the release computation
    # before #154's fix round (B11): nothing between this line and the
    # old site mutates a component's status, so the move changes nothing
    # else this function reports.
    ctx.factory_result.merge_pending = ids_in_status(ctx.manifest, ComponentStatus.MERGE_PENDING)
    ctx.factory_result.awaiting_approval = ids_in_status(
        ctx.manifest, ComponentStatus.AWAITING_APPROVAL
    )

    release_ref, release_withheld_reason = _release_decision(ctx)

    # Summary
    factory_duration = time.monotonic() - ctx.factory_start
    ctx.bus.emit(
        RunCompleted(
            completed=len(ctx.factory_result.completed),
            failed=len(ctx.factory_result.failed),
            skipped=len(ctx.factory_result.skipped),
            duration_seconds=round(factory_duration, 2),
            release_ref=release_ref,
            release_ref_rule=RELEASE_REF_RULE,
            release_withheld=release_withheld_reason,
        )
    )
    # Detach (not close) the console bus: post-run cli narration must
    # not reopen the run's files. The file sinks themselves close.
    for _sink in ctx.run_file_sinks:
        ctx.bus.remove_sink(_sink)
        _sink.close()

    _print_summary(ctx, factory_duration)

    ctx.factory_result.exit_code = resolve_exit_code(
        ctx.factory_result,
        ctx.manifest,
        ctx.ui,
        stopped=ctx.stop is not None and ctx.stop.is_set(),
    )

    # R3.3: the run reached its terminal state; stamp the manifest so a
    # resume (and Linear, later) can tell a finished run from a crash.
    # Stamped BEFORE the completion notification so a hook that reads
    # the manifest sees the terminal state.
    ctx.manifest.completed_at = iso_now()
    ctx.manifest.save(ctx.manifest_path)

    _record_autonomy(ctx)

    # R3.2: run-end notification. Fires on every run that reached the
    # summary, whatever the outcome; early refusals (invalid DAG, held
    # lock, stale branches) never notify because no work was started.
    ctx.notify.fire_complete(
        f"completed={len(ctx.factory_result.completed)} "
        f"failed={len(ctx.factory_result.failed)} "
        f"skipped={len(ctx.factory_result.skipped)} "
        f"merge_pending={len(ctx.factory_result.merge_pending)} "
        f"exit_code={ctx.factory_result.exit_code}"
    )

    _journal_run(ctx)

    return ctx.factory_result


def _create_remaining_prs(ctx: RunContext) -> None:
    """Create the PRs that no per-component step created."""
    if ctx.factory_config.create_prs:
        if ctx.factory_config.single_pr:
            _create_single_pr(ctx)
        else:
            _create_straggler_prs(ctx)


def _create_single_pr(ctx: RunContext) -> None:
    """Create the one PR of a single-pr run."""
    result = create_single_pr(
        ctx.manifest,
        ctx.root_dir,
        ctx.ui,
        isolation=pr_isolation(ctx.factory_config),
        requirements=ctx.factory_config.requirements,
    )
    if result:
        ctx.factory_result.pr_urls.append(result[1])
    ctx.manifest.save(ctx.manifest_path)


def _create_straggler_prs(ctx: RunContext) -> None:
    """Create the PRs of the completed components that have none."""
    # Per-component PRs are created in _handle_result; only handle stragglers
    remaining = [c for c in ctx.manifest.components if c.status == "completed" and not c.pr_url]
    if remaining:
        pr_results = create_prs_in_order(
            ctx.manifest,
            ctx.root_dir,
            ctx.ui,
            isolation=pr_isolation(ctx.factory_config),
            requirements=ctx.factory_config.requirements,
        )
        ctx.factory_result.pr_urls.extend(url for _, url in pr_results)
        ctx.manifest.save(ctx.manifest_path)


def _release_decision(ctx: RunContext) -> tuple[str, str]:
    """The run's release ref, and the reason that no release followed it."""
    # R8.7 slice 1 (#154): the run's release ref and the reason no
    # release followed it. Nothing here starts anything; see
    # kstrl/pipeline/release.py for why the last rung cannot be configured open.
    #
    # run_is_clean is the SAME predicate resolve_exit_code's exit code
    # is built from (#154 fix round, A1): before this, run_clean was a
    # second, independent expression that missed the `stopped` and the
    # #263 unfinished-components terms, so an operator-stopped run with
    # one component merged and the rest PENDING recorded
    # release_withheld="no_driver" (fully permitted) while
    # resolve_exit_code returned 130 for the identical run.
    stopped = ctx.stop is not None and ctx.stop.is_set()
    release_ref = release_ref_from(ctx.manifest.components, since=ctx.run_started_at)
    run_clean = run_is_clean(ctx.factory_result, ctx.manifest, stopped=stopped)
    release_withheld_reason = release_withheld(
        ReleaseInputs(
            release_enabled=ctx.run_envelope.release.enabled,
            environment=ctx.run_envelope.release.environment,
            run_clean=run_clean,
            stopped=stopped,
            release_ref=release_ref,
            policy_enabled=ctx.run_envelope.policy.enabled,
            policy_deploy=ctx.run_envelope.policy.deploy,
            ladder_deploy_permitted=(
                ctx.ladder.bundle.deploy_permitted if ctx.ladder is not None else None
            ),
        )
    )
    return release_ref, release_withheld_reason


def _print_summary(ctx: RunContext, factory_duration: float) -> None:
    """Print the summary of the run."""
    ctx.ui.section("Factory: Summary")
    ctx.ui.kv("Completed", str(len(ctx.factory_result.completed)))
    ctx.ui.kv("Failed", str(len(ctx.factory_result.failed)))
    ctx.ui.kv("Skipped", str(len(ctx.factory_result.skipped)))
    # R3.3 failure summary: per failed component, which gate fired and
    # where the last attempt's evidence lives, so the run is diagnosable
    # without reading raw JSON.
    if ctx.factory_result.failed:
        _print_failure_summary(ctx)
    if ctx.factory_result.contract_failures:
        ctx.ui.kv("Contract failures", str(len(ctx.factory_result.contract_failures)))
        for line in ctx.factory_result.contract_failures:
            ctx.ui.err(f"  {line}")
    report_integration(ctx.integration_record, ctx.ui)
    if not ctx.factory_config.create_prs:
        _print_branches_to_merge(ctx)
    if ctx.factory_result.merge_pending:
        ctx.ui.kv("Merge pending", str(len(ctx.factory_result.merge_pending)))
    report_awaiting_approval(ctx.factory_result, ctx.ui)
    ctx.ui.kv("Duration", f"{factory_duration:.0f}s")
    # R3.1 usage rollup: per component, per phase, plus the run total.
    print_usage_rollup(
        ctx.ui,
        ctx.pipeline.usage_meter,
        ctx.pipeline.run_usage,
        title="Usage rollup",
    )
    if ctx.pipeline.token_budget_exceeded():
        ctx.ui.err(
            f"TOKEN BUDGET EXCEEDED: {ctx.pipeline.run_usage.total_tokens} total tokens "
            f"recorded >= max_total_tokens ({ctx.factory_config.max_total_tokens})"
        )
    if ctx.pipeline.cost_budget_exceeded():
        ctx.ui.err(
            f"COST BUDGET EXCEEDED: ${ctx.pipeline.run_usage.cost_usd:.6f} recorded "
            f">= max_cost_usd (${ctx.factory_config.max_cost_usd})"
        )
    if ctx.factory_result.pr_urls:
        ctx.ui.kv("PRs created", str(len(ctx.factory_result.pr_urls)))
        for url in ctx.factory_result.pr_urls:
            ctx.ui.info(f"  {url}")
    if ctx.factory_result.merge_pending:
        ctx.ui.warn(
            "Some PR merges are unconfirmed; re-run the factory to "
            "re-poll them: " + ", ".join(ctx.factory_result.merge_pending)
        )


def _print_branches_to_merge(ctx: RunContext) -> None:
    """With no PRs, name the branches that hold the completed work: each
    component's branch merged its dependencies' branches (#543), so those
    of completed components no other completed component depends on."""
    done = [c for c in ctx.manifest.components if c.id in ctx.factory_result.completed]
    needed = {dep for comp in done for dep in comp.dependencies}
    branches = sorted({c.branch_name for c in done if c.id not in needed})
    if branches:
        ctx.ui.kv("Merge by hand", ", ".join(branches))


def _print_failure_summary(ctx: RunContext) -> None:
    """Print one block for each failed component."""
    ctx.ui.subsection("Failure summary")
    for failed_id in ctx.factory_result.failed:
        _print_failed_component(ctx, failed_id)


def _print_failed_component(ctx: RunContext, failed_id: str) -> None:
    """Print the gate, the error and the evidence of one failed component."""
    failed_comp = ctx.manifest.get_component(failed_id)
    if failed_comp is None:
        return
    ctx.ui.err(
        f"  {failed_id}: "
        f"phase={failed_comp.failed_phase or 'unknown'} "
        f"check={failed_comp.failed_check or 'unknown'} "
        f"(attempt {failed_comp.retries + 1})"
    )
    if failed_comp.error:
        ctx.ui.info(f"    error: {failed_comp.error.splitlines()[0]}")
    if failed_comp.evidence_worktree:
        ctx.ui.info(f"    worktree: {failed_comp.evidence_worktree}")
    elif ctx.factory_config.use_worktrees:
        ctx.ui.info("    worktree: removed (re-run with --keep-worktrees-on-failure to keep it)")
    if failed_comp.evidence_debug_dir:
        ctx.ui.info(f"    raw outputs: {failed_comp.evidence_debug_dir}")
    if failed_comp.journal_offset_start >= 0 and ctx.journal_path is not None:
        end = str(failed_comp.journal_offset_end) if failed_comp.journal_offset_end >= 0 else "end"
        ctx.ui.info(
            f"    journal: {ctx.journal_path} bytes [{failed_comp.journal_offset_start}:{end}]"
        )
    ctx.ui.info(f"    retry with: ks retry {failed_id}")


def _journal_run(ctx: RunContext) -> None:
    """Record the run in the evolution journal."""
    # Record run to evolution journal
    try:
        from kstrl.learning import EvolutionConfig, EvolutionJournal

        evo_config = EvolutionConfig.load(ctx.root_dir)
        if evo_config.enabled:
            journal = EvolutionJournal(evo_config)
            journal.record_run(
                ctx.run_id,
                ctx.manifest,
                ctx.factory_result,
                usage_by_component={
                    comp_id: {phase: totals.to_dict() for phase, totals in phases.items()}
                    for comp_id, phases in ctx.pipeline.usage_meter.items()
                },
                run_usage=ctx.pipeline.run_usage.to_dict(),
                failure_signatures=ctx.run_state.component_failure_signatures,
                fact_utilization={
                    comp_id: util.to_dict()
                    for comp_id, util in ctx.pipeline.fact_utilization.items()
                },
            )
    except Exception as exc:
        # Evolution recording is non-fatal, but never silent (R6.1).
        ctx.ui.warn(f"Evolution journal recording failed (non-fatal): {exc}")
