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

from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import git
from kstrl.base_gates import measure_base_gates, reading_document, refusal_lines, warning_lines
from kstrl.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN, DoctorCheck, _not_evaluated
from kstrl.factory import FactoryConfig
from kstrl.verify import VerifyConfig

if TYPE_CHECKING:
    from kstrl.ui.base import UI

#: The row's name in the report.
CHECK_NAME = "base_gates"

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
