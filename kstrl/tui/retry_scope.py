"""What a retry will do, stated before it is offered (#433 Q5, advice-r1 section 3).

``retry_plan.preview_retry`` answers part of it on a copy of the
manifest: the component and the skipped dependents it resets, and what
stays failed or skipped. The rest of what ``prepare_retry`` then does
depends on the disk and on git, so it is looked up here, off the event
loop:

- whether the failed attempt's evidence worktree still exists (it is
  removed, and what runs in it is killed first, #537);
- whether the failed branch exists (``git rev-parse``). It is deleted and
  recreated from the base branch, except in ``single_pr`` mode where the
  shared branch carries completed work and is kept;
- what the relaunch runs under (``plan_resume``: the recorded flags and
  the cost ceiling), or why the TUI cannot carry it.

A retry restarts the component: ``Manifest.reset_for_retry`` clears its
phase, iterations, findings and PR, and the branch is recreated, so the
engineer runs again and every gate after it. That part is known from
the code, not looked up.

Anything the lookups cannot determine is shown as ``unknown`` and the
retry is not offered until it is known: the effect of a retry nobody can
state is not a thing to confirm.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kstrl.manifest import Manifest
    from kstrl.retry_plan import RetryPreview

UNKNOWN = "unknown"


@dataclass(frozen=True)
class ScopeLine:
    label: str
    value: str
    known: bool = True


@dataclass(frozen=True)
class RetryScope:
    component_id: str
    lines: tuple[ScopeLine, ...]
    preview: RetryPreview | None
    #: Why the retry cannot run from this screen, "" when it can.
    refusal: str = ""

    @property
    def unknown(self) -> tuple[str, ...]:
        return tuple(line.label for line in self.lines if not line.known)

    @property
    def offerable(self) -> bool:
        return self.preview is not None and not self.unknown and not self.refusal


def branch_probe(root_dir: Path, branch: str) -> int | None:
    """``git rev-parse --verify --quiet refs/heads/<branch>``'s exit code,
    the probe ``prepare_retry`` makes; None when git could not be run."""
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root_dir,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return probe.returncode


def _worktree_line(path: str) -> ScopeLine:
    if not path:
        return ScopeLine("worktree", "none recorded; nothing to remove")
    try:
        exists = Path(path).exists()
    except OSError:
        return ScopeLine("worktree", f"{UNKNOWN}: cannot tell whether {path} exists", known=False)
    if exists:
        return ScopeLine("worktree", f"removes {path}; kills what still runs in it first")
    return ScopeLine("worktree", f"none to remove ({path} is gone)")


def _branch_line(preview: RetryPreview, base: str, code: int | None) -> ScopeLine:
    """What ``prepare_retry`` does to the failed branch, from the same probe.

    It deletes the branch only when ``git rev-parse`` exits 0, so any
    other exit code is a known effect: nothing is deleted. Only a probe
    that could not run leaves the effect unknown.
    """
    branch = preview.failed_branch
    if preview.single_pr:
        shared = branch or "the shared branch"
        return ScopeLine("branch", f"keeps {shared} (single_pr: it carries completed work)")
    if not branch:
        return ScopeLine("branch", "none recorded; the retry creates one")
    if code is None:
        return ScopeLine("branch", f"{UNKNOWN}: git could not be run to find {branch}", known=False)
    if code == 0:
        return ScopeLine("branch", f"deletes {branch}; the retry recreates it from {base}")
    if code == 1:
        return ScopeLine("branch", f"{branch} does not exist; the retry creates it from {base}")
    return ScopeLine("branch", f"git rev-parse exited {code} for {branch}; nothing is deleted")


def retry_scope(
    root_dir: Path,
    manifest: Manifest,
    component_id: str,
    carry: Callable[[], tuple[str, str]],
    probe_branch: Callable[[Path, str], int | None] | None = None,
) -> RetryScope:
    """The retry's scope, every line known or marked unknown.

    ``carry`` returns ``(runs_under, refusal)``: what the relaunch runs
    under, or why the TUI cannot carry the recorded run's configuration.
    """
    from kstrl.retry_plan import preview_retry

    try:
        preview = preview_retry(manifest, component_id)
    except ValueError as exc:
        return RetryScope(component_id, (), None, refusal=str(exc))
    runs_under, refusal = carry()
    probe = probe_branch or branch_probe
    resets = ", ".join([component_id, *preview.reset_dependents])
    lines = [
        ScopeLine("starts at", "the beginning: the engineer runs again, then every gate after it"),
        ScopeLine("resets", f"{resets} to pending"),
        ScopeLine(
            "stays out", "; ".join(preview.not_in_retry) or "nothing else is failed or skipped"
        ),
        _worktree_line(preview.evidence_worktree),
        _branch_line(
            preview,
            manifest.base_branch,
            probe(root_dir, preview.failed_branch)
            if preview.failed_branch and not preview.single_pr
            else None,
        ),
        ScopeLine(
            "keeps", "the run records under .kstrl/runs, the debug directory, the evolution journal"
        ),
        ScopeLine("runs under", runs_under or f"cannot be carried here: {refusal}"),
        ScopeLine(
            "plan from", "preview_retry on a copy of the manifest, and the run's launch record"
        ),
    ]
    return RetryScope(component_id, tuple(lines), preview, refusal=refusal)
