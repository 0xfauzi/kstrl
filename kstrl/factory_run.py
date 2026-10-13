"""The start steps of a factory run and the state that its steps share (#776, B3).

``_run_factory_locked`` builds one ``RunContext`` and gives it to the
finish steps. The start steps divided out of it are here: they resolve
the run configuration, open the sinks, announce the run, take over the
manifest and resolve the launch values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.breaker import BreakerConfig
from kstrl.events import (
    EventBus,
    EventSink,
    JsonlSink,
    NotifyConfig,
    NotifyHooks,
    NullProgressLog,
    ProgressLog,
    RunPaths,
    RunPlan,
    RunStarted,
    V1CompatSink,
)
from kstrl.factory_acceptance import _stamp_feature_base
from kstrl.factory_agents import (
    AdversarialAgentSelection,
    resolve_engineer_sandbox,
    warn_unsandboxable_reviewers,
)
from kstrl.factory_autonomy import _report_ladder, _resolve_ladder
from kstrl.factory_config import FactoryConfig, warn_unreachable_gates
from kstrl.factory_launch import (
    LaunchPlan,
    _print_execution_header,
    _resolve_max_parallel,
    build_launch_plan,
)
from kstrl.factory_preflight import (
    _emit_stack_confirmation,
    _record_run_scope,
    report_preflight,
)
from kstrl.factory_worker import report_operator_files
from kstrl.factory_worktrees import _interrupted_run_branches
from kstrl.git import kstrl_version
from kstrl.knowledge_facts import KnowledgeConfig
from kstrl.linear import LinearConfig, build_linear_sink
from kstrl.plan import ComponentStatus, RunScope
from kstrl.proc import TimeoutConfig
from kstrl.runenvelope import RunEnvelope
from kstrl.runstate import RunState
from kstrl.sandbox import warn_unconfined

if TYPE_CHECKING:
    from kstrl.autonomy_config import AutonomyConfig
    from kstrl.config import KstrlConfig
    from kstrl.decisions import DecisionRegister
    from kstrl.factory_autonomy import _LadderOutcome
    from kstrl.factory_result import FactoryResult
    from kstrl.integration_phase import IntegrationRecord
    from kstrl.pipeline import ComponentPipeline
    from kstrl.plan import Manifest
    from kstrl.proc import StopController
    from kstrl.ui import UI
    from kstrl.verify_model import VerifyConfig


@dataclass(frozen=True)
class RunContext:
    """References to the objects of one run, never copies of them.

    The class is frozen so that a step cannot rebind a field. A step
    changes the run through the objects that a field refers to
    (``factory_config``, ``manifest``, ``run_state``, ``factory_result``
    and ``pipeline``). A copy of one of them would leave a green suite
    and no effect (#193).
    """

    manifest: Manifest
    manifest_path: Path
    factory_config: FactoryConfig
    ui: UI
    root_dir: Path
    stop: StopController | None
    run_id: str
    run_started_at: str
    factory_start: float
    factory_result: FactoryResult
    run_state: RunState
    run_envelope: RunEnvelope
    autonomy_config: AutonomyConfig
    ladder: _LadderOutcome | None
    bus: EventBus
    run_file_sinks: list[EventSink]
    journal_path: Path | None
    pipeline: ComponentPipeline
    notify: NotifyHooks
    integration_record: IntegrationRecord


def _run_plan_event(manifest: Manifest, factory_config: FactoryConfig) -> RunPlan:
    """The component DAG plus budget caps (chunk 4). Emitted at run start and
    again when the integration loop appends a fix component (#483), because
    a reader replaces its plan only on RunPlan."""
    return RunPlan(
        components=tuple(
            {"id": c.id, "title": c.title, "deps": list(c.dependencies)}
            for c in manifest.components
        ),
        max_total_tokens=factory_config.max_total_tokens,
        max_adversarial_calls=factory_config.max_adversarial_calls,
        max_cost_usd=factory_config.max_cost_usd,
    )


def _resolve_envelope(
    root_dir: Path,
    factory_config: FactoryConfig,
    ui: UI,
    factory_result: FactoryResult,
) -> tuple[RunEnvelope, AutonomyConfig, _LadderOutcome | None] | None:
    """The run configuration envelope, the autonomy config and the ladder.

    None when the envelope cannot be resolved: the refusal is reported and
    ``factory_result`` carries exit code 2.
    """
    # #192: the run's config envelope, resolved ONCE, HERE, and injected
    # into the pipeline, which never resolves one of its own.
    # kstrl/runenvelope.py records the divergence that costs.
    #
    # Placed above the run directory's sinks deliberately, and that is
    # the round-2 fix rather than the original placement. A section the
    # entry preflight passed can be broken by the time this line runs -
    # on `ks factory --spec` the architect spends 119 to 210 seconds
    # between the two - and resolving seven sections in front of the run
    # means seven that can reject. Rejecting HERE gives the operator the
    # sentence and exit code 2 every other pre-spend refusal gives, and
    # it happens before the run directory exists, so #257's invariant
    # (nothing between the sinks and record_architect_usage returns
    # early) is untouched.
    #
    # What it does to the money: a refusal here leaves no factory run, so
    # the architect's spend is only in the decompose run it ran as (#567).
    # `serve` charges that run to the launch whose process wrote it, the
    # same path a spec blocker takes (#587).
    resolved = RunEnvelope.resolve(
        root_dir,
        policy_override=factory_config.policy_config,
    )
    run_envelope = resolved.envelope
    if run_envelope is None:
        report_preflight(
            ui,
            "the run configuration cannot be resolved",
            list(resolved.problems),
        )
        factory_result.exit_code = 2
        return None
    # Off the envelope rather than a second AutonomyConfig.load: the two
    # reads were nine lines apart and cost two parses of the same file
    # (measured 0.988ms, and a nested toml_parse_scope does not collapse
    # them because the inner scope shadows the outer cache).
    autonomy_config = run_envelope.autonomy
    # R8.2: the level this run OPERATES at, resolved BEFORE the pipeline
    # is constructed so the pipeline's envelope is the clamped one by
    # construction rather than by a re-assignment 155 lines below it.
    # Only the RESOLUTION is here; the bus event, the operator warnings
    # and the FactoryConfig flag overrides stay at their original place
    # below, where the sinks exist and the ordering against
    # _adversarial_phase_gates is unchanged.
    run_envelope, ladder = _resolve_ladder(run_envelope, factory_config, root_dir)
    return run_envelope, autonomy_config, ladder


def _open_run_sinks(
    factory_config: FactoryConfig,
    root_dir: Path,
    run_id: str,
    ui: UI,
    bus: EventBus,
) -> tuple[ProgressLog, Path | None, RunPaths | None, list[EventSink], RunPaths]:
    """The progress log, the file sinks of the run and the accounting paths."""
    # Set up progress log. R3.2: defaults ON under .kstrl/ so a
    # walk-away run always leaves an event trail `ks status` can
    # join; [factory] progress_log_enabled = false (or env) opts out.
    # Every event carries run_id so runs sharing the default file stay
    # distinguishable.
    # Dual-write (TUI rewrite chunk 3): typed schema-v2 events go to
    # .kstrl/runs/<run_id>/events.jsonl via the EventBus; V1CompatSink
    # delegates the v1-named subset to a real ProgressLog so the
    # progress.jsonl byte format AND its attached ProgressSink
    # observers (Linear, R7.4) stay untouched. progress_log_enabled =
    # false suppresses BOTH files (symmetric opt-out).
    progress_log: ProgressLog
    journal_path: Path | None = None
    run_paths: RunPaths | None = None
    run_file_sinks: list[EventSink] = []
    # R8 (review finding P2-d): accounting storage is allocated for
    # EVERY run, including progress_log_enabled = false. It used to be
    # `run_paths`, which is gated on progress logging, so turning the
    # observability opt-out on also deleted the only place a worker
    # could publish its spend - a killed worker then recorded nothing
    # and the run silently under-reported. Money is not observability:
    # an opt-out may drop the narration, never the meter. This is the
    # same directory as `run_paths` when progress logging is on; when it
    # is off the run writes exactly one small JSON file per component
    # attempt under .kstrl/runs/<run_id>/ and no events at all.
    usage_paths = RunPaths.for_run(root_dir, run_id)
    if not factory_config.progress_log_enabled:
        progress_log = NullProgressLog()
    else:
        log_path = factory_config.progress_log_path or root_dir / ".kstrl" / "progress.jsonl"
        progress_log = ProgressLog(log_path, run_id=run_id, warn=ui.warn)
        journal_path = log_path
        run_paths = RunPaths.for_run(root_dir, run_id)
        run_file_sinks = [
            JsonlSink(run_paths.events_file),
            V1CompatSink(progress_log),
        ]
        for _sink in run_file_sinks:
            bus.add_sink(_sink)
    return progress_log, journal_path, run_paths, run_file_sinks, usage_paths


def _announce_run(
    manifest: Manifest,
    factory_config: FactoryConfig,
    root_dir: Path,
    run_id: str,
    ui: UI,
    bus: EventBus,
    progress_log: ProgressLog,
    architect_run_id: str,
) -> NotifyHooks:
    """Attach the Linear sink, make the notify hooks and emit the start events."""
    # R7.4: Linear sink - mirrors failure/budget events onto the issues
    # the decompose hook mapped in the manifest. Observability only;
    # build_linear_sink returns None (with a warning) rather than ever
    # failing the run, and emit() isolates sink exceptions.
    linear_sink = build_linear_sink(
        manifest,
        factory_config.linear_config or LinearConfig.load(root_dir),
        run_id=run_id,
        warn=ui.warn,
    )
    if linear_sink is not None:
        progress_log.attach_sink(linear_sink)

    # R3.2: notification hooks - each condition fires at most once per
    # run, and hook failures only ever warn.
    notify = NotifyHooks(
        factory_config.notify_config or NotifyConfig.load(root_dir),
        run_id=run_id,
        project=manifest.project_name,
        warn=ui.warn,
    )

    bus.emit(
        RunStarted(
            project=manifest.project_name,
            components=len(manifest.components),
            pid=os.getpid(),
            architect_run_id=architect_run_id,
        )
    )
    # Chunk 4: the component DAG + budget caps as one event, so a
    # dashboard can draw the board without reading the manifest.
    bus.emit(_run_plan_event(manifest, factory_config))
    _emit_stack_confirmation(bus, factory_config.project_stack)
    return notify


def _make_run_state(
    manifest: Manifest,
    root_dir: Path,
    base_config: KstrlConfig,
    ui: UI,
    bus: EventBus,
    factory_result: FactoryResult,
) -> tuple[KnowledgeConfig, RunState, set[str], RunScope]:
    """The knowledge config, the state the scheduler and the pipeline share, and the scope."""
    # Load knowledge config once for the entire factory run, BEFORE the
    # pipeline is constructed. Binding it at construction removes the
    # late-binding accident where the old _handle_result closure read a
    # name that was only assigned further down the function (R7.3).
    knowledge_config = KnowledgeConfig.load(root_dir)

    # #193: the mutable structures the scheduler below and the
    # pipeline SHARE. Constructed once, here, and handed to the
    # pipeline whole at the construction site below. Every field is
    # an ALIAS and nothing may copy one, so everything from here on
    # reads through `run_state.<field>`: a surviving local beside the
    # struct is how one shared object silently becomes two.
    # kstrl/runstate.py carries the measured evidence per field,
    # including the one whose copy the whole suite missed.
    #
    # `factory_result` is the one field that also keeps a local name.
    # It is built far above because the pre-flight refusal path
    # returns it before this line is reached, so it cannot be created
    # here. The local and `run_state.factory_result` are the same
    # object and the suite does see them diverge: a fresh
    # FactoryResult() here gives 11 failed / 176 passed on
    # test_factory + test_pipeline + test_timeout_enforcement against
    # a 187-passed control (#193).
    run_state = RunState(factory_result=factory_result)
    # Components abandoned by the scheduler backstop; their workers may
    # still be alive, so their worktrees are never cleaned up here.
    leaked_component_ids: set[str] = set()

    # #269: every component's write scope, resolved HERE and nowhere
    # else. Both guards read this one snapshot - the in-loop guard
    # through _submit_args -> _run_component -> run_loop, and Phase 1
    # through the pipeline below - so neither has to read a PRD to learn
    # what a component may write, and the file the agent is allowed to
    # edit stops being the file that decides what it may edit. Resolved
    # before the pipeline exists and long before the scheduler can
    # launch anything, which is what makes the value one the agent never
    # had access to, with or without worktrees.
    run_scope = RunScope.resolve(manifest, root_dir, base_config)
    _record_run_scope(run_scope, bus, ui, manifest=manifest)
    return knowledge_config, run_state, leaked_component_ids, run_scope


def _dag_refusal(manifest: Manifest, ui: UI, factory_result: FactoryResult) -> bool:
    """True when the manifest DAG is invalid: the refusal is reported and recorded."""
    # Validate DAG
    ui.section("Factory: Validating DAG")
    dag_errors = manifest.validate_dag()
    if dag_errors:
        for err in dag_errors:
            ui.err(f"  {err}")
        # A refusal before any component runs: 2 under the #452 contract,
        # which is also the code `ks serve` reads as a refusal (#531).
        factory_result.exit_code = 2
        return True
    return False


def _resume_manifest(
    manifest: Manifest,
    root_dir: Path,
    ui: UI,
    pipeline: ComponentPipeline,
) -> dict[str, str]:
    """Reset the components that a crash left in flight and carry the interrupted run.

    Returns the branches that the interrupted run provably made (#460).
    """
    topo_order = manifest.topological_order()
    ui.ok(f"DAG valid: {len(topo_order)} components in dependency order")

    # #460: read BEFORE the reset below rewrites the statuses it keys on,
    # and before _run_preflights prunes the worktree registrations it reads.
    interrupted_branches = _interrupted_run_branches(manifest, root_dir)

    # Crash recovery: reset intermediate states
    for comp in manifest.components:
        if comp.status in (
            ComponentStatus.RUNNING.value,
            ComponentStatus.VERIFYING.value,
        ):
            ui.info(f"  Resetting '{comp.id}' from {comp.status} to PENDING")
            comp.status = ComponentStatus.PENDING.value

    # #463: a run the manifest names that never reached its summary hands
    # its attempts, readings and spend to this one. After the reset above,
    # so the components this run will run again are PENDING; before the
    # run id below is saved, so a resume that dies here leaves the manifest
    # naming the run whose record it had not yet taken over.
    pipeline.carry_interrupted_run()
    return interrupted_branches


def _start_manifest(
    manifest: Manifest,
    manifest_path: Path,
    root_dir: Path,
    ui: UI,
    pipeline: ComponentPipeline,
    run_id: str,
    factory_config: FactoryConfig,
    bus: EventBus,
    ladder: _LadderOutcome | None,
    factory_result: FactoryResult,
) -> dict[str, str] | None:
    """Validate the DAG, take over the manifest and apply the autonomy level.

    None when the DAG is invalid (exit code 2 on ``factory_result``).
    """
    if _dag_refusal(manifest, ui, factory_result):
        return None

    interrupted_branches = _resume_manifest(manifest, root_dir, ui, pipeline)

    # R3.3: persist which run owns this manifest state. completed_at is
    # blanked while the run is in flight and stamped in the summary
    # epilogue, so "did the last run finish?" is answerable from the
    # manifest alone (and later, from Linear).
    manifest.run_id = run_id
    manifest.kstrl_version = kstrl_version()
    manifest.completed_at = ""
    # R8.2: apply this run's permissions from the autonomy level. The
    # bundle is computed at run start and WINS over contradicting config,
    # so a hand-edited flag cannot grant autonomy the ladder never
    # awarded; contradictions are recorded rather than silently dropped.
    # Opt-in: when [autonomy] is disabled the config's own flags stand.
    #
    # RESOLVED far above, before the pipeline was constructed, because
    # the level clamps the envelope (autonomy_level) and the pipeline
    # must hold the clamped one. What is left here is the reporting and
    # the FactoryConfig overrides, which stay in this position for two
    # orderings that are load-bearing: the bus and its sinks exist by
    # now, and `_adversarial_phase_gates` above read the CONFIGURED
    # review_mode rather than the bundle's, exactly as before.
    if ladder is not None:
        _report_ladder(ladder, factory_config, bus, ui)

    warn_unreachable_gates(factory_config, ui)

    # R8.1: the manifest is a self-contained audit record of what merge
    # guardrails were in force for this run. #192: read back off the
    # PIPELINE, not off a local, so the hash is taken from the object
    # the pipeline will enforce and an edit that forgets the assignment
    # above fails a test rather than diverging silently.
    manifest.policy_hash = pipeline.run_envelope.policy_hash()
    manifest.save(manifest_path)

    # R0.2 crash recovery: MERGE_PENDING is re-pollable, not failed.
    # Re-poll before scheduling so confirmed merges unblock dependents.
    pipeline.repoll_merge_pending()

    return interrupted_branches


def _resolve_limits(
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    root_dir: Path,
    ui: UI,
    run_envelope: RunEnvelope,
    review_selection: AdversarialAgentSelection,
    security_selection: AdversarialAgentSelection | None,
) -> tuple[int, TimeoutConfig, float, BreakerConfig]:
    """The parallel limit, the timeouts and the breaker, and the sandbox warnings."""
    max_parallel = _resolve_max_parallel(factory_config, ui)

    # R0.1: TimeoutConfig is the single source for the agent-iteration and
    # component wall-clock limits. Enforcement layers: the adapters kill
    # their subprocess group, run_loop aborts on the component wall clock,
    # and the scheduler backstop below catches a worker that hangs outside
    # both (e.g. a stuck scaffold or codebase scan step).
    timeout_cfg = factory_config.timeout_config or TimeoutConfig.load(root_dir)
    backstop_seconds = (
        timeout_cfg.component_total + timeout_cfg.scheduler_backstop_margin
        if timeout_cfg.component_total > 0
        else 0.0
    )

    # R7.5: no-progress circuit breaker limits, forwarded into every
    # engineer loop. It reads the diff only (#696 decision 10).
    breaker_cfg = BreakerConfig.load(root_dir)

    # R7.5: OS-level sandbox intent for the agent subprocesses, off the
    # envelope (#192). An engineer with no sandbox (a custom agent command,
    # or [sandbox] enabled = false) is a warning the run records in its
    # events.jsonl, never a refusal (#700, owner decision 2026-10-09).
    warn_unconfined(ui.warn, run_envelope.sandbox, {"engineer": base_config.agent_cmd})
    warn_unsandboxable_reviewers(ui, review_selection, security_selection)
    factory_config.engineer_sandbox = resolve_engineer_sandbox(run_envelope.sandbox, base_config)
    return max_parallel, timeout_cfg, backstop_seconds, breaker_cfg


def _stage_launch(
    manifest: Manifest,
    manifest_path: Path,
    root_dir: Path,
    run_id: str,
    ui: UI,
    pipeline: ComponentPipeline,
    run_state: RunState,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    knowledge_config: KnowledgeConfig,
    run_register: DecisionRegister,
    max_parallel: int,
    timeout_cfg: TimeoutConfig,
    breaker_cfg: BreakerConfig,
    run_envelope: RunEnvelope,
    run_paths: RunPaths | None,
    usage_paths: RunPaths,
) -> tuple[LaunchPlan, VerifyConfig | None]:
    """Stamp the feature base, apply the merge decisions and resolve the launch values."""
    # #481: after the refusals (a refused run stamps nothing) and the
    # worktree preflight's fetch, and before the merge decisions below,
    # which can merge a component into the base.
    _stamp_feature_base(manifest, manifest_path, root_dir, ui)
    # #465: after every pre-spend refusal (a refused run must not have
    # pushed or merged anything) and before anything is scheduled, so the
    # dependents of an approved component are cut from a base that holds it.
    pipeline.apply_merge_decisions()
    # #595: after the merge decisions and before any engineer is
    # scheduled, so an approval made mid-run changes nothing in it.
    pipeline.snapshot_waivers()

    _print_execution_header(ui, factory_config, max_parallel, timeout_cfg)

    launch = build_launch_plan(
        manifest,
        root_dir,
        run_id,
        ui,
        pipeline,
        run_state,
        factory_config,
        base_config,
        knowledge_config,
        run_register,
        timeout_cfg,
        breaker_cfg,
        run_envelope,
        run_paths,
        usage_paths,
    )

    report_operator_files(base_config, root_dir, ui)

    # #261: run-invariant, so it is resolved once here rather than per
    # component. None when Phase 1 is off.
    engineer_verify = factory_config.engineer_verify_config()
    return launch, engineer_verify
