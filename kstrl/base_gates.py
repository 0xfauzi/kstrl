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

Under a ``[stack]`` (#696) the gates are the stack's checks and the base is
held to more: any check that did not pass refuses, measured or not, a failed
setup refuses, and so does anything ``git status`` shows in the base's
worktree after the checks ran (:data:`BaseGates.left_behind`). A check whose
output is not ignored by the project's own .gitignore would otherwise read as
an out-of-scope edit of every engineer's.

``ks factory --accept-red-base <sha>`` (slice 4) runs on a base that
refuses, for one run and one commit: :func:`apply_acceptance` waives a
refusal only when the value is at least :data:`ACCEPT_MIN_DIGITS`
characters of the measured sha, refuses anything else, and waives only the
lines :func:`_acceptable_lines` names: a gate that ran and measurably
failed. A failed setup, a check that measured nothing and what a check
left behind still refuse.

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
from kstrl.rung import HOST_LABEL, label_of
from kstrl.verify import (
    CheckResult,
    VerificationResult,
    VerifyConfig,
    gate_names,
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

#: The fewest leading characters of the measured base sha that
#: ``--accept-red-base`` takes (#654 slice 4).
ACCEPT_MIN_DIGITS = 12


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
    #: #696: the ``[stack]`` digest the base was measured under; "" with no stack.
    stack_digest: str = ""
    #: #696: what ``git status`` showed in the base worktree after the checks
    #: ran, under a stack only: each entry, or one line saying why it could
    #: not be read.
    left_behind: tuple[str, ...] = ()
    #: #700 slice 2: the isolation the checks ran under, the TEST-zone
    #: rung's label or :data:`HOST_LABEL`.
    isolation: str = HOST_LABEL


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
    left_behind: tuple[str, ...] = ()
    try:
        setup_error = setup.prepare(worktree)
        digest = verify_digest(
            config.project_stack or resolve_verify_commands(config, worktree),
            config.subprocess_timeout,
        )
        result = (
            None
            if setup_error
            else run_fast_checks(
                worktree, replace(config, fast_iteration_checks=list(gate_names(config)))
            )
        )
        if config.project_stack is not None and result is not None:
            left_behind = _left_behind(worktree)
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
        config.project_stack.digest if config.project_stack is not None else "",
        left_behind,
        label_of(config.rung),
    )


def _left_behind(worktree: Path) -> tuple[str, ...]:
    """What ``git status`` shows in ``worktree``, or why it could not say."""
    try:
        return tuple(git.status_entries(worktree))
    except git.GitDiffError as exc:
        return (f"(git status could not be read: {exc})",)


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
    Under a ``[stack]`` the rules are :func:`_stack_refusal_lines`.
    """
    if reading.stack_digest:
        return _stack_refusal_lines(reading)
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


def _stack_refusal_lines(reading: BaseGates) -> list[str]:
    """The base refusal under a ``[stack]`` (#696): a failed setup, every
    check that did not pass whether or not it measured anything, and every
    entry ``git status`` showed after the checks ran."""
    at = f"{reading.base_branch} at {reading.base_sha[:12]}"
    if reading.setup_error:
        return [f"the setup fails on {at}: {reading.setup_error}"]
    if reading.result is None:
        return []
    lines = [_stack_check_line(check, at) for check in reading.result.checks if not check.passed]
    if reading.left_behind:
        lines.append(
            f"git status after the checks on {at}: "
            + ", ".join(reading.left_behind)
            + "; what a check writes must be committed or ignored in .gitignore"
        )
    return lines


def _stack_check_line(check: CheckResult, at: str) -> str:
    return f"{check.name} fails on {at}: {check.message}"


def _acceptable_lines(reading: BaseGates) -> list[str]:
    """The refusal lines ``--accept-red-base`` may waive (#654 slice 4).

    Only a gate that ran on the base and measurably failed: that is the red
    reading the operator accepts. With no stack that is every line
    :func:`refusal_lines` returns. Under a ``[stack]`` it is each check that
    exited with a status other than 0, 126 or 127. Never a failed setup (no
    check ran), a check that measured nothing (a missing tool or a timeout
    that no commit of the engineer's fixes), or what a check left in ``git
    status`` (every engineer's diff would carry it). A refusal this list does
    not name is never waived, so a kind of refusal added later refuses under
    an acceptance until it is named here.
    """
    if not reading.stack_digest:
        return refusal_lines(reading)
    if reading.setup_error or reading.result is None:
        return []
    at = f"{reading.base_branch} at {reading.base_sha[:12]}"
    return [
        _stack_check_line(check, at)
        for check in reading.result.checks
        if not check.passed and check.measured
    ]


def apply_acceptance(
    reading: BaseGates, reasons: list[str], accept: str
) -> tuple[list[str], list[str]]:
    """What still refuses under ``--accept-red-base accept``, and what it waived.

    The acceptance binds to the measured commit, not to a set of failures:
    ``accept`` must be at least :data:`ACCEPT_MIN_DIGITS` characters and a
    prefix of the sha this run measured, so a base that moved since the
    operator looked refuses again. Anything else refuses, naming both. A
    match waives the lines :func:`_acceptable_lines` names and nothing else.
    With no ``accept`` the reasons stand as they are.
    """
    if not accept:
        return reasons, []
    if len(accept) < ACCEPT_MIN_DIGITS or not reading.base_sha.startswith(accept):
        measured = reading.base_sha or "nothing, because no base was measured"
        return [
            *reasons,
            f"--accept-red-base {accept} does not name the measured base: it must be "
            f"at least {ACCEPT_MIN_DIGITS} characters of the sha of {reading.base_branch}, "
            f"which measured {measured}",
        ], []
    acceptable = _acceptable_lines(reading)
    return (
        [line for line in reasons if line not in acceptable],
        [line for line in reasons if line in acceptable],
    )


def warning_lines(reading: BaseGates) -> list[str]:
    """What this reading could not measure, one line each.

    Under a ``[stack]`` a failed setup and an unmeasured check are
    refusals (:func:`_stack_refusal_lines`), not warnings.
    """
    lines = [reading.error] if reading.error else []
    if reading.stack_digest:
        return lines
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
        "stackDigest": reading.stack_digest,
        "leftBehind": list(reading.left_behind),
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
        "isolation": reading.isolation,
    }


def write_record(
    root_dir: Path,
    run_id: str,
    reading: BaseGates,
    reasons: list[str],
    skipped_reason: str = "",
    *,
    accept: str = "",
    accepted: tuple[str, ...] = (),
) -> list[str]:
    """Write the reading's record; return why it could not be written, or [].

    ``accept`` is the run's ``--accept-red-base`` value, "" without one, and
    ``accepted`` the refusals it waived (#654 slice 4).
    """
    path = base_gates_path(root_dir, run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path,
            {
                "runId": run_id,
                "kstrlVersion": kstrl_version(),
                **reading_document(reading, reasons, skipped_reason),
                "acceptRedBase": accept,
                "accepted": list(accepted),
            },
        )
    except OSError as exc:
        return [f"the base reading cannot be recorded at {path}: {exc}"]
    return []
