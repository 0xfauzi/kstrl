"""Read the prompt records of a run (the reader of ``kstrl.agents.prompt_record``).

kstrl writes the records and no kstrl code reads them (#863), so the
reader lives with the tests that examine the records.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.agents.prompt_record import _FIELDS, AGENT_CLIS, RECORD_SCHEMA, prompts_dir
from kstrl.jsonread import read_json


@dataclass(frozen=True)
class PromptRecord:
    """One call's record, as :func:`read_prompt_records` returns it."""

    run_id: str
    kstrl_version: str
    component: str
    role: str
    attempt: int
    call: int
    agent_cli: str
    prompt: str


class PromptRecordError(ValueError):
    """A record the reader refuses: malformed, or not the identity asked for."""


def read_prompt_records(run_root: Path, *, run_id: str, component: str) -> list[PromptRecord]:
    """Every record for ``component`` in the run at ``run_root``, in call order.

    Refuses with :class:`PromptRecordError` a file that does not parse, a
    field that is missing or mistyped, a schema or ``agent_cli`` it does
    not know, a ``run_id`` or ``component`` other than the one asked for,
    and a file whose name disagrees with its own role, attempt and call.
    A component with no directory has no records: ``[]``. An unreadable
    directory or file raises ``OSError``.
    """
    directory = prompts_dir(run_root, component)
    if not directory.is_dir():
        return []
    records = [_read_one(path, run_id, component) for path in sorted(directory.glob("*.json"))]
    return sorted(records, key=lambda r: (r.role, r.attempt, r.call))


def _read_one(path: Path, run_id: str, component: str) -> PromptRecord:
    raw = path.read_bytes()
    try:
        payload: Any = read_json(raw)
    except ValueError as exc:
        raise PromptRecordError(f"{path}: not a JSON document: {exc}") from exc
    if not isinstance(payload, dict):
        raise PromptRecordError(f"{path}: expected an object, got {type(payload).__name__}")
    for name, kind in _FIELDS.items():
        value = payload.get(name)
        # bool is an int subclass; a record that says `"attempt": true` is malformed.
        if not isinstance(value, kind) or isinstance(value, bool):
            raise PromptRecordError(
                f"{path}: field {name!r} must be {kind.__name__}, got {value!r}"
            )
    if payload["schema"] != RECORD_SCHEMA:
        raise PromptRecordError(f"{path}: schema {payload['schema']} is not {RECORD_SCHEMA}")
    if payload["agent_cli"] not in AGENT_CLIS:
        raise PromptRecordError(f"{path}: unknown agent_cli {payload['agent_cli']!r}")
    if payload["run_id"] != run_id or payload["component"] != component:
        raise PromptRecordError(
            f"{path}: records run {payload['run_id']!r} component {payload['component']!r}, "
            f"not run {run_id!r} component {component!r}"
        )
    expected_name = f"{payload['role']}-a{payload['attempt']}-c{payload['call']}.json"
    if path.name != expected_name:
        raise PromptRecordError(f"{path}: the record inside names {expected_name}")
    return PromptRecord(
        run_id=payload["run_id"],
        kstrl_version=payload["kstrl_version"],
        component=payload["component"],
        role=payload["role"],
        attempt=payload["attempt"],
        call=payload["call"],
        agent_cli=payload["agent_cli"],
        prompt=payload["prompt"],
    )
