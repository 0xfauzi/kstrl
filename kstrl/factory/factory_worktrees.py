"""The component worktrees and branches a run creates, reuses, merges, recuts and removes."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.git import (
    branch_sha,
    fetch_base_branch,
    get_head_sha,
    is_ancestor,
    merge_branch,
    resolve_base_ref,
)
from kstrl.plan import Component, ComponentStatus, Manifest, has_merged
from kstrl.proc import WorktreeSweep, sweep_worktree, warn_sweep

if TYPE_CHECKING:
    from kstrl.pipeline import ComponentPipeline, FactoryConfig, RunState
    from kstrl.ui import UI


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
    recut_at: str | None = None,
    *,
    ui: UI,
) -> Path:
    """Create a git worktree for a component.

    Worktrees are keyed ``.kstrl/worktrees/<run_id>/<component_id>``
    (R0.5, H-7): two invocations never share a worktree path, so setup
    can only ever remove a leftover from an earlier attempt of THIS run
    (a retry), never another invocation's in-flight worktree. Run-level
    exclusion itself is the ``.kstrl/factory.lock`` flock in run_factory.

    A per-host fcntl flock on ``.kstrl/worktrees/<component_id>.lock``
    (run-agnostic on purpose) still serializes the git commands here for
    the degraded modes that run without the run-level lock (Windows,
    ``--force-lock``), where two invocations could otherwise race on the
    shared branch and .git metadata.

    ``fresh_from_base=True`` (used for retries after a timeout kill, and
    for merge-conflict re-runs under the R7.5 re-run doctrine)
    additionally resets the component branch to ``recut_at``, or to
    ``base_branch`` when that is None, so the worktree does not silently
    reuse possibly-dirty state from the killed attempt (R0.1). single_pr
    passes ``recut_at``: its branch is shared, and the commit the
    component started at holds every earlier component's commits (#566).

    A process an earlier attempt's phases left running in the worktree
    is killed before the worktree is recreated, and each kill is written
    to ``ui`` as a warning, which a factory run records in its
    events.jsonl (#642).

    POSIX only. On Windows the fcntl import fails; we degrade to the
    pre-lock behavior and document the limitation in the runbook.
    """
    worktree_base = root_dir / ".kstrl" / "worktrees" / run_id
    worktree_base.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_base / component_id
    lock_path = root_dir / ".kstrl" / "worktrees" / f"{component_id}.lock"

    lock_fp = None
    try:
        try:
            import fcntl

            # utf-8 named though nothing is ever written through this
            # handle: it exists for flock on its fileno. Naming it costs
            # nothing at run time and keeps the package free of any text
            # open whose encoding the locale decides (#320).
            lock_fp = open(lock_path, "w", encoding="utf-8")
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

        # Unconditional: a crashed attempt's directory may be gone (tmp
        # cleaner, operator rm -rf) while its .git/worktrees/<comp>/
        # registration survives, and `git worktree add` refuses over a
        # registered-but-missing entry. remove --force clears the
        # registration in that state too (measured on git 2.47); when
        # nothing is registered it fails harmlessly, like `branch -D`.
        # #461: kill what an earlier attempt's phases left running there
        # first, and record each kill (#642).
        warn_sweep(sweep_worktree(worktree_path), ui, "worktree setup")
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )

        # R0.2: cut from origin/<base> when a remote exists so this
        # component builds on the squash-merged history of its
        # dependencies, not a stale local base ref. The fetch is
        # freshness-only and non-fatal: offline runs fall back to the
        # current tracking ref, local-only repos to the local base.
        fetch_base_branch(base_branch, root_dir, timeout=60.0)
        base_ref = resolve_base_ref(base_branch, root_dir)

        if fresh_from_base:
            # Reset the branch past the killed attempt's commits. -B resets
            # it in the same command that checks it out, so the branch is
            # never deleted: a single_pr branch holds earlier components'
            # commits, and a failure here must not take them with it
            # (#566). No reuse fallback: that would resume the killed
            # attempt's commits.
            result = subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-B",
                    branch_name,
                    str(worktree_path),
                    recut_at or base_ref,
                ],
                cwd=root_dir,
                capture_output=True,
                encoding="utf-8",
                timeout=30,
            )
        else:
            result = subprocess.run(
                ["git", "worktree", "add", str(worktree_path), "-b", branch_name, base_ref],
                cwd=root_dir,
                capture_output=True,
                encoding="utf-8",
                timeout=30,
            )
            if result.returncode != 0:
                # Branch already exists: reuse it WITH its commits. After the
                # run-start preflight (_preflight_component_branches) this can
                # only be a branch created during THIS run - a non-timeout
                # retry resuming its own progress, or single_pr components
                # stacking on the shared branch. Stale branches from previous
                # runs were deleted (fully merged) or refused at preflight,
                # never silently reused here (R0.5).
                # #646: or a branch ks retry kept for a re-judge, which
                # _kept_for_rejudge let through the preflight at the kept commit.
                result = subprocess.run(
                    ["git", "worktree", "add", str(worktree_path), branch_name],
                    cwd=root_dir,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=30,
                )

        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Failed to create worktree for '{component_id}': {error}")

        return worktree_path
    finally:
        if lock_fp is not None:
            try:
                import fcntl

                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            lock_fp.close()


def _dependency_branches(manifest: Manifest, comp: Component, cwd: Path) -> list[str]:
    """The branches of ``comp``'s dependencies whose code is not in the base (#543).

    A dependency that merged (``has_merged``) is already in the base the
    worktree is cut from, which is how per-component PR mode gives a
    dependent its dependency's code. Under ``create_prs=False``, or with
    no gh, a COMPLETED dependency's code exists only on its own branch,
    until a person merges that branch into the base branch, which git in
    ``cwd`` (any checkout of the repository) says. In single_pr mode a
    dependency shares ``comp``'s branch and is already in it.
    """
    branches: list[str] = []
    for dep_id in comp.dependencies:
        dep = manifest.get_component(dep_id)
        if dep is None or has_merged(dep) or dep.branch_name in (comp.branch_name, *branches):
            continue
        if is_ancestor(dep.branch_name, manifest.base_branch, cwd):
            continue
        branches.append(dep.branch_name)
    return branches


def _merge_dependency_branches(worktree_path: Path, branches: list[str]) -> None:
    """Merge each dependency branch into the component's worktree (#543).

    The same ``git.merge_branch`` the deferred-merge contract phase uses to
    put a tier's branches together. A merge that fails is raised: the
    component cannot start from a tree that holds its dependencies' code,
    and starting it without that code is the defect this exists to remove.
    The worktree is already registered for the pass-end cleanup, which
    removes it with ``--force`` (or keeps it, conflict and all, as the
    failure's evidence under ``keep_worktrees_on_failure``).
    """
    for branch in branches:
        if not merge_branch(branch, worktree_path):
            raise RuntimeError(
                f"could not merge dependency branch '{branch}' into {worktree_path}: "
                f"`git merge --no-edit -- {branch}` failed (it conflicts with "
                f"another dependency's change, or the branch no longer exists)"
            )


def _judged_base(worktree_path: Path, base_branch: str, holds_unmerged_work: bool) -> str:
    """What a freshly cut component branch is judged against (#543).

    The base branch, unless the worktree holds code the base lacks: then
    the commit the worktree starts at, so ``<base>...HEAD`` measures the
    component's own change and not its dependencies' or its single_pr
    predecessors'. Raises when HEAD cannot be read, because judging the
    change against the base branch instead would count that code again.
    """
    if not holds_unmerged_work:
        return base_branch
    head = get_head_sha(worktree_path)
    if head is None:
        raise RuntimeError(f"could not read HEAD in {worktree_path} to record the component's base")
    return head


def _start_from_dependencies(
    manifest: Manifest,
    comp: Component,
    worktree_path: Path,
    bases: dict[str, str],
    *,
    fresh_from_base: bool,
) -> None:
    """Put ``comp``'s dependencies' code in its new worktree and record its base (#543).

    ``bases`` is the run's ``RunState.component_bases``. It is written once
    per branch: a retry that keeps the branch keeps the base it was cut
    at, so an earlier attempt's commits are still judged, and a retry
    that recreates the branch (``fresh_from_base``) records it again.
    """
    branches = _dependency_branches(manifest, comp, worktree_path)
    _merge_dependency_branches(worktree_path, branches)
    if fresh_from_base or comp.id not in bases:
        bases[comp.id] = _judged_base(
            worktree_path,
            manifest.base_branch,
            bool(branches) or manifest.single_pr,
        )


def _recut_point(manifest: Manifest, comp: Component, bases: dict[str, str]) -> str | None:
    """The commit a ``fresh_from_base`` retry resets ``comp``'s branch to (#566).

    None resets it to the base branch, which is right when the branch is
    the component's own. A single_pr branch is shared: reset to the base
    branch, it would lose every earlier component's commits. It is reset
    to the commit this component started at, which
    ``_start_from_dependencies`` recorded in ``bases`` when the branch was
    first provisioned for it, so only the killed attempt's commits go.
    """
    if not manifest.single_pr:
        return None
    start = bases.get(comp.id)
    if start is None:
        raise RuntimeError(
            f"no recorded start commit for '{comp.id}' on the shared branch "
            f"'{comp.branch_name}'; resetting it to '{manifest.base_branch}' "
            f"would drop every earlier component's commits"
        )
    return start


def _cleanup_worktree(component_id: str, root_dir: Path, run_id: str) -> WorktreeSweep:
    """Kill what is still running in a component's worktree, then remove it.

    Returns what the sweep found so the caller can record it (#461).
    """
    worktree_path = root_dir / ".kstrl" / "worktrees" / run_id / component_id
    if not worktree_path.exists():
        return WorktreeSweep()
    sweep = sweep_worktree(worktree_path)
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=root_dir,
        capture_output=True,
        timeout=30,
    )
    return sweep


def _evidence_worktrees_to_keep(manifest: Manifest) -> set[str]:
    """Worktree paths the stale-prune pass must preserve (R3.3).

    A worktree kept by ``keep_worktrees_on_failure`` stays referenced as
    the FAILED component's evidence pointer; once the component leaves
    FAILED (retried, or reset) the reference is cleared and the next
    prune removes it. Both the recorded and resolved spellings are
    included so path normalization differences cannot defeat the match.
    """
    keep: set[str] = set()
    for comp in manifest.components:
        if comp.status == ComponentStatus.FAILED.value and comp.evidence_worktree:
            keep.add(comp.evidence_worktree)
            try:
                keep.add(str(Path(comp.evidence_worktree).resolve()))
            except OSError:
                pass
    return keep


def _flat_worktree_refusals(worktree_root: Path, run_id: str) -> list[str]:
    """One refusal per pre-R0.5 flat worktree under ``worktree_root``."""
    return [
        f"{entry} is a worktree directly under {worktree_root}, the layout before R0.5, "
        "and kstrl no longer sweeps it. Remove it, then start the run again: "
        f"git worktree remove --force {shlex.quote(str(entry))}"
        for entry in sorted(worktree_root.iterdir())
        if entry.name != run_id and entry.is_dir() and (entry / ".git").exists()
    ]


def _prune_stale_worktrees(
    root_dir: Path,
    run_id: str,
    ui: UI,
    keep: set[str] | None = None,
) -> list[str]:
    """Remove worktrees left behind by previous (crashed/aborted) runs.

    Returns one refusal per flat-layout entry (see below) and removes
    nothing when it returns any.

    Only called when the run-level flock is genuinely held: any prior
    holder has exited (flock dies with its process), so everything under
    ``.kstrl/worktrees/`` that is not ours - other runs' ``<run_id>/``
    dirs - is orphaned and safe to remove. A pre-R0.5 flat-layout
    ``<component_id>/`` worktree is NOT swept (#863): the run dir branch
    would delete its files without ``git worktree remove``. It is refused
    by name, and the operator removes it. Includes worktrees kept for leaked
    workers (R0.1): their owning run is gone, so by the next invocation
    they are stale state, matching the pre-R0.5 force-remove behavior.

    ``keep`` (R3.3) lists evidence worktrees of still-FAILED components
    (kept via keep_worktrees_on_failure); those are preserved so a
    resume does not destroy the post-mortem state it exists to protect.

    A process still running in a stale worktree is killed before the
    worktree is removed, and each kill is written to ``ui`` as a
    warning, which a factory run records in its events.jsonl (#642).
    """
    keep = keep or set()

    def _kept(path: Path) -> bool:
        if str(path) in keep:
            return True
        try:
            return str(path.resolve()) in keep
        except OSError:
            return False

    worktree_root = root_dir / ".kstrl" / "worktrees"
    if not worktree_root.exists():
        return []
    flat = _flat_worktree_refusals(worktree_root, run_id)
    if flat:
        return flat
    removed = 0
    kept = 0
    for entry in sorted(worktree_root.iterdir()):
        if entry.name == run_id or not entry.is_dir():
            continue  # our own run dir, or a per-component .lock file
        # <run_id>/ dir from a previous run: remove each component
        # worktree inside it.
        entry_kept = 0
        for wt in sorted(entry.iterdir()):
            if wt.is_dir():
                if _kept(wt):
                    entry_kept += 1
                    continue
                warn_sweep(sweep_worktree(wt), ui, "stale worktree")
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt)],
                    cwd=root_dir,
                    capture_output=True,
                    timeout=30,
                )
                # Whatever git could not remove goes with the dir.
                shutil.rmtree(wt, ignore_errors=True)
                removed += 1
        kept += entry_kept
        if entry_kept:
            # Evidence lives inside: keep the run dir itself.
            continue
        # Whatever git could not remove (or non-worktree debris) goes
        # with the dir; `git worktree prune` below drops any metadata
        # orphaned by this.
        shutil.rmtree(entry, ignore_errors=True)
    if removed:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )
        ui.info(f"  Pruned {removed} stale worktree(s) from previous runs")
    if kept:
        ui.info(
            f"  Preserved {kept} evidence worktree(s) of failed "
            f"components (keep_worktrees_on_failure)"
        )
    return []


def _worktree_records(listing: str) -> dict[str, tuple[str, str]]:
    """``git worktree list --porcelain -z`` as {resolved path: (branch ref, head)}.

    Every field ends in a NUL and every record ends in one more, so an
    empty field closes a record. A field is ``<key> <value>`` or a bare
    ``<key>`` (``detached``, ``locked``). Split on single NULs: a literal
    holding two is the static guards' hole marker (tests/helpers/astfold.py).
    """
    records: dict[str, tuple[str, str]] = {}
    fields: dict[str, str] = {}
    for entry in listing.split("\0"):
        if entry:
            key, _, value = entry.partition(" ")
            fields[key] = value
            continue
        path = fields.get("worktree", "")
        if path:
            records[str(Path(path).resolve())] = (fields.get("branch", ""), fields.get("HEAD", ""))
        fields = {}
    return records


def _interrupted_run_branches(manifest: Manifest, root_dir: Path) -> dict[str, str]:
    """Branches the interrupted run that last held this manifest provably made (#460).

    A component the manifest still records RUNNING or VERIFYING was
    mid-attempt when that run stopped without finishing. Its branch is
    that run's own when git still registers a worktree at
    ``.kstrl/worktrees/<manifest.run_id>/<component id>`` with the branch
    checked out: ``_setup_worktree`` is the only thing that creates a
    worktree at that path, the run id in the path is the interrupted
    run's, and that run's own branch preflight refused any unmerged branch
    it did not create. Returns {branch: why it may be deleted}.

    Everything it cannot prove is absent, and
    ``_preflight_component_branches`` refuses it exactly as before. It must
    run BEFORE the crash-recovery reset, which rewrites the statuses it
    reads, and before ``_prune_stale_worktrees``, which removes the
    registrations it reads. Never in single_pr mode: there every component
    shares one branch that carries completed components' commits.
    """
    if manifest.single_pr or not manifest.run_id or manifest.completed_at:
        return {}
    run_dir = (root_dir / ".kstrl" / "worktrees" / manifest.run_id).resolve()
    wanted = {
        str(run_dir / comp.id): comp.branch_name
        for comp in manifest.components
        if comp.status in (ComponentStatus.RUNNING.value, ComponentStatus.VERIFYING.value)
    }
    if not wanted:
        return {}
    try:
        listing = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            cwd=root_dir,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    records = _worktree_records(listing.stdout) if listing.returncode == 0 else {}
    found: dict[str, str] = {}
    for path, branch in wanted.items():
        ref, head = records.get(path, ("", ""))
        if ref == f"refs/heads/{branch}" and head:
            found[branch] = (
                f"left by interrupted run {manifest.run_id} at {head}; recover "
                f"its commits with: git branch {branch} {head}"
            )
    return found


def _kept_for_rejudge(manifest: Manifest, root_dir: Path, ui: UI) -> tuple[set[str], list[str]]:
    """The branches ``ks retry`` kept for a re-judge (#646), and a refusal for each that moved.

    A PENDING component with ``rejudge_sha`` keeps its branch when the
    branch is still at that commit, and the stale-branch checks skip it.
    A branch anywhere else, or gone, is refused: the re-judge would judge a
    commit no approval was taken on.
    """
    kept: set[str] = set()
    errors: list[str] = []
    for comp in manifest.components:
        if comp.status != ComponentStatus.PENDING.value or not comp.rejudge_sha:
            continue
        kept.add(comp.branch_name)
        tip = branch_sha(comp.branch_name, root_dir) or ""
        if tip == comp.rejudge_sha:
            ui.info(f"  Keeping branch '{comp.branch_name}' at {tip[:12]}: ks retry kept it")
            continue
        errors.append(
            f"branch '{comp.branch_name}' (component '{comp.id}') is at "
            f"{tip[:12] or 'no commit'}, and ks retry kept it at {comp.rejudge_sha[:12]}; "
            f"refusing to judge a commit no approval was taken on. Run ks retry {comp.id} "
            "again."
        )
    return kept, errors


def _preflight_component_branches(
    manifest: Manifest,
    root_dir: Path,
    ui: UI,
    interrupted: Mapping[str, str],
) -> list[str]:
    """Refuse to silently reuse component branches from previous runs.

    For every branch a PENDING component would be provisioned on: if it
    already exists and is fully merged into the base branch, delete it
    (setup recreates it from base); if it exists with unmerged commits,
    return an error naming it - the caller refuses the run and the
    operator decides (merge or ``git branch -D``). Previously such
    branches were silently reused with their old commits via the
    worktree-add fallback (R0.5, H-7).

    #460: the one other branch deleted rather than refused is one the
    interrupted run that last held this manifest provably made
    (``interrupted``, from ``_interrupted_run_branches``). The deletion
    line names the commit, so the attempt's work stays recoverable.

    Note: a squash-merged branch is NOT an ancestor of base (the squash
    rewrites history), so leftovers from squash-merge flows are refused
    rather than auto-deleted. Loud beats lossy.
    """
    fetch_base_branch(manifest.base_branch, root_dir, timeout=60.0)
    base_ref = resolve_base_ref(manifest.base_branch, root_dir)
    # #646: a branch ks retry kept is neither deleted nor refused below.
    seen, errors = _kept_for_rejudge(manifest, root_dir, ui)
    for comp in manifest.components:
        if comp.status != ComponentStatus.PENDING.value:
            continue
        branch = comp.branch_name
        if branch in seen:
            continue  # single_pr: all components share one branch
        seen.add(branch)
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )
        if exists.returncode != 0:
            continue
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base_ref],
            cwd=root_dir,
            capture_output=True,
            timeout=30,
        )
        if merged.returncode != 0 and branch not in interrupted:
            errors.append(
                f"branch '{branch}' (component '{comp.id}') already exists "
                f"with commits not merged into '{manifest.base_branch}'; "
                f"refusing to silently reuse it. Merge it or delete it "
                f"(git branch -D {branch}) and re-run."
            )
            continue
        why = (
            f"fully merged into {manifest.base_branch}"
            if merged.returncode == 0
            else interrupted[branch]
        )
        deleted = subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=root_dir,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        if deleted.returncode == 0:
            ui.info(f"  Deleted stale branch '{branch}' from a previous run ({why})")
        else:
            errors.append(
                f"stale branch '{branch}' (component '{comp.id}', {why}) could "
                f"not be deleted: {deleted.stderr.strip()}"
            )
    return errors


def _cleanup_pass_worktrees(
    manifest: Manifest,
    manifest_path: Path,
    root_dir: Path,
    run_id: str,
    factory_config: FactoryConfig,
    ui: UI,
    run_state: RunState,
    pipeline: ComponentPipeline,
    leaked_component_ids: set[str],
) -> None:
    """Remove component worktrees left behind by a scheduling pass.

    With ``keep_worktrees_on_failure`` (R3.3), a FAILED component's
    worktree is kept and recorded as its evidence pointer instead of
    removed, so the failure summary can point the operator at it.
    """
    if not factory_config.use_worktrees:
        return
    ui.section("Factory: Cleanup")
    for comp_id in run_state.worktree_paths:
        if comp_id in leaked_component_ids:
            # A possibly-live worker still owns this worktree; removing
            # it under the worker risks corrupting the main repo's
            # worktree metadata.
            ui.warn(f"  Keeping worktree for '{comp_id}' (leaked worker may still be running)")
            continue
        comp = manifest.get_component(comp_id)
        if (
            factory_config.keep_worktrees_on_failure
            and comp is not None
            and comp.status == ComponentStatus.FAILED.value
        ):
            comp.evidence_worktree = str(run_state.worktree_paths[comp_id])
            ui.info(
                f"  Keeping failed worktree for post-mortem: {run_state.worktree_paths[comp_id]}"
            )
            pipeline.record_worktree_sweep(
                comp_id,
                sweep_worktree(run_state.worktree_paths[comp_id]),
                "cleanup",
            )
            continue
        pipeline.record_worktree_sweep(
            comp_id,
            _cleanup_worktree(comp_id, root_dir, run_id),
            "cleanup",
        )
    manifest.save(manifest_path)
    # Drop the run's now-empty worktree dir; leaked workers' and
    # failed components' kept worktrees leave it non-empty and it
    # stays for the next run's prune pass (which preserves recorded
    # evidence worktrees of still-FAILED components).
    try:
        os.rmdir(root_dir / ".kstrl" / "worktrees" / run_id)
    except OSError:
        pass
    ui.ok("Worktrees cleaned up")
