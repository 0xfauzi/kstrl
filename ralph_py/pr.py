"""PR creation, merge, and management via gh CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ralph_py.findings import render_findings_markdown
from ralph_py.git import fetch_base_branch

if TYPE_CHECKING:
    from ralph_py.manifest import Component, Manifest
    from ralph_py.ui.base import UI

# Explicit budgets for every subprocess in this module (R0.2): a hung
# gh or git call previously blocked the factory scheduler forever.
GH_TIMEOUT = 60.0
GH_POLL_TIMEOUT = 30.0
PUSH_TIMEOUT = 300.0

MergeState = Literal["merged", "closed", "pending"]


@dataclass(frozen=True)
class PrOutcome:
    """Typed result of the per-component PR lifecycle (R0.2).

    Replaces the lossy ``tuple | None`` return: the factory gates
    COMPLETED on ``merged`` and maps ``merge_pending`` to the
    MERGE_PENDING manifest status, so no failure shape can fall through
    to "completed" (CRIT-2).
    """

    pushed: bool = False
    pr_number: int | None = None
    pr_url: str = ""
    merged: bool = False
    # True when a merge was initiated but not confirmed within the
    # timeout: re-pollable, unlike a push/create/merge failure.
    merge_pending: bool = False
    error: str | None = None


def pr_number_from_url(pr_url: str) -> int:
    """Extract the PR number from a GitHub PR URL, 0 if unparseable."""
    if "/pull/" in pr_url:
        try:
            return int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            pass
    return 0


def is_gh_available() -> bool:
    """Check if gh CLI is available and authenticated."""
    if shutil.which("gh") is None:
        return False
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def push_branch(branch: str, cwd: Path) -> str | None:
    """Push a branch to the remote with tracking.

    Returns an error message, or None on success.
    """
    # "--" makes a crafted branch value an invalid refspec instead of a
    # git option (R0.6).
    try:
        result = subprocess.run(
            ["git", "push", "-u", "--", "origin", branch],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=PUSH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"git push of {branch} timed out after {PUSH_TIMEOUT}s"
    if result.returncode != 0:
        return result.stderr.strip() or f"git push of {branch} failed"
    return None


def merge_pr(pr_number: int, cwd: Path, method: str = "squash") -> str | None:
    """Merge a PR via gh CLI with auto-merge.

    Uses --auto so GitHub merges once status checks pass.
    Uses --delete-branch to clean up the feature branch.

    Returns an error message, or None when a merge was initiated
    (confirmation is wait_for_merge's job).

    Args:
        pr_number: PR number to merge.
        cwd: Working directory (must be in the git repo).
        method: Merge method - "squash", "merge", or "rebase".
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "merge", str(pr_number),
                f"--{method}", "--delete-branch", "--auto",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
        # If --auto fails (no required checks), try direct merge
        if result.returncode != 0:
            result = subprocess.run(
                [
                    "gh", "pr", "merge", str(pr_number),
                    f"--{method}", "--delete-branch",
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT,
            )
    except subprocess.TimeoutExpired:
        return f"gh pr merge #{pr_number} timed out after {GH_TIMEOUT}s"
    if result.returncode != 0:
        return (
            result.stderr.strip()
            or f"gh pr merge #{pr_number} failed"
        )
    return None


def _pr_state(pr_number: int, cwd: Path) -> str | None:
    """Fetch a PR's state via gh: "MERGED", "CLOSED", "OPEN", or None
    when the state could not be determined (gh error, timeout, bad
    JSON)."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GH_POLL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    state = data.get("state", "") if isinstance(data, dict) else ""
    return str(state) or None


def wait_for_merge(
    pr_number: int,
    cwd: Path,
    timeout: float = 300,
    poll_interval: float = 10,
) -> MergeState:
    """Poll until a PR is merged, closed, or the timeout elapses.

    Returns "merged", "closed" (closed without merge), or "pending"
    (timeout: state unknown, re-pollable). Each poll is individually
    bounded so a hung gh cannot outlive the deadline by much.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _pr_state(pr_number, cwd)
        if state == "MERGED":
            return "merged"
        if state == "CLOSED":
            return "closed"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    return "pending"


def _merge_and_wait(
    pr_number: int,
    pr_url: str,
    base_branch: str,
    cwd: Path,
    ui: UI,
    merge_method: str,
    merge_timeout: float,
) -> PrOutcome:
    """Shared merge-initiate + confirm + fetch tail of the PR lifecycle."""
    merge_error = merge_pr(pr_number, cwd, merge_method)
    if merge_error:
        ui.warn(f"  Failed to merge #{pr_number}: {merge_error}")
        return PrOutcome(
            pushed=True, pr_number=pr_number, pr_url=pr_url,
            error=f"merge failed for PR #{pr_number}: {merge_error}",
        )

    state = wait_for_merge(pr_number, cwd, timeout=merge_timeout)
    if state == "merged":
        ui.ok(f"  PR #{pr_number} merged")
        # Fetch (never pull, H-1) so origin/<base> includes the merged
        # code for downstream worktrees; the operator's checkout is
        # untouched. A failed fetch does not un-merge the PR, but it
        # leaves origin/<base> stale, so it is recorded loudly.
        fetch_error = fetch_base_branch(base_branch, cwd)
        if fetch_error:
            ui.warn(
                f"  Fetch of origin/{base_branch} after merge failed: "
                f"{fetch_error}; downstream worktrees retry the fetch "
                f"at setup"
            )
        return PrOutcome(
            pushed=True, pr_number=pr_number, pr_url=pr_url,
            merged=True, error=fetch_error,
        )

    if state == "closed":
        ui.warn(f"  PR #{pr_number} was closed without merging")
        return PrOutcome(
            pushed=True, pr_number=pr_number, pr_url=pr_url,
            error=f"PR #{pr_number} closed without merge",
        )

    ui.warn(
        f"  PR #{pr_number} not merged within {merge_timeout}s - "
        f"marked merge-pending; a factory re-run re-polls it"
    )
    return PrOutcome(
        pushed=True, pr_number=pr_number, pr_url=pr_url,
        merge_pending=True,
        error=f"PR #{pr_number} not merged within {merge_timeout}s",
    )


def push_create_and_merge_pr(
    component: Component,
    manifest: Manifest,
    cwd: Path,
    ui: UI,
    merge_method: str = "squash",
    merge_timeout: float = 300,
) -> PrOutcome:
    """Push branch, create PR, merge it, and fetch the base branch.

    Full per-component PR lifecycle:
    1. Push branch to origin
    2. Create PR via gh
    3. Merge PR (auto-merge if checks required, direct otherwise)
    4. Wait for merge to complete
    5. Fetch origin/<base> so downstream worktrees get the merged code
       (never ``git pull``: the operator's checkout is not ours to move)

    Returns a PrOutcome; the caller decides component status from it.
    """
    if component.pr_url:
        # Resume/retry path: a PR already exists. Its state must be
        # verified, not assumed - the pre-R0.2 code returned success
        # here even when the PR was never merged.
        pr_number = component.pr_number or pr_number_from_url(component.pr_url)
        if not pr_number:
            return PrOutcome(
                pushed=True, pr_url=component.pr_url,
                error=(
                    f"existing PR {component.pr_url} has no usable PR "
                    f"number; cannot verify merge state"
                ),
            )
        ui.info(f"  {component.id}: PR already exists ({component.pr_url})")
        state = _pr_state(pr_number, cwd)
        if state == "MERGED":
            return PrOutcome(
                pushed=True, pr_number=pr_number, pr_url=component.pr_url,
                merged=True,
            )
        if state == "CLOSED":
            return PrOutcome(
                pushed=True, pr_number=pr_number, pr_url=component.pr_url,
                error=f"PR #{pr_number} closed without merge",
            )
        return _merge_and_wait(
            pr_number, component.pr_url, manifest.base_branch,
            cwd, ui, merge_method, merge_timeout,
        )

    # Push
    ui.info(f"  Pushing {component.branch_name}...")
    push_error = push_branch(component.branch_name, cwd)
    if push_error:
        ui.warn(f"  Failed to push {component.branch_name}: {push_error}")
        return PrOutcome(
            error=f"push of {component.branch_name} failed: {push_error}",
        )

    # Create PR
    try:
        pr_number, pr_url = create_component_pr(component, manifest, cwd)
    except RuntimeError as exc:
        ui.warn(f"  {exc}")
        return PrOutcome(pushed=True, error=str(exc))

    component.pr_number = pr_number
    component.pr_url = pr_url
    ui.ok(f"  PR created: {pr_url}")

    return _merge_and_wait(
        pr_number, pr_url, manifest.base_branch,
        cwd, ui, merge_method, merge_timeout,
    )


def _generate_pr_body(
    component: Component,
    manifest: Manifest,
) -> str:
    """Generate a PR description for a component."""
    lines: list[str] = []

    lines.append("## Summary")
    lines.append("")
    lines.append(component.description)
    lines.append("")

    # Dependencies
    if component.dependencies:
        lines.append("## Dependencies")
        lines.append("")
        for dep_id in component.dependencies:
            dep = manifest.get_component(dep_id)
            if dep and dep.pr_url:
                lines.append(f"- [{dep.title}]({dep.pr_url})")
            elif dep:
                lines.append(f"- {dep.title} (`{dep_id}`)")
            else:
                lines.append(f"- `{dep_id}`")
        lines.append("")

    # Review findings (if any). The string at component.review_findings
    # is the canonical human-readable PR-body content because it carries
    # information the typed Finding stream does not: PASS criteria
    # confirmations, criterion-level pass/fail/advisory counts, and the
    # criterion text as headers (Finding.category is "prd_criterion" for
    # all of them, which obscures what the criterion actually asserted).
    # The typed component.findings is still load-bearing -- it ships to
    # the evolution journal via record_run for dashboards and
    # aggregations -- but pr.py is the wrong consumer for it.
    # render_findings_markdown remains available for ad-hoc dumping.
    if component.review_findings:
        lines.append(component.review_findings)
        lines.append("")

    # R1.2 (sec-pr-body): phases that never executed must be visible in
    # the PR body. In advisory mode a security infrastructure error used
    # to produce NO security section at all - "did not run" was
    # indistinguishable from "ran clean". Render the existing
    # render_findings_markdown callouts for exactly the non-execution
    # subset (infra errors + deliberate skips); real findings stay with
    # the richer review_findings string above.
    non_execution = [
        f for f in component.findings
        if f.is_infrastructure_error or f.is_phase_skip
    ]
    if non_execution:
        lines.append(render_findings_markdown(non_execution).rstrip())
        lines.append("")

    # PRD reference
    lines.append("## PRD")
    lines.append("")
    lines.append(f"- File: `{component.prd_path}`")
    lines.append(f"- Component: `{component.id}`")
    lines.append("")

    # R7.4: Linear magic-word trailer. "Fixes EXC-42" makes Linear's
    # GitHub integration move the issue to Done when this PR merges -
    # status transitions cost ralph zero API calls.
    if component.linear_issue_identifier:
        lines.append(f"Fixes {component.linear_issue_identifier}")
        lines.append("")

    lines.append("---")
    lines.append("Generated by [Ralph](https://github.com/0xfauzi/ralph-loop)")

    return "\n".join(lines)


def create_component_pr(
    component: Component,
    manifest: Manifest,
    cwd: Path,
) -> tuple[int, str]:
    """Create a PR for one component.

    Returns (pr_number, pr_url).
    Raises RuntimeError on failure.
    """
    body = _generate_pr_body(component, manifest)
    title = f"[{manifest.project_name}] {component.title}"

    # --base=/--head= bind the branch values to their flags even if a
    # crafted value starts with "-" (R0.6).
    try:
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--title", title,
                "--body", body,
                f"--base={manifest.base_branch}",
                f"--head={component.branch_name}",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Failed to create PR for '{component.id}': gh pr create "
            f"timed out after {GH_TIMEOUT}s"
        ) from None

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Failed to create PR for '{component.id}': {error}")

    pr_url = result.stdout.strip()
    return pr_number_from_url(pr_url), pr_url


def create_prs_in_order(
    manifest: Manifest,
    cwd: Path,
    ui: UI,
) -> list[tuple[int, str]]:
    """Create PRs in topological order.

    All PRs target base_branch directly (not stacked).
    Updates manifest components with PR numbers and URLs.

    Returns list of (pr_number, pr_url) tuples.
    """
    ui.section("Creating Pull Requests")

    if not is_gh_available():
        ui.warn("gh CLI not available or not authenticated. Skipping PR creation.")
        return []

    order = manifest.topological_order()
    completed_ids = {
        c.id for c in manifest.components
        if c.status == "completed"
    }

    results: list[tuple[int, str]] = []

    for comp_id in order:
        if comp_id not in completed_ids:
            continue

        component = manifest.get_component(comp_id)
        if component is None:
            continue

        if component.pr_url:
            ui.info(f"  {comp_id}: PR already exists ({component.pr_url})")
            results.append((component.pr_number or 0, component.pr_url))
            continue

        ui.info(f"  {comp_id}: pushing branch {component.branch_name}...")
        push_error = push_branch(component.branch_name, cwd)
        if push_error:
            ui.warn(
                f"  {comp_id}: failed to push branch ({push_error}), "
                f"skipping PR"
            )
            continue

        try:
            pr_number, pr_url = create_component_pr(component, manifest, cwd)
            component.pr_number = pr_number
            component.pr_url = pr_url
            results.append((pr_number, pr_url))
            ui.ok(f"  {comp_id}: {pr_url}")
        except RuntimeError as exc:
            ui.warn(f"  {comp_id}: {exc}")

    return results


def create_single_pr(
    manifest: Manifest,
    cwd: Path,
    ui: UI,
) -> tuple[int, str] | None:
    """Create a single PR for all completed components.

    Returns (pr_number, pr_url) or None on failure.
    """
    ui.section("Creating Single Pull Request")

    if not is_gh_available():
        ui.warn("gh CLI not available or not authenticated. Skipping PR creation.")
        return None

    # All components should be on the same branch in single-pr mode
    branches = {
        c.branch_name for c in manifest.components if c.status == "completed"
    }
    if not branches:
        ui.warn("No completed components. Skipping PR creation.")
        return None

    branch = next(iter(branches))

    ui.info(f"  Pushing branch {branch}...")
    push_error = push_branch(branch, cwd)
    if push_error:
        ui.warn(f"  Failed to push branch ({push_error}), skipping PR")
        return None

    # Build combined body
    lines: list[str] = []
    lines.append("## Summary")
    lines.append("")
    lines.append(f"Factory run for **{manifest.project_name}** from `{manifest.spec_file}`.")
    lines.append("")
    lines.append("## Components")
    lines.append("")

    for comp in manifest.components:
        status = "completed" if comp.status == "completed" else comp.status
        lines.append(f"- **{comp.title}** (`{comp.id}`): {status}")
        if comp.description:
            lines.append(f"  {comp.description}")

    lines.append("")
    lines.append("---")
    lines.append("Generated by [Ralph](https://github.com/0xfauzi/ralph-loop)")

    body = "\n".join(lines)
    title = f"[{manifest.project_name}] Factory: all components"

    # --base=/--head= bind the branch values to their flags even if a
    # crafted value starts with "-" (R0.6).
    try:
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--title", title,
                "--body", body,
                f"--base={manifest.base_branch}",
                f"--head={branch}",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        ui.warn(f"  Failed to create PR: timed out after {GH_TIMEOUT}s")
        return None

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        ui.warn(f"  Failed to create PR: {error}")
        return None

    pr_url = result.stdout.strip()
    ui.ok(f"  PR created: {pr_url}")
    return pr_number_from_url(pr_url), pr_url
