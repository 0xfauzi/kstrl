"""The launch of one component: the plan that its worker arguments read (#776, B3).

``_run_factory_locked`` builds one ``LaunchPlan`` and gives it to the
scheduler. ``submit_args`` turns the plan and one component into the
positional tuple of ``_run_component``. The scheduler stays in
``kstrl.factory.factory``, because the tests patch ``_run_component`` there.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.agents import with_stack_writable
from kstrl.architect import build_decisions_context
from kstrl.config import relative_to_root
from kstrl.knowledge import authored_paths, paths_by_component, retrieve_knowledge_context
from kstrl.loop import LoopBudget
from kstrl.proc import NO_LIMIT, describe_limit_seconds
from kstrl.verify import SCOPE_UNREADABLE_CHECK, scope_unreadable_error

if TYPE_CHECKING:
    from kstrl.architect import DecisionRegister
    from kstrl.config import KstrlConfig
    from kstrl.events import RunPaths
    from kstrl.knowledge import KnowledgeConfig
    from kstrl.loop import BreakerConfig
    from kstrl.pipeline import ComponentPipeline, FactoryConfig, RunEnvelope, RunState
    from kstrl.plan import Component, Manifest, RunScope
    from kstrl.proc import TimeoutConfig
    from kstrl.ui import UI


@dataclass(frozen=True)
class LaunchPlan:
    """References to the run objects that a launch reads, never copies.

    The class is frozen so that a launch cannot rebind a field. A launch
    changes the run through the objects that a field refers to
    (``factory_config``, ``manifest``, ``run_state`` and ``pipeline``).
    A copy of one of them would leave a green suite and no effect (#193).
    The four path strings and ``ff_config_dict`` are run-invariant values.
    """

    manifest: Manifest
    root_dir: Path
    run_id: str
    ui: UI
    pipeline: ComponentPipeline
    run_state: RunState
    factory_config: FactoryConfig
    base_config: KstrlConfig
    knowledge_config: KnowledgeConfig
    run_register: DecisionRegister
    timeout_cfg: TimeoutConfig
    breaker_cfg: BreakerConfig
    run_envelope: RunEnvelope
    run_paths: RunPaths | None
    usage_paths: RunPaths
    prompt_file_rel: str
    codebase_map_file_rel: str
    golden_patterns_file_rel: str
    memory_file_rel: str
    ff_config_dict: dict[str, Any] | None


def build_launch_plan(
    manifest: Manifest,
    root_dir: Path,
    run_id: str,
    ui: UI,
    pipeline: ComponentPipeline,
    run_state: RunState,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    knowledge_config: KnowledgeConfig,
    run_register: DecisionRegister,
    timeout_cfg: TimeoutConfig,
    breaker_cfg: BreakerConfig,
    run_envelope: RunEnvelope,
    run_paths: RunPaths | None,
    usage_paths: RunPaths,
) -> LaunchPlan:
    """Resolve the run-invariant launch values once, before any component starts."""

    def _path_relative_to_root(path: Path) -> str:
        """Render `path` relative to root_dir for use inside per-component
        worktrees. Falls back to the absolute path string when relativization
        fails (e.g. a path on a different mount)."""
        return relative_to_root(path, root_dir)

    prompt_file_rel = _path_relative_to_root(base_config.prompt_file)
    # The progress path is deliberately NOT hoisted out of the per-component
    # loop: unless it was explicitly configured, each component derives its
    # own next to its PRD so the engineer writes inside its allowedPaths
    # (base_config.component_progress_file, called from _submit_args).
    codebase_map_file_rel = _path_relative_to_root(base_config.codebase_map_file)
    golden_patterns_file_rel = _path_relative_to_root(base_config.golden_patterns_file)
    memory_file_rel = _path_relative_to_root(base_config.memory_file)
    # Build codebase scan config dict for serialization to worker processes
    ff_config_dict: dict[str, Any] | None = None
    if factory_config.codebase_scan_config and factory_config.codebase_scan_config.enabled:
        fc = factory_config.codebase_scan_config
        ff_config_dict = {
            "enabled": fc.enabled,
            "module_map": fc.module_map,
            "max_context_tokens": fc.max_context_tokens,
        }

    return LaunchPlan(
        manifest=manifest,
        root_dir=root_dir,
        run_id=run_id,
        ui=ui,
        pipeline=pipeline,
        run_state=run_state,
        factory_config=factory_config,
        base_config=base_config,
        knowledge_config=knowledge_config,
        run_register=run_register,
        timeout_cfg=timeout_cfg,
        breaker_cfg=breaker_cfg,
        run_envelope=run_envelope,
        run_paths=run_paths,
        usage_paths=usage_paths,
        prompt_file_rel=prompt_file_rel,
        codebase_map_file_rel=codebase_map_file_rel,
        golden_patterns_file_rel=golden_patterns_file_rel,
        memory_file_rel=memory_file_rel,
        ff_config_dict=ff_config_dict,
    )


def _print_execution_header(
    ui: UI,
    factory_config: FactoryConfig,
    max_parallel: int,
    timeout_cfg: TimeoutConfig,
) -> None:
    """Print the Execution header: the limits and modes of the run."""
    ui.section("Factory: Execution")
    ui.kv("Max parallel", str(max_parallel))
    ui.kv("Max retries", str(factory_config.max_retries))
    ui.kv("Review mode", factory_config.review_mode)
    contract_mode = (
        factory_config.contract_config.mode if factory_config.contract_config else "skip"
    )
    ui.kv("Contract check", contract_mode)
    ui.kv("Worktree setup", factory_config.worktree_setup_summary())
    for label, value in _execution_limit_rows(timeout_cfg, factory_config):
        ui.kv(label, value)


def submit_args(plan: LaunchPlan, comp: Component, wt_path: Path) -> tuple[Any, ...]:
    """The positional arguments of ``_run_component`` for one component."""
    ctx_json = plan.run_state.component_contexts.get(comp.id)
    engineer_usage = plan.pipeline.engineer_usage_totals()
    scope = plan.pipeline.run_scope.for_component(comp.id)

    def _warn_knowledge_failure(exc: Exception, comp_id: str = comp.id) -> None:
        # Non-fatal, but NOT a metrics detail: the engineer runs
        # without any of its facts when this fires. That is a real
        # degradation of the run, and it used to be a bare
        # `except: pass` that said nothing (#191).
        plan.ui.warn(f"  Knowledge retrieval failed for {comp_id}: {exc}")

    injected_knowledge = retrieve_knowledge_context(
        plan.manifest,
        comp,
        plan.knowledge_config,
        allowed_paths=authored_paths(scope),
        dependency_paths=paths_by_component(plan.manifest, plan.pipeline.run_scope),
        worktree=wt_path,
        on_failure=_warn_knowledge_failure,
    )
    plan.pipeline.record_injected_knowledge(comp.id, injected_knowledge)
    knowledge_prefix = injected_knowledge or ""
    return (
        comp.id,
        comp.prd_path,
        str(wt_path),
        str(plan.root_dir),
        plan.prompt_file_rel,
        plan.base_config.agent_cmd,
        plan.base_config.model,
        plan.base_config.model_reasoning_effort,
        plan.base_config.agent_type,
        plan.base_config.sleep_seconds,
        ctx_json,
        plan.ff_config_dict,
        plan.factory_config.worktree_setup_for_component(comp),
        knowledge_prefix,
        # #260: rides the same context-prefix path the distilled
        # facts already ride, so the register reaches the engineer
        # without a second delivery mechanism. Per component: its
        # own decisions in full, the rest of the run summarised.
        build_decisions_context(
            plan.run_register.decisions, comp.id, requirements=plan.factory_config.requirements
        ),
        # Per-component, not run-wide: KstrlConfig.component_progress_file
        # keeps the engineer's progress log inside allowedPaths.
        plan.base_config.component_progress_file(comp.prd_path, plan.root_dir),
        plan.codebase_map_file_rel,
        # R10.8 and R10.9: the worker joins each of these onto
        # root_dir and nothing else. Sent as root-relative strings
        # for the same reason the other path slots are: the worker
        # is a separate process and gets paths, not a config object.
        plan.golden_patterns_file_rel,
        plan.memory_file_rel,
        plan.timeout_cfg.agent_iteration,
        plan.timeout_cfg.component_total,
        # R2.3 (CRIT-8): forward the invoking config's loop settings;
        # they were previously dropped here and _run_component ran a
        # hardcoded 30 non-interactive iterations with no path guard.
        plan.base_config.max_iterations,
        plan.base_config.interactive,
        # R8 review: the COMPONENT's allowedPaths, not the run-wide
        # --allowed-paths flag. That flag is empty in every ordinary
        # factory run, so `if config.allowed_paths` in the loop was
        # False and guards.enforce_allowed_paths never ran: the
        # in-loop guard existed and was inert. A scope violation was
        # therefore only caught at Phase 1, AFTER the whole engineer
        # loop had been paid for - measured at $12.93 for one such
        # component, versus ~$2.50 for the single iteration it takes
        # to catch it here.
        #
        # #269: the plan-time snapshot, resolved from root_dir
        # before the first engineer call and the SAME object Phase 1
        # is judged with. Read here rather than re-derived per
        # submission, which is what makes a retry inherit the scope
        # the run started with: this closure runs once per attempt,
        # so a per-attempt re-read would let an agent that edited
        # the PRD in attempt 1 widen its own guard for attempt 2
        # (which, under use_worktrees=False, it can do to the file
        # this used to read).
        scope,
        # R7.5: no-progress circuit breaker limits.
        plan.breaker_cfg.no_progress_iterations,
        # R7.5: OS-level sandbox intent for the engineer's agent CLI.
        plan.run_envelope.sandbox.enabled,
        plan.run_envelope.sandbox.allow_network,
        # #700: the paths the confirmed [stack] grants its commands.
        with_stack_writable(
            plan.run_envelope.sandbox, plan.root_dir, plan.factory_config.project_stack
        ).writable,
        # R7.6: in-loop USD budget for the claude-sdk engineer.
        plan.base_config.agent_budget_usd,
        # Chunk 6: worker event channel (None when progress logging
        # is disabled - transcripts and events off together).
        str(plan.run_paths.root) if plan.run_paths is not None else None,
        # R8: accounting channel. Always set, precisely because the
        # line above may be None (review finding P2-d).
        str(plan.usage_paths.root),
        plan.run_id,
        # R8: run-level ceilings (tokens AND cost) + the spend
        # recorded so far, snapshotted AT LAUNCH so the engineer loop
        # can halt itself between iterations. With max_parallel > 1 a
        # worker cannot see a concurrent sibling's spend, only what
        # the parent had recorded when this worker started.
        # prior_calls plus prior_token_calls/prior_cost_calls carry
        # the ENGINEER's non-reporting call counts down so the
        # unenforceable rule does not reset on every attempt and
        # component (review finding P1-a), while staying blind to
        # other roles' timeouts - those say nothing about whether the
        # engineer's adapter reports tokens or cost. The two counters
        # are separate because the two axes have separate coverage:
        # codex reports tokens and no cost, claude can report cost
        # with no usage dict. prior_total_tokens/prior_cost_usd stay
        # run-wide: the overrun checks ask what the RUN has spent.
        LoopBudget(
            max_total_tokens=plan.factory_config.max_total_tokens,
            prior_total_tokens=plan.pipeline.run_usage.total_tokens,
            prior_known_calls=plan.pipeline.run_usage.known_calls,
            prior_calls=engineer_usage.calls,
            prior_token_calls=engineer_usage.token_calls,
            max_cost_usd=plan.factory_config.max_cost_usd,
            prior_cost_usd=plan.pipeline.run_usage.cost_usd,
            prior_cost_calls=engineer_usage.cost_calls,
        ),
    )


def _resolve_max_parallel(factory_config: FactoryConfig, ui: UI) -> int:
    """Effective parallelism, and a warning for every setting it discards.

    Extracted from ``_run_factory_locked`` (#292) so the decision has a
    name and can be tested. It is also the decision that hid a fake test
    for months: the spine SIGTERM test asked for ``max_parallel=2``,
    silently got 1 and therefore ``_InlineExecutor``, and nothing said so.

    Both discards are REPORTED, and only when they actually discard
    something. The worktree branch used to fire unconditionally as an
    info line that never named ``max_parallel``, so an operator who
    configured 8 was never told the 8 was thrown away.
    ``ui.kv("Max parallel", ...)`` prints the effective value, which
    cannot distinguish "I asked for 1" from "I asked for 8 and lost it".
    """
    max_parallel = factory_config.max_parallel
    if not factory_config.use_worktrees:
        if max_parallel > 1:
            ui.warn(
                f"worktrees disabled: components cannot run in parallel; "
                f"forcing max_parallel=1 (you configured {max_parallel})"
            )
        return 1
    if factory_config.single_pr and max_parallel > 1:
        # R0.5 (H-8): single_pr components all live on ONE branch, and a
        # branch can only be checked out in one worktree at a time -
        # parallel same-tier components would hard-fail on "already
        # checked out". Sequential is the only layout that works.
        ui.warn(
            f"single_pr mode: components share one branch; forcing "
            f"max_parallel=1 (you configured {max_parallel})"
        )
        return 1
    return max_parallel


def _refused_before_launch(
    pipeline: ComponentPipeline,
    comp: Component,
    run_scope: RunScope,
) -> bool:
    """Every reason to fail a READY component instead of launching it.

    Returns True when the component was transitioned without a launch,
    which the caller counts so the scheduling loop re-derives its ready
    set rather than stopping while schedulable siblings remain.

    One function because the three refusals share a shape and a
    contract - fail loudly, spend nothing, do not halt the run - and
    because the scheduling loop is already far past the repo's
    complexity gate, so a fourth inline branch is a refusal at commit
    time. None of them halts: by this point siblings may have merged,
    and discarding their work to punish one component is the worse
    trade.
    """
    # R3.1 scheduling gate: a blown ceiling (tokens or cost) fails
    # pending components loudly instead of launching an engineer loop
    # that would only add spend.
    if pipeline.budget_exceeded():
        pipeline.fail_for_budget(comp, "scheduling")
        return True
    # R8 review: a loop that emits COMPLETE returns before its own
    # budget check, so an adapter that finishes on its first silent call
    # never halts itself - the ordinary success path for a custom
    # agent_cmd. This gate is what stops the run handing out NEW work
    # under a ceiling that can no longer fire. Halts only when EVERY
    # configured ceiling is dead: a cost-reporting adapter still
    # enforces max_cost_usd even though its token cap is beyond saving.
    #
    # Identity is derived inside fail_for_budget: nothing was BREACHED
    # here, so the rule falls through to the dead ceilings and names
    # only those actually configured.
    unenforceable = pipeline.budget_unenforceable()
    if unenforceable is not None:
        pipeline.fail_for_budget(comp, "scheduling", reason=unenforceable)
        return True
    # #294: the LAST point before spend. The plan-time preflight refuses
    # an unresolved scope before the run starts, but it only inspects
    # components that are PENDING then, and the contract breaker resets
    # a COMPLETED component to PENDING inside the scheduling loop, long
    # after _run_preflights returned. Without this gate such a component
    # runs a full engineer loop - up to max_iterations LLM calls - and
    # only then does Phase 1 refuse it. Measured against the same
    # dogfooding run _preflight_component_scope cites (14.49 dollars /
    # 41 minutes for three attempts), Phase 1's FAIL alone still left
    # roughly 4.83 dollars and 14 minutes on the table per reset.
    #
    # This also closes --no-verify, which returns from _phase_verify
    # before the ungated check can run: a component with no trustworthy
    # scope now never launches, so it cannot merge with the guard inert.
    comp_scope = run_scope.for_component(comp.id)
    if not comp_scope.is_trustworthy:
        pipeline.fail(
            comp,
            scope_unreadable_error(f"Error: {comp_scope.error}"),
            phase="scope",
            check=SCOPE_UNREADABLE_CHECK,
            signatures=[f"{SCOPE_UNREADABLE_CHECK}:no-trustworthy-scope"],
        )
        return True
    return False


def _execution_limit_rows(
    timeout_cfg: TimeoutConfig, factory_config: FactoryConfig
) -> list[tuple[str, str]]:
    """Every time and spend limit the Execution header states, set or not.

    An unset limit is printed as "no limit" rather than left out (#467):
    a missing line reads the same as a header written before the limit
    existed.

    R8 (measured): each ceiling states what it counts, before the run
    spends anything. An operator set --max-cost-usd 25.0 on a run whose
    cross-family reviewer reports tokens and no cost, and got a ceiling
    that bounded the engineer alone; nothing had told them the ceiling is
    denominated in REPORTED dollars. Nothing here names which roles will
    be covered: no call has been made yet, so that would be a prediction.
    The measured per-role figure follows as a `budget_coverage` event at
    the first call that reports nothing.
    """
    tokens = factory_config.max_total_tokens
    cost = factory_config.max_cost_usd
    calls = factory_config.max_adversarial_calls
    return [
        ("Agent timeout", describe_limit_seconds(timeout_cfg.agent_iteration)),
        ("Component timeout", describe_limit_seconds(timeout_cfg.component_total)),
        (
            "Token ceiling",
            f"{tokens} total tokens (counts only calls whose agent reports a token count)"
            if tokens > 0
            else NO_LIMIT,
        ),
        (
            "Cost ceiling",
            f"${cost} (counts only calls whose agent reports a cost; roles whose "
            "agent reports none are unpriced and unbounded by it)"
            if cost > 0
            else NO_LIMIT,
        ),
        ("Adversarial calls", str(calls) if calls > 0 else NO_LIMIT),
    ]
