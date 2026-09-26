"""The manifest's component key vocabulary, and the plan id rule (#568).

Split out of ``kstrl/manifest.py`` when #568 took that file past the
800-line ratchet. ``Manifest.validate_schema`` refuses a component with a
key outside these two sets, so a new field is added here or every
manifest carrying it is refused.
"""

from __future__ import annotations

from typing import Any

from kstrl.names import COMPONENT_ID_PATTERN, validate_component_id

COMPONENT_REQUIRED_KEYS = frozenset(
    {"id", "title", "description", "dependencies", "prdPath", "branchName"}
)

COMPONENT_OPTIONAL_KEYS = frozenset(
    {
        "status",
        "error",
        "planId",
        "retries",
        "firstAttempt",
        "prNumber",
        "prUrl",
        "mergeSha",
        "linearIssueId",
        "linearIssueIdentifier",
        "startedAt",
        "completedAt",
        "durationSeconds",
        "iterationCount",
        "verificationPassed",
        "reviewPassed",
        "reviewFindings",
        "findings",
        "scaffold",
        "failedPhase",
        "failedCheck",
        "evidenceWorktree",
        "evidenceDebugDir",
        "journalOffsetStart",
        "journalOffsetEnd",
    }
)


def plan_id_errors(comp: dict[str, Any], prefix: str) -> list[str]:
    """``planId`` is a path segment (``statedir.plan_prd_path``), so it is
    held to the component id rule; "" (or absent) is a component with no
    plan."""
    plan_id = comp.get("planId", "")
    if not isinstance(plan_id, str):
        return [f"{prefix}.planId: must be a string"]
    if plan_id and validate_component_id(plan_id) is not None:
        return [
            f"{prefix}.planId: {plan_id!r} is invalid: a plan id must match "
            f"{COMPONENT_ID_PATTERN} and contain no '..'"
        ]
    return []
