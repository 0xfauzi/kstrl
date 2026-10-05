"""`ks doctor --measure`: Tier B, the base branch's own gates (#654).

Runs the reading `ks factory` takes before any engineer is called
(:func:`kstrl.base_gates.measure_base_gates`: Phase 1's test, typecheck and
lint commands in a throwaway worktree of the base commit) and turns it into
one more doctor row. A gate that measurably fails there is a failed row, so
the verdict is not-ready exactly where `ks factory` would refuse. A gate that
ran and measured nothing (pytest collecting no tests, a timeout) is a
warning, as it is in `ks factory`. A base that could not be measured at all
is a failed row: `ks factory` goes on in that case and Phase 1 judges each
component, but an agent-ready verdict must rest on a reading.

The branch is the one `ks factory --spec`, `ks decompose` and `ks check`
use when none is named (:func:`kstrl.git.detect_base_branch`), so the base
this measures is the one the next plan is cut from.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import git
from kstrl.base_gates import measure_base_gates, reading_document, refusal_lines, warning_lines
from kstrl.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN, DoctorCheck, _not_evaluated
from kstrl.factory import FactoryConfig
from kstrl.isolation import prove_zones
from kstrl.replay import Replay, replay_stack
from kstrl.rung import HostFallback, ProvenRung, Rung, release
from kstrl.stack import (
    REPLAY_BOUNDARY_REFUSED,
    Stack,
    file_stack_item,
    replay_refuses,
    stack_in_force,
)
from kstrl.timeout import limit_seconds
from kstrl.verify import VerifyConfig

if TYPE_CHECKING:
    from kstrl.ui.base import UI

#: The row's name in the report.
CHECK_NAME = "base_gates"

#: The isolation row's name in the report (#700).
ISOLATION_CHECK_NAME = "isolation"

#: The replay row's name in the report (#700 slice 3).
REPLAY_CHECK_NAME = "replay"

#: What to do about a base branch whose gates fail: the escapes the
#: `ks factory` refusal leaves (docs/runbook.md).
BASE_GATES_FIX = (
    "Commit a fix for the failing gate on the base branch, or commit "
    "xfail(strict=True) marks for its failing tests; `ks factory --no-verify` "
    "runs anyway with every Phase 1 gate off."
)


def measure(
    root: Path, checks: list[DoctorCheck], ui: UI
) -> tuple[DoctorCheck, dict[str, Any] | None]:
    """The base_gates row and the reading behind it (None when not measured).

    Not measured on a kstrl.toml that does not load: every command that
    would build on the base refuses on it, and the kstrl_config row
    already says why.
    """
    if any(check.name == "kstrl_config" and check.status == STATUS_FAIL for check in checks):
        return DoctorCheck(CHECK_NAME, *_not_evaluated(CHECK_NAME)), None
    base = git.detect_base_branch(root)
    ui.info(f"Measuring the gates on the base branch {base}...")
    setup = FactoryConfig.load(root).worktree_setup()
    reading = measure_base_gates(root, base, VerifyConfig.load(root), setup, ui)
    reasons, warnings = refusal_lines(reading), warning_lines(reading)
    if reasons or reading.result is None:
        row = (STATUS_FAIL, "; ".join(reasons or warnings), BASE_GATES_FIX if reasons else "")
    elif warnings:
        row = (STATUS_WARN, "; ".join(warnings), "")
    else:
        names = ", ".join(check.name for check in reading.result.checks)
        row = (STATUS_OK, f"{names} pass on {base} at {reading.base_sha[:12]}", "")
    return DoctorCheck(CHECK_NAME, *row), reading_document(reading, reasons)


def measure_isolation(root: Path, ui: UI) -> tuple[DoctorCheck, dict[str, Any]]:
    """The isolation row and the reading behind it: the setup zone and
    the test zone, each proven by canaries through nono, every canary's
    verdict, nono's version and each policy's digest (#700).

    Record only. A refused zone warns and never fails the verdict: this
    reading proves the zones with no ``[stack]`` paths, and the base gates
    above run on the host. ``ks factory`` under a ``[stack]`` proves its
    own rungs and refuses below them (#700 slice 2). The canaries point
    into scratch directories removed afterwards, and the control
    directory is denied to both zones. On a platform with no prover the
    row is the host fallback's label alone, a warning, with no canary
    run (owner decision 2026-10-05).
    """
    ui.info("Reading the isolation rung: canaries through nono where a prover exists...")
    rungs = prove_zones(root, [], [], browser=False)
    release(rungs.values())
    detail = "; ".join(dict.fromkeys(_reading(zone, rung) for zone, rung in rungs.items()))
    proven = all(isinstance(rung, ProvenRung) and not rung.refusal for rung in rungs.values())
    return (
        DoctorCheck(ISOLATION_CHECK_NAME, STATUS_OK if proven else STATUS_WARN, detail),
        {zone: dataclasses.asdict(rung) for zone, rung in rungs.items()},
    )


def _reading(zone: str, rung: Rung) -> str:
    """What the isolation row says about one zone. The host fallback is its
    label alone: it names no zone and claims no proof."""
    if isinstance(rung, HostFallback):
        return rung.label
    return f"{zone} zone: {rung.refusal or 'proven: ' + rung.label}"


def measure_replay(
    root: Path, checks: list[DoctorCheck], ui: UI
) -> tuple[DoctorCheck | None, dict[str, Any] | None]:
    """The replay row and its record, or ``(None, None)`` with no ``[stack]``
    or a kstrl.toml that does not load (#700 slice 3).

    The replay runs whether or not the stack is confirmed: it runs only
    inside the proven rung, in a throwaway worktree, and its record is the
    evidence a person confirms the stack on. It is filed on the stack's
    confirmation item when the stack is not confirmed, and when it failed
    (:func:`kstrl.stack.replay_refuses`), so a stack whose replay failed
    is never confirmed. A refused boundary warns, as the isolation row
    does; any other failure fails the row."""
    if any(check.name == "kstrl_config" and check.status == STATUS_FAIL for check in checks):
        return None, None
    stack = stack_in_force(root)
    if stack is None:
        return None, None
    ui.info("Replaying the [stack] recipe in a throwaway worktree of the base...")
    record = replay_stack(
        root,
        stack,
        setup_limit=limit_seconds(FactoryConfig.load(root).worktree_setup_timeout),
        check_limit=limit_seconds(VerifyConfig.load(root).subprocess_timeout),
        ui=ui,
    )
    document = dataclasses.asdict(record)
    filed = _file_replay(root, stack, record, document)
    if record.failed == REPLAY_BOUNDARY_REFUSED:
        row = (STATUS_WARN, f"{record.failed}: {record.detail}{filed}")
    elif record.failed or record.error:
        said = f"{record.failed}: {record.detail}" if record.failed else record.error
        row = (STATUS_FAIL, f"{said}{filed}")
    else:
        stages = ", ".join(stage.name for stage in record.stages)
        row = (
            STATUS_OK,
            f"the recipe replays on {record.base_branch} at {record.base_sha[:12]} "
            f"({stages}){filed}",
        )
    return DoctorCheck(REPLAY_CHECK_NAME, *row), document


def _file_replay(root: Path, stack: Stack, record: Replay, document: dict[str, Any]) -> str:
    """File ``document`` on the stack's confirmation item when the stack is
    not confirmed or the replay failed; what the row says about it."""
    if not stack.unconfirmed and not replay_refuses(record.failed):
        return ""
    try:
        item = file_stack_item(root, stack, replay=document)
    except Exception as exc:  # noqa: BLE001 - a filing that failed is said, never raised
        return f"; no confirmation item was filed: {exc}"
    return f"; filed on inbox item {item.id[:8]}"


def measure_tier_b(root: Path, checks: list[DoctorCheck], ui: UI) -> dict[str, Any]:
    """Run every Tier B reading, the base gates, the isolation rung and the
    replay of a ``[stack]``, append their rows to ``checks`` in that order,
    and return the readings for the report document by their keys."""
    row, reading = measure(root, checks, ui)
    isolation_row, isolation = measure_isolation(root, ui)
    checks.extend((row, isolation_row))
    replay_row, replay = measure_replay(root, checks, ui)
    if replay_row is not None:
        checks.append(replay_row)
    return {"base_gates": reading, "isolation": isolation, "replay": replay}
