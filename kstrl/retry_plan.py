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
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.factory import FactoryConfig, validate_cost_ceiling, validate_token_ceiling
from kstrl.launch_record import (
    REMOVED_OPTIONS,
    FlagValue,
    LaunchRecord,
    LaunchRecordError,
    flags_argv,
    launch_record_path,
    read_launch_record,
    run_limits,
)
from kstrl.manifest import ComponentStatus
from kstrl.timeout import NO_LIMIT, TimeoutConfig
from kstrl.worktree_sweep import sweep_worktree, warn_sweep

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
    #: What stays FAILED or SKIPPED after the reset, one line each (#485).
    not_in_retry: list[str]


def _failed_dependencies(manifest: Manifest, component_id: str) -> list[str]:
    """Every FAILED component among *component_id*'s transitive dependencies."""
    seen: set[str] = set()
    stack = [component_id]
    while stack:
        comp = manifest.get_component(stack.pop())
        for dep in comp.dependencies if comp is not None else []:
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return [
        c.id
        for c in manifest.components
        if c.id in seen and c.status == ComponentStatus.FAILED.value
    ]


def _not_in_retry(manifest: Manifest) -> list[str]:
    """The FAILED and SKIPPED components a retry leaves out, in manifest order (#485).

    Read AFTER ``reset_for_retry``: the retried component and every
    dependent it reset are PENDING by then, so neither can be listed.
    """
    retryable = set(manifest.retryable_component_ids())
    lines: list[str] = []
    for comp in manifest.components:
        if comp.id in retryable:
            lines.append(f"{comp.id}: FAILED; retry it after this run with ks retry {comp.id}")
        elif comp.status == ComponentStatus.SKIPPED.value:
            waits = _failed_dependencies(manifest, comp.id)
            lines.append(
                f"{comp.id}: SKIPPED; waits on {', '.join(waits) or 'no FAILED component'}"
            )
    return lines


def _print_not_in_retry(ui: UI, not_in_retry: list[str]) -> None:
    """One `Not in this retry` row per component the retry leaves out, or `(none)`."""
    for line in not_in_retry or ["(none)"]:
        ui.kv("Not in this retry", line)


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
        not_in_retry=_not_in_retry(scratch),
    )


def failed_branch_probe(root_dir: Path, branch: str) -> int:
    """The exit code of ``git rev-parse --verify --quiet refs/heads/<branch>``.

    :func:`prepare_retry` deletes the failed branch only when this is 0,
    and the TUI's retry scope preview (#433) calls this same probe, so
    the preview cannot describe a different test than the one the retry
    makes. OSError and a timeout propagate, as they always did here.
    """
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=root_dir,
        capture_output=True,
        timeout=30,
    ).returncode


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
    not_in_retry = _not_in_retry(manifest)
    _print_not_in_retry(ui, not_in_retry)
    ui.kv("Manifest", str(manifest_file))

    # The failed attempt's worktree and branch are superseded by the
    # fresh attempt; remove them so provisioning and the stale-branch
    # preflight start clean. In single_pr mode every component shares
    # one branch carrying completed components' commits - never delete
    # it here. #528: what still runs in the evidence worktree (a server
    # started while looking at the failure) is killed and named first.
    if evidence_worktree and Path(evidence_worktree).exists():
        warn_sweep(sweep_worktree(Path(evidence_worktree)), ui, "retry")
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
        if failed_branch_probe(root_dir, failed_branch) == 0:
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
        not_in_retry=not_in_retry,
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

_LIMIT_REMEDY = (
    "pass each option named above to the retry: a value to keep that limit, or 0 to run without it"
)


def _opt(name: str) -> str:
    """The `ks factory` / `ks retry` option spelling of a run limit."""
    return "--" + name.replace("_", "-")


@dataclass(frozen=True)
class ResumePlan:
    """What a retry re-enters `ks factory` with (#436)."""

    #: The run the manifest names, "" when it names none.
    run_id: str
    #: Whether that run left a launch record whose flags are replayed.
    carried: bool
    #: The replayed flags plus the retry's own, as `ks factory` options.
    argv: tuple[str, ...]
    #: Every run limit the retry runs under, by option name (#526).
    limits: tuple[tuple[str, float], ...]
    max_parallel: int
    #: Recorded flags left out because `ks factory` no longer has them (#539).
    dropped: tuple[str, ...]


def _ceiling_problems(
    run_id: str,
    record: LaunchRecord | None,
    resolved: Mapping[str, float],
    stated: Collection[str],
) -> list[str]:
    """Why the retry must refuse over its run limits, or [] when it may run.

    A limit the retry states on its own command line, or resolves above 0,
    is kept. Otherwise the retry refuses when the run it resumes ran under
    that limit, or when the record does not say (no record, or a record
    written before the limit was recorded). One rule for every entry of
    ``run_limits``, so a limit added there is covered here unedited (#526).
    """
    ran_under = dict(record.limits) if record is not None else {}
    problems: list[str] = []
    for name in {**ran_under, **resolved}:
        if name in stated or resolved.get(name, 0) > 0:
            continue
        if name not in ran_under:
            problems.append(
                f"{_opt(name)}: run {run_id or '(none)'} left no launch record of this "
                "limit, so the value it ran under is unknown, and the environment and "
                "kstrl.toml set none"
            )
        elif ran_under[name] > 0:
            problems.append(
                f"{_opt(name)}: run {run_id} ran under {_opt(name)} {ran_under[name]}, and "
                "this retry resolves none: the limit came from the environment or "
                "kstrl.toml, which no longer set it"
            )
    return [*problems, _LIMIT_REMEDY] if problems else []


def plan_resume(
    root_dir: Path,
    manifest: Manifest,
    manifest_file: Path,
    command: click.Command,
    *,
    max_cost_usd: float | None,
    max_parallel: int | None,
    keep_worktrees_on_failure: bool,
    limits: Mapping[str, float | None] | None = None,
) -> tuple[ResumePlan | None, list[str]]:
    """The flags a retry replays and the limits it runs under, or why it refuses.

    Changes nothing, so a refusal leaves the manifest, branch and worktree
    exactly as the failed run left them. The retry's own options win over
    the recorded ones, the same way a flag wins over env and kstrl.toml.
    ``limits`` holds the retry's own values for the run limits other than
    the cost ceiling, by option name; None is "not given".
    """
    overrides: dict[str, FlagValue] = {
        name: value
        for name, value in (
            ("max_cost_usd", max_cost_usd),
            ("max_parallel", max_parallel),
            ("keep_worktrees_on_failure", keep_worktrees_on_failure or None),
            *(limits or {}).items(),
        )
        if value is not None
    }
    try:
        record = read_launch_record(root_dir, manifest, manifest_file)
        recorded = dict(record.flags) if record is not None else {}
        dropped = tuple(name for name in recorded if name in REMOVED_OPTIONS)
        flags = {name: value for name, value in recorded.items() if name not in REMOVED_OPTIONS}
        flags.update(overrides)
        argv = flags_argv(command, flags)
    except LaunchRecordError as exc:
        path = launch_record_path(root_dir, manifest.run_id)
        return None, [str(exc), f"delete {path} to retry without the recorded flags"]
    loaded = FactoryConfig.load(root_dir)
    resolved = {
        name: float(flags.get(name, value))
        for name, value in run_limits(loaded, TimeoutConfig.load(root_dir)).items()
    }
    # The two limits `ks factory` validates in its budget preflight, checked
    # here too so a bad value is refused before prepare_retry changes anything.
    validate_cost_ceiling(resolved["max_cost_usd"], "--max-cost-usd")
    validate_token_ceiling(int(resolved["max_total_tokens"]), "--max-total-tokens")
    problems = _ceiling_problems(manifest.run_id, record, resolved, overrides.keys())
    if problems:
        return None, problems
    plan = ResumePlan(
        run_id=manifest.run_id,
        carried=record is not None,
        argv=tuple(argv),
        limits=tuple(resolved.items()),
        max_parallel=int(flags.get("max_parallel", loaded.max_parallel)),
        dropped=dropped,
    )
    return plan, []


def limits_line(plan: ResumePlan) -> str:
    """Every run limit the retry runs under, on one line, spelled as the
    options that set them (#526), so the TUI states what the CLI prints."""
    set_ = [f"{_opt(name)} {value:g}" for name, value in plan.limits if value > 0]
    return ", ".join(set_) if set_ else "no run limit"


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
    for name in plan.dropped:
        ui.warn(f"Not replayed from run {plan.run_id}: {_opt(name)}, {REMOVED_OPTIONS[name]}")
    for name, value in plan.limits:
        ui.kv(_opt(name), str(value) if value > 0 else NO_LIMIT)
    ui.kv("Max parallel", str(plan.max_parallel))
