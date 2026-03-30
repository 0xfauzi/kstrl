"""Factory orchestrator - parallel component execution with git worktrees."""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py.config import RalphConfig
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.pr import create_prs_in_order, create_single_pr

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
    """Create a git worktree for a component.

    Returns the worktree path.
    Raises RuntimeError on failure.
    """
    worktree_base = root_dir / ".ralph" / "worktrees"
    worktree_base.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_base / component_id

    if worktree_path.exists():
        # Clean up stale worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=root_dir,
            capture_output=True,
        )

    result = subprocess.run(
        [
            "git", "worktree", "add",
            str(worktree_path),
            "-b", branch_name,
            base_branch,
        ],
        cwd=root_dir,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        # Branch may already exist - try without -b
        result = subprocess.run(
            [
                "git", "worktree", "add",
                str(worktree_path),
                branch_name,
            ],
            cwd=root_dir,
            capture_output=True,
            text=True,
        )

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Failed to create worktree for '{component_id}': {error}"
        )

    return worktree_path


def _cleanup_worktree(component_id: str, root_dir: Path) -> None:
    """Remove a git worktree for a component."""
    worktree_path = root_dir / ".ralph" / "worktrees" / component_id
    if not worktree_path.exists():
        return

    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=root_dir,
        capture_output=True,
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
) -> ComponentResult:
    """Run a single component's feature pipeline.

    This is a top-level function so it's picklable for ProcessPoolExecutor.
    It creates all its own objects internally - no shared state.
    """
    from ralph_py.agents import get_agent
    from ralph_py.loop import run_loop
    from ralph_py.ui.plain import PlainUI

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

    # Build config for implementation loop
    config = RalphConfig(
        max_iterations=10,
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
        result = run_loop(config, ui, agent, worktree_path)
        return ComponentResult(
            component_id=component_id,
            success=result.completed,
            iterations=result.iterations,
            error=None if result.completed else "Did not complete",
        )
    except Exception as exc:
        return ComponentResult(
            component_id=component_id,
            success=False,
            iterations=0,
            error=str(exc),
        )


def _verify_component(
    worktree_path: Path,
    verify_command: str | None,
) -> bool:
    """Run verification in a component's worktree.

    Returns True if verification passes.
    """
    if not verify_command:
        return True

    result = subprocess.run(
        verify_command,
        shell=True,
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def run_factory(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: RalphConfig,
    ui: UI,
    root_dir: Path,
) -> FactoryResult:
    """Run the factory orchestrator.

    Executes components in parallel using git worktrees, respecting the
    dependency DAG. Saves manifest after every state change for crash recovery.
    """
    factory_result = FactoryResult()

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

    # Reset any RUNNING components (crash recovery)
    for comp in manifest.components:
        if comp.status == ComponentStatus.RUNNING.value:
            ui.info(f"  Resetting '{comp.id}' from RUNNING to PENDING (crash recovery)")
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

    # Main scheduling loop
    running_futures: dict[Future[ComponentResult], str] = {}
    worktree_paths: dict[str, Path] = {}

    if base_config.prompt_file.is_absolute():
        prompt_file_rel = base_config.prompt_file.relative_to(root_dir).as_posix()
    else:
        prompt_file_rel = str(base_config.prompt_file)

    class _ComponentKwargs:
        """Typed container for _run_component arguments."""

        def __init__(
            self,
            component_id: str,
            prd_path_str: str,
            worktree_path_str: str,
            prompt_file_str: str,
            agent_cmd: str | None,
            model: str | None,
            reasoning: str | None,
            agent_type: str | None,
            sleep_seconds: float,
        ) -> None:
            self.component_id = component_id
            self.prd_path_str = prd_path_str
            self.worktree_path_str = worktree_path_str
            self.prompt_file_str = prompt_file_str
            self.agent_cmd = agent_cmd
            self.model = model
            self.reasoning = reasoning
            self.agent_type = agent_type
            self.sleep_seconds = sleep_seconds

    def _build_component_kwargs(comp_id: str, prd_path: str, wt_path: Path) -> _ComponentKwargs:
        return _ComponentKwargs(
            component_id=comp_id,
            prd_path_str=prd_path,
            worktree_path_str=str(wt_path),
            prompt_file_str=prompt_file_rel,
            agent_cmd=base_config.agent_cmd,
            model=base_config.model,
            reasoning=base_config.model_reasoning_effort,
            agent_type=base_config.agent_type,
            sleep_seconds=base_config.sleep_seconds,
        )

    def _handle_result(
        comp_id: str, comp_result: ComponentResult,
    ) -> None:
        comp = manifest.get_component(comp_id)
        if comp is None:
            return

        if comp_result.success:
            wt_path = worktree_paths.get(comp_id, root_dir)
            verified = _verify_component(wt_path, factory_config.verify_command)

            if verified:
                comp.status = ComponentStatus.COMPLETED.value
                comp.error = ""
                factory_result.completed.append(comp_id)
                ui.ok(f"  Completed: {comp_id} ({comp_result.iterations} iterations)")
            else:
                ui.warn(f"  Verification failed for '{comp_id}'")
                if comp.retries < factory_config.max_retries:
                    comp.retries += 1
                    comp.status = ComponentStatus.PENDING.value
                    comp.error = "Verification failed"
                    ui.info(
                        f"  Retrying '{comp_id}' "
                        f"(attempt {comp.retries}/{factory_config.max_retries})"
                    )
                else:
                    comp.status = ComponentStatus.FAILED.value
                    comp.error = "Verification failed after max retries"
                    skipped = manifest.cascade_skip(comp_id)
                    factory_result.failed.append(comp_id)
                    factory_result.skipped.extend(skipped)
                    ui.err(f"  Failed: {comp_id} (verification)")
        else:
            error_msg = comp_result.error or "Unknown error"
            if comp.retries < factory_config.max_retries:
                comp.retries += 1
                comp.status = ComponentStatus.PENDING.value
                comp.error = error_msg
                ui.info(
                    f"  Retrying '{comp_id}' "
                    f"(attempt {comp.retries}/{factory_config.max_retries}): "
                    f"{error_msg}"
                )
                time.sleep(factory_config.retry_delay)
            else:
                comp.status = ComponentStatus.FAILED.value
                comp.error = error_msg
                skipped = manifest.cascade_skip(comp_id)
                factory_result.failed.append(comp_id)
                factory_result.skipped.extend(skipped)
                ui.err(f"  Failed: {comp_id}: {error_msg}")

        manifest.save(manifest_path)

    def _launch_component(comp: Component) -> Path | None:
        """Set up and prepare a component for execution. Returns worktree path."""
        try:
            if factory_config.use_worktrees:
                wt_path = _setup_worktree(
                    comp.id, comp.branch_name, manifest.base_branch, root_dir
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

    # Sequential mode: run directly in-process (no executor)
    if max_parallel <= 1:
        while True:
            ready = manifest.get_ready_components()
            if not ready:
                break

            comp = ready[0]
            comp.status = ComponentStatus.RUNNING.value
            manifest.save(manifest_path)
            ui.info(f"  Starting: {comp.id}")

            wt_path = _launch_component(comp)
            if wt_path is None:
                continue

            kw = _build_component_kwargs(comp.id, comp.prd_path, wt_path)
            try:
                comp_result = _run_component(
                    kw.component_id, kw.prd_path_str, kw.worktree_path_str,
                    kw.prompt_file_str, kw.agent_cmd, kw.model,
                    kw.reasoning, kw.agent_type, kw.sleep_seconds,
                )
            except Exception as exc:
                comp_result = ComponentResult(
                    component_id=comp.id, success=False, error=str(exc)
                )

            _handle_result(comp.id, comp_result)
    else:
        # Parallel mode with ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=max_parallel) as executor:
            while True:
                ready = manifest.get_ready_components()
                slots = max_parallel - len(running_futures)

                for comp in ready[:slots]:
                    comp.status = ComponentStatus.RUNNING.value
                    manifest.save(manifest_path)
                    ui.info(f"  Starting: {comp.id}")

                    wt_path = _launch_component(comp)
                    if wt_path is None:
                        continue

                    kw = _build_component_kwargs(comp.id, comp.prd_path, wt_path)
                    future = executor.submit(
                        _run_component,
                        kw.component_id, kw.prd_path_str, kw.worktree_path_str,
                        kw.prompt_file_str, kw.agent_cmd, kw.model,
                        kw.reasoning, kw.agent_type, kw.sleep_seconds,
                    )
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
                            component_id=comp_id, success=False, error=str(exc)
                        )

                    _handle_result(comp_id, comp_result)
                    break  # Re-check ready after each completion

                for future in done_futures:
                    del running_futures[future]

    # Cleanup worktrees
    if factory_config.use_worktrees:
        ui.section("Factory: Cleanup")
        for comp_id in worktree_paths:
            _cleanup_worktree(comp_id, root_dir)
        ui.ok("Worktrees cleaned up")

    # Create PRs
    if factory_config.create_prs:
        if factory_config.single_pr:
            result = create_single_pr(manifest, root_dir, ui)
            if result:
                factory_result.pr_urls.append(result[1])
        else:
            pr_results = create_prs_in_order(manifest, root_dir, ui)
            factory_result.pr_urls.extend(url for _, url in pr_results)

        manifest.save(manifest_path)

    # Summary
    ui.section("Factory: Summary")
    ui.kv("Completed", str(len(factory_result.completed)))
    ui.kv("Failed", str(len(factory_result.failed)))
    ui.kv("Skipped", str(len(factory_result.skipped)))
    if factory_result.pr_urls:
        ui.kv("PRs created", str(len(factory_result.pr_urls)))
        for url in factory_result.pr_urls:
            ui.info(f"  {url}")

    if factory_result.failed:
        factory_result.exit_code = 1
    elif factory_result.skipped and not factory_result.completed:
        factory_result.exit_code = 1

    return factory_result
