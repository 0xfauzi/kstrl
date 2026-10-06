"""The lines an acceptance head record is shown as (#700 slices 4 and 6).

:func:`render_lines` is the one renderer of a record: the terminal prints
its lines, and the pull request's ``## Acceptance`` section is the same
lines (:func:`pr_section`). When a person approved a halt that covers the
failing checks on this head (owner decision 14), the last line names the
approval, who gave it and when.

:func:`told_lines` is what an engineer's retry is told about the checks
that did not pass, under ``context.ACCEPTANCE_RETRY_PROMPT``. A held-out
check is its row line alone, which names its id and nothing it runs or
printed (owner decision 3).
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kstrl.acceptance import FAIL, PASS, RECORD_FILE, Check, evidence_dir
from kstrl.jsonread import read_json
from kstrl.rung import HOST_LABEL

#: Under the header of a record whose checks a model wrote (#700 slice 7).
DESIGNED_LINE = (
    "- the verification designer wrote these checks: record only, they gate nothing "
    "until the acceptance floors are set (owner decision 10)"
)


def render_lines(record: Mapping[str, Any]) -> list[str]:
    """The lines a head record is shown as, on the terminal and in the PR
    body alike."""
    isolation = record["isolation"].get("test", HOST_LABEL)
    base = f", {record['base']}" if record["base"] else ""
    lines = [
        f"Acceptance for {record['component']}, attempt {record['attempt']}: "
        f"plan {record['planId'][:12]}, head {record['headSha'][:12]}{base}; {isolation}"
    ]
    if record.get("writtenBy") == "designer":
        lines.append(DESIGNED_LINE)
    replay = record["replay"]
    if replay["error"] or replay["failed"]:
        lines.append(f"- the head replay stopped: {replay['error'] or replay['detail']}")
    lines += [row_line(row, record["headRuns"]) for row in record["checks"]]
    override = record.get("override")
    if override:
        failing = ", ".join(row["id"] for row in record["checks"] if row["verdict"] != PASS)
        lines.append(
            f"- merged over the failing checks {failing} by inbox approval "
            f"{override['item'][:8]} ({override['by']} at {override['at']})"
        )
    return lines


def row_line(row: Mapping[str, Any], runs: int) -> str:
    passed = sum(1 for code in row["headExits"] if code == 0)
    if row["verdict"] == PASS:
        said = f"passed {passed} of {runs} runs"
    elif row["verdict"] == FAIL:
        said = f"passed {passed} of {runs} runs -> fail"
    else:
        said = "did not run (" + ", ".join(str(code) for code in row["headExits"]) + ")"
    base = "no base exit" if row["baseExit"] is None else f"base exit {row['baseExit']}"
    return f"- {row['id']} ({row['kind']}): {said}; {base}"


def told_lines(
    record: Mapping[str, Any], checks: Sequence[Check], tails: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    """What a retry tells the engineer: where the head replay stopped, when
    it stopped, then each check that did not pass, and for a visible one
    its criterion, its command and what its last run printed."""
    replay = record["replay"]
    told = [f"- the head replay stopped: {replay['detail']}"] if replay["failed"] else []
    visible = {check.id: check for check in checks if not check.held_out}
    for row in record["checks"]:
        if row["verdict"] == PASS:
            continue
        told.append(row_line(row, record["headRuns"]))
        check = visible.get(row["id"])
        if check is not None:
            told += [f"  criterion: {check.criterion}", f"  command: {shlex.join(check.argv)}"]
            told += [f"  | {line}" for line in tails.get(check.id, ())]
    return tuple(told)


def pr_section(root: Path | None, run_id: str, comp_id: str) -> list[str]:
    """The ``## Acceptance`` section of a component's PR body: the lines of
    its latest head record, or [] when this run judged none (or the caller
    named no root)."""
    if root is None or not run_id:
        return []
    attempts = evidence_dir(root, run_id) / comp_id
    if not attempts.is_dir():
        return []
    numbered = [
        (int(path.name.removeprefix("attempt-")), path)
        for path in attempts.iterdir()
        if path.name.removeprefix("attempt-").isdigit()
    ]
    if not numbered:
        return ["## Acceptance", "", f"No acceptance record was written under {attempts}.", ""]
    path = max(numbered)[1] / RECORD_FILE
    try:
        record = read_json(path.read_text(encoding="utf-8"))
        lines = render_lines(record)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        lines = [f"The acceptance record {path} cannot be read: {exc}"]
    return ["## Acceptance", "", *lines, ""]
