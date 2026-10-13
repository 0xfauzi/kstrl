"""Plan part of a decomposition: Linear sync, branch names, PRD and manifest write."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.decompose_reply import (
    _component_branch,
    _generate_component_prd,
    _prd_summary_line,
    _routed_prd_issues,
)
from kstrl.event_catalog import ArtifactWritten, Event
from kstrl.io import mint_run_id, plan_prd_path
from kstrl.linear import (
    DecomposeSync,
    LinearClient,
    LinearConfig,
    linear_branch_name,
    sync_decompose,
)
from kstrl.manifest import (
    Component,
    ComponentStatus,
    Manifest,
)
from kstrl.names import validate_branch_name

if TYPE_CHECKING:
    from kstrl.factory_lock import RunLock
    from kstrl.spec_issues import SpecIssue
    from kstrl.ui import UI


def _sync_linear(
    data: dict[str, Any],
    spec_issues: list[SpecIssue],
    *,
    ui: UI,
    root_dir: Path,
    project_name: str,
    single_pr: bool,
) -> DecomposeSync | None:
    """Sync the plan to Linear when it is enabled and give back the sync result."""
    # R7.4: Linear hook - one project per manifest, one issue per
    # component, non-blocker spec findings into Triage. Runs BEFORE
    # branch derivation so issue identifiers can ride the branch names
    # (Linear's GitHub integration links PRs by identifier-in-branch,
    # so status transitions cost zero API calls). The hook never
    # raises; any failure warns and decompose proceeds without Linear.
    linear_config = LinearConfig.load(root_dir)
    linear_sync = None
    if linear_config.enabled:
        ui.section("Syncing to Linear")
        linear_sync = sync_decompose(
            project_name=project_name,
            components=data["components"],
            spec_issues=spec_issues,
            config=linear_config,
            client=LinearClient(linear_config, warn=ui.warn, log=ui.info),
            warn=ui.warn,
        )
        if linear_sync is not None:
            ui.ok(f"Linear project created with {len(linear_sync.issues)} component issue(s)")
        if linear_sync is not None and single_pr:
            # All components share one branch in single-PR mode, so a
            # per-component identifier cannot ride the branch name.
            ui.warn(
                "linear: single_pr mode - branch names and PR bodies "
                "are not issue-linked; issues and the progress sink "
                "still work"
            )
    return linear_sync


def _component_branches(
    data: dict[str, Any],
    linear_sync: DecomposeSync | None,
    *,
    ui: UI,
    project_name: str,
    single_pr: bool,
) -> dict[str, str]:
    """Derive and validate the git branch of each component."""
    # Pre-validate every branch name before any file is written.
    # Component ids were validated in the retry loop; this can only
    # fire for a project_name (user input) that is not branch-safe.
    # Reject rather than sanitize so the caller sees exactly what
    # was wrong (R0.6).
    component_branches: dict[str, str] = {}
    for comp_data in data["components"]:
        comp_id = comp_data["id"]
        branch = _component_branch(comp_id, project_name, single_pr)
        issue_ref = (
            linear_sync.issues.get(comp_id) if linear_sync is not None and not single_pr else None
        )
        if issue_ref is not None:
            linear_branch = linear_branch_name(issue_ref.identifier, comp_id)
            if validate_branch_name(linear_branch) is None:
                branch = linear_branch
            else:
                # An identifier that breaks branch validation would be
                # a Linear-side surprise; degrade to the default branch
                # (losing auto-linking) rather than failing decompose.
                ui.warn(
                    f"linear: issue identifier "
                    f"'{issue_ref.identifier}' does not form a valid "
                    f"branch; component '{comp_id}' keeps its default "
                    f"branch name"
                )
        branch_error = validate_branch_name(branch)
        if branch_error:
            raise ValueError(
                f"Cannot derive a git branch for component '{comp_id}': {branch_error}"
            )
        component_branches[comp_id] = branch
    return component_branches


def _write_component_prds(
    data: dict[str, Any],
    linear_sync: DecomposeSync | None,
    *,
    ui: UI,
    root_dir: Path,
    component_branches: dict[str, str],
    routed_issues: dict[str, Any],
    plan_id: str,
    manifest_components: list[Component],
    written_prds: list[Path],
    created_dirs: list[Path],
) -> None:
    """Write one PRD file for each component and build its manifest entry.

    ``manifest_components``, ``written_prds`` and ``created_dirs`` are the caller's
    lists, so its cleanup scope sees the files written before a failure.
    """
    for comp_data in data["components"]:
        comp_id = comp_data["id"]
        branch = component_branches[comp_id]

        # Track directories this run creates so cleanup can remove
        # them; pre-existing directories are left alone.
        probe = plan_prd_path(root_dir, comp_id, plan_id=plan_id).parent
        while not probe.exists() and probe != root_dir:
            created_dirs.append(probe)
            probe = probe.parent

        prd_path = _generate_component_prd(
            comp_data,
            root_dir,
            branch,
            routed_issues[comp_id],
            plan_id=plan_id,
        )
        written_prds.append(prd_path)
        # The path the engineer's copy has in its worktree and on
        # the component branch. Nothing writes it in the root
        # checkout (#545).
        rel_prd = f"scripts/kstrl/feature/{comp_id}/prd.json"

        issue_ref = linear_sync.issues.get(comp_id) if linear_sync is not None else None
        manifest_components.append(
            Component(
                id=comp_id,
                title=comp_data["title"],
                description=comp_data["description"],
                dependencies=comp_data.get("dependencies", []),
                prd_path=rel_prd,
                branch_name=branch,
                status=ComponentStatus.PENDING.value,
                plan_id=plan_id,
                linear_issue_id=issue_ref.id if issue_ref else "",
                linear_issue_identifier=(issue_ref.identifier if issue_ref else ""),
            )
        )
        ui.ok(
            _prd_summary_line(
                comp_id,
                len(comp_data["userStories"]),
                routed_issues[comp_id],
            )
        )


def _write_plan(
    data: dict[str, Any],
    linear_sync: DecomposeSync | None,
    component_branches: dict[str, str],
    *,
    ui: UI,
    root_dir: Path,
    project_name: str,
    base_branch: str,
    single_pr: bool,
    spec_path: Path,
    spec_source: str,
    pin: str,
    stack_pin: str,
    run_lock: RunLock | None,
    force_lock: bool,
    commit_register: Callable[[], object],
) -> tuple[Manifest, Path, list[Path]]:
    """Write the PRD files, the manifest and the register, and give back what was written.

    ``commit_register`` writes the register. It is made in ``decompose.py``, so the
    ``write_decisions`` lookup stays in the module that tests patch.
    """
    # Generate PRDs and build manifest components. Everything below has
    # already validated, so a failure here is an I/O problem or a
    # harness bug; either way, remove the files written so far rather
    # than leaving partial decompose state for the next run to trip
    # over (R1.8). The spec-issues artifact is deliberately NOT cleaned
    # up - it is the audit record.
    ui.section("Generating PRDs")
    manifest_components: list[Component] = []
    written_prds: list[Path] = []
    created_dirs: list[Path] = []
    # #260: recomputed from the same payload the retry loop validated,
    # so the specIssues block written below is the one that passed
    # PRD.validate_schema up there.
    routed_issues = _routed_prd_issues(data)
    # #568: one id for this decompose, stamped on every component it puts
    # in the manifest and naming the directory its planned PRDs go in.
    plan_id = mint_run_id("decompose")
    try:
        _write_component_prds(
            data,
            linear_sync,
            ui=ui,
            root_dir=root_dir,
            component_branches=component_branches,
            routed_issues=routed_issues,
            plan_id=plan_id,
            manifest_components=manifest_components,
            written_prds=written_prds,
            created_dirs=created_dirs,
        )

        manifest = Manifest(
            version="1",
            spec_file=spec_path.name,
            project_name=project_name,
            base_branch=base_branch,
            single_pr=single_pr,
            components=manifest_components,
            linear_project_id=(linear_sync.project_id if linear_sync is not None else ""),
            linear_sync_key=(linear_sync.sync_key if linear_sync is not None else ""),
            spec_path=spec_source,
            spec_digest=pin,
            stack_digest=stack_pin,
        )

        # Validate DAG
        dag_errors = manifest.validate_dag()
        if dag_errors:
            ui.warn("DAG validation warnings:")
            for err in dag_errors:
                ui.warn(f"  {err}")

        # Save manifest (atomic write; covered by the cleanup scope so
        # a save failure does not strand PRDs without a manifest)
        manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
        # #597: a live factory run owns this manifest and saves its whole
        # in-memory copy, so the manifest and the register are written under
        # the run lock. `ks factory --spec` already holds it (acquired
        # before this call, ahead of the architect's spend, #597); `ks
        # decompose` has no caller's lock and takes its own here. Either
        # way a held lock raises FactoryLockHeldError inside the cleanup
        # scope, so a refused decompose leaves no PRDs behind.
        from kstrl.factory_lock import held_or_acquired_run_lock

        with held_or_acquired_run_lock(run_lock, root_dir, ui, force_lock):
            manifest.save(manifest_path)
            ui.ok(f"Manifest saved: {manifest_path}")
            # AFTER the manifest, never before: the register binds engineers
            # to this manifest, so it must not exist unless this manifest
            # does. Inside the cleanup scope for the same reason.
            commit_register()
    except BaseException:
        for prd_file in written_prds:
            try:
                prd_file.unlink()
            except OSError:
                pass
        # Deepest-first so children go before parents; rmdir refuses
        # non-empty directories, which protects anything user-owned.
        for created in sorted(set(created_dirs), key=lambda p: len(p.parts), reverse=True):
            try:
                created.rmdir()
            except OSError:
                pass
        raise

    return manifest, manifest_path, written_prds


def _publish_plan(
    data: dict[str, Any],
    manifest: Manifest,
    manifest_path: Path,
    written_prds: list[Path],
    *,
    ui: UI,
    emit: Callable[[Event], None],
    rel_display: Callable[[Path], str],
) -> None:
    """Emit the artifact events of the written plan and print the summary."""
    # Publish transactional artifacts only after the PRD + manifest
    # write set commits. If a later write failed, the cleanup above
    # removed the PRDs and the event stream must not claim they exist.
    for component_data, prd_file in zip(
        data["components"],
        written_prds,
        strict=True,
    ):
        emit(
            ArtifactWritten(
                component=component_data["id"],
                label="prd",
                path=rel_display(prd_file),
            )
        )
    emit(
        ArtifactWritten(
            label="manifest",
            path=rel_display(manifest_path),
        )
    )

    ui.section("Decomposition Summary")
    ui.kv("Components", str(len(manifest.components)))
    total_stories = sum(len(comp_data.get("userStories", [])) for comp_data in data["components"])
    ui.kv("Total stories", str(total_stories))
