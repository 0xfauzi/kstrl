"""Factory orchestrator - parallel component execution with 3-phase verification."""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ralph_py.config import RalphConfig
from ralph_py.context import IterationContext
from ralph_py.contract import ContractConfig, ContractMode, run_contract_testing
from ralph_py.feedforward import FeedforwardConfig, build_feedforward_context
from ralph_py.knowledge import (
    KnowledgeConfig,
    build_knowledge_context,
    distill_facts,
    measure_fact_utilization,
)
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.observability import NullProgressLog, ProgressLog
from ralph_py.pr import create_prs_in_order, create_single_pr
from ralph_py.prd import PRD
from ralph_py.review import ReviewMode, run_review
from ralph_py.security import SecurityConfig, SecurityMode, run_security_review
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
    # Phase 2.5: security review (separate LLM call after Phase 2 review)
    security_config: SecurityConfig | None = None
    # Phase 3: contract testing
    contract_config: ContractConfig | None = None
    # Phase 0: feedforward
    feedforward_config: FeedforwardConfig | None = None
    # Observability
    progress_log_path: Path | None = None
    # E4: per-run hard cap on adversarial LLM calls (review + security
    # + knowledge distill). 0 means unbounded. Once exceeded the
    # remaining components skip those phases with an informational log
    # line; mechanical verify + the implementing agent continue. This
    # protects against runaway-cost factory runs.
    max_adversarial_calls: int = 0
    # E6: when True, pause and prompt the user before each component's
    # PR creation step. Off by default; opt-in for sensitive projects.
    pause_before_pr_merge: bool = False

    @classmethod
    def from_env(cls) -> FactoryConfig:
        """Load factory config from environment variables."""
        return cls(
            max_parallel=int(os.environ.get("FACTORY_MAX_PARALLEL", "4")),
            max_retries=int(os.environ.get("FACTORY_MAX_RETRIES", "3")),
            retry_delay=float(os.environ.get("FACTORY_RETRY_DELAY", "5.0")),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> FactoryConfig:
        """Load factory config with precedence: env > toml > defaults.

        Reads the ``[factory]`` section from ``<root_dir>/ralph.toml`` if
        present, then overlays any matching env vars on top.
        """
        from ralph_py.config import load_toml_section
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(root_dir / "ralph.toml", "factory")
        if "max_parallel" in section:
            config.max_parallel = int(section["max_parallel"])
        if "max_retries" in section:
            config.max_retries = int(section["max_retries"])
        if "retry_delay" in section:
            config.retry_delay = float(section["retry_delay"])
        if "use_worktrees" in section:
            config.use_worktrees = bool(section["use_worktrees"])
        if "single_pr" in section:
            config.single_pr = bool(section["single_pr"])
        if "create_prs" in section:
            config.create_prs = bool(section["create_prs"])
        if "review_mode" in section:
            config.review_mode = str(section["review_mode"])
        # Env overrides (consistent with from_env)
        if "FACTORY_MAX_PARALLEL" in os.environ:
            config.max_parallel = int(os.environ["FACTORY_MAX_PARALLEL"])
        if "FACTORY_MAX_RETRIES" in os.environ:
            config.max_retries = int(os.environ["FACTORY_MAX_RETRIES"])
        if "FACTORY_RETRY_DELAY" in os.environ:
            config.retry_delay = float(os.environ["FACTORY_RETRY_DELAY"])
        return config


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
    """Create a git worktree for a component.

    A per-host fcntl flock on ``.ralph/worktrees/<component_id>.lock``
    serializes setup across concurrent factory invocations on the same
    machine. Without it, two simultaneously-running factories could
    clobber each other's worktree at the same path.

    POSIX only. On Windows the fcntl import fails; we degrade to the
    pre-lock behavior and document the limitation in the runbook.
    """
    worktree_base = root_dir / ".ralph" / "worktrees"
    worktree_base.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_base / component_id
    lock_path = worktree_base / f"{component_id}.lock"

    lock_fp = None
    try:
        try:
            import fcntl
            lock_fp = open(lock_path, "w")
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # Windows / unusual filesystems where flock isn't available.
            # We've already opened lock_fp on the Windows path? No -
            # ImportError on fcntl skips the open above. Just continue
            # without the lock; documented as a Windows non-support
            # caveat.
            lock_fp = None

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
            raise RuntimeError(
                f"Failed to create worktree for '{component_id}': {error}"
            )

        return worktree_path
    finally:
        if lock_fp is not None:
            try:
                import fcntl
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            lock_fp.close()


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
    feedforward_config_dict: dict[str, Any] | None = None,
    scaffold_cmd: str | None = None,
    component_deps: list[str] | None = None,
    knowledge_prefix: str = "",
    progress_file_str: str = "scripts/ralph/progress.txt",
    codebase_map_file_str: str = "scripts/ralph/codebase_map.md",
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

    # Note: the prompt template uses $prd_path which is substituted at runtime
    # by loop.py with config.prd_file, so the agent reads the correct component
    # PRD without needing to overwrite scripts/ralph/prd.json.

    # Copy prompt into worktree if needed
    worktree_prompt = worktree_path / prompt_file_str
    prompt_source = Path(prompt_file_str)
    if not worktree_prompt.exists() and prompt_source.exists():
        worktree_prompt.parent.mkdir(parents=True, exist_ok=True)
        worktree_prompt.write_text(prompt_source.read_text())

    # Copy CLAUDE.md into worktree. AGENTS.md is a symlink to CLAUDE.md,
    # so copying CLAUDE.md and recreating the symlink gives the agent both.
    # When use_worktrees=False, worktree_path IS the repo root so CLAUDE.md
    # is already in place - the .exists() guards handle this correctly.
    worktree_base = worktree_path / ".ralph" / "worktrees"
    if worktree_base.exists() and worktree_path.name != worktree_path.parent.name:
        # This is a real worktree: .ralph/worktrees/<id> -> root is 3 levels up
        repo_root = worktree_path.parent.parent.parent
    else:
        # No worktree (use_worktrees=False) - worktree_path is the repo root
        repo_root = worktree_path
    claude_dest = worktree_path / "CLAUDE.md"
    if not claude_dest.exists():
        claude_src = repo_root / "CLAUDE.md"
        if claude_src.exists():
            claude_dest.write_text(claude_src.read_text())
    agents_dest = worktree_path / "AGENTS.md"
    if not agents_dest.exists() and claude_dest.exists():
        agents_dest.symlink_to("CLAUDE.md")

    # Run scaffold script if configured
    if scaffold_cmd:
        try:
            subprocess.run(
                scaffold_cmd, shell=True, cwd=worktree_path,
                capture_output=True, timeout=120,
            )
        except Exception:
            pass  # scaffold failure is non-fatal

    # Build feedforward context (Phase 0)
    feedforward_prefix: str = ""
    if feedforward_config_dict:
        try:
            ff_config = FeedforwardConfig(**feedforward_config_dict)
            feedforward_prefix = build_feedforward_context(
                worktree_path, ff_config,
                component_id=component_id,
                component_deps=component_deps,
            )
        except Exception:
            pass  # feedforward failure is non-fatal

    # Build context prefix from previous retries
    context_prefix: str | None = None
    parts: list[str] = []
    if knowledge_prefix:
        parts.append(knowledge_prefix)
    if feedforward_prefix:
        parts.append(feedforward_prefix)
    if previous_context_json:
        ctx = IterationContext.from_json(previous_context_json)
        formatted = ctx.format_for_prompt()
        if formatted.strip():
            parts.append(formatted)
    if parts:
        context_prefix = "\n\n".join(parts)

    config = RalphConfig(
        max_iterations=30,
        prompt_file=worktree_prompt,
        prd_file=worktree_prd,
        progress_file=worktree_path / progress_file_str,
        codebase_map_file=worktree_path / codebase_map_file_str,
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
    import secrets

    from ralph_py.agents import get_agent

    factory_start = time.monotonic()
    factory_result = FactoryResult()
    # Stable run id shared by evolution journal and knowledge layer.
    # Includes a random nonce so two factory invocations launched within
    # the same UTC second do not collide on the run_id (and therefore on
    # .ralph/knowledge/<comp>/<run_id>/ directories).
    run_id = (
        f"factory-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        f"-{secrets.token_hex(3)}"
    )

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

    # E4: adversarial-call counter shared across review / security /
    # knowledge phases. When max_adversarial_calls is 0 the budget is
    # unbounded (current behavior); otherwise we skip the LLM phase
    # once the budget is exhausted, with an informational log line.
    adversarial_calls: dict[str, int] = {"count": 0}

    def _adversarial_budget_ok() -> bool:
        cap = factory_config.max_adversarial_calls
        if cap <= 0:
            return True
        return adversarial_calls["count"] < cap

    def _adversarial_budget_consume() -> None:
        adversarial_calls["count"] += 1

    def _path_relative_to_root(path: Path) -> str:
        """Render `path` relative to root_dir for use inside per-component
        worktrees. Falls back to the absolute path string when relativization
        fails (e.g. a path on a different mount)."""
        if not path.is_absolute():
            return str(path)
        try:
            return path.relative_to(root_dir).as_posix()
        except ValueError:
            try:
                return path.resolve().relative_to(root_dir.resolve()).as_posix()
            except ValueError:
                return str(path)

    prompt_file_rel = _path_relative_to_root(base_config.prompt_file)
    progress_file_rel = _path_relative_to_root(base_config.progress_file)
    codebase_map_file_rel = _path_relative_to_root(base_config.codebase_map_file)

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
        # Per-component allowed_paths comes from the PRD (architect-emitted
        # via DECOMPOSE_PROMPT v1.1.0+, REQUIRED for v1.2.0+). Without
        # this, the diff-scope check silently passes and a rogue agent
        # can touch anything in the worktree -- the end-to-end validation
        # run on 2026-05-27 caught an agent editing factory internals
        # because allowed_paths was always None here. Legacy PRDs without
        # the field load with allowed_paths=None which preserves the
        # prior "no constraint" behavior; v1.2.0+ architect outputs are
        # gated upstream in decompose._validate_decompose_output.
        #
        # Acknowledged limitation: the try/except below briefly masks
        # real PRD problems by falling through to allowed_paths=None.
        # The verifier surfaces real PRD problems on its own checks
        # (check_prd_stories) so this is a one-iteration grace period,
        # not silent forever -- but a downstream consumer reading
        # comp.findings should not assume allowed_paths=None means
        # "intentionally unconstrained" without cross-referencing the
        # verifier's PRD check status.
        component_allowed_paths: list[str] | None = None
        try:
            prd_for_scope = PRD.load(wt_path / comp.prd_path)
            component_allowed_paths = prd_for_scope.allowed_paths
        except (FileNotFoundError, ValueError):
            pass
        verification = run_mechanical_verification(
            wt_path,
            wt_path / comp.prd_path,
            manifest.base_branch,
            component_allowed_paths,
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

        # Fetch the component diff once and share it across Phase 2,
        # Phase 2.5, and knowledge distillation. Without this each phase
        # would shell out to `git diff` independently, redundantly
        # rebuilding the same patch on every component.
        from ralph_py import git as _git_for_diff
        shared_diff = _git_for_diff.get_diff_content(
            manifest.base_branch, wt_path,
        )

        # PHASE 2: Second-opinion review
        review_mode = ReviewMode(factory_config.review_mode)
        if review_mode != ReviewMode.SKIP and not _adversarial_budget_ok():
            ui.warn(
                f"  Phase 2 SKIPPED for {comp_id}: "
                f"adversarial LLM budget ({factory_config.max_adversarial_calls}) exhausted"
            )
            review_mode = ReviewMode.SKIP
        if review_mode != ReviewMode.SKIP:
            _adversarial_budget_consume()
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
                diff_content=shared_diff,
            )
            comp.review_passed = review_result.passed
            # E3: typed findings are the source of truth; the rendered
            # string is a derived view kept for backward-compat consumers.
            comp.findings.extend(review_result.as_findings())
            comp.review_findings = review_result.as_pr_body_section()
            # Observability gets criterion-only counts to preserve the
            # historical meaning of fail_count = "failed PRD criteria".
            # Concern counts ride along separately via fail_concerns /
            # advisory_concerns so dashboards can distinguish.
            progress_log.review_result(
                comp_id, review_result.passed,
                mode=review_mode.value,
                fail_count=review_result.criterion_fail_count,
                advisory_count=review_result.criterion_advisory_count,
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

        # PHASE 2.5: Security review (adversarial pass focused on vulns).
        # Runs as a separate LLM call with its own threat-model framing so
        # it catches what the correctness reviewer misses. Hard-mode
        # fails the component on findings at or above
        # SecurityConfig.fail_threshold OR on infrastructure errors.
        sec_config = factory_config.security_config
        if (
            sec_config and sec_config.mode != SecurityMode.SKIP.value
            and not _adversarial_budget_ok()
        ):
            ui.warn(
                f"  Phase 2.5 SKIPPED for {comp_id}: "
                f"adversarial LLM budget exhausted"
            )
            sec_config = None
        if sec_config and sec_config.mode != SecurityMode.SKIP.value:
            _adversarial_budget_consume()
            from ralph_py.agents import get_agent as _get_sec_agent

            ui.info(
                f"  Phase 2.5: security review ({sec_config.mode}) for {comp_id}..."
            )
            sec_result = None
            # The try/except deliberately wraps ONLY the agent-driven
            # work (getting the agent + running the review). Errors in
            # the retry-or-fail path below must NOT be swallowed - if
            # they were, a hard-mode security failure could fall through
            # to PR creation as if it had passed.
            try:
                sec_model = sec_config.model or base_config.model
                sec_agent = _get_sec_agent(
                    sec_config.agent_cmd or base_config.agent_cmd,
                    sec_model,
                    base_config.model_reasoning_effort,
                    sec_config.agent_type or base_config.agent_type,
                )
                sec_result = run_security_review(
                    sec_agent,
                    wt_path / comp.prd_path,
                    wt_path,
                    manifest.base_branch,
                    sec_config,
                    ui,
                    diff_content=shared_diff,
                )
            except Exception as exc:  # noqa: BLE001
                # Agent infrastructure failed before run_security_review
                # could classify the outcome. Hard mode must block;
                # advisory passes through.
                ui.warn(f"  Security review crashed: {exc}")
                if sec_config.mode == SecurityMode.HARD.value:
                    ctx = IterationContext.from_json(
                        comp_result.context_json or "{}",
                    )
                    ctx.add_review_finding(
                        f"Security review infrastructure error: {exc}",
                    )
                    _retry_or_fail(
                        comp,
                        f"Security review crashed: {exc}",
                        ctx.to_json(),
                    )
                    return

            if sec_result is not None:
                progress_log.review_result(
                    comp_id, sec_result.passed,
                    mode=f"security-{sec_config.mode}",
                    fail_count=sec_result.critical_count + sec_result.high_count,
                    advisory_count=len(sec_result.findings),
                    duration=sec_result.duration_seconds,
                )

                # E3: source-of-truth typed findings list, plus the
                # legacy rendered string for PR body / manifest readers.
                comp.findings.extend(sec_result.as_findings())
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
                    ui.warn(
                        f"  Phase 2.5 FAILED for {comp_id}: "
                        f"{sec_result.critical_count} critical, "
                        f"{sec_result.high_count} high"
                    )
                    ctx = IterationContext.from_json(
                        comp_result.context_json or "{}",
                    )
                    ctx.add_review_finding(sec_result.as_retry_context())
                    _retry_or_fail(comp, reason, ctx.to_json())
                    return

        # Knowledge distillation (Voyager-style post-gate write).
        # Runs after Phase 2 succeeds (or is skipped) but BEFORE the PR
        # merge step pulls main into the worktree, so the diff is the
        # component's true delta. Non-fatal on any failure.
        #
        # In single_pr mode every component shares one branch, which
        # means `git diff base...HEAD` for component B also includes
        # A's changes - distillation would write facts for B citing
        # A's code as evidence. Skip the phase entirely until A2's
        # follow-up wires up per-component diff isolation.
        if knowledge_config.enabled and manifest.single_pr:
            ui.info(
                f"  Knowledge: skipped for {comp_id} "
                f"(single_pr mode produces a polluted per-component diff)"
            )
        elif knowledge_config.enabled and not _adversarial_budget_ok():
            ui.info(
                f"  Knowledge: skipped for {comp_id} "
                f"(adversarial budget exhausted)"
            )
        elif knowledge_config.enabled:
            _adversarial_budget_consume()
            try:
                from ralph_py.agents import get_agent as _get_agent

                # Reuse the diff already fetched at the top of this
                # method - the worktree state hasn't changed between
                # Phase 1 and here.
                diff_content = shared_diff
                distill_model = (
                    knowledge_config.distill_model or base_config.model
                )
                distill_agent = _get_agent(
                    base_config.agent_cmd,
                    distill_model,
                    base_config.model_reasoning_effort,
                    base_config.agent_type,
                )
                written, status = distill_facts(
                    distill_agent,
                    comp,
                    diff_content,
                    wt_path / comp.prd_path,
                    comp_result.iterations,
                    run_id,
                    knowledge_config.knowledge_root,
                    knowledge_config,
                    wt_path,
                    comp.review_passed,
                )
                if written > 0:
                    ui.ok(f"  Knowledge: {status}")
                else:
                    ui.info(f"  Knowledge: {status}")
            except Exception as exc:  # noqa: BLE001 - non-fatal
                ui.warn(f"  Knowledge distillation failed: {exc}")

            # Fact-utilization metric: did the agent reference any of
            # the facts we injected at the top of the worker prompt?
            # Crude substring match against the post-iteration diff and
            # progress.txt; under-counts when the LLM paraphrases.
            try:
                prefix = build_knowledge_context(
                    manifest, comp,
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
                    util = measure_fact_utilization(
                        prefix, shared_diff, progress_text,
                    )
                    if util["injected"] > 0:
                        ui.info(
                            f"  Knowledge utilization: "
                            f"{util['referenced']}/{util['injected']} "
                            f"facts referenced in diff or progress.txt"
                        )
            except Exception:  # noqa: BLE001
                pass

        # All verification phases passed - create PR and merge
        if factory_config.create_prs:
            from ralph_py.pr import is_gh_available, push_create_and_merge_pr

            # E6: human-in-the-loop checkpoint. When opt-in, prompt
            # before pushing+merging so a human can inspect the diff,
            # the review findings, and the security findings before
            # the PR goes through. Skip the prompt when no UI is
            # interactive - automation should fail loudly rather than
            # block indefinitely.
            proceed = True
            if factory_config.pause_before_pr_merge:
                if ui.can_prompt():
                    ui.section(f"Human checkpoint: {comp_id}")
                    ui.info(comp.review_findings or "(no review findings)")
                    choice = ui.choose(
                        f"Approve PR creation and merge for {comp_id}?",
                        ["Approve", "Reject and abort component"],
                        default=0,
                    )
                    proceed = choice == 0
                    if not proceed:
                        ui.warn(
                            f"  Human rejected {comp_id} at PR checkpoint"
                        )
                        ctx = IterationContext.from_json(
                            comp_result.context_json or "{}",
                        )
                        ctx.add_review_finding(
                            "Human reviewer rejected at PR checkpoint",
                        )
                        _retry_or_fail(
                            comp, "Rejected at HITL checkpoint", ctx.to_json(),
                        )
                        return
                else:
                    ui.warn(
                        f"  pause_before_pr_merge requested but UI is "
                        f"non-interactive; proceeding without prompt for {comp_id}"
                    )

            if proceed and is_gh_available():
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

    # Build feedforward config dict for serialization to worker processes
    ff_config_dict: dict[str, Any] | None = None
    if factory_config.feedforward_config and factory_config.feedforward_config.enabled:
        fc = factory_config.feedforward_config
        ff_config_dict = {
            "enabled": fc.enabled,
            "module_map": fc.module_map,
            "public_interfaces": fc.public_interfaces,
            "dependency_graph": fc.dependency_graph,
            "conventions": fc.conventions,
            "max_context_tokens": fc.max_context_tokens,
        }

    # Load knowledge config once for the entire factory run
    knowledge_config = KnowledgeConfig.load(root_dir)

    def _submit_args(comp: Component, wt_path: Path) -> tuple[Any, ...]:
        ctx_json = component_contexts.get(comp.id)
        knowledge_prefix = ""
        if knowledge_config.enabled:
            try:
                knowledge_prefix = build_knowledge_context(
                    manifest, comp, knowledge_config.knowledge_root, knowledge_config,
                )
            except Exception:
                pass  # knowledge retrieval is non-fatal
        return (
            comp.id, comp.prd_path, str(wt_path),
            prompt_file_rel, base_config.agent_cmd, base_config.model,
            base_config.model_reasoning_effort, base_config.agent_type,
            base_config.sleep_seconds, ctx_json,
            ff_config_dict,
            comp.scaffold or None,
            comp.dependencies or None,
            knowledge_prefix,
            progress_file_rel,
            codebase_map_file_rel,
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

    # Record run to evolution journal
    try:
        from ralph_py.evolution import EvolutionConfig, EvolutionJournal

        evo_config = EvolutionConfig()
        if evo_config.enabled:
            journal = EvolutionJournal(evo_config)
            journal.record_run(run_id, manifest, factory_result)
    except Exception:
        pass  # evolution recording is non-fatal

    return factory_result
