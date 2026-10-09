"""The head record of a component's acceptance checks, and the engineer's
dispute of a check it can see (#700 slices 4, 6 and 10a).

:func:`head_record` is the document :func:`kstrl.acceptance.judge_head`
writes as ``record.json`` for each head. Two of its keys come from here:

- ``visibleFailures`` (:func:`visible_failures`): each visible check that
  did not pass, with its argv and what its last failed run printed. A
  held-out check is never in it: its argv and output go to no engineer and
  into no file of the repository (decision 3), so neither a retry nor a
  halt text nor a PR body can show them.
- ``dispute`` (:func:`read_dispute`): what the engineer disputes. A retry
  shows the engineer each visible check that did not pass. When it is sure
  that such a check is incorrect or impossible, ``DEFAULT_PROMPT`` tells it
  to write one line in its progress entry, in :data:`DISPUTE_FORM`, and not
  to change code only to make the check pass (owner decision 13). A
  dispute is accepted when it names a visible check of this component that
  did not pass on this head; the pipeline then halts the component as a
  failed held-out check halts it, and a person decides through the halt's
  approval (decision 14). Every other line whose first word is "dispute"
  is rejected with its reason, and the check result stands. A dispute
  never passes a check.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kstrl.acceptance import (
    HEAD_RUNS,
    HELD_OUT_READ_DENIED,
    BaseReading,
    Check,
    PinnedPlan,
    _entry_sha,
    head_verdict,
)
from kstrl.replay import Replay, Stage
from kstrl.verify import latest_entry

#: The one form of a dispute line, which DEFAULT_PROMPT gives the engineer.
DISPUTE_FORM = "Dispute: <check-id>: <cause>"

#: A line that sets out to be a dispute: its first word, after any list
#: marker, heading hashes or bold marks, is "dispute". It is read in the
#: form below or rejected, never passed over.
_DISPUTE_WORD = re.compile(r"^[\s#*>-]*dispute\b", re.IGNORECASE)

#: :data:`DISPUTE_FORM`, after an optional "- " list marker.
_DISPUTE = re.compile(r"^(?:- )?Dispute: (?P<id>[^\s:]+): (?P<cause>\S.*)$")


def head_record(
    plan: PinnedPlan,
    base: BaseReading,
    record: Replay,
    comp_id: str,
    head: tuple[str, int, str],
    runs: Mapping[tuple[str, str], list[Stage]],
    evidence: Path,
) -> dict[str, Any]:
    """The ``record.json`` of one head run of ``comp_id``'s checks: what
    each check exited on each run, and what the run was judged under."""
    head_sha, attempt, run_id = head
    part = base.kept[comp_id]
    rows = []
    for check in part.checks:
        stages = runs.get((comp_id, check.id), [])
        exits = [stage.exit for stage in stages]
        logs = [evidence / "logs" / f"{check.id}-{run}.log" for run in range(1, len(stages) + 1)]
        rows.append(
            {
                "id": check.id,
                "sha256": check.sha256,
                "kind": "held out" if check.held_out else "visible",
                "heldOut": check.held_out,
                "baseExit": base.exits[comp_id][check.id],
                "headExits": exits,
                "seconds": [stage.seconds for stage in stages],
                "logSha256": [_file_sha(log) for log in logs],
                "verdict": head_verdict(exits),
            }
        )
    return {
        "run": run_id,
        "component": comp_id,
        "attempt": attempt,
        "planId": plan.digest,
        "writtenBy": "designer" if plan.designed else "operator",
        "stackDigest": record.stack_digest,
        "checksDigest": _entry_sha({"checks": [check.sha256 for check in part.checks]}),
        "baseSha": base.sha,
        "headSha": head_sha,
        "isolation": record.isolation,
        "base": base.said.get(comp_id, ""),
        "headRuns": HEAD_RUNS,
        "heldOutReadDenied": HELD_OUT_READ_DENIED,
        "replay": {"failed": record.failed, "detail": record.detail, "error": record.error},
        "checks": rows,
        "removed": [c.id for c in plan.components[comp_id].checks if c not in part.checks],
    }


def _file_sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def visible_failures(
    comp_id: str,
    checks: Sequence[Check],
    failing: Sequence[str],
    runs: Mapping[tuple[str, str], list[Stage]],
) -> list[dict[str, Any]]:
    """Each visible check in ``failing``: its id, its argv and what its last
    run that did not exit 0 printed. Never a held-out check (decision 3)."""
    return [
        {
            "id": check.id,
            "argv": list(check.argv),
            "tail": next(
                (
                    list(stage.tail)
                    for stage in reversed(runs.get((comp_id, check.id), []))
                    if stage.exit != 0
                ),
                [],
            ),
        }
        for check in checks
        if not check.held_out and check.id in failing
    ]


def read_dispute(
    progress: Path | None, checks: Sequence[Check], failing: Sequence[str]
) -> dict[str, list[Any]]:
    """The disputes in the latest entry of the progress log ``progress``:
    ``accepted``, each ``{"id", "cause"}``, and ``rejected``, each the
    reason one line was not taken. No log, or no line whose first word is
    "dispute", is no dispute. A log that cannot be read is a rejection, so
    nothing it holds is taken."""
    if progress is None:
        return {"accepted": [], "rejected": []}
    try:
        text = progress.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"accepted": [], "rejected": []}
    except (OSError, ValueError) as exc:
        # A UnicodeDecodeError is a ValueError.
        return {"accepted": [], "rejected": [f"the progress log {progress} cannot be read: {exc}"]}
    lines = text.splitlines()
    start = latest_entry(lines) or 0
    by_id = {check.id: check for check in checks}
    accepted: list[dict[str, str]] = []
    rejected: list[str] = []
    for number, line in enumerate(lines[start:], start + 1):
        if not _DISPUTE_WORD.match(line):
            continue
        found = _DISPUTE.match(line.strip())
        taken = [entry["id"] for entry in accepted]
        reason = _refusal(line.strip(), found, by_id, failing, taken)
        if reason:
            rejected.append(f"line {number} of {progress.name}: {reason}")
        elif found is not None:
            accepted.append({"id": found["id"], "cause": found["cause"].strip()})
    return {"accepted": accepted, "rejected": rejected}


def _refusal(
    line: str,
    found: re.Match[str] | None,
    by_id: Mapping[str, Check],
    failing: Sequence[str],
    taken: Sequence[str],
) -> str:
    """Why one dispute line is not taken, or "" when it is."""
    if found is None:
        return f"{line!r} is not in the form {DISPUTE_FORM}"
    name = found["id"]
    check = by_id.get(name)
    if check is None:
        return f"{name} is not an acceptance check of this component"
    if check.held_out:
        return f"{name} is held out, and only a check the engineer can see can be disputed"
    if name not in failing:
        return f"{name} passed on this head, so there is nothing to dispute"
    if name in taken:
        return f"{name} is disputed on an earlier line"
    return ""
