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
from kstrl.integration import FINDING_KINDS, FINDING_STATUSES, OpenedFinding
from kstrl.jsonread import read_json
from kstrl.manifest import Manifest
from kstrl.version import kstrl_version

STATE_SCHEMA_VERSION = 1
FINDING_ID_PREFIX = "IF-"
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
    return errors


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
    locations = entry.get("locations")
    if not isinstance(locations, list) or not all(isinstance(p, str) for p in locations):
        errors.append(f"{prefix}.locations must be a list of strings")
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
    }
    if replaced is not None:
        state["replacedBinding"] = replaced
    return state


def has_fix_component(manifest: Manifest) -> bool:
    return any(c.id.startswith(FIX_COMPONENT_PREFIX) for c in manifest.components)


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
                "history": [
                    {
                        "runId": run_id,
                        "kstrlVersion": kstrl_version(),
                        "reviewedSha": reviewed_sha,
                        "event": "opened",
                    }
                ],
            }
        )
    state["lastReviewedSha"] = reviewed_sha


def add_stop(
    state: dict[str, Any],
    run_id: str,
    sha: str,
    outcome: str,
    reason: str,
    evidence: Path | None,
) -> None:
    state["stops"].append(
        {
            "runId": run_id,
            "kstrlVersion": kstrl_version(),
            "at": _now(),
            "reviewedSha": sha,
            "outcome": outcome,
            "reason": reason,
            "gates": False,
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
