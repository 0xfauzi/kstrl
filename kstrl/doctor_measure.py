"""`ks doctor --measure`: Tier B, the base branch's own gates (#654).

Runs the reading `ks factory` takes before any engineer is called
(:func:`kstrl.base_gates.measure_base_gates`: Phase 1's [stack] checks in a
throwaway worktree of the base commit) and turns it into
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
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import git
from kstrl.base_gates import measure_base_gates, reading_document, refusal_lines, warning_lines
from kstrl.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN, DoctorCheck, _not_evaluated
from kstrl.factory import FactoryConfig
from kstrl.isolation import SETUP_ZONE, TEST_ZONE, prove_rung
from kstrl.statedir import control_dir
from kstrl.verify import VerifyConfig

if TYPE_CHECKING:
    from kstrl.ui.base import UI

#: The row's name in the report.
CHECK_NAME = "base_gates"

#: The isolation row's name in the report (#700).
ISOLATION_CHECK_NAME = "isolation"

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
    own rungs and refuses below them (#700 slice 2). The canaries point into a scratch
    directory that is removed afterwards, and the control directory is
    denied to both zones.
    """
    ui.info("Proving the isolation rung with canaries through nono...")
    with tempfile.TemporaryDirectory(prefix="kstrl-rung-") as scratch:
        rungs = []
        for zone in (SETUP_ZONE, TEST_ZONE):
            directory = Path(scratch) / zone
            directory.mkdir()
            rungs.append(prove_rung(root, directory, [control_dir(root)], zone))
    detail = "; ".join(
        f"{rung.zone} zone: {rung.refusal or 'proven: ' + rung.label}" for rung in rungs
    )
    status = STATUS_WARN if any(rung.refusal for rung in rungs) else STATUS_OK
    return (
        DoctorCheck(ISOLATION_CHECK_NAME, status, detail),
        {rung.zone: dataclasses.asdict(rung) for rung in rungs},
    )


def measure_tier_b(
    root: Path, checks: list[DoctorCheck], ui: UI
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Run both Tier B readings, the base gates and the isolation rung,
    append their rows to ``checks`` in that order, and return the two
    readings for the report document."""
    row, reading = measure(root, checks, ui)
    isolation_row, isolation = measure_isolation(root, ui)
    checks.extend((row, isolation_row))
    return reading, isolation
