"""Manifest for factory orchestration - DAG, component state, topological sort."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ralph_py.findings import Finding


def _iso_now() -> str:
    """Current UTC time as ISO 8601, matching the factory's timestamps."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# R0.6 input hygiene: component ids and branch names are LLM-emitted
# (architect output) and flow into filesystem paths
# (.ralph/worktrees/<id>, scripts/ralph/feature/<id>) and git argv
# (git worktree add, git push -u origin <branch>). Both are validated
# against conservative allowlists at every parse boundary. Rejection is
# deliberate - silent sanitizing would hide architect drift.
COMPONENT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_COMPONENT_ID_RE = re.compile(COMPONENT_ID_PATTERN)

# ASCII allowlist for branch names. Anything outside it (whitespace,
# ':', control characters, unicode dash confusables like U+2011) is
# rejected wholesale rather than enumerated.
_BRANCH_CHARSET_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_BRANCH_NAME_LENGTH = 200


def validate_component_id(comp_id: str) -> str | None:
    """Validate a component id, returning an error message or None.

    Component ids become path segments and branch segments, so the rules
    are strict: lowercase alphanumeric start, then letters/digits/./_/-
    only (max 64 chars total), no '..' sequence, no '.'/'.lock' suffix
    ('<id>.lock' would collide with the worktree lock file for id
    '<id>', and git refuses refs ending in '.' or '.lock').

    Error messages state the rule so the decompose retry loop can feed
    them back to the architect verbatim.
    """
    if not comp_id:
        return "component id must be a non-empty string"
    if not _COMPONENT_ID_RE.match(comp_id):
        return (
            f"component id {comp_id!r} is invalid: ids must match "
            f"{COMPONENT_ID_PATTERN} - start with a lowercase letter or "
            "digit, contain only lowercase letters, digits, '.', '_', "
            "'-', and be at most 64 characters (no '/', no spaces, no "
            "uppercase, ASCII only); e.g. 'auth-service'"
        )
    if ".." in comp_id:
        return f"component id {comp_id!r} is invalid: '..' is not allowed"
    if comp_id.endswith("."):
        return f"component id {comp_id!r} is invalid: must not end with '.'"
    if comp_id.endswith(".lock"):
        return f"component id {comp_id!r} is invalid: must not end with '.lock'"
    return None


def validate_branch_name(branch: str) -> str | None:
    """Validate a git branch name, returning an error message or None.

    Branch names reach git argv in ref position (git push, git worktree
    add, git merge). The rules reject option injection (leading '-'),
    traversal ('..'), whitespace, ':', and unicode lookalikes via an
    ASCII allowlist, while accepting the ralph/factory/<id> pattern and
    ordinary user branches.
    """
    if not branch:
        return "branch name must be a non-empty string"
    if len(branch) > MAX_BRANCH_NAME_LENGTH:
        return (
            f"branch name is too long ({len(branch)} chars, max "
            f"{MAX_BRANCH_NAME_LENGTH})"
        )
    if not _BRANCH_CHARSET_RE.match(branch):
        return (
            f"branch name {branch!r} contains disallowed characters: only "
            "ASCII letters, digits, '.', '_', '/', '-' are allowed "
            "(no whitespace, no ':', no non-ASCII characters)"
        )
    if branch.startswith("-"):
        return (
            f"branch name {branch!r} must not start with '-' "
            "(git would parse it as a command-line option)"
        )
    if ".." in branch:
        return f"branch name {branch!r} must not contain '..'"
    if branch.startswith("/") or branch.endswith("/") or "//" in branch:
        return (
            f"branch name {branch!r} must not have empty path segments "
            "(leading '/', trailing '/', or '//')"
        )
    if any(seg.startswith(".") for seg in branch.split("/")):
        return (
            f"branch name {branch!r} must not have a path segment "
            "starting with '.'"
        )
    if branch.endswith("."):
        return f"branch name {branch!r} must not end with '.'"
    if branch.endswith(".lock"):
        return f"branch name {branch!r} must not end with '.lock'"
    return None


class ComponentStatus(StrEnum):
    """Component execution states."""

    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    # R0.2: the component's PR was created and a merge was initiated,
    # but wait_for_merge could not confirm the merge within the timeout.
    # Not a failure: crash recovery re-polls the PR state on the next
    # run. Dependents are NOT scheduled past it (get_ready_components
    # requires COMPLETED), because they would build without the
    # dependency's merged code (CRIT-2).
    MERGE_PENDING = "merge_pending"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Component:
    """A single component in the factory manifest."""

    id: str
    title: str
    description: str
    dependencies: list[str]
    prd_path: str
    branch_name: str
    status: str = ComponentStatus.PENDING.value
    error: str = ""
    retries: int = 0
    pr_number: int | None = None
    pr_url: str = ""
    # R7.4: Linear issue mapping stamped by the decompose hook. The
    # UUID is the mutation target for the sink; the human identifier
    # (e.g. EXC-42) rides branch names and the PR "Fixes" trailer so
    # Linear's GitHub integration drives status with zero API calls.
    # Persisted here so retries and resumed runs UPDATE the same issue
    # instead of duplicating it.
    linear_issue_id: str = ""
    linear_issue_identifier: str = ""
    # Observability fields
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    iteration_count: int = 0
    # Verification/review results
    verification_passed: bool | None = None
    review_passed: bool | None = None
    # E3: source-of-truth typed findings from review + security roles.
    # Populated by factory.run_one via ReviewResult.as_findings() and
    # SecurityResult.as_findings(). The rendered string ``review_findings``
    # below is a derived view kept for backward compat with downstream
    # consumers (PR body, manifest.json readers).
    findings: list[Finding] = field(default_factory=list)
    review_findings: str = ""
    # Optional scaffold script to run before the agent
    scaffold: str = ""
    # R3.3 post-mortem fields. failed_phase/failed_check name where the
    # last failure happened (phase = engineer/verify/review/security/pr/
    # contract/..., check = the specific gate within it). The evidence
    # pointers say where the last attempt's artifacts live:
    # evidence_worktree is the kept worktree path ("" when removed),
    # evidence_debug_dir the .ralph/debug/<run>/<comp> raw-output dir,
    # and the journal offsets bracket the attempt's slice of the
    # progress log (byte offsets; -1 = not recorded).
    failed_phase: str = ""
    failed_check: str = ""
    evidence_worktree: str = ""
    evidence_debug_dir: str = ""
    journal_offset_start: int = -1
    journal_offset_end: int = -1


@dataclass
class Manifest:
    """Factory manifest describing the component DAG."""

    version: str
    spec_file: str
    project_name: str
    base_branch: str
    single_pr: bool
    components: list[Component] = field(default_factory=list)
    # R3.3: id of the factory run that last operated on this manifest,
    # and when that run finished ("" while a run is in flight). Lets a
    # resume - and later the Linear integration - correlate manifest
    # state with the journals keyed by run_id.
    run_id: str = ""
    completed_at: str = ""
    # R7.4: Linear project mapping. linear_sync_key is the run id of
    # the run that first synced this manifest to Linear - the seed for
    # every derived idempotency UUID - and deliberately survives the
    # per-run rewrite of run_id above.
    linear_project_id: str = ""
    linear_sync_key: str = ""

    @classmethod
    def from_prd(
        cls,
        prd_path: Path,
        branch: str,
        project_name: str = "",
        base_branch: str = "main",
    ) -> Manifest:
        """Create a single-component manifest from an existing PRD.

        Used by ``ralph run`` to delegate to the factory pipeline.
        If *project_name* is not given, it is derived from the branch name
        (e.g. ``ralph/auth`` becomes ``auth``) or the PRD file stem.
        """
        rel_prd = str(prd_path)

        # Derive a meaningful project name when not provided
        if not project_name:
            if branch:
                # "ralph/auth-service" -> "auth-service"
                project_name = branch.rsplit("/", 1)[-1]
            else:
                project_name = prd_path.stem or "run"

        effective_branch = branch or f"ralph/{project_name}"
        branch_error = validate_branch_name(effective_branch)
        if branch_error:
            raise ValueError(f"Invalid branch name for run: {branch_error}")
        base_error = validate_branch_name(base_branch)
        if base_error:
            raise ValueError(f"Invalid base branch for run: {base_error}")

        comp = Component(
            id="main",
            title=project_name,
            description="Single-component run via ralph run",
            dependencies=[],
            prd_path=rel_prd,
            branch_name=effective_branch,
            status=ComponentStatus.PENDING.value,
        )

        return cls(
            version="1",
            spec_file="",
            project_name=project_name,
            base_branch=base_branch,
            single_pr=False,
            components=[comp],
        )

    @classmethod
    def load(cls, path: Path) -> Manifest:
        """Load manifest from JSON file."""
        with open(path) as f:
            data = json.load(f)

        errors = cls.validate_schema(data)
        if errors:
            raise ValueError(f"Invalid manifest schema: {'; '.join(errors)}")

        components = [
            Component(
                id=c["id"],
                title=c["title"],
                description=c["description"],
                dependencies=c["dependencies"],
                prd_path=c["prdPath"],
                branch_name=c["branchName"],
                status=c.get("status", ComponentStatus.PENDING.value),
                error=c.get("error", ""),
                retries=c.get("retries", 0),
                pr_number=c.get("prNumber"),
                pr_url=c.get("prUrl", ""),
                linear_issue_id=c.get("linearIssueId", ""),
                linear_issue_identifier=c.get("linearIssueIdentifier", ""),
                started_at=c.get("startedAt", ""),
                completed_at=c.get("completedAt", ""),
                duration_seconds=c.get("durationSeconds", 0.0),
                iteration_count=c.get("iterationCount", 0),
                verification_passed=c.get("verificationPassed"),
                review_passed=c.get("reviewPassed"),
                findings=[
                    Finding.from_dict(d)
                    for d in c.get("findings", [])
                    if isinstance(d, dict)
                ],
                review_findings=c.get("reviewFindings", ""),
                scaffold=c.get("scaffold", ""),
                failed_phase=c.get("failedPhase", ""),
                failed_check=c.get("failedCheck", ""),
                evidence_worktree=c.get("evidenceWorktree", ""),
                evidence_debug_dir=c.get("evidenceDebugDir", ""),
                journal_offset_start=c.get("journalOffsetStart", -1),
                journal_offset_end=c.get("journalOffsetEnd", -1),
            )
            for c in data["components"]
        ]

        return cls(
            version=data["version"],
            spec_file=data["specFile"],
            project_name=data["projectName"],
            base_branch=data["baseBranch"],
            single_pr=data["singlePr"],
            components=components,
            run_id=data.get("runId", ""),
            completed_at=data.get("completedAt", ""),
            linear_project_id=data.get("linearProjectId", ""),
            linear_sync_key=data.get("linearSyncKey", ""),
        )

    def save(self, path: Path) -> None:
        """Save manifest to JSON file with atomic write."""
        data = {
            "version": self.version,
            "specFile": self.spec_file,
            "projectName": self.project_name,
            "baseBranch": self.base_branch,
            "singlePr": self.single_pr,
            "runId": self.run_id,
            "completedAt": self.completed_at,
            "linearProjectId": self.linear_project_id,
            "linearSyncKey": self.linear_sync_key,
            "components": [
                {
                    "id": c.id,
                    "title": c.title,
                    "description": c.description,
                    "dependencies": c.dependencies,
                    "prdPath": c.prd_path,
                    "branchName": c.branch_name,
                    "status": c.status,
                    "error": c.error,
                    "retries": c.retries,
                    "prNumber": c.pr_number,
                    "prUrl": c.pr_url,
                    "linearIssueId": c.linear_issue_id,
                    "linearIssueIdentifier": c.linear_issue_identifier,
                    "startedAt": c.started_at,
                    "completedAt": c.completed_at,
                    "durationSeconds": c.duration_seconds,
                    "iterationCount": c.iteration_count,
                    "verificationPassed": c.verification_passed,
                    "reviewPassed": c.review_passed,
                    "findings": [f.to_dict() for f in c.findings],
                    "reviewFindings": c.review_findings,
                    "scaffold": c.scaffold,
                    "failedPhase": c.failed_phase,
                    "failedCheck": c.failed_check,
                    "evidenceWorktree": c.evidence_worktree,
                    "evidenceDebugDir": c.evidence_debug_dir,
                    "journalOffsetStart": c.journal_offset_start,
                    "journalOffsetEnd": c.journal_offset_end,
                }
                for c in self.components
            ],
        }

        path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: temp file in same directory then os.replace
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=".manifest-"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @classmethod
    def validate_schema(cls, data: Any) -> list[str]:
        """Validate manifest JSON schema, returning list of errors."""
        errors: list[str] = []

        if not isinstance(data, dict):
            errors.append("Manifest must be a JSON object")
            return errors

        required_keys = {
            "version", "specFile", "projectName", "baseBranch", "singlePr", "components",
        }
        actual_keys = set(data.keys())

        missing = required_keys - actual_keys
        if missing:
            errors.append(f"Missing required keys: {', '.join(sorted(missing))}")
            return errors

        if not isinstance(data.get("version"), str):
            errors.append("version must be a string")
        if not isinstance(data.get("specFile"), str):
            errors.append("specFile must be a string")
        if not isinstance(data.get("projectName"), str):
            errors.append("projectName must be a string")
        elif not data["projectName"]:
            errors.append("projectName must be non-empty")
        if not isinstance(data.get("baseBranch"), str):
            errors.append("baseBranch must be a string")
        else:
            base_error = validate_branch_name(data["baseBranch"])
            if base_error:
                errors.append(f"baseBranch: {base_error}")
        if not isinstance(data.get("singlePr"), bool):
            errors.append("singlePr must be a boolean")
        if "runId" in data and not isinstance(data["runId"], str):
            errors.append("runId must be a string")
        if "completedAt" in data and not isinstance(data["completedAt"], str):
            errors.append("completedAt must be a string")

        components = data.get("components")
        if not isinstance(components, list):
            errors.append("components must be an array")
            return errors

        component_required = {"id", "title", "description", "dependencies", "prdPath", "branchName"}
        component_optional = {
            "status", "error", "retries", "prNumber", "prUrl",
            "linearIssueId", "linearIssueIdentifier",
            "startedAt", "completedAt", "durationSeconds", "iterationCount",
            "verificationPassed", "reviewPassed", "reviewFindings",
            "findings", "scaffold",
            "failedPhase", "failedCheck", "evidenceWorktree",
            "evidenceDebugDir", "journalOffsetStart", "journalOffsetEnd",
        }
        component_all = component_required | component_optional

        for i, comp in enumerate(components):
            prefix = f"components[{i}]"

            if not isinstance(comp, dict):
                errors.append(f"{prefix}: must be an object")
                continue

            comp_keys = set(comp.keys())
            comp_missing = component_required - comp_keys
            comp_extra = comp_keys - component_all
            if comp_missing:
                errors.append(f"{prefix}: missing keys: {', '.join(sorted(comp_missing))}")
                continue
            if comp_extra:
                errors.append(f"{prefix}: unexpected keys: {', '.join(sorted(comp_extra))}")
                continue

            if not isinstance(comp.get("id"), str):
                errors.append(f"{prefix}.id: must be a string")
            else:
                id_error = validate_component_id(comp["id"])
                if id_error:
                    errors.append(f"{prefix}.id: {id_error}")
            if not isinstance(comp.get("title"), str):
                errors.append(f"{prefix}.title: must be a string")
            if not isinstance(comp.get("description"), str):
                errors.append(f"{prefix}.description: must be a string")
            if not isinstance(comp.get("dependencies"), list):
                errors.append(f"{prefix}.dependencies: must be an array")
            elif not all(isinstance(d, str) for d in comp["dependencies"]):
                errors.append(f"{prefix}.dependencies: all items must be strings")
            if not isinstance(comp.get("prdPath"), str):
                errors.append(f"{prefix}.prdPath: must be a string")
            if not isinstance(comp.get("branchName"), str):
                errors.append(f"{prefix}.branchName: must be a string")
            else:
                branch_error = validate_branch_name(comp["branchName"])
                if branch_error:
                    errors.append(f"{prefix}.branchName: {branch_error}")

        return errors

    def validate_dag(self) -> list[str]:
        """Validate the dependency graph, returning list of errors.

        Checks:
        - All dependency references point to existing component IDs
        - No cycles (via Kahn's algorithm)
        - No duplicate component IDs
        """
        errors: list[str] = []

        ids = [c.id for c in self.components]
        id_set = set(ids)

        # Check for duplicates
        if len(ids) != len(id_set):
            seen: set[str] = set()
            for cid in ids:
                if cid in seen:
                    errors.append(f"Duplicate component ID: {cid}")
                seen.add(cid)

        # Check all dependencies reference existing IDs
        for comp in self.components:
            for dep in comp.dependencies:
                if dep not in id_set:
                    errors.append(
                        f"Component '{comp.id}' depends on unknown component '{dep}'"
                    )

        if errors:
            return errors

        # Cycle detection via Kahn's algorithm
        in_degree: dict[str, int] = {c.id: 0 for c in self.components}
        adj: dict[str, list[str]] = {c.id: [] for c in self.components}
        for comp in self.components:
            for dep in comp.dependencies:
                adj[dep].append(comp.id)
                in_degree[comp.id] += 1

        queue: deque[str] = deque()
        for cid, degree in in_degree.items():
            if degree == 0:
                queue.append(cid)

        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited != len(self.components):
            errors.append("Dependency cycle detected in component graph")

        return errors

    def topological_order(self) -> list[str]:
        """Return component IDs in topological order.

        Raises ValueError if the graph contains cycles.
        """
        dag_errors = self.validate_dag()
        cycle_errors = [e for e in dag_errors if "cycle" in e.lower()]
        if cycle_errors:
            raise ValueError(cycle_errors[0])

        in_degree: dict[str, int] = {c.id: 0 for c in self.components}
        adj: dict[str, list[str]] = {c.id: [] for c in self.components}
        for comp in self.components:
            for dep in comp.dependencies:
                adj[dep].append(comp.id)
                in_degree[comp.id] += 1

        queue: deque[str] = deque()
        for cid, degree in in_degree.items():
            if degree == 0:
                queue.append(cid)

        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for neighbor in sorted(adj[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return order

    def get_component(self, component_id: str) -> Component | None:
        """Look up a component by ID."""
        for comp in self.components:
            if comp.id == component_id:
                return comp
        return None

    def get_ready_components(self) -> list[Component]:
        """Return components that are ready to execute.

        A component is ready when:
        - Its status is PENDING
        - All its dependencies have status COMPLETED
        """
        completed = {
            c.id for c in self.components if c.status == ComponentStatus.COMPLETED.value
        }
        ready = []
        for comp in self.components:
            if comp.status != ComponentStatus.PENDING.value:
                continue
            if all(dep in completed for dep in comp.dependencies):
                ready.append(comp)
        return ready

    def cascade_skip(self, failed_id: str) -> list[str]:
        """Mark all transitive dependents of a failed component as SKIPPED.

        Returns list of skipped component IDs.
        """
        # Build reverse dependency map (who depends on whom)
        dependents: dict[str, list[str]] = {c.id: [] for c in self.components}
        for comp in self.components:
            for dep in comp.dependencies:
                if dep in dependents:
                    dependents[dep].append(comp.id)

        # BFS from failed component
        skipped: list[str] = []
        bfs_queue: deque[str] = deque()
        bfs_queue.append(failed_id)
        visited: set[str] = {failed_id}

        while bfs_queue:
            current = bfs_queue.popleft()
            for dependent_id in dependents.get(current, []):
                if dependent_id in visited:
                    continue
                visited.add(dependent_id)
                dep_comp = self.get_component(dependent_id)
                if dep_comp is not None and dep_comp.status not in (
                    ComponentStatus.COMPLETED.value,
                    ComponentStatus.SKIPPED.value,
                ):
                    dep_comp.status = ComponentStatus.SKIPPED.value
                    dep_comp.error = f"Dependency '{failed_id}' failed"
                    # R3.3: SKIPPED is terminal for this run.
                    dep_comp.completed_at = _iso_now()
                    skipped.append(dependent_id)
                    bfs_queue.append(dependent_id)

        return skipped

    def _transitive_dependents(self, component_id: str) -> set[str]:
        """All components that transitively depend on *component_id*."""
        dependents: dict[str, list[str]] = {c.id: [] for c in self.components}
        for comp in self.components:
            for dep in comp.dependencies:
                if dep in dependents:
                    dependents[dep].append(comp.id)
        found: set[str] = set()
        queue: deque[str] = deque([component_id])
        while queue:
            current = queue.popleft()
            for dependent_id in dependents.get(current, []):
                if dependent_id not in found:
                    found.add(dependent_id)
                    queue.append(dependent_id)
        return found

    def reset_for_retry(self, component_id: str) -> list[str]:
        """Reset a FAILED component and its cascade-SKIPPED dependents to
        PENDING so a factory re-run schedules them again (R3.3).

        A SKIPPED dependent is only reset when every one of its
        dependencies will be runnable afterwards: COMPLETED, PENDING, or
        itself part of this reset. A dependent that was also skipped
        because of a DIFFERENT still-failed component stays SKIPPED -
        resetting it would leave it permanently unschedulable.

        Returns the ids of the reset dependents. Raises ValueError when
        the component does not exist or is not FAILED.
        """
        comp = self.get_component(component_id)
        if comp is None:
            raise ValueError(f"Unknown component '{component_id}'")
        if comp.status != ComponentStatus.FAILED.value:
            raise ValueError(
                f"Component '{component_id}' is '{comp.status}', not "
                f"'{ComponentStatus.FAILED.value}'; only failed components "
                f"can be retried"
            )

        dependents = self._transitive_dependents(component_id)
        reset_ids = {component_id}
        runnable = {
            ComponentStatus.COMPLETED.value,
            ComponentStatus.PENDING.value,
        }
        reset_dependents: list[str] = []
        for cid in self.topological_order():
            if cid not in dependents:
                continue
            dep_comp = self.get_component(cid)
            if dep_comp is None or dep_comp.status != ComponentStatus.SKIPPED.value:
                continue
            if all(
                dep in reset_ids
                or (
                    (d := self.get_component(dep)) is not None
                    and d.status in runnable
                )
                for dep in dep_comp.dependencies
            ):
                reset_ids.add(cid)
                reset_dependents.append(cid)

        for cid in [component_id, *reset_dependents]:
            target = self.get_component(cid)
            assert target is not None
            target.status = ComponentStatus.PENDING.value
            target.error = ""
            target.retries = 0
            target.started_at = ""
            target.completed_at = ""
            target.duration_seconds = 0.0
            target.iteration_count = 0
            target.pr_number = None
            target.pr_url = ""
            target.verification_passed = None
            target.review_passed = None
            # Findings from the failed attempt are already in the
            # evolution journal (record_run wrote them, attempt-tagged,
            # when the failed run finished); the fresh attempt starts
            # with a clean stream.
            target.findings = []
            target.review_findings = ""
            target.failed_phase = ""
            target.failed_check = ""
            target.evidence_worktree = ""
            target.evidence_debug_dir = ""
            target.journal_offset_start = -1
            target.journal_offset_end = -1

        return reset_dependents

    def compute_tiers(self) -> list[list[str]]:
        """Compute DAG tier levels using Kahn's algorithm with level tracking.

        Tier 0: components with no dependencies.
        Tier N: components whose dependencies are all in tiers < N.

        Returns list of tiers, each tier is a list of component IDs.
        Raises ValueError if the graph contains cycles.
        """
        dag_errors = self.validate_dag()
        cycle_errors = [e for e in dag_errors if "cycle" in e.lower()]
        if cycle_errors:
            raise ValueError(cycle_errors[0])

        in_degree: dict[str, int] = {c.id: 0 for c in self.components}
        adj: dict[str, list[str]] = {c.id: [] for c in self.components}
        for comp in self.components:
            for dep in comp.dependencies:
                adj[dep].append(comp.id)
                in_degree[comp.id] += 1

        tiers: list[list[str]] = []
        current_tier = [cid for cid, deg in in_degree.items() if deg == 0]

        while current_tier:
            tiers.append(sorted(current_tier))
            next_tier: list[str] = []
            for node in current_tier:
                for neighbor in adj[node]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        next_tier.append(neighbor)
            current_tier = next_tier

        return tiers
