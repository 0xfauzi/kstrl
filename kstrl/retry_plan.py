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
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.factory import FactoryConfig, validate_cost_ceiling
from kstrl.launch_record import (
    FlagValue,
    LaunchRecord,
    LaunchRecordError,
    flags_argv,
    launch_record_path,
    read_launch_record,
)
from kstrl.timeout import NO_LIMIT

if TYPE_CHECKING:
    import click

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
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )
        shutil.rmtree(evidence_worktree, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )
        ui.info(f"Removed the failed attempt's evidence worktree: {evidence_worktree}")
    if failed_branch and not manifest.single_pr:
        branch_exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{failed_branch}"],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )
        if branch_exists.returncode == 0:
            deleted = subprocess.run(
                ["git", "branch", "-D", failed_branch],
                cwd=root_dir,
                capture_output=True,
                encoding="utf-8",
                timeout=30,
            )
            if deleted.returncode == 0:
                ui.info(
                    f"Deleted branch '{failed_branch}' from the failed "
                    f"attempt; the retry recreates it from "
                    f"'{manifest.base_branch}'"
                )
            else:
                ui.err(f"Could not delete branch '{failed_branch}': {deleted.stderr.strip()}")
                ui.info(
                    "Delete it manually (git branch -D "
                    f"{failed_branch}) and re-run; the factory refuses "
                    "to silently reuse stale branches (R0.5)."
                )
                raise RetryError(f"could not delete branch '{failed_branch}'")
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


def retry_confirm_header(preview: RetryPreview) -> str:
    """The confirmation question, naming every component the retry re-enters (#436)."""
    if not preview.reset_dependents:
        return f"Re-enter the factory to retry '{preview.component_id}'?"
    dependents = ", ".join(f"'{cid}'" for cid in preview.reset_dependents)
    return (
        f"Re-enter the factory to retry '{preview.component_id}' "
        f"and the dependents it reset: {dependents}?"
    )


#: The headline every refusal from :func:`plan_resume` prints under.
RESUME_REFUSAL = "the retry cannot carry over the configuration of the run it resumes"

_CEILING_REMEDY = "pass --max-cost-usd N to cap this retry, or --max-cost-usd 0 to run it uncapped"


@dataclass(frozen=True)
class ResumePlan:
    """What a retry re-enters `ks factory` with (#436)."""

    #: The run the manifest names, "" when it names none.
    run_id: str
    #: Whether that run left a launch record whose flags are replayed.
    carried: bool
    #: The replayed flags plus the retry's own, as `ks factory` options.
    argv: tuple[str, ...]
    max_cost_usd: float
    max_parallel: int


def _ceiling_problems(
    run_id: str,
    record: LaunchRecord | None,
    ceiling: float,
    stated_on_retry: bool,
) -> list[str]:
    """Why the retry must refuse over its cost ceiling, or [] when it may run."""
    if stated_on_retry or ceiling > 0:
        return []
    if record is None:
        return [
            f"run {run_id or '(none)'} left no launch record, so the cost ceiling "
            "it ran under is unknown, and the environment and kstrl.toml set none",
            _CEILING_REMEDY,
        ]
    if record.max_cost_usd == 0:
        return []
    return [
        f"run {run_id} ran under a cost ceiling of ${record.max_cost_usd}, and this "
        "retry resolves none: the ceiling came from the environment or kstrl.toml, "
        "which no longer set it",
        _CEILING_REMEDY,
    ]


def plan_resume(
    root_dir: Path,
    manifest: Manifest,
    manifest_file: Path,
    command: click.Command,
    *,
    max_cost_usd: float | None,
    max_parallel: int | None,
    keep_worktrees_on_failure: bool,
) -> tuple[ResumePlan | None, list[str]]:
    """The flags a retry replays and the ceiling it runs under, or why it refuses.

    Changes nothing, so a refusal leaves the manifest, branch and worktree
    exactly as the failed run left them. The retry's own options win over
    the recorded ones, the same way a flag wins over env and kstrl.toml.
    """
    overrides: dict[str, FlagValue] = {
        name: value
        for name, value in (
            ("max_cost_usd", max_cost_usd),
            ("max_parallel", max_parallel),
            ("keep_worktrees_on_failure", keep_worktrees_on_failure or None),
        )
        if value is not None
    }
    try:
        record = read_launch_record(root_dir, manifest, manifest_file)
        flags = dict(record.flags) if record is not None else {}
        flags.update(overrides)
        argv = flags_argv(command, flags)
    except LaunchRecordError as exc:
        path = launch_record_path(root_dir, manifest.run_id)
        return None, [str(exc), f"delete {path} to retry without the recorded flags"]
    loaded = FactoryConfig.load(root_dir)
    ceiling = validate_cost_ceiling(
        float(flags.get("max_cost_usd", loaded.max_cost_usd)), "--max-cost-usd"
    )
    problems = _ceiling_problems(manifest.run_id, record, ceiling, max_cost_usd is not None)
    if problems:
        return None, problems
    plan = ResumePlan(
        run_id=manifest.run_id,
        carried=record is not None,
        argv=tuple(argv),
        max_cost_usd=ceiling,
        max_parallel=int(flags.get("max_parallel", loaded.max_parallel)),
    )
    return plan, []


def print_resume_plan(ui: UI, plan: ResumePlan) -> None:
    """Say, before the confirmation and before any spend, what the retry runs under."""
    if plan.carried:
        shown = shlex.join(plan.argv) if plan.argv else "(none were passed)"
        ui.info(f"Resuming with the flags of run {plan.run_id}: {shown}")
    else:
        ui.warn(
            f"No launch record for run {plan.run_id or '(none)'}: "
            "the flags of the run being resumed are not carried over"
        )
    ui.kv("Cost ceiling", f"${plan.max_cost_usd}" if plan.max_cost_usd > 0 else NO_LIMIT)
    ui.kv("Max parallel", str(plan.max_parallel))
