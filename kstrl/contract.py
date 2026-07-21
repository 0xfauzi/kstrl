"""Phase 3: Cross-component contract testing in detached temp worktrees.

All tier merging happens in a throwaway worktree created with
``git worktree add --detach`` under ``.kstrl/contract/`` - never in the
user's checkout (R0.3 / CRIT-6). The recovery path on any failure is
``git merge --abort`` in the temp worktree followed by
``git worktree remove --force``; if the worktree survives removal a
:class:`ContractCleanupError` is raised so the run fails loudly instead
of silently leaving stale state behind.

Blame attribution (bisection) honesty:

- Merge-order bisection only runs in deferred-merge mode (PRs not yet
  merged to base). When components were already squash-merged to base
  (``create_prs`` per-component mode), re-merging their branches is a
  content no-op and bisection would blame the first component
  unconditionally; in that mode a single integrated check of the base
  branch runs instead, reporting pass/fail with the failing test output
  and NO breaker attribution.
- Known limitation of merge-order bisection: a failure caused by the
  interaction of two components attributes to whichever component merges
  later in topological order - the earlier component is never blamed
  even if it contributed the incompatibility. Within a tier the merge
  order follows the manifest's component order.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import git
from kstrl.manifest import Manifest
from kstrl.verify import run_scrubbed

if TYPE_CHECKING:
    from kstrl.ui.base import UI


class ContractMode(StrEnum):
    TIER = "tier"
    FINAL = "final"
    SKIP = "skip"


class ContractCleanupError(RuntimeError):
    """A contract temp worktree could not be removed.

    Raised so the factory fails loudly: a surviving temp worktree means
    ``.kstrl/contract/`` holds stale state and git's worktree metadata
    still references it, which would make every later contract pass
    (and possibly component worktree setup) fail in confusing ways.
    """


@dataclass
class ContractResult:
    """Result of a contract test for one tier or final merge."""

    passed: bool
    tier: int
    components_tested: list[str]
    breaker: str | None = None
    test_output: str = ""
    duration_seconds: float = 0.0


@dataclass
class ContractConfig:
    """Configuration for contract testing."""

    mode: str = ContractMode.TIER.value
    test_command: str = "uv run pytest"
    timeout: float = 600.0

    def __post_init__(self) -> None:
        # B8: reject typo'd modes loudly instead of letting them silently
        # drop through to default branches downstream.
        if self.mode not in {m.value for m in ContractMode}:
            raise ValueError(
                f"Invalid ContractConfig.mode {self.mode!r}; "
                f"must be one of {[m.value for m in ContractMode]}"
            )

    @classmethod
    def from_env(cls) -> ContractConfig:
        """Load contract config from environment variables."""
        return cls(
            mode=os.environ.get("KSTRL_CONTRACT_MODE", ContractMode.TIER.value),
            test_command=os.environ.get("KSTRL_CONTRACT_TEST_CMD", "uv run pytest"),
            timeout=float(os.environ.get("KSTRL_TIMEOUT_CONTRACT", "600")),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> ContractConfig:
        """Load contract config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "contract")
        if "mode" in section:
            config.mode = str(section["mode"])
        if "test_command" in section:
            config.test_command = str(section["test_command"])
        if "timeout" in section:
            config.timeout = float(section["timeout"])
        if "KSTRL_CONTRACT_MODE" in os.environ:
            config.mode = os.environ["KSTRL_CONTRACT_MODE"]
        if "KSTRL_CONTRACT_TEST_CMD" in os.environ:
            config.test_command = os.environ["KSTRL_CONTRACT_TEST_CMD"]
        if "KSTRL_TIMEOUT_CONTRACT" in os.environ:
            config.timeout = float(os.environ["KSTRL_TIMEOUT_CONTRACT"])
        # Re-validate after assignment (env / toml may have introduced typos)
        config.__post_init__()
        return config


def compute_tiers(manifest: Manifest) -> list[list[str]]:
    """Compute DAG tier levels.

    Tier 0: components with no dependencies.
    Tier N: components whose dependencies are all in tiers < N.

    Returns list of tiers, each a list of component IDs.
    """
    return manifest.compute_tiers()


def _create_temp_worktree(
    base: str,
    root_dir: Path,
    label: str,
    timeout: float = 60.0,
) -> tuple[Path | None, str]:
    """Create a detached throwaway worktree at ``base``.

    Returns ``(path, "")`` on success or ``(None, error)`` on failure.
    Detached HEAD means merges move only the temp worktree's HEAD; no
    branch is created and the user's checkout is never touched.
    """
    contract_base = root_dir / ".kstrl" / "contract"
    contract_base.mkdir(parents=True, exist_ok=True)
    worktree_path = contract_base / f"{label}-{secrets.token_hex(4)}"
    try:
        result = run_scrubbed(
            ["git", "worktree", "add", "--detach", str(worktree_path), base],
            cwd=root_dir,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"git worktree add timed out after {timeout}s"
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip()
    return worktree_path, ""


def _abort_merge(worktree_path: Path, timeout: float = 30.0) -> None:
    """Abort any in-flight merge in the temp worktree.

    Safe to call unconditionally: with no merge in progress git exits
    nonzero and that is fine - the goal is only that no conflicted
    index survives into worktree removal.
    """
    try:
        run_scrubbed(
            ["git", "merge", "--abort"],
            cwd=worktree_path,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        pass  # removal below is forced; a hung abort must not block it


def _remove_temp_worktree(
    worktree_path: Path,
    root_dir: Path,
    timeout: float = 60.0,
) -> None:
    """Remove a contract temp worktree, asserting the removal succeeded.

    Raises :class:`ContractCleanupError` when the worktree directory
    survives the forced removal - the one case where silent continuation
    would leave the repo's worktree metadata pointing at stale state.
    """
    try:
        result = run_scrubbed(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=root_dir,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContractCleanupError(
            f"git worktree remove timed out after {timeout}s for "
            f"{worktree_path}; remove it manually with "
            f"'git worktree remove --force {worktree_path}'"
        ) from exc
    if worktree_path.exists():
        raise ContractCleanupError(
            f"Contract temp worktree {worktree_path} survived removal "
            f"(git: {result.stderr.strip() or result.stdout.strip()}); "
            f"remove it manually with "
            f"'git worktree remove --force {worktree_path}'"
        )
    if result.returncode != 0:
        # Directory is gone but git may still track it; prune metadata.
        run_scrubbed(
            ["git", "worktree", "prune"],
            cwd=root_dir,
            timeout=timeout,
        )


def _run_tests(
    cwd: Path, test_command: str, timeout: float,
) -> tuple[bool, str]:
    """Run test suite and return (passed, output)."""
    try:
        result = run_scrubbed(test_command, cwd=cwd, timeout=timeout)
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Test suite timed out after {timeout}s"


def bisect_breaker(
    base_branch: str,
    prior_branches: list[str],
    tier_branches: list[tuple[str, str]],
    root_dir: Path,
    test_command: str,
    timeout: float = 600.0,
) -> str | None:
    """Linear bisection to identify which component broke integration.

    Deferred-merge mode only: merges tier branches one at a time in
    topological order inside a detached temp worktree, testing after
    each. See the module docstring for the two-component-interaction
    attribution limitation.

    Args:
        base_branch: Base branch to merge from
        prior_branches: Already-tested branches from prior tiers
        tier_branches: List of (component_id, branch_name) for current tier
        root_dir: Repository root
        test_command: Command to run tests
        timeout: Timeout per test run

    Returns:
        Component ID of the breaker, or None if unclear.
    """
    worktree_path, _error = _create_temp_worktree(
        base_branch, root_dir, "bisect",
    )
    if worktree_path is None:
        return None

    try:
        # Merge prior tier branches (should be clean)
        for branch in prior_branches:
            if not git.merge_branch(branch, worktree_path):
                return None

        # Merge tier branches one at a time, test after each
        for comp_id, branch in tier_branches:
            if not git.merge_branch(branch, worktree_path):
                return comp_id

            passed, _ = _run_tests(worktree_path, test_command, timeout)
            if not passed:
                return comp_id

        return None
    finally:
        _abort_merge(worktree_path)
        _remove_temp_worktree(worktree_path, root_dir)


def run_tier_check(
    manifest: Manifest,
    tier_component_ids: list[str],
    prior_branches: list[str],
    root_dir: Path,
    config: ContractConfig,
    ui: UI,
    tier_index: int = 0,
) -> ContractResult:
    """Run contract test for one DAG tier (deferred-merge mode).

    Merges all prior + current tier branches into a detached temp
    worktree, runs tests there. On failure, bisects to find the breaker.
    The user's checkout is never touched; any merge conflict is aborted
    in the temp worktree before it is removed.
    """
    start = time.monotonic()

    tier_branches: list[tuple[str, str]] = []
    for comp_id in tier_component_ids:
        comp = manifest.get_component(comp_id)
        if comp is None:
            continue
        tier_branches.append((comp_id, comp.branch_name))

    ui.info(
        f"  Tier {tier_index}: testing {len(tier_branches)} components "
        f"({', '.join(c for c, _ in tier_branches)})"
    )

    worktree_path, error = _create_temp_worktree(
        manifest.base_branch, root_dir, f"tier{tier_index}",
    )
    if worktree_path is None:
        return ContractResult(
            passed=False,
            tier=tier_index,
            components_tested=[c for c, _ in tier_branches],
            test_output=f"Failed to create contract worktree: {error}",
            duration_seconds=time.monotonic() - start,
        )

    output = ""
    try:
        # Merge prior tiers
        for branch in prior_branches:
            if not git.merge_branch(branch, worktree_path):
                return ContractResult(
                    passed=False,
                    tier=tier_index,
                    components_tested=[c for c, _ in tier_branches],
                    test_output=f"Merge conflict with prior branch: {branch}",
                    duration_seconds=time.monotonic() - start,
                )

        # Merge current tier
        for comp_id, branch in tier_branches:
            if not git.merge_branch(branch, worktree_path):
                return ContractResult(
                    passed=False,
                    tier=tier_index,
                    components_tested=[c for c, _ in tier_branches],
                    breaker=comp_id,
                    test_output=f"Merge conflict with {comp_id} ({branch})",
                    duration_seconds=time.monotonic() - start,
                )

        # Run tests
        passed, output = _run_tests(
            worktree_path, config.test_command, config.timeout,
        )

        if passed:
            ui.ok(f"  Tier {tier_index}: contract tests passed")
            return ContractResult(
                passed=True,
                tier=tier_index,
                components_tested=[c for c, _ in tier_branches],
                test_output=output[:2000],
                duration_seconds=time.monotonic() - start,
            )

        ui.warn(f"  Tier {tier_index}: contract tests FAILED, bisecting...")

    finally:
        # Recovery path: abort any in-flight merge, then remove the temp
        # worktree. _remove_temp_worktree raises ContractCleanupError if
        # the worktree survives - fail loudly, never leave a conflicted
        # checkout behind.
        _abort_merge(worktree_path)
        _remove_temp_worktree(worktree_path, root_dir)

    # Bisect to find breaker (fresh temp worktree of its own)
    breaker = bisect_breaker(
        manifest.base_branch, prior_branches, tier_branches,
        root_dir, config.test_command, config.timeout,
    )

    if breaker:
        ui.err(f"  Tier {tier_index}: breaker identified: {breaker}")
    else:
        ui.warn(f"  Tier {tier_index}: could not identify single breaker")

    return ContractResult(
        passed=False,
        tier=tier_index,
        components_tested=[c for c, _ in tier_branches],
        breaker=breaker,
        test_output=output[:2000],
        duration_seconds=time.monotonic() - start,
    )


def run_integrated_base_check(
    manifest: Manifest,
    component_ids: list[str],
    root_dir: Path,
    config: ContractConfig,
    ui: UI,
) -> ContractResult:
    """Contract check for already-merged components (create_prs mode).

    Per-component PRs were squash-merged into the base branch as each
    component completed, so the integrated state IS the base branch:
    re-merging component branches would be content no-ops and bisection
    would blame the first component unconditionally. Instead, run the
    test suite once against the base branch in a detached temp worktree
    and report pass/fail with NO breaker attribution.
    """
    start = time.monotonic()
    ui.info(
        f"  Integrated check: testing '{manifest.base_branch}' with "
        f"{len(component_ids)} merged components "
        f"({', '.join(component_ids)})"
    )

    worktree_path, error = _create_temp_worktree(
        manifest.base_branch, root_dir, "integrated",
    )
    if worktree_path is None:
        return ContractResult(
            passed=False,
            tier=0,
            components_tested=list(component_ids),
            test_output=f"Failed to create contract worktree: {error}",
            duration_seconds=time.monotonic() - start,
        )

    try:
        passed, output = _run_tests(
            worktree_path, config.test_command, config.timeout,
        )
    finally:
        _remove_temp_worktree(worktree_path, root_dir)

    if passed:
        ui.ok("  Integrated check: contract tests passed")
    else:
        ui.err(
            "  Integrated check: tier failed (components already merged "
            "to base; no blame attribution)"
        )

    return ContractResult(
        passed=passed,
        tier=0,
        components_tested=list(component_ids),
        breaker=None,
        test_output=output[:2000],
        duration_seconds=time.monotonic() - start,
    )


def run_contract_testing(
    manifest: Manifest,
    root_dir: Path,
    config: ContractConfig,
    ui: UI,
    components_merged: bool = False,
) -> list[ContractResult]:
    """Run contract testing across DAG tiers.

    ``components_merged=True`` (create_prs per-component mode) runs a
    single integrated check of the base branch with no blame
    attribution - see :func:`run_integrated_base_check`.

    Otherwise (deferred-merge mode):
    In TIER mode: tests each tier incrementally.
    In FINAL mode: tests all completed components at once.
    In SKIP mode: returns empty list.
    """
    if config.mode == ContractMode.SKIP.value:
        return []

    ui.section("Contract Testing")

    completed_ids = {
        c.id for c in manifest.components if c.status == "completed"
    }

    if not completed_ids:
        ui.info("  No completed components, skipping contract tests")
        return []

    tiers = compute_tiers(manifest)

    if components_merged:
        ordered_ids = [
            comp_id for tier in tiers for comp_id in tier
            if comp_id in completed_ids
        ]
        return [
            run_integrated_base_check(
                manifest, ordered_ids, root_dir, config, ui,
            )
        ]

    results: list[ContractResult] = []

    if config.mode == ContractMode.FINAL.value:
        # Run once with all completed components
        all_branches: list[tuple[str, str]] = []
        for tier in tiers:
            for comp_id in tier:
                if comp_id in completed_ids:
                    comp = manifest.get_component(comp_id)
                    if comp:
                        all_branches.append((comp_id, comp.branch_name))

        result = run_tier_check(
            manifest,
            [c for c, _ in all_branches],
            [],
            root_dir,
            config,
            ui,
            tier_index=0,
        )
        results.append(result)
    else:
        # Tier-by-tier testing
        prior_branches: list[str] = []
        for tier_idx, tier in enumerate(tiers):
            tier_completed = [cid for cid in tier if cid in completed_ids]
            if not tier_completed:
                continue

            result = run_tier_check(
                manifest,
                tier_completed,
                prior_branches,
                root_dir,
                config,
                ui,
                tier_index=tier_idx,
            )
            results.append(result)

            # Accumulate branches for next tier
            for comp_id in tier_completed:
                comp = manifest.get_component(comp_id)
                if comp:
                    prior_branches.append(comp.branch_name)

    return results
