"""The integration review's durable state file and evidence files (#482)."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl import events as ev
from kstrl.atomicio import atomic_write_json
from kstrl.decisions import enum_field_error, required_field_error
from kstrl.integration import (
    FINDING_ID_PREFIX,
    FINDING_KINDS,
    FINDING_STATUSES,
    STATUS_CLOSED,
    STATUS_HANDOFF,
    STATUS_OPEN,
    OpenedFinding,
)
from kstrl.jsonread import read_json
from kstrl.manifest import Component, Manifest
from kstrl.version import kstrl_version

STATE_SCHEMA_VERSION = 1
FIX_COMPONENT_PREFIX = "integration-fix-"
BINDING_KEYS = ("manifestPath", "project", "specFile", "featureBaseSha")
OUTCOME_CLEAN = "clean"
OUTCOME_OPEN_FINDINGS = "open_findings"
OUTCOME_RED = "red"
OUTCOME_NOT_RUN = "not_run"
STOP_OUTCOMES = (OUTCOME_CLEAN, OUTCOME_OPEN_FINDINGS, OUTCOME_RED, OUTCOME_NOT_RUN)
STATE_MISSING = "missing"
STATE_UNREADABLE = "unreadable"
STATE_OK = "ok"


def state_path(root_dir: Path) -> Path:
    return root_dir / ".kstrl" / "integration" / "state.json"


@dataclass(frozen=True)
class StateRead:
    status: str
    detail: str = ""
    data: dict[str, Any] | None = None


def read_state(root_dir: Path) -> StateRead:
    path = state_path(root_dir)
    # The I/O outside the parse guard, the launch_record.py shape.
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return StateRead(STATE_MISSING, f"no state at {path}")
    except OSError as exc:
        return StateRead(STATE_UNREADABLE, f"{path}: {exc}")
    try:
        data = read_json(raw)
    except ValueError as exc:
        return StateRead(STATE_UNREADABLE, f"{path}: {exc}")
    errors = state_payload_errors(data)
    if errors:
        return StateRead(STATE_UNREADABLE, f"{path}: {'; '.join(errors[:3])}")
    return StateRead(STATE_OK, "", data)


def state_payload_errors(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["state must be a JSON object"]
    errors: list[str] = []
    if data.get("schemaVersion") != STATE_SCHEMA_VERSION:
        errors.append(f"schemaVersion must be {STATE_SCHEMA_VERSION}")
    for key in (*BINDING_KEYS, "lastReviewedSha"):
        if not isinstance(data.get(key), str):
            errors.append(f"{key} must be a string")
    errors.extend(_list_errors(data, "findings", _finding_errors))
    errors.extend(_list_errors(data, "stops", _stop_errors))
    # #483: optional, because a state file slice 2 wrote has no fixes. The
    # reconcile in integration_fix refuses a fix component with no entry.
    if "fixes" in data:
        errors.extend(_list_errors(data, "fixes", _fix_errors))
    return errors


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _list_errors(data: dict[str, Any], key: str, check: Any) -> list[str]:
    entries = data.get(key)
    if not isinstance(entries, list):
        return [f"{key} must be a list"]
    errors: list[str] = []
    for index, entry in enumerate(entries):
        errors.extend(check(index, entry))
    return errors


def _finding_errors(index: int, entry: Any) -> list[str]:
    prefix = f"findings[{index}]"
    if not isinstance(entry, dict):
        return [f"{prefix} must be an object"]
    found = [
        required_field_error(prefix, "id", entry.get("id")),
        enum_field_error(prefix, "status", entry.get("status"), FINDING_STATUSES),
        enum_field_error(prefix, "kind", entry.get("kind"), FINDING_KINDS),
    ]
    errors = [error for error in found if error is not None]
    if not _string_list(entry.get("locations")):
        errors.append(f"{prefix}.locations must be a list of strings")
    return errors


def _fix_errors(index: int, entry: Any) -> list[str]:
    prefix = f"fixes[{index}]"
    if not isinstance(entry, dict):
        return [f"{prefix} must be an object"]
    found = [
        required_field_error(prefix, "id", entry.get("id")),
        required_field_error(prefix, "prdPath", entry.get("prdPath")),
    ]
    errors = [error for error in found if error is not None]
    for key in ("findings", "scope"):
        if not _string_list(entry.get(key)):
            errors.append(f"{prefix}.{key} must be a list of strings")
    return errors


def _stop_errors(index: int, entry: Any) -> list[str]:
    prefix = f"stops[{index}]"
    if not isinstance(entry, dict):
        return [f"{prefix} must be an object"]
    error = enum_field_error(prefix, "outcome", entry.get("outcome"), STOP_OUTCOMES)
    return [] if error is None else [error]


def state_binding(manifest: Manifest, manifest_path: Path) -> dict[str, str]:
    return {
        "manifestPath": str(manifest_path.resolve()),
        "project": manifest.project_name,
        "specFile": manifest.spec_file,
        "featureBaseSha": manifest.feature_base_sha,
    }


def fresh_state(binding: dict[str, str], replaced: dict[str, str] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        **binding,
        "lastReviewedSha": "",
        "findings": [],
        "stops": [],
        "fixes": [],
    }
    if replaced is not None:
        state["replacedBinding"] = replaced
    return state


def fix_components(manifest: Manifest) -> list[Component]:
    """The integration fix components, in the order they were appended (#483).
    Their count is the loop's round count R: derived, never stored."""
    return [c for c in manifest.components if c.id.startswith(FIX_COMPONENT_PREFIX)]


def has_fix_component(manifest: Manifest) -> bool:
    return bool(fix_components(manifest))


@dataclass(frozen=True)
class BoundState:
    state: dict[str, Any] | None
    refusal: str = ""


def bind_state(read: StateRead, binding: dict[str, str], fix_component: bool) -> BoundState:
    """Decision 5 of the #482 plan: which state this round may write, or why none."""
    if read.status == STATE_MISSING:
        if fix_component:
            return BoundState(
                None,
                f"integration state is missing ({read.detail}) but the manifest holds an "
                f"{FIX_COMPONENT_PREFIX}* component",
            )
        return BoundState(fresh_state(binding))
    if read.status == STATE_UNREADABLE or read.data is None:
        return BoundState(
            None,
            f"integration state is unreadable: {read.detail}. Move it aside to start a new record",
        )
    old = {key: read.data[key] for key in BINDING_KEYS}
    if old == binding:
        return BoundState(read.data)
    if fix_component:
        return BoundState(
            None, f"integration state belongs to {old}, not to this feature {binding}"
        )
    return BoundState(fresh_state(binding, replaced=old))


def next_finding_ids(state: dict[str, Any], count: int) -> list[str]:
    numbers = [0]
    for finding in state["findings"]:
        tail = str(finding.get("id", "")).removeprefix(FINDING_ID_PREFIX)
        if tail.isdigit():
            numbers.append(int(tail))
    start = max(numbers) + 1
    return [f"{FINDING_ID_PREFIX}{start + offset}" for offset in range(count)]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _history(run_id: str, reviewed_sha: str, event: str) -> dict[str, str]:
    return {
        "runId": run_id,
        "kstrlVersion": kstrl_version(),
        "reviewedSha": reviewed_sha,
        "event": event,
    }


def add_findings(
    state: dict[str, Any],
    run_id: str,
    reviewed_sha: str,
    pairs: Sequence[tuple[str, OpenedFinding]],
) -> None:
    for finding_id, finding in pairs:
        state["findings"].append(
            {
                "id": finding_id,
                "status": finding.status,
                "kind": finding.kind,
                "storyId": finding.story_id,
                "category": finding.category,
                "text": finding.text,
                "suggestion": finding.suggestion,
                "locations": list(finding.locations),
                "missingLocations": list(finding.missing_locations),
                "history": [_history(run_id, reviewed_sha, "opened")],
            }
        )
    state["lastReviewedSha"] = reviewed_sha


def carried_findings(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The open findings the last built fix carried (#483): this round's
    IF stories. Empty before any fix."""
    fixes = state.get("fixes", [])
    if not fixes:
        return []
    carried = set(fixes[-1]["findings"])
    return [f for f in state["findings"] if f["id"] in carried and f["status"] == STATUS_OPEN]


def close_findings(
    state: dict[str, Any], finding_ids: Sequence[str], run_id: str, reviewed_sha: str
) -> None:
    for finding in state["findings"]:
        if finding["id"] in finding_ids:
            finding["status"] = STATUS_CLOSED
            finding["history"].append(_history(run_id, reviewed_sha, "closed"))


def hand_off(
    state: dict[str, Any], finding_id: str, reason: str, run_id: str, reviewed_sha: str
) -> None:
    for finding in state["findings"]:
        if finding["id"] == finding_id:
            finding["status"] = STATUS_HANDOFF
            finding["handoffReason"] = reason
            finding["history"].append(_history(run_id, reviewed_sha, "handed_off"))


def add_fix(
    state: dict[str, Any],
    component_id: str,
    finding_ids: Sequence[str],
    scope: Sequence[str],
    prd_path: str,
    run_id: str,
    reviewed_sha: str,
) -> None:
    """Stage 1 of the fix (design 3.4): the decision, recorded before any file
    the fix needs exists."""
    state.setdefault("fixes", []).append(
        {
            "id": component_id,
            "findings": list(finding_ids),
            "scope": list(scope),
            "prdPath": prd_path,
            "runId": run_id,
            "kstrlVersion": kstrl_version(),
            "reviewedSha": reviewed_sha,
        }
    )


def add_stop(
    state: dict[str, Any],
    run_id: str,
    sha: str,
    outcome: str,
    reason: str,
    evidence: Path | None,
    *,
    gates: bool = False,
) -> None:
    state["stops"].append(
        {
            "runId": run_id,
            "kstrlVersion": kstrl_version(),
            "at": _now(),
            "reviewedSha": sha,
            "outcome": outcome,
            "reason": reason,
            "gates": gates,
            "evidence": str(evidence) if evidence else "",
        }
    )


def write_state(root_dir: Path, state: dict[str, Any]) -> str:
    path = state_path(root_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, state)
    except OSError as exc:
        return str(exc)
    return ""


def evidence_dir(root_dir: Path, run_id: str) -> Path:
    return ev.RunPaths.for_run(root_dir, run_id).root / "integration"


def next_review_number(directory: Path) -> int:
    return 1 + len(list(directory.glob("review-*.json")))


def write_evidence(
    directory: Path, number: int, payload: dict[str, Any]
) -> tuple[Path | None, str]:
    path = directory / f"review-{number}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload)
    except OSError as exc:
        return None, str(exc)
    return path, ""
