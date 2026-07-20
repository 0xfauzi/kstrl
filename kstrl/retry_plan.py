"""Retry planning and preparation (extracted from cli.retry, B1).

``preview_retry`` answers "what WOULD a retry do" without touching
anything - the retry screen renders it in its confirm modal.
``prepare_retry`` is the real mutation: reset statuses, remove the
failed attempt's worktree and branch, save the manifest. Narration
stays byte-identical to the original command; the only behavior
change is RetryError instead of sys.exit so a TUI caller can surface
the failure without the process dying.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kstrl.manifest import Manifest
    from kstrl.ui.base import UI


class RetryError(Exception):
    """A retry preparation step failed after narrating the details."""


@dataclass(frozen=True)
class RetryPreview:
    component_id: str
    reset_dependents: list[str]
    evidence_worktree: str
    failed_branch: str
    single_pr: bool


def preview_retry(manifest: Manifest, component_id: str) -> RetryPreview:
    """Non-mutating preview: runs reset_for_retry on a deep copy.

    Raises ValueError exactly as reset_for_retry does (unknown
    component, component not failed).
    """
    comp = manifest.get_component(component_id)
    scratch = copy.deepcopy(manifest)
    reset_dependents = scratch.reset_for_retry(component_id)
    return RetryPreview(
        component_id=component_id,
        reset_dependents=reset_dependents,
        evidence_worktree=comp.evidence_worktree if comp else "",
        failed_branch=comp.branch_name if comp else "",
        single_pr=manifest.single_pr,
    )


def prepare_retry(
    manifest: Manifest,
    component_id: str,
    manifest_file: Path,
    root_dir: Path,
    ui: UI,
) -> RetryPreview:
    """Mutate the manifest for a retry and clean up the failed attempt.

    Verbatim move of the cli.retry block: reset statuses, narrate the
    plan, remove the kept evidence worktree, delete the failed branch
    (never in single_pr mode - the shared branch carries completed
    components' commits), save. ValueError propagates from
    reset_for_retry; a branch-delete failure raises RetryError after
    narrating the manual fix.
    """
    comp = manifest.get_component(component_id)
    evidence_worktree = comp.evidence_worktree if comp else ""
    failed_branch = comp.branch_name if comp else ""

    reset_dependents = manifest.reset_for_retry(component_id)

    ui.section("Retry plan")
    ui.kv("Component", component_id)
    ui.kv(
        "Cascade-skipped dependents reset",
        ", ".join(reset_dependents) if reset_dependents else "(none)",
    )
    ui.kv("Manifest", str(manifest_file))

    # The failed attempt's worktree and branch are superseded by the
    # fresh attempt; remove them so provisioning and the stale-branch
    # preflight start clean. In single_pr mode every component shares
    # one branch carrying completed components' commits - never delete
    # it here.
    if evidence_worktree and Path(evidence_worktree).exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", evidence_worktree],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        shutil.rmtree(evidence_worktree, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        ui.info(
            f"Removed the failed attempt's evidence worktree: "
            f"{evidence_worktree}"
        )
    if failed_branch and not manifest.single_pr:
        branch_exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet",
             f"refs/heads/{failed_branch}"],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        if branch_exists.returncode == 0:
            deleted = subprocess.run(
                ["git", "branch", "-D", failed_branch],
                cwd=root_dir, capture_output=True, text=True, timeout=30,
            )
            if deleted.returncode == 0:
                ui.info(
                    f"Deleted branch '{failed_branch}' from the failed "
                    f"attempt; the retry recreates it from "
                    f"'{manifest.base_branch}'"
                )
            else:
                ui.err(
                    f"Could not delete branch '{failed_branch}': "
                    f"{deleted.stderr.strip()}"
                )
                ui.info(
                    "Delete it manually (git branch -D "
                    f"{failed_branch}) and re-run; the factory refuses "
                    "to silently reuse stale branches (R0.5)."
                )
                raise RetryError(
                    f"could not delete branch '{failed_branch}'"
                )
    elif manifest.single_pr:
        ui.warn(
            "single_pr mode: the shared branch is left in place; if the "
            "run is refused at branch preflight, resolve it manually"
        )

    manifest.save(manifest_file)
    return RetryPreview(
        component_id=component_id,
        reset_dependents=reset_dependents,
        evidence_worktree=evidence_worktree,
        failed_branch=failed_branch,
        single_pr=manifest.single_pr,
    )
