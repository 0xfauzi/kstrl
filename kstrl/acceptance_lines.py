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
from typing import TYPE_CHECKING, Any

from kstrl.acceptance import FAIL, PASS, RECORD_FILE, Check, HeadOutcome, evidence_dir
from kstrl.jsonread import read_json
from kstrl.rung import HOST_LABEL, label_of

if TYPE_CHECKING:
    from kstrl.factory import FactoryConfig

#: Under the header of a record whose checks a model wrote (#700 slice 7).
DESIGNED_LINE = "- the verification designer wrote these checks"

#: In a record whose checks a model wrote, when a check did not pass: what
#: the halt text and the PR body say (#700 owner decision 13, part 3).
DESIGNED_STOP_LINE = (
    "- the verification designer wrote these checks, so a check that did not pass "
    "can be incorrect, or the code can be"
)


def engineer_line(harness: str) -> str:
    """Whether the engineer ran confined, and by which harness's sandbox
    (#700, owner decision 2026-10-09): one line of a head record and of a
    PR body's ``## Isolation`` section."""
    if harness:
        return f"- the engineer ran in the sandbox of {harness}"
    return "- the engineer ran with no sandbox"


def pr_isolation(config: FactoryConfig) -> str:
    """A PR body's ``## Isolation`` text: the label the checks ran under,
    then :func:`engineer_line`."""
    return f"{label_of(config.test_rung)}\n{engineer_line(config.engineer_sandbox)}"


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
    if "engineerSandbox" in record:
        lines.append(engineer_line(record["engineerSandbox"]))
    replay = record["replay"]
    if replay["error"] or replay["failed"]:
        lines.append(f"- the head replay stopped: {replay['error'] or replay['detail']}")
    lines += [row_line(row, record["headRuns"]) for row in record["checks"]]
    lines += stop_lines(record) + rejected_lines(record)
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


def told_lines(record: Mapping[str, Any], checks: Sequence[Check]) -> tuple[str, ...]:
    """What a retry tells the engineer: where the head replay stopped, when
    it stopped, then each check that did not pass, and for a visible one
    its criterion, its command and what its last failed run printed; then
    each dispute line kstrl rejected, and why."""
    replay = record["replay"]
    told = [f"- the head replay stopped: {replay['detail']}"] if replay["failed"] else []
    visible = {check.id: check for check in checks if not check.held_out}
    shown = {entry["id"]: entry for entry in record["visibleFailures"]}
    for row in record["checks"]:
        if row["verdict"] == PASS:
            continue
        told.append(row_line(row, record["headRuns"]))
        check = visible.get(row["id"])
        if check is not None:
            told += [f"  criterion: {check.criterion}", f"  command: {shlex.join(check.argv)}"]
            told += [f"  | {line}" for line in shown[check.id]["tail"]]
    return (*told, *rejected_lines(record))


def stop_lines(record: Mapping[str, Any]) -> list[str]:
    """What a halt says after its cause, and what the terminal and the PR
    body repeat: each check the engineer disputes, with its cause, argv and
    output (owner decision 13, part 2); then, when a model wrote the checks
    and one did not pass, that the check or the code can be incorrect, and
    the argv and output of each other visible check that did not pass (part
    3). A held-out check is never shown here (decision 3)."""
    shown = {entry["id"]: entry for entry in record["visibleFailures"]}
    lines: list[str] = []
    for entry in record["dispute"]["accepted"]:
        lines.append(f"- the engineer disputes {entry['id']}: {entry['cause']}")
        lines += _shown(shown[entry["id"]])
    failed = any(row["verdict"] != PASS for row in record["checks"])
    if record.get("writtenBy") == "designer" and failed:
        disputed = {entry["id"] for entry in record["dispute"]["accepted"]}
        lines.append(DESIGNED_STOP_LINE)
        for entry in record["visibleFailures"]:
            lines += [] if entry["id"] in disputed else [f"- {entry['id']}:", *_shown(entry)]
    return lines


def _shown(entry: Mapping[str, Any]) -> list[str]:
    return [f"  argv: {shlex.join(entry['argv'])}", *(f"  | {line}" for line in entry["tail"])]


def rejected_lines(record: Mapping[str, Any]) -> list[str]:
    """Each dispute line kstrl did not take, and why: the check result stands."""
    return [f"- dispute rejected: {reason}" for reason in record["dispute"]["rejected"]]


def halt_text(outcome: HeadOutcome, head: str, comp_id: str) -> str:
    """The text of a halt with no retry (owner decisions 3, 13 and 14): its
    cause, the approval that merges over it, then :func:`stop_lines`. A
    failed held-out check is the cause before a dispute, and the stop lines
    name the dispute, so a dispute never hides a held-out failure from the
    person who approves. With no cause to name, the record's own lines are
    the cause, and they already hold the stop lines."""
    stopped = list(outcome.stopped)
    if outcome.held_out:
        said = f"the held-out acceptance checks {', '.join(outcome.held_out)} failed"
    elif outcome.disputed:
        said = f"the engineer disputes the acceptance checks {', '.join(outcome.disputed)}"
    else:
        said, stopped = "; ".join(outcome.lines), []
    first = (
        f"{said} on {head[:12]}; halted with no retry. Approving this halt and then "
        f"`ks retry {comp_id}` merges over the failing checks on that commit"
    )
    return "\n".join([first, *stopped])


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
