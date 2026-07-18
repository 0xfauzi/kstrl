"""Factory orchestrator - parallel component execution with 3-phase verification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from ralph_py.config import RalphConfig
from ralph_py.context import IterationContext
from ralph_py.contract import (
    ContractCleanupError,
    ContractConfig,
    ContractMode,
    ContractResult,
    run_contract_testing,
)
from ralph_py.feedforward import FeedforwardConfig, build_feedforward_context
from ralph_py.findings import Finding
from ralph_py.git import GitDiffError, fetch_base_branch, resolve_base_ref
from ralph_py.knowledge import (
    KnowledgeConfig,
    build_knowledge_context,
    current_run_id,
    distill_facts,
    measure_fact_utilization,
)
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.observability import NullProgressLog, ProgressLog
from ralph_py.pr import create_prs_in_order, create_single_pr
from ralph_py.prd import PRD
from ralph_py.review import ReviewMode, ReviewResult, run_review
from ralph_py.security import (
    SecurityConfig,
    SecurityMode,
    SecurityResult,
    run_security_review,
)
from ralph_py.timeout import TimeoutConfig
from ralph_py.verify import (
    VerificationResult,
    VerifyConfig,
    run_mechanical_verification,
)

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
    # R2.3 (CRIT-8): explicit skip sentinel for Phase 1. verify_config=None
    # keeps its historical meaning of "use the default checks"; only this
    # flag (set by --no-verify) genuinely skips mechanical verification.
    # The skip is stated in the run output and recorded as a phase_skipped
    # finding so "ran clean" and "never ran" stay distinguishable.
    skip_verification: bool = False
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
    # R0.1: timeout limits (agent iteration, component wall clock,
    # scheduler backstop margin). None means run_factory loads
    # TimeoutConfig.load(root_dir) - toml [timeout] section + env.
    timeout_config: TimeoutConfig | None = None
    # R0.2: how long push_create_and_merge_pr waits for merge
    # confirmation before the component is parked as MERGE_PENDING.
    merge_timeout: float = 300.0
    # R0.5: proceed even when another invocation holds the run-level
    # .ralph/factory.lock. Deliberately CLI-only (no toml/env source):
    # forcing past the lock can corrupt a live run's worktrees and
    # manifest, so it must be an explicit per-invocation decision.
    force_lock: bool = False

    @classmethod
    def from_env(cls) -> FactoryConfig:
        """Load factory config from environment variables."""
        from ralph_py.config import _parse_bool

        return cls(
            max_parallel=int(os.environ.get("FACTORY_MAX_PARALLEL", "4")),
            max_retries=int(os.environ.get("FACTORY_MAX_RETRIES", "3")),
            retry_delay=float(os.environ.get("FACTORY_RETRY_DELAY", "5.0")),
            merge_timeout=float(os.environ.get("FACTORY_MERGE_TIMEOUT", "300.0")),
            max_adversarial_calls=int(
                os.environ.get("RALPH_FACTORY_MAX_ADVERSARIAL_CALLS", "0")
            ),
            pause_before_pr_merge=_parse_bool(
                os.environ.get("RALPH_FACTORY_PAUSE_BEFORE_PR_MERGE")
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> FactoryConfig:
        """Load factory config with precedence: env > toml > defaults.

        Reads the ``[factory]`` section from ``<root_dir>/ralph.toml`` if
        present, then overlays any matching env vars on top.
        """
        from ralph_py.config import _parse_bool, load_toml_section
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
        if "merge_timeout" in section:
            config.merge_timeout = float(section["merge_timeout"])
        # R2.2: the two safety knobs are reachable via toml (here), env
        # (below) and CLI flags (cli.py factory command).
        if "max_adversarial_calls" in section:
            config.max_adversarial_calls = int(section["max_adversarial_calls"])
        if "pause_before_pr_merge" in section:
            config.pause_before_pr_merge = bool(section["pause_before_pr_merge"])
        # Env overrides (consistent with from_env)
        if "FACTORY_MAX_PARALLEL" in os.environ:
            config.max_parallel = int(os.environ["FACTORY_MAX_PARALLEL"])
        if "FACTORY_MAX_RETRIES" in os.environ:
            config.max_retries = int(os.environ["FACTORY_MAX_RETRIES"])
        if "FACTORY_RETRY_DELAY" in os.environ:
            config.retry_delay = float(os.environ["FACTORY_RETRY_DELAY"])
        if "FACTORY_MERGE_TIMEOUT" in os.environ:
            config.merge_timeout = float(os.environ["FACTORY_MERGE_TIMEOUT"])
        if "RALPH_FACTORY_MAX_ADVERSARIAL_CALLS" in os.environ:
            config.max_adversarial_calls = int(
                os.environ["RALPH_FACTORY_MAX_ADVERSARIAL_CALLS"]
            )
        if "RALPH_FACTORY_PAUSE_BEFORE_PR_MERGE" in os.environ:
            config.pause_before_pr_merge = _parse_bool(
                os.environ["RALPH_FACTORY_PAUSE_BEFORE_PR_MERGE"]
            )
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
    # R0.2: components whose PR merge was initiated but not confirmed.
    # Not failed - a factory re-run re-polls them - but their dependents
    # were not scheduled, so the run is incomplete (nonzero exit code).
    merge_pending: list[str] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)
    # R0.3: unresolved contract failures (one human-readable line per
    # failed check). Non-empty forces a nonzero exit code even when no
    # single component could be blamed.
    contract_failures: list[str] = field(default_factory=list)
    exit_code: int = 0


class FactoryLockHeldError(RuntimeError):
    """Another factory invocation holds the run-level lock on this root."""


@dataclass
class _RunLock:
    """Handle for the run-level factory lock.

    ``held=True`` means we hold an exclusive flock for the whole run and
    may safely prune state left by previous runs. ``held=False`` means we
    are running WITHOUT exclusion (Windows/no-fcntl degrade, or
    ``--force-lock``): stale-state cleanup must be skipped because another
    live invocation may own it.
    """

    fp: IO[str] | None
    held: bool

    def release(self) -> None:
        if self.fp is None:
            return
        try:
            import fcntl
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self.fp.close()
        self.fp = None


def _acquire_run_lock(root_dir: Path, ui: UI, force: bool) -> _RunLock:
    """Take the run-level flock on ``.ralph/factory.lock`` (R0.5, H-7).

    Held for the entire run so a second ``ralph factory`` / ``ralph run``
    on the same root refuses to start instead of destroying the first
    invocation's in-flight worktrees and clobbering its manifest. flock
    releases automatically if the holder dies, so a crashed run never
    wedges the root.

    POSIX only, like the A4 per-component lock: without fcntl we degrade
    to no exclusion with a warning. ``force=True`` proceeds past a held
    lock with a warning instead of raising FactoryLockHeldError.
    """
    lock_path = root_dir / ".ralph" / "factory.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        ui.warn(
            "Run-level factory lock unavailable on this platform (no "
            "fcntl); concurrent invocations on this root are not excluded"
        )
        return _RunLock(fp=None, held=False)

    # "a+" so a refused attempt can read the holder's pid without
    # truncating it.
    fp = open(lock_path, "a+")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = ""
        try:
            fp.seek(0)
            holder = fp.read(64).strip()
        except OSError:
            pass
        fp.close()
        holder_note = f" (pid {holder})" if holder else ""
        if force:
            ui.warn(
                f"--force-lock: proceeding while {lock_path} is held"
                f"{holder_note}; concurrent runs can corrupt each "
                f"other's worktrees and manifest"
            )
            return _RunLock(fp=None, held=False)
        raise FactoryLockHeldError(
            f"Another ralph invocation{holder_note} holds {lock_path}; "
            f"refusing to start a second factory run on this root. "
            f"Wait for it to finish, or re-run with --force-lock to "
            f"override."
        ) from None

    # Holder pid is diagnostic only (shown in the refusal message of a
    # contending invocation); the flock itself is the exclusion.
    try:
        fp.seek(0)
        fp.truncate()
        fp.write(f"{os.getpid()}\n")
        fp.flush()
    except OSError:
        pass
    return _RunLock(fp=fp, held=True)


def _remove_stale_index_lock(root_dir: Path, component_id: str) -> None:
    """Remove a stale index.lock left behind by a killed git operation.

    A timed-out agent is SIGKILLed and can die mid-git-op inside its
    worktree; git then refuses every subsequent operation there. The lock
    for a worktree lives under the MAIN repo's .git/worktrees/<name>/.
    Only the component's own lock is touched - the main repo's
    .git/index.lock may belong to a live operator process and is left
    alone.
    """
    lock = root_dir / ".git" / "worktrees" / component_id / "index.lock"
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        pass


def _setup_worktree(
    component_id: str,
    branch_name: str,
    base_branch: str,
    root_dir: Path,
    run_id: str,
    fresh_from_base: bool = False,
) -> Path:
    """Create a git worktree for a component.

    Worktrees are keyed ``.ralph/worktrees/<run_id>/<component_id>``
    (R0.5, H-7): two invocations never share a worktree path, so setup
    can only ever remove a leftover from an earlier attempt of THIS run
    (a retry), never another invocation's in-flight worktree. Run-level
    exclusion itself is the ``.ralph/factory.lock`` flock in run_factory.

    A per-host fcntl flock on ``.ralph/worktrees/<component_id>.lock``
    (run-agnostic on purpose) still serializes the git commands here for
    the degraded modes that run without the run-level lock (Windows,
    ``--force-lock``), where two invocations could otherwise race on the
    shared branch and .git metadata.

    ``fresh_from_base=True`` (used for retries after a timeout kill)
    additionally deletes the component branch so the worktree is recreated
    from ``base_branch`` instead of silently reusing possibly-dirty state
    from the killed attempt (R0.1).

    POSIX only. On Windows the fcntl import fails; we degrade to the
    pre-lock behavior and document the limitation in the runbook.
    """
    worktree_base = root_dir / ".ralph" / "worktrees" / run_id
    worktree_base.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_base / component_id
    lock_path = root_dir / ".ralph" / "worktrees" / f"{component_id}.lock"

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

        # A killed prior attempt may have left git mid-operation.
        _remove_stale_index_lock(root_dir, component_id)

        if worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=root_dir, capture_output=True, timeout=30,
            )

        if fresh_from_base:
            # Delete the branch from the killed attempt so the add below
            # recreates it from base rather than reusing its commits.
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=root_dir, capture_output=True, timeout=30,
            )

        # R0.2: cut from origin/<base> when a remote exists so this
        # component builds on the squash-merged history of its
        # dependencies, not a stale local base ref. The fetch is
        # freshness-only and non-fatal: offline runs fall back to the
        # current tracking ref, local-only repos to the local base.
        fetch_base_branch(base_branch, root_dir, timeout=60.0)
        base_ref = resolve_base_ref(base_branch, root_dir)

        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch_name, base_ref],
            cwd=root_dir, capture_output=True, text=True, timeout=30,
        )

        if result.returncode != 0:
            # Branch already exists: reuse it WITH its commits. After the
            # run-start preflight (_preflight_component_branches) this can
            # only be a branch created during THIS run - a non-timeout
            # retry resuming its own progress, or single_pr components
            # stacking on the shared branch. Stale branches from previous
            # runs were deleted (fully merged) or refused at preflight,
            # never silently reused here (R0.5).
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


def _cleanup_worktree(component_id: str, root_dir: Path, run_id: str) -> None:
    """Remove a git worktree for a component of the current run."""
    worktree_path = root_dir / ".ralph" / "worktrees" / run_id / component_id
    if not worktree_path.exists():
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=root_dir, capture_output=True, timeout=30,
    )


def _prune_stale_worktrees(root_dir: Path, run_id: str, ui: UI) -> None:
    """Remove worktrees left behind by previous (crashed/aborted) runs.

    Only called when the run-level flock is genuinely held: any prior
    holder has exited (flock dies with its process), so everything under
    ``.ralph/worktrees/`` that is not ours - other runs' ``<run_id>/``
    dirs, and pre-R0.5 flat-layout ``<component_id>/`` worktrees - is
    orphaned and safe to remove. Includes worktrees kept for leaked
    workers (R0.1): their owning run is gone, so by the next invocation
    they are stale state, matching the pre-R0.5 force-remove behavior.
    """
    worktree_root = root_dir / ".ralph" / "worktrees"
    if not worktree_root.exists():
        return
    removed = 0
    for entry in sorted(worktree_root.iterdir()):
        if entry.name == run_id or not entry.is_dir():
            continue  # our own run dir, or a per-component .lock file
        if (entry / ".git").exists():
            # Pre-R0.5 flat layout: the entry itself is a worktree.
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(entry)],
                cwd=root_dir, capture_output=True, timeout=30,
            )
            removed += 1
        else:
            # <run_id>/ dir from a previous run: remove each component
            # worktree inside it.
            for wt in sorted(entry.iterdir()):
                if wt.is_dir():
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(wt)],
                        cwd=root_dir, capture_output=True, timeout=30,
                    )
                    removed += 1
        # Whatever git could not remove (or non-worktree debris) goes
        # with the dir; `git worktree prune` below drops any metadata
        # orphaned by this.
        shutil.rmtree(entry, ignore_errors=True)
    if removed:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        ui.info(f"  Pruned {removed} stale worktree(s) from previous runs")


def _preflight_component_branches(
    manifest: Manifest, root_dir: Path, ui: UI,
) -> list[str]:
    """Refuse to silently reuse component branches from previous runs.

    For every branch a PENDING component would be provisioned on: if it
    already exists and is fully merged into the base branch, delete it
    (setup recreates it from base); if it exists with unmerged commits,
    return an error naming it - the caller refuses the run and the
    operator decides (merge or ``git branch -D``). Previously such
    branches were silently reused with their old commits via the
    worktree-add fallback (R0.5, H-7).

    Note: a squash-merged branch is NOT an ancestor of base (the squash
    rewrites history), so leftovers from squash-merge flows are refused
    rather than auto-deleted. Loud beats lossy.
    """
    errors: list[str] = []
    fetch_base_branch(manifest.base_branch, root_dir, timeout=60.0)
    base_ref = resolve_base_ref(manifest.base_branch, root_dir)
    seen: set[str] = set()
    for comp in manifest.components:
        if comp.status != ComponentStatus.PENDING.value:
            continue
        branch = comp.branch_name
        if branch in seen:
            continue  # single_pr: all components share one branch
        seen.add(branch)
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        if exists.returncode != 0:
            continue
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base_ref],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        if merged.returncode == 0:
            deleted = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=root_dir, capture_output=True, text=True, timeout=30,
            )
            if deleted.returncode == 0:
                ui.info(
                    f"  Deleted stale branch '{branch}' from a previous "
                    f"run (fully merged into {manifest.base_branch})"
                )
            else:
                errors.append(
                    f"stale branch '{branch}' (component '{comp.id}') is "
                    f"fully merged but could not be deleted: "
                    f"{deleted.stderr.strip()}"
                )
        else:
            errors.append(
                f"branch '{branch}' (component '{comp.id}') already exists "
                f"with commits not merged into '{manifest.base_branch}'; "
                f"refusing to silently reuse it. Merge it or delete it "
                f"(git branch -D {branch}) and re-run."
            )
    return errors


def _run_component(
    component_id: str,
    prd_path_str: str,
    worktree_path_str: str,
    root_dir_str: str,
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
    agent_iteration_timeout: float = 1800.0,
    component_timeout: float = 7200.0,
    max_iterations: int = 10,
    interactive: bool = False,
    allowed_paths: list[str] | None = None,
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
    # R0.4: every copy source below resolves against root_dir, never the
    # worker's inherited CWD. prompt.md and the PRD live under gitignored
    # scripts/ralph/, so a fresh worktree NEVER contains them via git; if
    # a CWD-relative lookup missed them (e.g. --root from another
    # directory) the copies silently no-op'd and the engineer fell back
    # to the harness DEFAULT_PROMPT (phase-f e2e validation, line 38).
    root_dir = Path(root_dir_str)

    ui = PlainUI(no_color=True)
    agent = get_agent(agent_cmd, model, reasoning, agent_type)

    # Copy PRD into worktree if needed
    worktree_prd = worktree_path / prd_path_str
    prd_source = root_dir / prd_path_str
    if not worktree_prd.exists() and prd_source.exists():
        worktree_prd.parent.mkdir(parents=True, exist_ok=True)
        worktree_prd.write_text(prd_source.read_text())

    # The prompt template's $prd_path placeholder (shipped in
    # DEFAULT_PROMPT >= 1.1.0 and the scaffolded prompt.md) is substituted
    # at runtime by loop.py with config.prd_file, so the agent reads the
    # SAME per-component PRD that check_prd_stories re-reads (R2.3, H-11)
    # without overwriting scripts/ralph/prd.json.

    # Copy prompt into worktree if needed
    worktree_prompt = worktree_path / prompt_file_str
    prompt_source = root_dir / prompt_file_str
    if not worktree_prompt.exists() and prompt_source.exists():
        worktree_prompt.parent.mkdir(parents=True, exist_ok=True)
        worktree_prompt.write_text(prompt_source.read_text())

    # Copy CLAUDE.md / AGENTS.md into the worktree from root_dir. When
    # use_worktrees=False, worktree_path IS the repo root so the files are
    # already in place - the .exists() guards handle this correctly.
    claude_dest = worktree_path / "CLAUDE.md"
    claude_src = root_dir / "CLAUDE.md"
    if not claude_dest.exists() and claude_src.exists():
        claude_dest.write_text(claude_src.read_text())
    agents_dest = worktree_path / "AGENTS.md"
    agents_src = root_dir / "AGENTS.md"
    if not agents_dest.exists():
        if agents_src.is_symlink() and claude_dest.exists():
            # Preserve the AGENTS.md -> CLAUDE.md symlink convention.
            agents_dest.symlink_to("CLAUDE.md")
        elif agents_src.exists():
            agents_dest.write_text(agents_src.read_text())
        elif claude_dest.exists():
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

    # R2.3 (CRIT-8): max_iterations, interactive, and allowed_paths come
    # from the invoking config via _submit_args. They were previously
    # hardcoded here (30 / False / unset), which made `ralph run N`, -i,
    # and --allowed-paths silent no-ops under the factory pipeline.
    config = RalphConfig(
        max_iterations=max_iterations,
        prompt_file=worktree_prompt,
        prd_file=worktree_prd,
        progress_file=worktree_path / progress_file_str,
        codebase_map_file=worktree_path / codebase_map_file_str,
        sleep_seconds=sleep_seconds,
        interactive=interactive,
        allowed_paths=list(allowed_paths) if allowed_paths else [],
        ralph_branch="",
        ralph_branch_explicit=True,
        agent_cmd=agent_cmd,
        model=model,
        model_reasoning_effort=reasoning,
        agent_type=agent_type,
        ui_mode="plain",
        no_color=True,
    )

    timeouts = TimeoutConfig(
        agent_iteration=agent_iteration_timeout,
        component_total=component_timeout,
    )

    try:
        result = run_loop(
            config, ui, agent, worktree_path,
            context_prefix=context_prefix, timeouts=timeouts,
        )
        # Report which limit fired so the retry/fail path can act on it
        # (timeout errors trigger the recreate-from-base retry hygiene).
        if result.completed:
            error = None
        elif result.timeout_limit == "component":
            error = (
                f"component timeout: exceeded {component_timeout}s wall clock "
                f"after {result.iterations} iteration(s)"
            )
        elif result.timed_out_iterations:
            error = (
                f"Did not complete ({result.timed_out_iterations} iteration(s) "
                f"hit the {agent_iteration_timeout}s agent iteration timeout)"
            )
        else:
            error = "Did not complete"
        return ComponentResult(
            component_id=component_id,
            success=result.completed,
            iterations=result.iterations,
            error=error,
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


def _next_backstop_wait(
    running: Mapping[Future[ComponentResult], str],
    deadlines: Mapping[Future[ComponentResult], float],
    now: float,
) -> float | None:
    """Seconds until the nearest scheduler-backstop deadline among running
    futures. None means no deadline is armed (wait indefinitely for the
    next completion, the pre-R0.1 behavior)."""
    pending = [deadlines[f] for f in running if f in deadlines]
    if not pending:
        return None
    return max(0.0, min(pending) - now)


def _expired_futures(
    running: Mapping[Future[ComponentResult], str],
    deadlines: Mapping[Future[ComponentResult], float],
    now: float,
) -> list[Future[ComponentResult]]:
    """Running futures whose scheduler-backstop deadline has passed."""
    return [
        f for f in running
        if not f.done() and f in deadlines and now >= deadlines[f]
    ]


def run_factory(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: RalphConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None = None,
) -> FactoryResult:
    """Run the factory orchestrator with 3-phase verification.

    Phase 1: Mechanical verification (tests, typecheck, lint, PRD, diff scope)
    Phase 2: Second-opinion review (separate agent reviews diff against spec)
    Phase 3: Contract testing (merge tier branches, run integration tests)

    ``manifest_path`` is where run state is SAVED as well as where it was
    loaded from (R0.5, H-15): ``--manifest /custom.json`` must persist to
    /custom.json and ``ralph run`` to its own run-manifest.json, never to
    another invocation's resumable ``scripts/ralph/manifest.json``. None
    keeps the historical default of ``<root>/scripts/ralph/manifest.json``.

    Holds the run-level ``.ralph/factory.lock`` flock for the whole run
    (R0.5, H-7); a contending invocation is refused with exit code 2
    unless it passes ``--force-lock``.
    """
    try:
        run_lock = _acquire_run_lock(
            root_dir, ui, force=factory_config.force_lock,
        )
    except FactoryLockHeldError as exc:
        ui.err(str(exc))
        refused = FactoryResult()
        refused.exit_code = 2
        return refused
    try:
        return _run_factory_locked(
            manifest, factory_config, base_config, ui, root_dir,
            manifest_path=manifest_path, lock_held=run_lock.held,
        )
    finally:
        run_lock.release()


def _run_factory_locked(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: RalphConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None,
    lock_held: bool,
) -> FactoryResult:
    """run_factory body; runs with the run-level lock resolved (held, or
    explicitly degraded via --force-lock / no-fcntl platforms)."""
    from ralph_py.agents import get_agent

    factory_start = time.monotonic()
    factory_result = FactoryResult()
    # Stable run id shared by evolution journal and knowledge layer.
    # current_run_id() carries microseconds plus a random nonce, so two
    # factory invocations launched within the same UTC second neither
    # collide on .ralph/knowledge/<comp>/<run_id>/ directories nor
    # order ambiguously (R1.6: same-second knowledge run dirs must sort
    # by creation time, not by nonce).
    run_id = current_run_id()

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

    if manifest_path is None:
        manifest_path = root_dir / "scripts" / "ralph" / "manifest.json"

    # R0.2 crash recovery: MERGE_PENDING is re-pollable, not failed. A
    # prior run initiated the merge but could not confirm it; check the
    # PR state again before scheduling so confirmed merges unblock their
    # dependents. Local imports keep the late binding tests rely on.
    merge_pending_comps = [
        c for c in manifest.components
        if c.status == ComponentStatus.MERGE_PENDING.value
    ]
    if merge_pending_comps:
        from ralph_py.pr import is_gh_available, pr_number_from_url, wait_for_merge

        if not factory_config.create_prs or not is_gh_available():
            ui.warn(
                f"  {len(merge_pending_comps)} component(s) are merge-pending "
                f"but PR polling is unavailable (create_prs off or gh "
                f"missing); their dependents stay blocked"
            )
        else:
            for comp in merge_pending_comps:
                pr_number = comp.pr_number or pr_number_from_url(comp.pr_url)
                if not pr_number:
                    ui.warn(
                        f"  Cannot re-poll '{comp.id}': no PR number recorded"
                    )
                    continue
                ui.info(
                    f"  Re-polling merge state for '{comp.id}' "
                    f"(PR #{pr_number})..."
                )
                merge_state = wait_for_merge(
                    pr_number, root_dir, timeout=factory_config.merge_timeout,
                )
                if merge_state == "merged":
                    fetch_base_branch(manifest.base_branch, root_dir)
                    comp.status = ComponentStatus.COMPLETED.value
                    comp.error = ""
                    factory_result.completed.append(comp.id)
                    progress_log.component_completed(
                        comp.id, comp.duration_seconds, comp.iteration_count,
                    )
                    ui.ok(f"  PR #{pr_number} merged; '{comp.id}' completed")
                elif merge_state == "closed":
                    comp.status = ComponentStatus.FAILED.value
                    comp.error = f"PR #{pr_number} closed without merge"
                    skipped = manifest.cascade_skip(comp.id)
                    factory_result.failed.append(comp.id)
                    factory_result.skipped.extend(skipped)
                    progress_log.component_failed(comp.id, comp.error)
                    ui.err(f"  Failed: {comp.id}: {comp.error}")
                else:
                    ui.warn(
                        f"  '{comp.id}' still awaiting merge of "
                        f"PR #{pr_number}; dependents stay blocked"
                    )
        manifest.save(manifest_path)

    # Determine effective parallelism
    max_parallel = factory_config.max_parallel
    if not factory_config.use_worktrees:
        max_parallel = 1
        ui.info("Worktrees disabled: running sequentially")
    if factory_config.single_pr and max_parallel > 1:
        # R0.5 (H-8): single_pr components all live on ONE branch, and a
        # branch can only be checked out in one worktree at a time -
        # parallel same-tier components would hard-fail on "already
        # checked out". Sequential is the only layout that works.
        max_parallel = 1
        ui.info(
            "single_pr mode: components share one branch; "
            "forcing max_parallel=1"
        )

    # R0.1: TimeoutConfig is the single source for the agent-iteration and
    # component wall-clock limits. Enforcement layers: the adapters kill
    # their subprocess group, run_loop aborts on the component wall clock,
    # and the scheduler backstop below catches a worker that hangs outside
    # both (e.g. a stuck scaffold or feedforward step).
    timeout_cfg = factory_config.timeout_config or TimeoutConfig.load(root_dir)
    backstop_seconds = (
        timeout_cfg.component_total + timeout_cfg.scheduler_backstop_margin
        if timeout_cfg.component_total > 0 else 0.0
    )

    # R0.5 worktree crash recovery + stale-branch policy. Both only
    # apply to worktree mode: without worktrees the factory neither
    # creates branches nor worktree dirs.
    if factory_config.use_worktrees:
        if lock_held:
            _prune_stale_worktrees(root_dir, run_id, ui)
        else:
            ui.warn(
                "  Run lock not held; skipping stale-worktree cleanup "
                "(another live invocation may own them)"
            )
        branch_errors = _preflight_component_branches(manifest, root_dir, ui)
        if branch_errors:
            ui.err("Refusing to run: stale component branches found")
            for line in branch_errors:
                ui.err(f"  {line}")
            factory_result.exit_code = 2
            return factory_result

    ui.section("Factory: Execution")
    ui.kv("Max parallel", str(max_parallel))
    ui.kv("Max retries", str(factory_config.max_retries))
    ui.kv("Review mode", factory_config.review_mode)
    contract_mode = (
        factory_config.contract_config.mode
        if factory_config.contract_config else "skip"
    )
    ui.kv("Contract check", contract_mode)
    ui.kv(
        "Agent timeout",
        f"{timeout_cfg.agent_iteration}s"
        if timeout_cfg.agent_iteration > 0 else "<disabled>",
    )
    ui.kv(
        "Component timeout",
        f"{timeout_cfg.component_total}s"
        if timeout_cfg.component_total > 0 else "<disabled>",
    )

    # Scheduling state
    worktree_paths: dict[str, Path] = {}
    component_contexts: dict[str, str] = {}  # comp_id -> context JSON
    # Components whose last failure was a timeout kill: their retry must
    # not trust the surviving worktree/branch state (R0.1 requirement 5).
    timeout_retry_ids: set[str] = set()
    # Components abandoned by the scheduler backstop; their workers may
    # still be alive, so their worktrees are never cleaned up here.
    leaked_component_ids: set[str] = set()

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
            # A timeout failure means the agent was killed mid-flight: the
            # worktree/branch state cannot be trusted. Note the hygiene
            # behavior in the error string so the audit trail explains why
            # the retry does not resume from the killed attempt's commits.
            if "timeout" in error.lower() and factory_config.use_worktrees:
                timeout_retry_ids.add(comp.id)
                error = (
                    error
                    + " [timeout retry: worktree recreated from base; "
                    "stale index.lock removed]"
                )
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

        def _record_phase_skip(phase: str, reason: str) -> None:
            """R1.2: a phase that never ran must leave a trace in both
            the findings stream and the journal, so "ran clean" and
            "never ran" are distinguishable downstream."""
            comp.findings.append(Finding.phase_skipped(phase, reason))
            progress_log.emit(
                "phase_skipped", comp_id,
                {"phase": phase, "reason": reason},
            )

        # PHASE 1: Mechanical verification
        comp.status = ComponentStatus.VERIFYING.value
        manifest.save(manifest_path)

        if factory_config.skip_verification:
            # R2.3: --no-verify. Previously verify_config=None fell
            # through to VerifyConfig() defaults here and Phase 1 ran
            # anyway - on a non-Python repo that burned every retry
            # against checks that could never pass. The empty
            # VerificationResult below is what downstream reviewers see:
            # no checks ran, none are claimed.
            ui.info(
                f"  Phase 1 SKIPPED for {comp_id}: mechanical "
                f"verification disabled (--no-verify)"
            )
            comp.verification_passed = None
            _record_phase_skip(
                "verify", "mechanical verification disabled (--no-verify)",
            )
            verification = VerificationResult(passed=True, checks=[])
        else:
            verify_config = factory_config.verify_config or VerifyConfig()
            ui.info(f"  Phase 1: mechanical verification for {comp_id}...")
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
            verification = run_mechanical_verification(
                wt_path,
                wt_path / comp.prd_path,
                manifest.base_branch,
                component_allowed_paths,
                verify_config,
                allowed_paths_error=allowed_paths_error,
            )
            verify_duration = time.monotonic() - verify_start
            comp.verification_passed = verification.passed
            progress_log.verification_result(
                comp_id, verification.passed,
                check_names=[c.name for c in verification.checks],
                failures=[
                    c.message for c in verification.checks if not c.passed
                ],
                duration=verify_duration,
            )

            if not verification.passed:
                failing = [c for c in verification.checks if not c.passed]
                ui.warn(
                    f"  Phase 1 FAILED for {comp_id}: "
                    f"{', '.join(c.name for c in failing)}"
                )
                ctx = IterationContext.from_json(
                    comp_result.context_json or "{}",
                )
                ctx.add_verification_failure(verification.as_context())
                _retry_or_fail(
                    comp, "Mechanical verification failed", ctx.to_json(),
                )
                return

            ui.ok(f"  Phase 1 passed for {comp_id}")

        # Fetch the component diff once and share it across Phase 2,
        # Phase 2.5, and knowledge distillation. Without this each phase
        # would shell out to `git diff` independently, redundantly
        # rebuilding the same patch on every component.
        #
        # R1.3 (H-14): a git failure here used to yield "" and all three
        # consumers silently reviewed an empty diff and passed. Now it
        # is an infrastructure failure for the component: record the
        # infra finding, journal it, and retry/fail closed.
        from ralph_py import git as _git_for_diff
        try:
            shared_diff = _git_for_diff.get_diff_content(
                manifest.base_branch, wt_path,
            )
        except GitDiffError as exc:
            ui.err(f"  Diff fetch FAILED for {comp_id}: {exc}")
            comp.findings.append(Finding.infrastructure_error(
                phase="diff",
                explanation=(
                    f"git diff against {manifest.base_branch} failed; "
                    f"review/security/knowledge cannot run: {exc}"
                ),
            ))
            progress_log.emit(
                "diff_fetch_failed", comp_id, {"error": str(exc)},
            )
            ctx = IterationContext.from_json(comp_result.context_json or "{}")
            ctx.add_verification_failure(
                f"git diff against {manifest.base_branch} failed: {exc}"
            )
            _retry_or_fail(
                comp, f"Diff fetch failed (infrastructure): {exc}",
                ctx.to_json(),
            )
            return

        # Forensic home for full raw reviewer output on parse failures
        # (R1.2; mirrors knowledge.py's _debug/<run_id>/ layout).
        adversarial_debug_dir = (
            root_dir / ".ralph" / "debug" / run_id / comp_id
        )

        # PHASE 2: Second-opinion review
        review_mode = ReviewMode(factory_config.review_mode)
        review_skip_reason: str | None = None
        if review_mode == ReviewMode.SKIP:
            review_skip_reason = "review disabled (mode=skip)"
        elif not _adversarial_budget_ok():
            ui.warn(
                f"  Phase 2 SKIPPED for {comp_id}: "
                f"adversarial LLM budget ({factory_config.max_adversarial_calls}) exhausted"
            )
            review_skip_reason = (
                f"adversarial LLM budget "
                f"({factory_config.max_adversarial_calls}) exhausted"
            )
            review_mode = ReviewMode.SKIP
        if review_mode != ReviewMode.SKIP:
            _adversarial_budget_consume()
            ui.info(f"  Phase 2: review ({review_mode.value}) for {comp_id}...")

            # R1.2: wrap the agent-driven work like Phase 2.5 does. A
            # reviewer crash degrades to a per-component infrastructure
            # failure; it must never abort the whole factory run.
            try:
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
                    debug_dir=adversarial_debug_dir,
                )
            except Exception as exc:  # noqa: BLE001
                ui.warn(f"  Review crashed: {exc}")
                review_result = ReviewResult(
                    passed=review_mode != ReviewMode.HARD,
                    mode=review_mode.value,
                    overall_notes=f"Review agent crashed: {exc}",
                    infrastructure_error=True,
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
                reason = (
                    "Review infrastructure error"
                    if review_result.infrastructure_error
                    else "Review failed"
                )
                ui.warn(
                    f"  Phase 2 FAILED for {comp_id}: "
                    f"{review_result.fail_count} failures"
                )
                ctx = IterationContext.from_json(comp_result.context_json or "{}")
                ctx.add_review_finding(review_result.as_retry_context())
                _retry_or_fail(comp, reason, ctx.to_json())
                return

            ui.ok(f"  Phase 2 passed for {comp_id}")
        else:
            comp.review_passed = None
            _record_phase_skip(
                "review", review_skip_reason or "review skipped",
            )

        # PHASE 2.5: Security review (adversarial pass focused on vulns).
        # Runs as a separate LLM call with its own threat-model framing so
        # it catches what the correctness reviewer misses. Hard-mode
        # fails the component on findings at or above
        # SecurityConfig.fail_threshold OR on infrastructure errors.
        sec_config = factory_config.security_config
        if sec_config is None:
            _record_phase_skip(
                "security", "security review not configured",
            )
        elif sec_config.mode == SecurityMode.SKIP.value:
            _record_phase_skip(
                "security", "security review disabled (mode=skip)",
            )
        elif not _adversarial_budget_ok():
            ui.warn(
                f"  Phase 2.5 SKIPPED for {comp_id}: "
                f"adversarial LLM budget exhausted"
            )
            _record_phase_skip(
                "security", "adversarial LLM budget exhausted",
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
                    debug_dir=adversarial_debug_dir,
                )
            except Exception as exc:  # noqa: BLE001
                # Agent infrastructure failed before run_security_review
                # could classify the outcome. Synthesize an infra result
                # and fall through to the shared recording block below:
                # hard mode blocks via passed=False, advisory continues
                # but the infra finding stays in the findings stream and
                # the PR body instead of vanishing (R1.2, sec-pr-body).
                ui.warn(f"  Security review crashed: {exc}")
                sec_result = SecurityResult(
                    passed=sec_config.mode != SecurityMode.HARD.value,
                    mode=sec_config.mode,
                    overall_notes=(
                        f"Security review agent failed before "
                        f"completion: {exc}"
                    ),
                    infrastructure_error=True,
                )

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
                    # as_retry_context is empty for infra results (no
                    # findings list); fall back to the notes so the
                    # retry prompt still says what went wrong.
                    ctx.add_review_finding(
                        sec_result.as_retry_context()
                        or "Security review infrastructure error: "
                        + sec_result.overall_notes
                    )
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
            _record_phase_skip(
                "knowledge",
                "single_pr mode produces a polluted per-component diff",
            )
        elif knowledge_config.enabled and not _adversarial_budget_ok():
            ui.info(
                f"  Knowledge: skipped for {comp_id} "
                f"(adversarial budget exhausted)"
            )
            _record_phase_skip(
                "knowledge", "adversarial LLM budget exhausted",
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

        # All verification phases passed - create PR and merge.
        # single_pr mode is exempt: every component shares one branch,
        # a single PR is created at end-of-run, and squash-merging the
        # shared branch per component would destroy the history the
        # remaining components build on.
        if factory_config.create_prs and not factory_config.single_pr:
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
                outcome = push_create_and_merge_pr(
                    comp, manifest, root_dir, ui,
                    merge_method="squash",
                    merge_timeout=factory_config.merge_timeout,
                )
                if outcome.pr_url:
                    factory_result.pr_urls.append(outcome.pr_url)
                manifest.save(manifest_path)

                # R0.2 (CRIT-2): COMPLETED requires a CONFIRMED merge.
                # Anything less and dependents would cut worktrees from
                # a base that lacks this component's code.
                if not outcome.merged:
                    if outcome.merge_pending:
                        comp.status = ComponentStatus.MERGE_PENDING.value
                        comp.error = (
                            outcome.error or "PR merge not confirmed"
                        )
                        ui.warn(
                            f"  MERGE PENDING: {comp_id}: {comp.error}; "
                            f"dependents stay blocked; a factory re-run "
                            f"re-polls the PR"
                        )
                    else:
                        comp.status = ComponentStatus.FAILED.value
                        comp.error = outcome.error or "PR flow failed"
                        skipped = manifest.cascade_skip(comp_id)
                        factory_result.failed.append(comp_id)
                        factory_result.skipped.extend(skipped)
                        progress_log.component_failed(comp_id, comp.error)
                        ui.err(f"  Failed: {comp_id}: {comp.error[:120]}")
                    manifest.save(manifest_path)
                    return
            elif proceed:
                # No gh: the PR/merge gate cannot run. Completing anyway
                # preserves local-only workflows, but say so loudly -
                # this component's code exists only on its local branch.
                ui.warn(
                    f"  gh CLI not available: {comp_id} completes without "
                    f"a PR; its code stays on branch {comp.branch_name}"
                )

        # Clean up worktree now that code is merged
        if factory_config.use_worktrees and comp_id in worktree_paths:
            _cleanup_worktree(comp_id, root_dir, run_id)
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
                fresh_from_base = comp.id in timeout_retry_ids
                timeout_retry_ids.discard(comp.id)
                wt_path = _setup_worktree(
                    comp.id, comp.branch_name, manifest.base_branch, root_dir,
                    run_id, fresh_from_base=fresh_from_base,
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
            comp.id, comp.prd_path, str(wt_path), str(root_dir),
            prompt_file_rel, base_config.agent_cmd, base_config.model,
            base_config.model_reasoning_effort, base_config.agent_type,
            base_config.sleep_seconds, ctx_json,
            ff_config_dict,
            comp.scaffold or None,
            comp.dependencies or None,
            knowledge_prefix,
            progress_file_rel,
            codebase_map_file_rel,
            timeout_cfg.agent_iteration,
            timeout_cfg.component_total,
            # R2.3 (CRIT-8): forward the invoking config's loop settings;
            # they were previously dropped here and _run_component ran a
            # hardcoded 30 non-interactive iterations with no path guard.
            base_config.max_iterations,
            base_config.interactive,
            base_config.allowed_paths or None,
        )

    def _run_scheduling_pass() -> None:
        """Run ready components until nothing is PENDING-and-ready.

        Called once per contract pass: a contract breaker reset to
        PENDING after a failed contract phase re-enters scheduling via
        the outer loop in run_factory (R0.3) - previously the reset
        happened after the only scheduling loop had exited, so the
        promised retry never ran.
        """
        if max_parallel <= 1:
            while True:
                ready = manifest.get_ready_components()
                if not ready:
                    break

                comp = ready[0]
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
                try:
                    comp_result = _run_component(*args)
                except Exception as exc:
                    comp_result = ComponentResult(
                        component_id=comp.id, success=False, error=str(exc),
                    )

                _handle_result(comp.id, comp_result)
            return

        # Manual executor lifecycle: on a backstop breach we must NOT wait
        # for the (possibly hung) worker at shutdown, which the
        # `with ProcessPoolExecutor(...)` form would do.
        executor = ProcessPoolExecutor(max_workers=max_parallel)
        running_futures: dict[Future[ComponentResult], str] = {}
        future_deadlines: dict[Future[ComponentResult], float] = {}
        try:
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
                    if backstop_seconds > 0:
                        future_deadlines[future] = (
                            time.monotonic() + backstop_seconds
                        )

                if not running_futures:
                    break

                # Wait for the next completion, bounded by the nearest
                # backstop deadline. The worker enforces its own timeouts
                # (adapter kill + loop wall clock); this scheduler-side
                # deadline is the last line of defense when a worker hangs
                # outside those layers.
                wait_timeout = _next_backstop_wait(
                    running_futures, future_deadlines, time.monotonic(),
                )
                done, _pending = wait(
                    set(running_futures),
                    timeout=wait_timeout,
                    return_when=FIRST_COMPLETED,
                )

                if done:
                    # Preserve pre-R0.1 semantics: process one completion
                    # per pass so freed slots are refilled promptly.
                    future = next(iter(done))
                    comp_id = running_futures.pop(future)
                    future_deadlines.pop(future, None)
                    try:
                        comp_result = future.result()
                    except Exception as exc:
                        comp_result = ComponentResult(
                            component_id=comp_id, success=False, error=str(exc),
                        )
                    _handle_result(comp_id, comp_result)
                    continue

                # Nothing completed inside the window: fail every
                # component past its backstop deadline and keep going.
                now = time.monotonic()
                for future in _expired_futures(
                    running_futures, future_deadlines, now,
                ):
                    comp_id = running_futures.pop(future)
                    future_deadlines.pop(future, None)
                    leaked_component_ids.add(comp_id)
                    timed_out_comp = manifest.get_component(comp_id)
                    if timed_out_comp is not None:
                        timed_out_comp.status = ComponentStatus.FAILED.value
                        timed_out_comp.error = "component timeout"
                        skipped = manifest.cascade_skip(comp_id)
                        factory_result.failed.append(comp_id)
                        factory_result.skipped.extend(skipped)
                        progress_log.component_failed(
                            comp_id, "component timeout",
                        )
                    ui.err(
                        f"  Failed: {comp_id}: component timeout "
                        f"(scheduler backstop after {backstop_seconds:.0f}s)"
                    )
                    ui.warn(
                        f"  A worker process for '{comp_id}' may be leaked; "
                        f"its worktree is left in place"
                    )
                    manifest.save(manifest_path)
        finally:
            if leaked_component_ids:
                ui.warn(
                    "Shutting down worker pool without waiting: "
                    f"{len(leaked_component_ids)} worker(s) may still be running"
                )
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=True)

    def _cleanup_pass_worktrees() -> None:
        """Remove component worktrees left behind by a scheduling pass."""
        if not factory_config.use_worktrees:
            return
        ui.section("Factory: Cleanup")
        for comp_id in worktree_paths:
            if comp_id in leaked_component_ids:
                # A possibly-live worker still owns this worktree; removing
                # it under the worker risks corrupting the main repo's
                # worktree metadata.
                ui.warn(
                    f"  Keeping worktree for '{comp_id}' "
                    f"(leaked worker may still be running)"
                )
                continue
            _cleanup_worktree(comp_id, root_dir, run_id)
        # Drop the run's now-empty worktree dir; leaked workers' kept
        # worktrees leave it non-empty and it stays for the next run's
        # prune pass.
        try:
            os.rmdir(root_dir / ".ralph" / "worktrees" / run_id)
        except OSError:
            pass
        ui.ok("Worktrees cleaned up")

    def _record_contract_event(cr: ContractResult) -> None:
        """Append a contract_result event to the evolution journal.

        Written for pass AND fail (R0.3): the journal is the audit trail
        for every contract phase outcome, including intermediate failures
        that a breaker retry later resolves. Non-fatal on I/O errors,
        matching EvolutionJournal.record_run.
        """
        from ralph_py.evolution import EvolutionConfig

        # R2.1: honor [evolution] in ralph.toml + env, resolved against
        # the factory root rather than whatever the process CWD is.
        evo_config = EvolutionConfig.load(root_dir)
        if not evo_config.enabled:
            return
        entry = {
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
            evo_config.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(evo_config.journal_path, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            pass  # evolution recording is non-fatal

    # Per-component PRs are squash-merged into base as each component
    # completes, so at contract time tier re-merges would be content
    # no-ops and blame attribution would be meaningless: the contract
    # phase instead tests the integrated base branch (R0.3). single_pr
    # defers its one PR until after the contract phase, so it stays in
    # deferred-merge (tier merge + bisection) mode.
    components_merged = factory_config.create_prs and not factory_config.single_pr

    # R0.3: scheduling + contract testing form one outer loop so a
    # contract breaker reset to PENDING actually re-enters scheduling.
    # Termination: every reset consumes one of the breaker's bounded
    # retries, and any pass without a reset breaks out.
    while True:
        _run_scheduling_pass()
        _cleanup_pass_worktrees()

        # PHASE 3: Contract testing
        contract_config = factory_config.contract_config
        if (
            contract_config is None
            or contract_config.mode == ContractMode.SKIP.value
        ):
            break

        try:
            contract_results = run_contract_testing(
                manifest, root_dir, contract_config, ui,
                components_merged=components_merged,
            )
        except ContractCleanupError as exc:
            # A contract temp worktree survived removal. The user's
            # checkout is untouched, but .ralph/contract holds stale
            # state - fail the run loudly instead of continuing.
            ui.err(f"  Contract cleanup FAILED: {exc}")
            factory_result.contract_failures.append(
                f"contract cleanup failed: {exc}"
            )
            break

        for cr in contract_results:
            progress_log.contract_result(
                cr.tier, cr.passed, cr.breaker, cr.duration_seconds,
            )
            _record_contract_event(cr)

        failures = [cr for cr in contract_results if not cr.passed]
        if not failures:
            break

        # Reset retryable breakers to PENDING; the outer loop then
        # re-enters scheduling so the promised retry actually runs.
        any_breaker_reset = False
        for cr in failures:
            if not cr.breaker:
                continue
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
                any_breaker_reset = True
                ui.warn(
                    f"  Contract breaker '{cr.breaker}' sent back for retry"
                )

        if any_breaker_reset:
            continue

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
                    breaker.error = (
                        f"Contract test failed at tier {cr.tier} "
                        f"(retries exhausted)"
                    )
                if cr.breaker in factory_result.completed:
                    factory_result.completed.remove(cr.breaker)
                if cr.breaker not in factory_result.failed:
                    factory_result.failed.append(cr.breaker)
                progress_log.component_failed(
                    cr.breaker,
                    f"Contract test failed at tier {cr.tier} "
                    f"(retries exhausted)",
                )
                factory_result.contract_failures.append(
                    f"tier {cr.tier}: breaker '{cr.breaker}' "
                    f"(retries exhausted): {summary_line}"
                )
            else:
                factory_result.contract_failures.append(
                    f"tier {cr.tier}: contract tests failed, no blame "
                    f"attributed (components: "
                    f"{', '.join(cr.components_tested)}): {summary_line}"
                )
            ui.err(
                f"  Contract failure recorded for tier {cr.tier}; "
                f"run will exit nonzero"
            )
        manifest.save(manifest_path)
        break

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

    # R0.2: collect components parked awaiting merge confirmation. Built
    # from the manifest (not accumulated during the run) so it reflects
    # the final state after any crash-recovery re-poll.
    factory_result.merge_pending = [
        c.id for c in manifest.components
        if c.status == ComponentStatus.MERGE_PENDING.value
    ]

    ui.section("Factory: Summary")
    ui.kv("Completed", str(len(factory_result.completed)))
    ui.kv("Failed", str(len(factory_result.failed)))
    ui.kv("Skipped", str(len(factory_result.skipped)))
    if factory_result.contract_failures:
        ui.kv("Contract failures", str(len(factory_result.contract_failures)))
        for line in factory_result.contract_failures:
            ui.err(f"  {line}")
    if factory_result.merge_pending:
        ui.kv("Merge pending", str(len(factory_result.merge_pending)))
    ui.kv("Duration", f"{factory_duration:.0f}s")
    if factory_result.pr_urls:
        ui.kv("PRs created", str(len(factory_result.pr_urls)))
        for url in factory_result.pr_urls:
            ui.info(f"  {url}")
    if factory_result.merge_pending:
        ui.warn(
            "Some PR merges are unconfirmed; re-run the factory to "
            "re-poll them: " + ", ".join(factory_result.merge_pending)
        )

    if factory_result.failed or factory_result.contract_failures:
        factory_result.exit_code = 1
    elif factory_result.merge_pending:
        # Incomplete, not failed: unconfirmed merges blocked their
        # dependents. Nonzero so automation notices; a re-run re-polls.
        factory_result.exit_code = 1
    elif factory_result.skipped and not factory_result.completed:
        factory_result.exit_code = 1

    # Record run to evolution journal
    try:
        from ralph_py.evolution import EvolutionConfig, EvolutionJournal

        evo_config = EvolutionConfig.load(root_dir)
        if evo_config.enabled:
            journal = EvolutionJournal(evo_config)
            journal.record_run(run_id, manifest, factory_result)
    except Exception:
        pass  # evolution recording is non-fatal

    return factory_result
