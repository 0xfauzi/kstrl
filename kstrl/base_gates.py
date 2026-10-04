"""The base branch's own gates, measured before any engineer runs (#654).

Phase 1 fails a component on any non-zero exit of the whole suite, so on a
base whose suite is already red every component fails Phase 1 after its
engineer has been paid. Measured with the real ``ks factory`` and a stub
engineer: on a base with one failing test the engineer was called, and Phase
1 then failed ``test_suite`` on a test the engineer never touched.

:func:`measure_base_gates` runs Phase 1's three command gates, with Phase
1's commands, parsers and timeout, in a throwaway worktree of the commit the
base branch names after a fetch. Never in the root checkout: an uncommitted
fix there would read a red base as green. :func:`refusal_lines` turns the
reading into the reasons the run refuses. A gate that measurably failed
refuses. A gate that ran and measured nothing (a timeout, a missing tool,
pytest collecting no tests) is warned about and recorded, never refused:
Phase 1 still fails such a row on every component, and refusing it would
refuse every run on a repository that has no tests yet.

:func:`write_record` writes ``.kstrl/runs/<run_id>/base-gates.json`` for
every reading, a skipped one included. The verdict is taken from the reading
in memory, never from the file read back.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import git
from kstrl.atomicio import atomic_write_json
from kstrl.baseline import verify_digest
from kstrl.contract import ContractCleanupError, _create_temp_worktree, _remove_temp_worktree
from kstrl.events import RunPaths
from kstrl.isolation import HOST_LABEL
from kstrl.verify import (
    FAST_ITERATION_GATES,
    CheckResult,
    VerificationResult,
    VerifyConfig,
    resolve_verify_commands,
    run_fast_checks,
)
from kstrl.version import kstrl_version
from kstrl.worktree_setup import WorktreeSetup

if TYPE_CHECKING:
    from kstrl.ui.base import UI

#: The record's file name inside a run directory, beside ``launch.json``.
BASE_GATES_FILE = "base-gates.json"

#: The label of the throwaway worktree, under ``.kstrl/contract/``.
WORKTREE_LABEL = "base-gates"

#: How many failing test or rule names one refusal line carries.
NAMED_FAILURES = 5


@dataclass(frozen=True)
class BaseGates:
    """One reading of the base branch's gates.

    ``result`` is None when no gate ran: the base did not resolve, its
    checkout failed, its worktree setup failed, or the run skipped the
    measurement. ``error`` says why the base could not be read, or why its
    throwaway worktree survived the reading.
    """

    base_branch: str
    base_sha: str = ""
    digest: str = ""
    setup_command: str = ""
    setup_error: str = ""
    error: str = ""
    result: VerificationResult | None = None
    seconds: float = 0.0


def measure_base_gates(
    root_dir: Path,
    base_branch: str,
    config: VerifyConfig,
    setup: WorktreeSetup,
    ui: UI,
) -> BaseGates:
    """Run Phase 1's test, typecheck and lint gates on the base branch's commit."""
    start = time.monotonic()
    # Non-fatal, as before a component is cut (``factory._setup_worktree``):
    # offline runs read the current tracking ref, local-only repos the branch.
    git.fetch_base_branch(base_branch, root_dir, timeout=60.0)
    try:
        sha = git.resolve_base_sha(base_branch, root_dir)
    except git.GitDiffError as exc:
        return BaseGates(
            base_branch,
            setup_command=setup.command,
            error=f"the base branch {base_branch} was not measured: {exc}",
            seconds=time.monotonic() - start,
        )
    worktree, checkout_error = _create_temp_worktree(sha, root_dir, WORKTREE_LABEL)
    if worktree is None:
        return BaseGates(
            base_branch,
            sha,
            setup_command=setup.command,
            error=f"the base {sha[:12]} was not measured: its checkout failed: {checkout_error}",
            seconds=time.monotonic() - start,
        )
    try:
        setup_error = setup.prepare(worktree)
        digest = verify_digest(resolve_verify_commands(config, worktree), config.subprocess_timeout)
        result = (
            None
            if setup_error
            else run_fast_checks(
                worktree, replace(config, fast_iteration_checks=list(FAST_ITERATION_GATES))
            )
        )
    finally:
        cleanup_error = _discard(worktree, root_dir, ui)
    return BaseGates(
        base_branch,
        sha,
        digest,
        setup.command,
        setup_error,
        cleanup_error,
        result,
        time.monotonic() - start,
    )


def _discard(worktree: Path, root_dir: Path, ui: UI) -> str:
    """Delete the throwaway worktree; "" on success, else why it survived."""
    try:
        _remove_temp_worktree(worktree, root_dir, ui, WORKTREE_LABEL)
    except ContractCleanupError as exc:
        return f"the throwaway worktree of the base survived: {exc}"
    return ""


def _failing(check: CheckResult) -> list[str]:
    """The test or rule names the gate's parser read, each once, in order."""
    if check.parsed is None:
        return []
    return list(dict.fromkeys(f.rule_or_test for f in check.parsed.failures if f.rule_or_test))


def refusal_lines(reading: BaseGates) -> list[str]:
    """Why a run must not start on this base, or [] to let it proceed.

    Only a gate that ran and measurably failed. A base that could not be
    read, a failed setup and a gate that measured nothing are warnings
    (:func:`warning_lines`): Phase 1 still runs every gate on every
    component, so letting them through passes nothing it did not pass.
    """
    if reading.result is None:
        return []
    at = f"{reading.base_branch} at {reading.base_sha[:12]}"
    lines: list[str] = []
    for check in reading.result.checks:
        if check.passed or not check.measured:
            continue
        names = _failing(check)
        line = f"{check.name} fails on {at}: {check.message}"
        if names:
            more = len(names) - NAMED_FAILURES
            line += f"; failing: {', '.join(names[:NAMED_FAILURES])}"
            line += f" and {more} more" if more > 0 else ""
        lines.append(line)
    return lines


def warning_lines(reading: BaseGates) -> list[str]:
    """What this reading could not measure, one line each."""
    lines = [reading.error] if reading.error else []
    if reading.setup_error:
        at = reading.base_sha[:12]
        lines.append(f"the base {at} was not measured: its setup failed: {reading.setup_error}")
    if reading.result is not None:
        lines += [
            f"{check.name} measured nothing on {reading.base_branch}: {check.message}"
            for check in reading.result.checks
            if not check.passed and not check.measured
        ]
    return lines


def base_gates_path(root_dir: Path, run_id: str) -> Path:
    return RunPaths.for_run(root_dir, run_id).root / BASE_GATES_FILE


def reading_document(
    reading: BaseGates, reasons: list[str], skipped_reason: str = ""
) -> dict[str, Any]:
    """The reading as JSON: the body of the run's record and of ``ks doctor --measure``."""
    result = reading.result
    return {
        "measured": result is not None,
        "skippedReason": skipped_reason,
        "baseBranch": reading.base_branch,
        "baseSha": reading.base_sha,
        "verifyDigest": reading.digest,
        "setupCommand": reading.setup_command,
        "setupError": reading.setup_error,
        "error": reading.error,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "measured": check.measured,
                "message": check.message,
                "failing": _failing(check),
            }
            for check in (result.checks if result is not None else [])
        ],
        "notMeasured": [
            gap.as_token() for gap in (result.not_measured if result is not None else [])
        ],
        "refused": bool(reasons),
        "reasons": reasons,
        "seconds": round(reading.seconds, 3),
        "isolation": HOST_LABEL,
    }


def write_record(
    root_dir: Path,
    run_id: str,
    reading: BaseGates,
    reasons: list[str],
    skipped_reason: str = "",
) -> list[str]:
    """Write the reading's record; return why it could not be written, or []."""
    path = base_gates_path(root_dir, run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path,
            {
                "runId": run_id,
                "kstrlVersion": kstrl_version(),
                **reading_document(reading, reasons, skipped_reason),
            },
        )
    except OSError as exc:
        return [f"the base reading cannot be recorded at {path}: {exc}"]
    return []
