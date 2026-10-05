"""The manifest's key vocabulary, the plan id rule (#568) and the spec pin rule (#639).

Split out of ``kstrl/manifest.py`` when #568 took that file past the
800-line ratchet, and again when #639 did. ``Manifest.validate_schema``
refuses a component with a key outside the two component sets, so a new
field is added here or every manifest carrying it is refused.
"""

from __future__ import annotations

import re
from typing import Any

from kstrl.names import COMPONENT_ID_PATTERN, validate_branch_name, validate_component_id

MANIFEST_REQUIRED_KEYS = frozenset(
    {"version", "specFile", "projectName", "baseBranch", "singlePr", "components"}
)

#: Optional top-level keys that must be strings when present.
MANIFEST_OPTIONAL_STRING_KEYS = (
    "runId",
    "completedAt",
    "policyHash",
    "kstrlVersion",
    "featureBaseSha",
    "planAwaitingApproval",
    "specPath",
)

#: ``specDigest`` is the sha256 of the spec text a plan was made from
#: (#639). Only an ABSENT key is a manifest from before the pin: "" or
#: any other non-digest is refused, so blanking the pin cannot turn the
#: staleness check off.
SPEC_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")

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
        "judgedSha",
        "rejudgeSha",
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


#: ``rejudgeSha``: "" or a full commit id, sha1 or sha256 (#646).
REJUDGE_SHA_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})?")


def rejudge_sha_errors(comp: dict[str, Any], prefix: str) -> list[str]:
    """``rejudgeSha`` is the commit ``ks retry`` kept for a re-judge (#646).

    Anything but "" or a full lowercase commit id is refused: a value the
    factory cannot compare to a branch tip must not reach the preflight
    that lets a kept branch through.
    """
    value = comp.get("rejudgeSha", "")
    if not isinstance(value, str) or REJUDGE_SHA_PATTERN.fullmatch(value) is None:
        return [f'{prefix}.rejudgeSha: must be "" or a full commit id, got {value!r}']
    return []


def manifest_top_level_errors(data: Any) -> list[str]:
    """Everything wrong with a manifest outside its ``components`` entries."""
    if not isinstance(data, dict):
        return ["Manifest must be a JSON object"]
    missing = MANIFEST_REQUIRED_KEYS - set(data.keys())
    if missing:
        return [f"Missing required keys: {', '.join(sorted(missing))}"]
    errors: list[str] = []
    for key in ("version", "specFile", "projectName", "baseBranch"):
        if not isinstance(data[key], str):
            errors.append(f"{key} must be a string")
    if isinstance(data["projectName"], str) and not data["projectName"]:
        errors.append("projectName must be non-empty")
    if isinstance(data["baseBranch"], str):
        base_error = validate_branch_name(data["baseBranch"])
        if base_error:
            errors.append(f"baseBranch: {base_error}")
    if not isinstance(data["singlePr"], bool):
        errors.append("singlePr must be a boolean")
    errors.extend(
        f"{key} must be a string"
        for key in MANIFEST_OPTIONAL_STRING_KEYS
        if key in data and not isinstance(data[key], str)
    )
    return errors + spec_pin_schema_errors(data)


def spec_pin_schema_errors(data: dict[str, Any]) -> list[str]:
    """``specDigest``, when present, is 64 lowercase hex and names a ``specPath`` (#639)."""
    if "specDigest" not in data:
        return []
    digest = data["specDigest"]
    if not isinstance(digest, str) or SPEC_DIGEST_PATTERN.fullmatch(digest) is None:
        return [f"specDigest must be 64 lowercase hex characters (a sha256), got {digest!r}"]
    if not data.get("specPath"):
        return ["specPath must be a non-empty string when specDigest is set"]
    return []
