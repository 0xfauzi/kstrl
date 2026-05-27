"""Manifest for factory orchestration - DAG, component state, topological sort."""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ralph_py.findings import Finding


class ComponentStatus(str, Enum):
    """Component execution states."""

    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
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


@dataclass
class Manifest:
    """Factory manifest describing the component DAG."""

    version: str
    spec_file: str
    project_name: str
    base_branch: str
    single_pr: bool
    components: list[Component] = field(default_factory=list)

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

        comp = Component(
            id="main",
            title=project_name,
            description="Single-component run via ralph run",
            dependencies=[],
            prd_path=rel_prd,
            branch_name=branch or f"ralph/{project_name}",
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
        )

    def save(self, path: Path) -> None:
        """Save manifest to JSON file with atomic write."""
        data = {
            "version": self.version,
            "specFile": self.spec_file,
            "projectName": self.project_name,
            "baseBranch": self.base_branch,
            "singlePr": self.single_pr,
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
                    "startedAt": c.started_at,
                    "completedAt": c.completed_at,
                    "durationSeconds": c.duration_seconds,
                    "iterationCount": c.iteration_count,
                    "verificationPassed": c.verification_passed,
                    "reviewPassed": c.review_passed,
                    "findings": [f.to_dict() for f in c.findings],
                    "reviewFindings": c.review_findings,
                    "scaffold": c.scaffold,
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
        elif not data["baseBranch"]:
            errors.append("baseBranch must be non-empty")
        if not isinstance(data.get("singlePr"), bool):
            errors.append("singlePr must be a boolean")

        components = data.get("components")
        if not isinstance(components, list):
            errors.append("components must be an array")
            return errors

        component_required = {"id", "title", "description", "dependencies", "prdPath", "branchName"}
        component_optional = {
            "status", "error", "retries", "prNumber", "prUrl",
            "startedAt", "completedAt", "durationSeconds", "iterationCount",
            "verificationPassed", "reviewPassed", "reviewFindings",
            "findings", "scaffold",
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
            elif not comp["id"]:
                errors.append(f"{prefix}.id: must be non-empty")
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
                    skipped.append(dependent_id)
                    bfs_queue.append(dependent_id)

        return skipped

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
