"""Factory orchestrator - parallel component execution with 3-phase verification."""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py.config import RalphConfig
from ralph_py.context import IterationContext
from ralph_py.contract import ContractConfig, ContractMode, run_contract_testing
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.observability import NullProgressLog, ProgressLog
from ralph_py.pr import create_prs_in_order, create_single_pr
from ralph_py.review import ReviewMode, run_review
from ralph_py.verify import VerifyConfig, run_mechanical_verification

if TYPE_CHECKING:
    from ralph_py.ui.base import UI


@dataclass
class FactoryConfig:
    """Configuration for factory orchestration."""

    max_parallel: int = 4
    max_retries: int = 3
    retry_delay: float = 5.0
    use_worktrees: bool = True
    single_pr: bool = False
    create_prs: bool = True
    verify_command: str | None = None
    # Phase 1: mechanical verification
    verify_config: VerifyConfig | None = None
    # Phase 2: reviewer agent
    review_mode: str = ReviewMode.HARD.value
    review_agent_cmd: str | None = None
    review_agent_type: str | None = None
    review_model: str | None = None
    # Phase 3: contract testing
    contract_config: ContractConfig | None = None
    # Observability
    progress_log_path: Path | None = None

    @classmethod
    def from_env(cls) -> FactoryConfig:
        """Load factory config from environment variables."""
        return cls(
            max_parallel=int(os.environ.get("FACTORY_MAX_PARALLEL", "4")),
            max_retries=int(os.environ.get("FACTORY_MAX_RETRIES", "3")),
            retry_delay=float(os.environ.get("FACTORY_RETRY_DELAY", "5.0")),
        )


@dataclass
class ComponentResult:
    """Result from running a single component."""

    component_id: str
    success: bool
    iterations: int = 0
    error: str | None = None
    duration_seconds: float = 0.0
    context_json: str | None = None


@dataclass
class FactoryResult:
    """Overall result from the factory run."""

    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)
    exit_code: int = 0


def _setup_worktree(
    component_id: str,
    branch_name: str,
    base_branch: str,
    root_dir: Path,
) -> Path:
    """Create a git worktree for a component."""
    worktree_base = root_dir / ".ralph" / "worktrees"
    worktree_base.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_base / component_id

    if worktree_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=root_dir, capture_output=True, timeout=30,
        )

    result = subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch_name, base_branch],
        cwd=root_dir, capture_output=True, text=True, timeout=30,
    )

    if result.returncode != 0:
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), branch_name],
            cwd=root_dir, capture_output=True, text=True, timeout=30,
        )

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Failed to create worktree for '{component_id}': {error}")

    return worktree_path


def _cleanup_worktree(component_id: str, root_dir: Path) -> None:
    """Remove a git worktree for a component."""
    worktree_path = root_dir / ".ralph" / "worktrees" / component_id
    if not worktree_path.exists():
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=root_dir, capture_output=True, timeout=30,
    )


def _run_component(
    component_id: str,
    prd_path_str: str,
    worktree_path_str: str,
    prompt_file_str: str,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str | None,
    sleep_seconds: float,
    previous_context_json: str | None = None,
) -> ComponentResult:
    """Run a single component's implementation loop.

    Top-level function (picklable for ProcessPoolExecutor).
    Creates all objects internally - no shared state.
    """
    from ralph_py.agents import get_agent
    from ralph_py.loop import run_loop
    from ralph_py.ui.plain import PlainUI

    start = time.monotonic()
    worktree_path = Path(worktree_path_str)
    prd_path = Path(prd_path_str)

    ui = PlainUI(no_color=True)
    agent = get_agent(agent_cmd, model, reasoning, agent_type)

    # Copy PRD into worktree if needed
    worktree_prd = worktree_path / prd_path_str
    if not worktree_prd.exists() and prd_path.exists():
        worktree_prd.parent.mkdir(parents=True, exist_ok=True)
        worktree_prd.write_text(prd_path.read_text())

    # Copy prompt into worktree if needed
    worktree_prompt = worktree_path / prompt_file_str
    prompt_source = Path(prompt_file_str)
    if not worktree_prompt.exists() and prompt_source.exists():
        worktree_prompt.parent.mkdir(parents=True, exist_ok=True)
        worktree_prompt.write_text(prompt_source.read_text())

    # Build context prefix from previous retries
    context_prefix: str | None = None
    if previous_context_json:
        ctx = IterationContext.from_json(previous_context_json)
        formatted = ctx.format_for_prompt()
        if formatted.strip():
            context_prefix = formatted

    config = RalphConfig(
        max_iterations=15,
        prompt_file=worktree_prompt,
        prd_file=worktree_prd,
        sleep_seconds=sleep_seconds,
        interactive=False,
        ralph_branch="",
        ralph_branch_explicit=True,
        agent_cmd=agent_cmd,
        model=model,
        model_reasoning_effort=reasoning,
        agent_type=agent_type,
        ui_mode="plain",
        no_color=True,
    )

    try:
        result = run_loop(config, ui, agent, worktree_path, context_prefix=context_prefix)
        return ComponentResult(
            component_id=component_id,
            success=result.completed,
            iterations=result.iterations,
            error=None if result.completed else "Did not complete",
            duration_seconds=time.monotonic() - start,
            context_json=previous_context_json,
        )
    except Exception as exc:
        return ComponentResult(
            component_id=component_id,
            success=False,
            iterations=0,
            error=str(exc),
            duration_seconds=time.monotonic() - start,
            context_json=previous_context_json,
        )


def run_factory(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: RalphConfig,
    ui: UI,
    root_dir: Path,
) -> FactoryResult:
    """Run the factory orchestrator with 3-phase verification.

    Phase 1: Mechanical verification (tests, typecheck, lint, PRD, diff scope)
    Phase 2: Second-opinion review (separate agent reviews diff against spec)
    Phase 3: Contract testing (merge tier branches, run integration tests)
    """
    from ralph_py.agents import get_agent

    factory_start = time.monotonic()
    factory_result = FactoryResult()

    # Set up progress log
    if factory_config.progress_log_path:
        progress_log = ProgressLog(factory_config.progress_log_path)
    else:
        progress_log = NullProgressLog()

    progress_log.factory_started(manifest.project_name, len(manifest.components))

    # Validate DAG
    ui.section("Factory: Validating DAG")
    dag_errors = manifest.validate_dag()
    if dag_errors:
        for err in dag_errors:
            ui.err(f"  {err}")
        factory_result.exit_code = 1
        return factory_result

    topo_order = manifest.topological_order()
    ui.ok(f"DAG valid: {len(topo_order)} components in dependency order")

    # Crash recovery: reset intermediate states
    for comp in manifest.components:
        if comp.status in (
            ComponentStatus.RUNNING.value,
            ComponentStatus.VERIFYING.value,
        ):
            ui.info(f"  Resetting '{comp.id}' from {comp.status} to PENDING")
            comp.status = ComponentStatus.PENDING.value

    manifest_path = root_dir / "scripts" / "ralph" / "manifest.json"

    # Determine effective parallelism
    max_parallel = factory_config.max_parallel
    if not factory_config.use_worktrees:
        max_parallel = 1
        ui.info("Worktrees disabled: running sequentially")

    ui.section("Factory: Execution")
    ui.kv("Max parallel", str(max_parallel))
    ui.kv("Max retries", str(factory_config.max_retries))
    ui.kv("Review mode", factory_config.review_mode)
    contract_mode = (
        factory_config.contract_config.mode
        if factory_config.contract_config else "skip"
    )
    ui.kv("Contract check", contract_mode)

    # Scheduling state
    running_futures: dict[Future[ComponentResult], str] = {}
    worktree_paths: dict[str, Path] = {}
    component_contexts: dict[str, str] = {}  # comp_id -> context JSON

    if base_config.prompt_file.is_absolute():
        prompt_file_rel = base_config.prompt_file.relative_to(root_dir).as_posix()
    else:
        prompt_file_rel = str(base_config.prompt_file)

    def _retry_or_fail(comp: Component, error: str, context_json: str | None) -> None:
        """Retry a component or mark it as failed."""
        if comp.retries < factory_config.max_retries:
            comp.retries += 1
            comp.status = ComponentStatus.PENDING.value
            comp.error = error
            if context_json:
                component_contexts[comp.id] = context_json
            progress_log.component_retrying(comp.id, comp.retries, error)
            ui.info(
                f"  Retrying '{comp.id}' "
                f"(attempt {comp.retries}/{factory_config.max_retries}): "
                f"{error[:80]}"
            )
            time.sleep(factory_config.retry_delay)
        else:
            comp.status = ComponentStatus.FAILED.value
            comp.error = error
            skipped = manifest.cascade_skip(comp.id)
            factory_result.failed.append(comp.id)
            factory_result.skipped.extend(skipped)
            progress_log.component_failed(comp.id, error)
            ui.err(f"  Failed: {comp.id}: {error[:80]}")
        manifest.save(manifest_path)

    def _handle_result(comp_id: str, comp_result: ComponentResult) -> None:
        """Process component result through 3-phase verification."""
        comp = manifest.get_component(comp_id)
        if comp is None:
            return

        # Record timing
        comp.duration_seconds = comp_result.duration_seconds
        comp.iteration_count = comp_result.iterations

        if not comp_result.success:
            from ralph_py.context import IterationRecord
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_iteration(IterationRecord(
                iteration=comp_result.iterations,
                success=False,
                error=comp_result.error,
            ))
            _retry_or_fail(comp, comp_result.error or "Unknown error", ctx.to_json())
            return

        wt_path = worktree_paths.get(comp_id, root_dir)

        # PHASE 1: Mechanical verification
        comp.status = ComponentStatus.VERIFYING.value
        manifest.save(manifest_path)

        verify_config = factory_config.verify_config or VerifyConfig()
        ui.info(f"  Phase 1: mechanical verification for {comp_id}...")
        verify_start = time.monotonic()
        verification = run_mechanical_verification(
            wt_path,
            wt_path / comp.prd_path,
            manifest.base_branch,
            None,
            verify_config,
        )
        verify_duration = time.monotonic() - verify_start
        comp.verification_passed = verification.passed
        progress_log.verification_result(
            comp_id, verification.passed,
            check_names=[c.name for c in verification.checks],
            failures=[c.message for c in verification.checks if not c.passed],
            duration=verify_duration,
        )

        if not verification.passed:
            failing = [c for c in verification.checks if not c.passed]
            ui.warn(
                f"  Phase 1 FAILED for {comp_id}: "
                f"{', '.join(c.name for c in failing)}"
            )
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_verification_failure(verification.as_context())
            _retry_or_fail(comp, "Mechanical verification failed", ctx.to_json())
            return

        ui.ok(f"  Phase 1 passed for {comp_id}")

        # PHASE 2: Second-opinion review
        review_mode = ReviewMode(factory_config.review_mode)
        if review_mode != ReviewMode.SKIP:
            ui.info(f"  Phase 2: review ({review_mode.value}) for {comp_id}...")

            review_agent = get_agent(
                factory_config.review_agent_cmd,
                factory_config.review_model,
                None,
                factory_config.review_agent_type or base_config.agent_type,
            )
            review_result = run_review(
                review_agent,
                wt_path / comp.prd_path,
                wt_path,
                manifest.base_branch,
                verification,
                review_mode,
                ui,
            )
            comp.review_passed = review_result.passed
            comp.review_findings = review_result.as_pr_body_section()
            progress_log.review_result(
                comp_id, review_result.passed,
                mode=review_mode.value,
                fail_count=review_result.fail_count,
                advisory_count=review_result.advisory_count,
                duration=review_result.duration_seconds,
            )

            if not review_result.passed:
                ui.warn(
                    f"  Phase 2 FAILED for {comp_id}: "
                    f"{review_result.fail_count} failures"
                )
                ctx = IterationContext.from_json(comp_result.context_json or "{}")
                ctx.add_review_finding(review_result.as_retry_context())
                _retry_or_fail(comp, "Review failed", ctx.to_json())
                return

            ui.ok(f"  Phase 2 passed for {comp_id}")
        else:
            comp.review_passed = None

        # All verification phases passed - create PR and merge
        if factory_config.create_prs:
            from ralph_py.pr import push_create_and_merge_pr, is_gh_available

            if is_gh_available():
                ui.info(f"  Creating and merging PR for {comp_id}...")
                pr_result = push_create_and_merge_pr(
                    comp, manifest, root_dir, ui,
                    merge_method="squash",
                    merge_timeout=300,
                )
                if pr_result:
                    factory_result.pr_urls.append(pr_result[1])
                manifest.save(manifest_path)

        # Clean up worktree now that code is merged
        if factory_config.use_worktrees and comp_id in worktree_paths:
            _cleanup_worktree(comp_id, root_dir)
            del worktree_paths[comp_id]

        # Mark completed
        comp.status = ComponentStatus.COMPLETED.value
        comp.error = ""
        factory_result.completed.append(comp_id)
        progress_log.component_completed(
            comp_id, comp_result.duration_seconds, comp_result.iterations,
        )
        ui.ok(
            f"  COMPLETED: {comp_id} "
            f"({comp_result.iterations} iterations, "
            f"{comp_result.duration_seconds:.0f}s)"
        )
        manifest.save(manifest_path)

    def _launch_component(comp: Component) -> Path | None:
        """Set up worktree for a component. Returns worktree path or None."""
        try:
            if factory_config.use_worktrees:
                wt_path = _setup_worktree(
                    comp.id, comp.branch_name, manifest.base_branch, root_dir,
                )
            else:
                wt_path = root_dir
            worktree_paths[comp.id] = wt_path
            return wt_path
        except RuntimeError as exc:
            ui.err(f"  Worktree setup failed for '{comp.id}': {exc}")
            comp.status = ComponentStatus.FAILED.value
            comp.error = str(exc)
            skipped = manifest.cascade_skip(comp.id)
            factory_result.failed.append(comp.id)
            factory_result.skipped.extend(skipped)
            manifest.save(manifest_path)
            return None

    def _submit_args(comp: Component, wt_path: Path) -> tuple:
        ctx_json = component_contexts.get(comp.id)
        return (
            comp.id, comp.prd_path, str(wt_path),
            prompt_file_rel, base_config.agent_cmd, base_config.model,
            base_config.model_reasoning_effort, base_config.agent_type,
            base_config.sleep_seconds, ctx_json,
        )

    # Main scheduling loop
    if max_parallel <= 1:
        while True:
            ready = manifest.get_ready_components()
            if not ready:
                break

            comp = ready[0]
            comp.status = ComponentStatus.RUNNING.value
            comp.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            manifest.save(manifest_path)
            progress_log.component_started(comp.id)
            ui.info(f"  Starting: {comp.id}")

            wt_path = _launch_component(comp)
            if wt_path is None:
                continue

            args = _submit_args(comp, wt_path)
            try:
                comp_result = _run_component(*args)
            except Exception as exc:
                comp_result = ComponentResult(
                    component_id=comp.id, success=False, error=str(exc),
                )

            _handle_result(comp.id, comp_result)
    else:
        with ProcessPoolExecutor(max_workers=max_parallel) as executor:
            while True:
                ready = manifest.get_ready_components()
                slots = max_parallel - len(running_futures)

                for comp in ready[:slots]:
                    comp.status = ComponentStatus.RUNNING.value
                    comp.started_at = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    )
                    manifest.save(manifest_path)
                    progress_log.component_started(comp.id)
                    ui.info(f"  Starting: {comp.id}")

                    wt_path = _launch_component(comp)
                    if wt_path is None:
                        continue

                    args = _submit_args(comp, wt_path)
                    future = executor.submit(_run_component, *args)
                    running_futures[future] = comp.id

                if not running_futures:
                    break

                done_futures: set[Future[ComponentResult]] = set()
                for future in as_completed(running_futures):
                    done_futures.add(future)
                    comp_id = running_futures[future]

                    try:
                        comp_result = future.result()
                    except Exception as exc:
                        comp_result = ComponentResult(
                            component_id=comp_id, success=False, error=str(exc),
                        )

                    _handle_result(comp_id, comp_result)
                    break

                for future in done_futures:
                    del running_futures[future]

    # Cleanup worktrees
    if factory_config.use_worktrees:
        ui.section("Factory: Cleanup")
        for comp_id in worktree_paths:
            _cleanup_worktree(comp_id, root_dir)
        ui.ok("Worktrees cleaned up")

    # PHASE 3: Contract testing
    contract_config = factory_config.contract_config
    if contract_config and contract_config.mode != ContractMode.SKIP.value:
        contract_results = run_contract_testing(
            manifest, root_dir, contract_config, ui,
        )
        for cr in contract_results:
            progress_log.contract_result(
                cr.tier, cr.passed, cr.breaker, cr.duration_seconds,
            )
            if not cr.passed and cr.breaker:
                breaker = manifest.get_component(cr.breaker)
                if breaker and breaker.retries < factory_config.max_retries:
                    breaker.retries += 1
                    breaker.status = ComponentStatus.PENDING.value
                    breaker.error = f"Contract test failed at tier {cr.tier}"
                    # Remove from completed list
                    if cr.breaker in factory_result.completed:
                        factory_result.completed.remove(cr.breaker)
                    ctx = IterationContext.from_json(
                        component_contexts.get(cr.breaker, "{}")
                    )
                    ctx.add_contract_failure(cr.test_output[:500])
                    component_contexts[cr.breaker] = ctx.to_json()
                    manifest.save(manifest_path)
                    ui.warn(
                        f"  Contract breaker '{cr.breaker}' sent back for retry"
                    )

    # Create PRs for any remaining components that weren't handled per-component
    # (e.g. single-pr mode, or stragglers from parallel execution)
    if factory_config.create_prs:
        if factory_config.single_pr:
            result = create_single_pr(manifest, root_dir, ui)
            if result:
                factory_result.pr_urls.append(result[1])
            manifest.save(manifest_path)
        else:
            # Per-component PRs are created in _handle_result; only handle stragglers
            remaining = [
                c for c in manifest.components
                if c.status == "completed" and not c.pr_url
            ]
            if remaining:
                pr_results = create_prs_in_order(manifest, root_dir, ui)
                factory_result.pr_urls.extend(url for _, url in pr_results)
                manifest.save(manifest_path)

    # Summary
    factory_duration = time.monotonic() - factory_start
    progress_log.factory_completed(
        len(factory_result.completed),
        len(factory_result.failed),
        len(factory_result.skipped),
        factory_duration,
    )

    ui.section("Factory: Summary")
    ui.kv("Completed", str(len(factory_result.completed)))
    ui.kv("Failed", str(len(factory_result.failed)))
    ui.kv("Skipped", str(len(factory_result.skipped)))
    ui.kv("Duration", f"{factory_duration:.0f}s")
    if factory_result.pr_urls:
        ui.kv("PRs created", str(len(factory_result.pr_urls)))
        for url in factory_result.pr_urls:
            ui.info(f"  {url}")

    if factory_result.failed:
        factory_result.exit_code = 1
    elif factory_result.skipped and not factory_result.completed:
        factory_result.exit_code = 1

    return factory_result
