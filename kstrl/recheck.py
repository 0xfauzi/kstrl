"""``ks recheck <record>``: run an acceptance record's saved checks again
and say whether they agree with it (#700 slice 5).

A head record (:func:`kstrl.acceptance.judge_head`) sits in its attempt
directory beside ``index.json``, the sha256 of every file written with
it, and ``checks/``, the byte copy of the plan its checks ran from. The
recheck takes none of it on trust:

1. every file must hash to what ``index.json`` says, and the index must
   name exactly the files there, so a changed, missing or added file
   refuses, naming it;
2. ``checks/`` must hash to the record's ``planId``, and the ``[stack]``
   in kstrl.toml to its ``stackDigest``;
3. the checks run again from that copy at the recorded head commit,
   ``headRuns`` times each, through the replay the factory ran them in.

A check agrees when the verdict the record states, the verdict of the
exits it records and the verdict of the new exits are the same. The
stated verdict is never read as the answer. Both isolation labels are
printed, the record's and the recheck's, and neither is said to equal
the other.

The index is compared with the files beside it, so a writer who changed
a file and the index together is caught by the plan digest and by the
new run, not by the index.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.acceptance import (
    DESIGNER_FILE,
    INDEX_FILE,
    PLAN_FILE,
    PinnedPlan,
    _digest,
    _no_log,
    _parsed,
    _plan_files,
    _probe,
    evidence_index,
    head_verdict,
    limits,
    plan_errors,
)
from kstrl.jsonread import read_json
from kstrl.replay import Replay, Stage, replay_stack
from kstrl.rung import HOST_LABEL
from kstrl.stack import REPLAY_BOUNDARY_REFUSED

if TYPE_CHECKING:
    from kstrl.factory import FactoryConfig
    from kstrl.stack import Stack
    from kstrl.ui.base import UI

#: What a recheck says: why the record could not be rechecked (when this
#: is not empty, nothing else is said), the lines to print, and whether
#: every verdict agrees with the record.
Said = tuple[list[str], list[str], bool]


def recheck(root: Path, record_path: Path, config: FactoryConfig, ui: UI) -> Said:
    """Recheck the record at ``record_path``. Nothing under the record's
    directory is written."""
    evidence = record_path.parent
    record, errors = _document(record_path)
    errors = errors or _changed_files(evidence)
    if errors:
        return errors, [], False
    try:
        comp, plan_id, head = record["component"], record["planId"], record["headSha"]
        runs, ran_under = int(record["headRuns"]), record["isolation"].get("test", HOST_LABEL)
        stated = {
            row["id"]: (row["verdict"], head_verdict(row["headExits"])) for row in record["checks"]
        }
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
            raise ValueError(f"headSha {head!r} is not a full commit id")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return [f"{record_path} is not an acceptance record: {exc!r}"], [], False
    plan, errors = _saved_plan(evidence, plan_id, comp)
    stack, stack_errors = _stack(config.project_stack, record.get("stackDigest"))
    errors += stack_errors
    if plan is not None and set(stated) != {check.id for check in plan.components[comp].checks}:
        errors.append(f"{record_path} does not name the checks of its plan's {comp}")
    if errors or plan is None or stack is None:
        return errors, [], False
    replay, exits = _run_again(root, config, ui, stack, plan, comp, head, runs)
    if replay.error or replay.failed == REPLAY_BOUNDARY_REFUSED:
        return (
            [f"the checks did not run at {head[:12]}: {replay.error or replay.detail}"],
            [],
            False,
        )
    lines = [
        f"Recheck of {comp}: plan {plan.digest[:12]}, head {head[:12]}, {runs} run(s) per check",
        f"- the record ran under: {ran_under}",
        f"- this recheck ran under: {replay.isolation.get('test', HOST_LABEL)}",
    ]
    if replay.failed:
        lines.append(f"- the head replay stopped: {replay.detail}")
    return [], *_compared(lines, stated, exits)


def _document(path: Path) -> tuple[Any, list[str]]:
    try:
        return read_json(path.read_text(encoding="utf-8")), []
    except (OSError, ValueError) as exc:
        return None, [f"{path} cannot be read: {exc}"]


def _said(sha: object) -> str:
    return f"sha256 {str(sha)[:12]}" if sha else "nothing"


def _changed_files(evidence: Path) -> list[str]:
    """Each file whose sha256 is not the one ``index.json`` gives it,
    including a file only one of the two has."""
    index, errors = _document(evidence / INDEX_FILE)
    if errors:
        return errors
    if not isinstance(index, dict):
        return [f"{evidence / INDEX_FILE} is not an object of file names"]
    try:
        found = evidence_index(evidence)
    except OSError as exc:
        return [f"the evidence under {evidence} cannot be read: {exc}"]
    return [
        f"{evidence / name} reads {_said(found.get(name))}, and {INDEX_FILE} says "
        f"{_said(index.get(name))}"
        for name in sorted(found.keys() | index.keys())
        if found.get(name) != index.get(name)
    ]


def _saved_plan(
    evidence: Path, plan_id: object, comp: object
) -> tuple[PinnedPlan | None, list[str]]:
    """The plan copy the checks ran from, when it is the plan the record
    names and it plans ``comp``."""
    checks = evidence / "checks"
    try:
        found = _digest(_plan_files(checks))
    except OSError as exc:
        return None, [f"the saved checks {checks} cannot be read: {exc}"]
    if found != plan_id:
        return None, [
            f"the saved checks {checks} read {found[:12]}, and the record's plan is "
            f"{str(plan_id)[:12]}"
        ]
    raw, errors = _document(checks / PLAN_FILE)
    components = raw.get("components") if isinstance(raw, dict) else None
    errors = errors or plan_errors(raw, list(components) if isinstance(components, dict) else [])
    if errors:
        return None, [f"{checks / PLAN_FILE}: {line}" for line in errors]
    parsed = _parsed(raw)
    if comp not in parsed:
        return None, [f"the saved plan {checks / PLAN_FILE} has no component {comp!r}"]
    return PinnedPlan(found, checks, parsed, checks, (checks / DESIGNER_FILE).is_file()), []


def _stack(stack: Stack | None, recorded: object) -> tuple[Stack | None, list[str]]:
    """The ``[stack]`` to replay under, when it is the one the record ran under."""
    if stack is None:
        return None, ["kstrl.toml has no [stack], and the checks run only in its replay"]
    if stack.digest != recorded:
        return None, [
            f"the record ran under the [stack] {str(recorded)[:12]}, and kstrl.toml now "
            f"reads {stack.digest[:12]}: a run under another recipe is not a recheck"
        ]
    return stack, []


def _run_again(
    root: Path,
    config: FactoryConfig,
    ui: UI,
    stack: Stack,
    plan: PinnedPlan,
    comp: str,
    head: str,
    runs: int,
) -> tuple[Replay, dict[str, list[int | None]]]:
    """Run ``comp``'s checks ``runs`` times each at ``head``, in the replay
    the factory ran them in, logging nothing: each check's exits, in order."""
    checks = plan.components[comp].checks
    found: dict[tuple[str, str], list[Stage]] = {}
    setup_limit, check_limit = limits(config)
    replay = replay_stack(
        root,
        stack,
        setup_limit=setup_limit,
        check_limit=check_limit,
        ui=ui,
        at=head,
        probe=_probe(plan, [(comp, c) for c in checks], stack, check_limit, found, runs, _no_log),
    )
    return replay, {c.id: [stage.exit for stage in found.get((comp, c.id), [])] for c in checks}


def _compared(
    lines: list[str],
    stated: Mapping[str, tuple[str, str]],
    exits: Mapping[str, list[int | None]],
) -> tuple[list[str], bool]:
    """One line per check, then whether every verdict agrees: the one the
    record states, the one its recorded exits give and the one the new
    exits give."""
    differ = []
    for check_id, found in exits.items():
        record_says, exits_say = stated[check_id]
        now = head_verdict(found)
        shown = ", ".join(str(code) for code in found) or "none"
        lines.append(
            f"- {check_id}: the record says {record_says}, its exits say {exits_say}, "
            f"the recheck says {now} (exits {shown})"
        )
        if not record_says == exits_say == now:
            differ.append(check_id)
    if differ:
        return [*lines, f"The recheck disagrees with the record on {', '.join(differ)}."], False
    return [*lines, "The recheck agrees with the record."], True
