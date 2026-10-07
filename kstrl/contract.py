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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import git
from kstrl.config_numbers import check_numbers
from kstrl.manifest import Manifest
from kstrl.rung import Rung
from kstrl.stack import NO_STACK, Stack, stack_in_force
from kstrl.timeout import limit_seconds
from kstrl.verify import (
    ChildOutputDecodeError,
    check_stack_command,
    run_scrubbed,
)
from kstrl.worktree_setup import NO_SETUP, WorktreeSetup
from kstrl.worktree_sweep import sweep_worktree, warn_sweep

if TYPE_CHECKING:
    from kstrl.acceptance import Check, PinnedPlan
    from kstrl.replay import Stage
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
    #: #481: the commit the integrated check was given to test. "" for a
    #: tier or final check, which merges branches onto the base instead.
    tested_sha: str = ""
    #: #700 slice 8: failed held-out checks; the breaker gets no retry (decision 3).
    held_out: tuple[str, ...] = ()


@dataclass
class ContractConfig:
    """Configuration for contract testing."""

    mode: str = ContractMode.TIER.value
    timeout: float = 0.0
    #: #696: the project's [stack], read by ``load``. Phase 3 runs every one
    #: of its checks (decision 10); with none it runs nothing and fails
    #: (:data:`NO_STACK_PHASE_3`). Provenance: no [contract] key of its own.
    project_stack: Stack | None = field(default=None, metadata={"provenance": True})
    #: #700 slice 2: the TEST-zone rung of a ``ks factory`` run under a
    #: [stack]; Phase 3 runs every check inside it. Set by the factory, never
    #: from kstrl.toml. Provenance: no [contract] key.
    rung: Rung | None = field(default=None, metadata={"provenance": True})

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
            timeout=float(os.environ.get("KSTRL_TIMEOUT_CONTRACT", "0")),
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
        if "timeout" in section:
            config.timeout = float(section["timeout"])
        if "KSTRL_CONTRACT_MODE" in os.environ:
            config.mode = os.environ["KSTRL_CONTRACT_MODE"]
        if "KSTRL_TIMEOUT_CONTRACT" in os.environ:
            config.timeout = float(os.environ["KSTRL_TIMEOUT_CONTRACT"])
        # Re-validate after assignment (env / toml may have introduced typos)
        config.__post_init__()
        config.project_stack = stack_in_force(root_dir)
        return check_numbers(config)


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

    The base is resolved through :func:`git.resolve_base_ref`, here
    rather than at the three call sites, so every contract checkout
    (bisection, the tier check, the integrated check) asks one question.
    A bare base name cuts from the operator's local branch, which a
    squash merge on GitHub leaves behind; #435 recorded a tier-1 contract
    check failing in 0.75s with ``Installed 28 packages in 23ms`` as the
    whole of its evidence, because the tree it checked out had no tests
    in it at all.
    """
    contract_base = root_dir / ".kstrl" / "contract"
    contract_base.mkdir(parents=True, exist_ok=True)
    worktree_path = contract_base / f"{label}-{secrets.token_hex(4)}"
    base_ref = git.resolve_base_ref(base, root_dir, timeout)
    try:
        result = run_scrubbed(
            ["git", "worktree", "add", "--detach", str(worktree_path), base_ref],
            cwd=root_dir,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"git worktree add timed out after {timeout}s"
    except ChildOutputDecodeError as exc:
        return None, f"git worktree add output could not be decoded: {exc}"
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
    except (subprocess.TimeoutExpired, ChildOutputDecodeError):
        # removal below is forced; neither a hung abort nor one
        # whose output cannot be decoded may block it
        pass


def _remove_temp_worktree(
    worktree_path: Path,
    root_dir: Path,
    ui: UI,
    phase: str,
    timeout: float = 60.0,
) -> None:
    """Remove a contract temp worktree, asserting the removal succeeded.

    First kills every process still running in it and warns on ``ui``
    naming each one under ``phase`` (#528): the project's test command and
    the integration reviewer's shell both run here, and a process either of
    them starts in a session of its own outlives the call that started it.

    Raises :class:`ContractCleanupError` when the worktree directory
    survives the forced removal - the one case where silent continuation
    would leave the repo's worktree metadata pointing at stale state.
    """
    warn_sweep(sweep_worktree(worktree_path), ui, phase)
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
    except ChildOutputDecodeError as exc:
        raise ContractCleanupError(
            f"git worktree remove output could not be decoded for "
            f"{worktree_path} ({exc}); check it and remove it manually with "
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
        try:
            run_scrubbed(
                ["git", "worktree", "prune"],
                cwd=root_dir,
                timeout=timeout,
            )
        except ChildOutputDecodeError:
            # This spawn's result was never read: the directory is already
            # gone and the prune is best-effort metadata tidying. A decode
            # failure carries nothing the caller acts on, for the same reason
            # _abort_merge's timeout does not block removal.
            pass


#: Phase 3's evidence when no ``[stack]`` names a check to run (#696 slice
#: 4). Every entry point refuses first; this is what a caller that built a
#: ContractConfig with no stack gets: a failure, never a pass on nothing.
NO_STACK_PHASE_3 = "Phase 3 ran nothing: " + NO_STACK


def _run_checks(
    cwd: Path,
    config: ContractConfig,
    root_dir: Path,
    ui: UI,
    plan: PinnedPlan | None,
    merged: Sequence[str],
) -> tuple[bool, str, tuple[str, ...]]:
    """Phase 3's verdict on ``cwd``, its evidence and the failed held-out checks.

    Every ``[stack]`` check runs, judged by its exit status (#696), then the
    plan's checks of the ``merged`` components (#700 slice 8). With no stack
    nothing runs and the verdict is a failure.
    """
    stack = config.project_stack
    if stack is None:
        return False, NO_STACK_PHASE_3, ()
    timeout = limit_seconds(config.timeout)
    rows = [
        check_stack_command(cwd, stack, name, command, timeout, config.rung)
        for name, command in stack.checks
    ]
    failed = [row for row in rows if not row.passed]
    evidence = [f"{row.name}: {row.message}\n{row.output or ''}".strip() for row in failed]
    accepted, lines, held_out = _replay_acceptance(cwd, config, root_dir, ui, plan, merged)
    evidence += lines
    return not failed and accepted, "\n".join(evidence), held_out


def _replay_acceptance(
    cwd: Path,
    config: ContractConfig,
    root_dir: Path,
    ui: UI,
    plan: PinnedPlan | None,
    merged: Sequence[str],
) -> tuple[bool, list[str], tuple[str, ...]]:
    """Replay the ``merged`` components' checks on the commit ``cwd`` holds,
    each ``HEAD_RUNS`` times and judged as a head is. A designed plan is
    record only (owner decision 10): its failures are printed, never failed."""
    from kstrl.acceptance import HEAD_RUNS, _no_log, _probe
    from kstrl.replay import replay_stack

    stack = config.project_stack
    wanted = _wanted(plan, merged)
    if plan is None or stack is None or not wanted:
        return True, [], ()
    sha = git.get_head_sha(cwd) or ""
    if not sha:
        return False, [f"acceptance: the commit of {cwd} cannot be read, so no check ran"], ()
    limit = limit_seconds(config.timeout)
    runs: dict[tuple[str, str], list[Stage]] = {}
    record = replay_stack(
        root_dir,
        stack,
        setup_limit=limit,
        check_limit=limit,
        ui=ui,
        at=sha,
        probe=_probe(plan, wanted, stack, limit, runs, HEAD_RUNS, _no_log),
    )
    failing, held_out = _failing_lines(wanted, runs)
    if not failing:
        return True, [], ()
    stopped = record.error or (f"{record.failed}: {record.detail}" if record.failed else "")
    lines = [f"acceptance replay at {sha[:12]} stopped: {stopped}"] if stopped else []
    lines += failing
    if plan.designed:
        for line in lines:
            ui.warn(f"  record only (designed checks): {line}")
        return True, [], ()
    return False, lines, held_out


def _wanted(plan: PinnedPlan | None, merged: Sequence[str]) -> list[tuple[str, Check]]:
    """The checks of the components in ``merged``; no other component's."""
    if plan is None:
        return []
    return [
        (comp, check)
        for comp in merged
        if comp in plan.components
        for check in plan.components[comp].checks
    ]


def _failing_lines(
    wanted: Sequence[tuple[str, Check]], runs: Mapping[tuple[str, str], Sequence[Stage]]
) -> tuple[list[str], tuple[str, ...]]:
    """One line per check that did not pass all its runs, followed by what
    its last run printed, and the ids of the held-out checks that failed. A
    held-out check is named by its id alone: this evidence is printed."""
    from kstrl.acceptance import FAIL, PASS, head_verdict

    lines: list[str] = []
    held_out: list[str] = []
    for comp, check in wanted:
        stages = runs.get((comp, check.id), [])
        exits = [stage.exit for stage in stages]
        verdict = head_verdict(exits)
        if verdict == PASS:
            continue
        lines.append(f"acceptance:{check.id} ({comp}): {verdict}, exits {exits}")
        if check.held_out and verdict == FAIL:
            held_out.append(check.id)
        if not check.held_out and stages:
            lines.extend(stages[-1].tail)
    return lines, tuple(held_out)


def bisect_breaker(
    base_branch: str,
    prior_branches: list[str],
    tier_branches: list[tuple[str, str]],
    root_dir: Path,
    ui: UI,
    config: ContractConfig,
    setup: WorktreeSetup = NO_SETUP,
    merged_before: Sequence[str] = (),
    plan: PinnedPlan | None = None,
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
        ui: Where the removal of the bisection worktree names what it killed
        config: The Phase 3 config, whose ``[stack]`` checks run after each merge
        setup: Worktree setup run after each merge, before its test run
            (#624); a failed setup ends the bisection with no breaker
        merged_before: The ids of the components ``prior_branches`` holds
        plan: The acceptance plan; the merged components' checks run too

    Returns:
        Component ID of the breaker, or None if unclear.
    """
    worktree_path, _error = _create_temp_worktree(
        base_branch,
        root_dir,
        "bisect",
    )
    if worktree_path is None:
        return None

    try:
        # Merge prior tier branches (should be clean)
        for branch in prior_branches:
            if not git.merge_branch(branch, worktree_path):
                return None

        # Merge tier branches one at a time, test after each
        merged = list(merged_before)
        for comp_id, branch in tier_branches:
            if not git.merge_branch(branch, worktree_path):
                return comp_id
            merged.append(comp_id)

            if setup.prepare(worktree_path):
                return None
            passed, _, _ = _run_checks(worktree_path, config, root_dir, ui, plan, merged)
            if not passed:
                return comp_id

        return None
    finally:
        _abort_merge(worktree_path)
        _remove_temp_worktree(worktree_path, root_dir, ui, "contract")


def _ids_of(manifest: Manifest, branches: Sequence[str]) -> list[str]:
    """The ids of the components whose branches are among ``branches``."""
    return [comp.id for comp in manifest.components if comp.branch_name in branches]


def run_tier_check(
    manifest: Manifest,
    tier_component_ids: list[str],
    prior_branches: list[str],
    root_dir: Path,
    config: ContractConfig,
    ui: UI,
    tier_index: int = 0,
    setup: WorktreeSetup = NO_SETUP,
    plan: PinnedPlan | None = None,
) -> ContractResult:
    """Run contract test for one DAG tier (deferred-merge mode).

    Merges all prior + current tier branches into a detached temp
    worktree, runs tests there. On failure, bisects to find the breaker.
    The user's checkout is never touched; any merge conflict is aborted
    in the temp worktree before it is removed.

    ``setup`` runs after the merges, so it installs from the merged
    lockfile (#624). When it fails no test runs and nothing is bisected:
    the tier fails with the setup's output and no breaker.
    """
    start = time.monotonic()

    tier_branches: list[tuple[str, str]] = []
    for comp_id in tier_component_ids:
        comp = manifest.get_component(comp_id)
        if comp is None:
            continue
        tier_branches.append((comp_id, comp.branch_name))
    merged_before = _ids_of(manifest, prior_branches)
    merged = [*merged_before, *dict(tier_branches)]

    ui.info(
        f"  Tier {tier_index}: testing {len(tier_branches)} components "
        f"({', '.join(c for c, _ in tier_branches)})"
    )

    worktree_path, error = _create_temp_worktree(
        manifest.base_branch,
        root_dir,
        f"tier{tier_index}",
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
    held_out: tuple[str, ...] = ()
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

        setup_error = setup.prepare(worktree_path)
        if setup_error:
            ui.err(f"  Tier {tier_index}: worktree setup failed, so no contract test ran")
            return ContractResult(
                passed=False,
                tier=tier_index,
                components_tested=[c for c, _ in tier_branches],
                test_output=setup_error[:2000],
                duration_seconds=time.monotonic() - start,
            )

        # Run tests
        passed, output, held_out = _run_checks(worktree_path, config, root_dir, ui, plan, merged)

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
        _remove_temp_worktree(worktree_path, root_dir, ui, "contract")

    # Bisect to find breaker (fresh temp worktree of its own)
    breaker = bisect_breaker(
        manifest.base_branch,
        prior_branches,
        tier_branches,
        root_dir,
        ui,
        config,
        setup,
        merged_before,
        plan,
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
        held_out=held_out,
    )


def run_integrated_base_check(
    manifest: Manifest,
    component_ids: list[str],
    root_dir: Path,
    config: ContractConfig,
    ui: UI,
    base_sha: str,
    setup: WorktreeSetup = NO_SETUP,
    plan: PinnedPlan | None = None,
) -> ContractResult:
    """Contract check for already-merged components (create_prs mode).

    Per-component PRs were squash-merged into the base branch as each
    component completed, so the integrated state IS the base branch:
    re-merging component branches would be content no-ops and bisection
    would blame the first component unconditionally. Instead, run the
    test suite once against the base in a detached temp worktree and
    report pass/fail with NO breaker attribution.

    #481: the worktree is cut at ``base_sha``, the commit the caller
    resolved the base branch to for this round, never at the branch
    name, which moves every time a PR merges. The result carries it as
    ``tested_sha`` so a later check can judge the same tree. An empty
    ``base_sha`` means the caller could not resolve the base, and the
    check fails having tested nothing.
    """
    start = time.monotonic()
    if not base_sha:
        return ContractResult(
            passed=False,
            tier=0,
            components_tested=list(component_ids),
            test_output=(
                f"The base branch '{manifest.base_branch}' did not resolve to a "
                "commit for this round, so nothing was tested"
            ),
            duration_seconds=time.monotonic() - start,
        )
    ui.info(
        f"  Integrated check: testing '{manifest.base_branch}' at {base_sha[:12]} with "
        f"{len(component_ids)} merged components "
        f"({', '.join(component_ids)})"
    )

    worktree_path, error = _create_temp_worktree(
        base_sha,
        root_dir,
        "integrated",
    )
    if worktree_path is None:
        return ContractResult(
            passed=False,
            tier=0,
            components_tested=list(component_ids),
            test_output=f"Failed to create contract worktree: {error}",
            duration_seconds=time.monotonic() - start,
            tested_sha=base_sha,
        )

    try:
        # #624: a failed setup is the result, and no test runs.
        output = setup.prepare(worktree_path)
        passed = False
        if not output:
            passed, output, _ = _run_checks(
                worktree_path, config, root_dir, ui, plan, component_ids
            )
    finally:
        _remove_temp_worktree(worktree_path, root_dir, ui, "contract")

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
        tested_sha=base_sha,
    )


def run_contract_testing(
    manifest: Manifest,
    root_dir: Path,
    config: ContractConfig,
    ui: UI,
    components_merged: bool = False,
    base_sha: str = "",
    setup: WorktreeSetup = NO_SETUP,
    plan: PinnedPlan | None = None,
) -> list[ContractResult]:
    """Run contract testing across DAG tiers.

    ``components_merged=True`` (create_prs per-component mode) runs a
    single integrated check of the base branch at ``base_sha``, the
    commit the caller resolved for this round, with no blame
    attribution - see :func:`run_integrated_base_check`. The tier and
    final checks below do not read ``base_sha``.

    ``setup`` is the worktree setup every contract worktree gets after
    its merges and before its tests (#624). The checks ``plan`` holds for
    the merged components are replayed on the merged commit (#700 slice 8).

    Otherwise (deferred-merge mode):
    In TIER mode: tests each tier incrementally.
    In FINAL mode: tests all completed components at once.
    In SKIP mode: returns empty list.
    """
    if config.mode == ContractMode.SKIP.value:
        return []

    ui.section("Contract Testing")

    completed_ids = {c.id for c in manifest.components if c.status == "completed"}

    if not completed_ids:
        ui.info("  No completed components, skipping contract tests")
        return []

    tiers = compute_tiers(manifest)

    if components_merged:
        ordered_ids = [comp_id for tier in tiers for comp_id in tier if comp_id in completed_ids]
        return [
            run_integrated_base_check(
                manifest,
                ordered_ids,
                root_dir,
                config,
                ui,
                base_sha,
                setup,
                plan,
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
            setup=setup,
            plan=plan,
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
                setup=setup,
                plan=plan,
            )
            results.append(result)

            # Accumulate branches for next tier
            for comp_id in tier_completed:
                comp = manifest.get_component(comp_id)
                if comp:
                    prior_branches.append(comp.branch_name)

    return results
