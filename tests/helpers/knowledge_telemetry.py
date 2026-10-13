"""Read the dependency-scope telemetry log that ``kstrl.knowledge`` writes.

No kstrl code reads the log (#863), so its reader lives with the tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kstrl.jsonread import read_json
from kstrl.knowledge import _E8_TELEMETRY_RELATIVE_PATH


def read_dependency_scope_telemetry(knowledge_root: Path) -> list[dict[str, Any]]:
    """Read the JSONL log produced by
    :func:`record_dependency_scope_gap`. Returns ``[]`` when the file
    does not exist or cannot be parsed.

    ONE clause for the open failure and the decode failure, because this
    telemetry reader reports to nobody: both answers are ``[]`` and no
    caller can tell them apart, so two handlers would be a distinction
    with no observer. Sites that DO say something get their own message;
    see :func:`read_facts` above.
    """
    target = knowledge_root / _E8_TELEMETRY_RELATIVE_PATH
    if not target.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(read_json(line))
            except json.JSONDecodeError:
                continue
    except (OSError, UnicodeDecodeError):
        return []
    return out
