"""PR creation, merge, and management via gh CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ralph_py.manifest import Component, Manifest
    from ralph_py.ui.base import UI


def is_gh_available() -> bool:
    """Check if gh CLI is available and authenticated."""
    if shutil.which("gh") is None:
        return False
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def push_branch(branch: str, cwd: Path) -> bool:
    """Push a branch to the remote with tracking."""
    # "--" makes a crafted branch value an invalid refspec instead of a
    # git option (R0.6).
    result = subprocess.run(
        ["git", "push", "-u", "--", "origin", branch],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def merge_pr(pr_number: int, cwd: Path, method: str = "squash") -> bool:
    """Merge a PR via gh CLI with auto-merge.

    Uses --auto so GitHub merges once status checks pass.
    Uses --delete-branch to clean up the feature branch.

    Args:
        pr_number: PR number to merge.
        cwd: Working directory (must be in the git repo).
        method: Merge method - "squash", "merge", or "rebase".
    """
    result = subprocess.run(
        [
            "gh", "pr", "merge", str(pr_number),
            f"--{method}", "--delete-branch", "--auto",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
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
        )
    return result.returncode == 0


def wait_for_merge(
    pr_number: int, cwd: Path, timeout: int = 300, poll_interval: int = 10,
) -> bool:
    """Poll until a PR is merged or timeout.

    Returns True if merged, False if timeout or closed without merge.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            state = data.get("state", "")
            if state == "MERGED":
                return True
            if state == "CLOSED":
                return False
        time.sleep(poll_interval)
    return False


def push_create_and_merge_pr(
    component: Component,
    manifest: Manifest,
    cwd: Path,
    ui: UI,
    merge_method: str = "squash",
    merge_timeout: int = 300,
) -> tuple[int, str] | None:
    """Push branch, create PR, merge it, and pull main.

    Full per-component PR lifecycle:
    1. Push branch to origin
    2. Create PR via gh
    3. Merge PR (auto-merge if checks required, direct otherwise)
    4. Wait for merge to complete
    5. Pull main so downstream components get the merged code

    Returns (pr_number, pr_url) on success, None on failure.
    """
    if component.pr_url:
        ui.info(f"  {component.id}: PR already exists ({component.pr_url})")
        return (component.pr_number or 0, component.pr_url)

    # Push
    ui.info(f"  Pushing {component.branch_name}...")
    if not push_branch(component.branch_name, cwd):
        ui.warn(f"  Failed to push {component.branch_name}")
        return None

    # Create PR
    try:
        pr_number, pr_url = create_component_pr(component, manifest, cwd)
    except RuntimeError as exc:
        ui.warn(f"  {exc}")
        return None

    component.pr_number = pr_number
    component.pr_url = pr_url
    ui.ok(f"  PR created: {pr_url}")

    # Merge
    if not merge_pr(pr_number, cwd, merge_method):
        ui.warn(f"  Failed to merge #{pr_number} - needs manual merge")
        return (pr_number, pr_url)

    # Wait for merge
    if wait_for_merge(pr_number, cwd, timeout=merge_timeout):
        ui.ok(f"  PR #{pr_number} merged")
        # Pull main so downstream worktrees start from merged state.
        # "--" keeps a crafted base branch out of option position (R0.6).
        subprocess.run(
            ["git", "pull", "--", "origin", manifest.base_branch],
            cwd=cwd, capture_output=True,
        )
        return (pr_number, pr_url)

    ui.warn(f"  PR #{pr_number} not merged within {merge_timeout}s - may need manual merge")
    return (pr_number, pr_url)


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

    # PRD reference
    lines.append("## PRD")
    lines.append("")
    lines.append(f"- File: `{component.prd_path}`")
    lines.append(f"- Component: `{component.id}`")
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
    )

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Failed to create PR for '{component.id}': {error}")

    pr_url = result.stdout.strip()

    # Extract PR number from URL (e.g., https://github.com/owner/repo/pull/42)
    pr_number = 0
    if "/pull/" in pr_url:
        try:
            pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            pass

    return pr_number, pr_url


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
        if not push_branch(component.branch_name, cwd):
            ui.warn(f"  {comp_id}: failed to push branch, skipping PR")
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
    if not push_branch(branch, cwd):
        ui.warn("  Failed to push branch, skipping PR")
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
    )

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        ui.warn(f"  Failed to create PR: {error}")
        return None

    pr_url = result.stdout.strip()
    pr_number = 0
    if "/pull/" in pr_url:
        try:
            pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            pass

    ui.ok(f"  PR created: {pr_url}")
    return pr_number, pr_url
