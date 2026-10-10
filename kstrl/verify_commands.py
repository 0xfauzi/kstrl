"""The gates that run the configured commands, and the bound on their output."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from kstrl.failure_excerpt import failure_excerpt
from kstrl.rung import Rung
from kstrl.scrubbed_run import ChildOutputDecodeError, _readable, run_scrubbed
from kstrl.stack import NO_STACK, Stack
from kstrl.timeout import limit_seconds
from kstrl.verify_model import (
    CheckResult,
    NotMeasured,
    VerificationResult,
    VerifyConfig,
    gate_names,
    validate_fast_iteration_checks,
)

#: The most characters of one gate's output kept for its log (#462).
#: Measured on kstrl's own tree: one failing test printed 699 characters,
#: 179 collection errors 273,184, mypy with 3,981 error lines 352,260, and
#: a shared helper broken under the full suite (1,265 failures) 4,523,001,
#: of which the closing short summary was the last 141,403. At this bound
#: the first three are kept whole and the fourth keeps its full summary.
GATE_OUTPUT_MAX_CHARS = 1_048_576


def bounded_gate_output(output: str, limit: int = GATE_OUTPUT_MAX_CHARS) -> str:
    """``output`` whole when it fits in ``limit`` characters; else its
    first and last ``limit // 2`` characters with a line between them
    saying how many were dropped (#462).

    Both ends, because they answer different questions: the head holds
    the first error, which is usually the cause (a collection error, an
    import failure), and the tail holds the tool's summary (pytest's
    short summary, mypy's count, and stderr, which comes after stdout).
    """
    if len(output) <= limit:
        return output
    half = limit // 2
    dropped = len(output) - 2 * half
    marker = (
        f"\n[kstrl: output truncated, {dropped} of {len(output)} characters "
        f"dropped here; kept the first {half} and the last {half}]\n"
    )
    return output[:half] + marker + output[-half:]


def _output_before_stop(stdout: bytes | str | None, stderr: bytes | str | None) -> str:
    """What a gate printed before it timed out or printed bytes that are
    not utf-8, joined and bounded as any failed gate's output is (#527).

    A hung test suite is the case where the operator most needs the last
    lines it printed, so these two exits keep the output the exception
    carries rather than returning a message alone.
    """
    return bounded_gate_output((_readable(stdout) + _readable(stderr)).strip())


def run_fast_checks(worktree_path: Path, config: VerifyConfig) -> VerificationResult:
    """The gates ``config.fast_iteration_checks`` names, run between
    engineer iterations (#233), in Phase 1's order.

    Each gate is the same per-gate function
    :func:`run_mechanical_verification` calls, with the same command
    and timeout, so the reading handed to the next iteration is the
    reading Phase 1 would take of the same tree. Direct calls rather than
    a lookup table, because every static guard that resolves a spawn's
    callee has to be able to read these three.
    """
    selected = validate_fast_iteration_checks(
        config.fast_iteration_checks, "[verify] fast_iteration_checks", gate_names(config)
    )
    checks, gaps = _command_gates(worktree_path, config, selected)
    return VerificationResult(
        passed=all(check.passed for check in checks), checks=checks, not_measured=gaps
    )


#: The statuses a shell returns when it could not run the command at all:
#: 126 found but not executable, 127 not found. A ``[stack]`` check that
#: ends with one failed and measured nothing (#696 decision 4).
SHELL_COULD_NOT_RUN: frozenset[int] = frozenset({126, 127})


def check_stack_command(
    cwd: Path,
    stack: Stack,
    name: str,
    command: str,
    timeout: float | None,
    rung: Rung | None = None,
) -> CheckResult:
    """Run one ``[stack]`` check in ``cwd``: the row ``stack:<name>`` (#696).

    ``rung`` is the run's TEST-zone rung, or None on the host (#700 slice 2).

    The verdict is the exit status (decision 4): 0 passes, and any other
    completed exit is a MEASURED failure. 126, 127, a timeout and output that
    is not utf-8 fail UNMEASURED. kstrl parses none of the output to decide;
    the details are the lines around each location inside ``cwd``, or the
    last five lines when the output names none.

    A stack no person confirmed runs nothing (slice 3): the row fails
    UNMEASURED with the reason, so every phase that reads it refuses.
    """
    row = f"stack:{name}"
    if stack.unconfirmed:
        refused = f"`{command}` not run: the [stack] in kstrl.toml {stack.unconfirmed}"
        return CheckResult(name=row, passed=False, message=refused, measured=False, output=refused)
    start = time.monotonic()
    try:
        result = run_scrubbed(command, cwd=cwd, timeout=timeout, declared_env=stack.env, rung=rung)
    except subprocess.TimeoutExpired as expired:
        return CheckResult(
            name=row,
            passed=False,
            message=f"`{command}` timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
            measured=False,
            output=_output_before_stop(expired.stdout, expired.stderr),
        )
    except ChildOutputDecodeError as exc:
        return CheckResult(
            name=row,
            passed=False,
            message=f"`{command}` output could not be decoded: {exc}",
            duration_seconds=time.monotonic() - start,
            measured=False,
            output=_output_before_stop(exc.stdout, exc.stderr),
        )
    if result.returncode == 0:
        return CheckResult(
            name=row,
            passed=True,
            message=f"`{command}` exited 0",
            duration_seconds=time.monotonic() - start,
        )
    output = (result.stdout + result.stderr).strip()
    excerpt = failure_excerpt(output, cwd) or "\n".join(output.splitlines()[-5:])
    return CheckResult(
        name=row,
        passed=False,
        message=f"`{command}` exited {result.returncode}",
        details=excerpt.splitlines(),
        duration_seconds=time.monotonic() - start,
        measured=result.returncode not in SHELL_COULD_NOT_RUN,
        output=bounded_gate_output(output),
    )


def _stack_gates(
    worktree_path: Path, config: VerifyConfig, stack: Stack, selected: Sequence[str]
) -> list[CheckResult]:
    """The ``[stack]`` checks named in ``selected``, in the stack's order (#696)."""
    timeout = limit_seconds(config.subprocess_timeout)
    return [
        check_stack_command(worktree_path, stack, name, command, timeout, config.rung)
        for name, command in stack.checks
        if name in selected
    ]


#: Phase 1's one row when no ``[stack]`` names its checks (#696 slice 4).
#: Every entry point refuses before this is reached (``stack.NO_STACK``); a
#: caller that builds a :class:`VerifyConfig` with no stack gets a failed,
#: unmeasured row here, never a pass and never a command kstrl chose.
NO_STACK_CHECK = "stack"


def _command_gates(
    worktree_path: Path, config: VerifyConfig, selected: Sequence[str]
) -> tuple[list[CheckResult], list[NotMeasured]]:
    """The ``[stack]`` checks named in ``selected``, in the stack's order (#696).

    Shared by :func:`run_mechanical_verification` and :func:`run_fast_checks`.
    With no stack there is nothing to run, and the one row says so: no
    command is chosen in its place.
    """
    if config.project_stack is None:
        return [
            CheckResult(
                name=NO_STACK_CHECK,
                passed=False,
                message=NO_STACK,
                measured=False,
                output=NO_STACK,
            )
        ], []
    return _stack_gates(worktree_path, config, config.project_stack, selected), []
