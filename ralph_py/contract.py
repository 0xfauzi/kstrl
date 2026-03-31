"""Phase 3: Cross-component contract testing via temp merge branches."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py import git
from ralph_py.manifest import Manifest

if TYPE_CHECKING:
    from ralph_py.ui.base import UI


class ContractMode(str, Enum):
    TIER = "tier"
    FINAL = "final"
    SKIP = "skip"


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

    @classmethod
    def from_env(cls) -> ContractConfig:
        """Load contract config from environment variables."""
        return cls(
            mode=os.environ.get("RALPH_CONTRACT_MODE", ContractMode.TIER.value),
            test_command=os.environ.get("RALPH_CONTRACT_TEST_CMD", "uv run pytest"),
            timeout=float(os.environ.get("RALPH_TIMEOUT_CONTRACT", "600")),
        )


def compute_tiers(manifest: Manifest) -> list[list[str]]:
    """Compute DAG tier levels.

    Tier 0: components with no dependencies.
    Tier N: components whose dependencies are all in tiers < N.

    Returns list of tiers, each a list of component IDs.
    """
    return manifest.compute_tiers()


def _create_temp_branch(
    branch_name: str,
    base: str,
    cwd: Path,
    timeout: float = 30.0,
) -> bool:
    """Create and checkout a temporary branch from base."""
    return git.create_branch_from(branch_name, base, cwd, timeout)


def _cleanup_temp_branch(
    branch_name: str,
    base_branch: str,
    cwd: Path,
    timeout: float = 30.0,
) -> None:
    """Return to base branch and delete the temporary branch."""
    git.checkout_existing(base_branch, cwd, timeout)
    git.delete_branch(branch_name, cwd, force=True, timeout=timeout)


def _run_tests(
    cwd: Path, test_command: str, timeout: float,
) -> tuple[bool, str]:
    """Run test suite and return (passed, output)."""
    try:
        result = subprocess.run(
            test_command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
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
    ts = int(time.time())
    bisect_branch = f"ralph/bisect-{ts}"

    if not _create_temp_branch(bisect_branch, base_branch, root_dir):
        return None

    try:
        # Merge prior tier branches (should be clean)
        for branch in prior_branches:
            if not git.merge_branch(branch, root_dir):
                return None

        # Merge tier branches one at a time, test after each
        for comp_id, branch in tier_branches:
            if not git.merge_branch(branch, root_dir):
                return comp_id

            passed, _ = _run_tests(root_dir, test_command, timeout)
            if not passed:
                return comp_id

        return None
    finally:
        _cleanup_temp_branch(bisect_branch, base_branch, root_dir)


def run_tier_check(
    manifest: Manifest,
    tier_component_ids: list[str],
    prior_branches: list[str],
    root_dir: Path,
    config: ContractConfig,
    ui: UI,
    tier_index: int = 0,
) -> ContractResult:
    """Run contract test for one DAG tier.

    Merges all prior + current tier branches, runs tests.
    On failure, bisects to find the breaker.
    """
    start = time.monotonic()
    ts = int(time.time())
    merge_branch = f"ralph/contract-{tier_index}-{ts}"

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

    # Create temp merge branch
    if not _create_temp_branch(merge_branch, manifest.base_branch, root_dir):
        return ContractResult(
            passed=False,
            tier=tier_index,
            components_tested=[c for c, _ in tier_branches],
            test_output="Failed to create merge branch",
            duration_seconds=time.monotonic() - start,
        )

    try:
        # Merge prior tiers
        for branch in prior_branches:
            if not git.merge_branch(branch, root_dir):
                return ContractResult(
                    passed=False,
                    tier=tier_index,
                    components_tested=[c for c, _ in tier_branches],
                    test_output=f"Merge conflict with prior branch: {branch}",
                    duration_seconds=time.monotonic() - start,
                )

        # Merge current tier
        for comp_id, branch in tier_branches:
            if not git.merge_branch(branch, root_dir):
                return ContractResult(
                    passed=False,
                    tier=tier_index,
                    components_tested=[c for c, _ in tier_branches],
                    breaker=comp_id,
                    test_output=f"Merge conflict with {comp_id} ({branch})",
                    duration_seconds=time.monotonic() - start,
                )

        # Run tests
        passed, output = _run_tests(root_dir, config.test_command, config.timeout)

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
        _cleanup_temp_branch(merge_branch, manifest.base_branch, root_dir)

    # Bisect to find breaker
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


def run_contract_testing(
    manifest: Manifest,
    root_dir: Path,
    config: ContractConfig,
    ui: UI,
) -> list[ContractResult]:
    """Run contract testing across DAG tiers.

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
