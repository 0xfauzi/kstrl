"""The base branch's own gates, measured before any engineer runs (#654).

Phase 1 fails a component on any non-zero exit of the whole suite, so on a
base whose suite is already red every component fails Phase 1 after its
engineer has been paid. Measured with the real ``ks factory`` and a stub
engineer: on a base with one failing test the engineer was called, and Phase
1 then failed ``test_suite`` on a test the engineer never touched.

:func:`measure_base_gates` runs Phase 1's command gates, the ``[stack]``'s
checks (#696), with Phase 1's timeout, in a throwaway worktree of the commit
the base branch names after a fetch. Never in the root checkout: an
uncommitted fix there would read a red base as green. :func:`refusal_lines`
turns the reading into the reasons the run refuses: any check that did not
pass, measured or not, a failed setup, and anything ``git status`` shows in
the base's worktree after the checks ran (:data:`BaseGates.left_behind`). A
check whose output is not ignored by the project's own .gitignore would
otherwise read as an out-of-scope edit of every engineer's.

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
from kstrl.verify import run_fast_checks
from kstrl.verify_model import CheckResult, VerificationResult, VerifyConfig, gate_names
from kstrl.version import kstrl_version
from kstrl.worktree_setup import WorktreeSetup

if TYPE_CHECKING:
    from kstrl.ui.base import UI

#: The record's file name inside a run directory, beside ``launch.json``.
BASE_GATES_FILE = "base-gates.json"

#: The label of the throwaway worktree, under ``.kstrl/contract/``.
WORKTREE_LABEL = "base-gates"

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
    *,
    measured: BaseGates | None = None,
) -> BaseGates:
    """Run Phase 1's command gates, the ``[stack]``'s checks, on the base branch's commit.

    ``measured`` is a reading taken earlier in this process (#696 slice 7, before
    the architect was paid): it is returned as it is when it measured, the base
    still names the commit it measured, and it was measured the same way (its
    ``digest``, the stack and the timeout), so the base is measured once.
    """
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
    digest = (
        verify_digest(config.project_stack, config.subprocess_timeout)
        if config.project_stack is not None
        else ""
    )
    if (
        measured is not None
        and measured.result is not None
        and (measured.base_sha, measured.digest) == (sha, digest)
    ):
        ui.info(f"  The base {sha[:12]} was measured before the architect ran; that reading holds.")
        return measured
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


def refusal_lines(reading: BaseGates) -> list[str]:
    """Why a run must not start on this base, or [] to let it proceed.

    The base is held to the ``[stack]`` rules (#696): a failed setup, every
    check that did not pass whether or not it measured anything, and every
    entry ``git status`` showed after the checks ran. Since the flag day
    there is no other rule: a reading with no stack carries Phase 1's one
    failed row (``verify.NO_STACK_CHECK``), which refuses like any other.
    A skipped reading (``--no-verify``) holds no result and refuses nothing.
    """
    return _stack_refusal_lines(reading)


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
    reading the operator accepts, each check that exited with a status other
    than 0, 126 or 127. Never a failed setup (no
    check ran), a check that measured nothing (a missing tool or a timeout
    that no commit of the engineer's fixes), or what a check left in ``git
    status`` (every engineer's diff would carry it). A refusal this list does
    not name is never waived, so a kind of refusal added later refuses under
    an acceptance until it is named here.
    """
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
    """What this reading could not measure, one line each: the base could
    not be read, or its throwaway worktree survived.

    A failed setup and an unmeasured check are refusals
    (:func:`refusal_lines`), not warnings (#696).
    """
    return [reading.error] if reading.error else []


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
