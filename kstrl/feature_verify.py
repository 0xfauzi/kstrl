"""#288: the `ks feature` verification report.

Split out of ``feature_cmd`` when that module crossed the 800-line
ratchet. One job: run the read-only mechanical checks against the
operator's live checkout and SAY what they found, to the terminal and to
the event stream. It has no say in what `ks feature` does next - no
control flow, no exit code, no repair-loop entry point is reachable from
here, and the only value that flows back is the set of check names that
failed, which the caller passes to the NEXT report so a failure that was
already there before the agent ran is named as pre-existing.

The narrowing that makes the report honest lives in ``verify``
(``narrow_to_undiffed`` and ``DIFF_DEPENDENT_CHECKS``), next to the
checks it turns off, because a check added there is a check this flow
must decide about.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.events import Event, VerificationResultEvent
from kstrl.loop import STOP_EXIT_CODE, LoopResult
from kstrl.verify import (
    DIFF_DEPENDENT_CHECKS,
    ResolvedVerifyCommands,
    VerificationResult,
    VerifyConfig,
    narrow_to_undiffed,
    resolve_verify_commands,
    run_mechanical_verification,
    self_critique_progress_path,
)

if TYPE_CHECKING:
    from kstrl.ui.base import UI


def resolve_feature_verify_config(root_dir: Path) -> VerifyConfig:
    """The project's ``[verify]`` config, narrowed to what it can measure here.

    The narrowing is ``verify.narrow_to_undiffed``, which lives next to
    the checks it turns off and to ``verify.DIFF_DEPENDENT_CHECKS``, the
    list this module narrates. Measured on a throwaway project (#288),
    one `ks feature` run per row, with a stand-in agent that either
    commits its work or does not:

        agent commits | PRD branchName | git diff main...HEAD
        yes           | feat/demo      | 7 files, incl. src/greet.py
        no            | feat/demo      | 0 files
        yes           | main           | 0 files

    Nothing in this flow commits - ``run_loop`` never does - and the
    branch the loop checks out comes from the PRD's ``branchName``, which
    the operator is free to point at the base branch itself. Two of those
    three configurations leave the diff empty, which is why the narrowing
    applies here.

    ONE object (#261): the same value is handed to the implement and
    repair loops, so the three commands ``VERIFY_COMMANDS_PROMPT`` states
    to the engineer are the three commands that then run on its output.
    The narrowing does not touch the command fields, which is all
    ``resolve_verify_commands`` reads, so the prompt says exactly what it
    would have said with the full config.

    Loaded unguarded, next to ``TimeoutConfig.load`` and
    ``BreakerConfig.load``: ``[verify]`` is a fatal section of the CLI's
    config preflight seam (``cli._PREFLIGHT_EXEMPT`` does not list
    ``feature``), so a ``kstrl.toml`` that would fail here has already
    stopped the command before this flow was entered.
    """
    return narrow_to_undiffed(VerifyConfig.load(root_dir))


#: Detail lines printed per failing check. A failing gate's details are
#: every parsed failure plus a source-context snippet, and under the
#: embedded TUI each printed line is one Log event on the run bus, up to
#: ``2 + repair_max_runs`` times a run. Twelve shows a couple of
#: failures in full and then says how many were dropped; the complete
#: output is in the command's own log, and `ks sense` prints all of it.
_MAX_DETAIL_LINES = 12


def _announce_verification(
    ui: UI,
    commands: ResolvedVerifyCommands,
    progress_path: Path | None,
    phase: str,
) -> None:
    """Say what is about to run, BEFORE it runs.

    The gates capture their output, so the terminal is otherwise dead for
    as long as the project's test suite takes - measured on this repo at
    246s - on a thread this module's docstring says must be able to host
    the TUI. Naming the commands first also lets an operator who does not
    want to wait recognise what they are waiting for.

    ``progress_path`` is ``verify.self_critique_progress_path``'s answer,
    and it is announced because it is a FOURTH check: an operator who set
    ``[verify] require_self_critique`` plus ``progress_file_path`` used to
    get a row in the table that this announcement never mentioned and the
    runbook said could not run (#288 review). Announcing whatever that
    one resolver returns is what keeps the two in step.
    """
    ui.section(f"Verification report ({phase})")
    ui.info(
        "Report only, not a gate: the exit code is unchanged. Not measured "
        "here (no diff to read, see docs/runbook.md): " + ", ".join(DIFF_DEPENDENT_CHECKS)
    )
    ui.info(f"  running: {commands.test}")
    ui.info(f"  running: {commands.typecheck}")
    ui.info(f"  running: {commands.lint}")
    if progress_path is not None:
        ui.info(f"  reading: {progress_path} (self_critique)")


def _narrate_verification(
    ui: UI,
    result: VerificationResult,
    already_failing: frozenset[str],
) -> None:
    """Print one line per check, then the verdict and its attribution.

    ``already_failing`` is the pre-implement baseline's failing set. A
    verdict here is about the WHOLE checkout, not about the diff, so
    without it a checkout whose lint was already red before the agent
    started reads as the agent having broken lint. Naming the overlap is
    what makes the verdict attributable; the machine-readable half is the
    baseline's own ``VerificationResultEvent``, which a consumer diffs
    against this one.
    """
    for line in result.report_lines(durations=False, max_detail_lines=_MAX_DETAIL_LINES):
        ui.info(line)
    failed = frozenset(check.name for check in result.checks if not check.passed)
    if result.passed:
        ui.ok(f"verification: PASS ({len(result.checks)} checks)")
    else:
        ui.warn(f"verification: FAIL ({len(failed)} of {len(result.checks)} checks failed)")
    pre_existing = sorted(failed & already_failing)
    if pre_existing:
        ui.warn(
            f"  already failing before the implement loop: {', '.join(pre_existing)}. "
            "This report measures the whole checkout, not the diff."
        )


def report_verification(
    ui: UI,
    emit: Callable[[Event], None],
    component: str,
    root_dir: Path,
    verify_config: VerifyConfig,
    phase: str,
    *,
    loop_result: LoopResult | None = None,
    stop_check: Callable[[], bool] | None = None,
    already_failing: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Run the read-only mechanical checks and REPORT what they found.

    Returns the set of check names that FAILED, which the caller carries
    forward as the next report's ``already_failing``. A report that did
    not run returns ``already_failing`` unchanged, so the chain never
    loses the baseline.

    Report only. `ks feature` keeps its control flow and its exit codes:
    a failing check does not halt the flow, does not change what it
    returns, and is not routed into the repair loop. The one thing that
    changes is that the operator - and, under
    ``--implementation-auto-run``, the event stream, where there is no
    operator at the screen - is told.

    Nothing here may raise into the flow, which is why the whole
    measurement is wrapped: ``check_test_suite`` catches
    ``TimeoutExpired`` and nothing else, so a ``Popen`` that fails on
    EMFILE or a removed cwd would otherwise propagate out of an ADVISORY
    report and take the command with it, ending ``events.jsonl`` at
    ``phase_started`` with no ``phase_completed`` and no
    ``run_completed`` - a component a dashboard shows as running forever.
    Every other blocking call in this flow already has that guard.

    ``loop_result`` carries the exit-path rule, and both halves of it are
    refusals. ``None`` means the pre-implement BASELINE, which is not
    about a loop and is subject only to the stop check.

    ``iterations == 0`` is a loop that never called the agent (a failed
    branch checkout, a stop before iteration 1). Every early return
    upstream - understand incomplete, review gate declined, review gate
    unavailable in a non-TTY, a PRD with no user stories - leaves the
    same way, with no production code written, and a verification verdict
    over work that was never attempted is noise.

    ``exit_code == 130`` is the operator pressing stop between
    iterations. ``stop_check`` is the same refusal one iteration earlier:
    ``run_loop`` returns that code only from its top-of-iteration probe
    (#288 review), so a stop pressed DURING the final iteration comes
    back as an ordinary exit 0 and the flag is still set here. Either
    way, making somebody who pressed stop wait out a test suite is the
    opposite of what they asked for: measured on this repo, 246s.

    A stop pressed during the measurement itself is NOT cancellable, and
    is bounded by ``3 x [verify] subprocess_timeout`` (900s at the
    default) because ``run_scrubbed`` kills each process group at its own
    deadline. That window is the same shape as, and half the size of, the
    one the agent call inside ``run_loop`` already has (``[timeout]``
    ``agent_iteration``, 1800s); closing it needs cooperative
    cancellation inside the shared checker, which is not this flow's to
    add.

    ``read_only=True`` is the mode ``ks sense`` already uses to point
    this same function at a live checkout (R10.1): the two checks that
    would rewrite the tree they measure are forbidden there.

    ``prd_path=None`` skips the PRD-derived checks, exactly as ``ks
    sense`` does. ``prd_stories`` re-reads the flag the agent itself
    set, which is a self-report rather than an independent measurement;
    and on the repair exits there are two PRDs (the operator's and the
    generated repair PRD) with no single right answer for which one this
    report is about. The independent measurement here is the commands.
    """
    if loop_result is not None and (
        loop_result.iterations == 0 or loop_result.exit_code == STOP_EXIT_CODE
    ):
        return already_failing
    if stop_check is not None and stop_check():
        return already_failing

    commands = resolve_verify_commands(verify_config, root_dir)
    progress_path = self_critique_progress_path(verify_config, root_dir, None)
    _announce_verification(ui, commands, progress_path, phase)
    started = time.monotonic()
    try:
        result = run_mechanical_verification(
            worktree_path=root_dir,
            prd_path=None,
            # Never read: ``narrow_to_undiffed`` turned off every check
            # that consumes a diff, and the two it cannot reach are
            # suppressed by not being passed at all. The empty string is
            # the honest value for "there is no base here", and
            # tests/test_feature_verification.py pins the set of checks
            # this call can produce so a new one cannot quietly start
            # resolving it into a phantom base.
            base_branch="",
            # Left None deliberately, and it matters more since #294
            # rewrote how this argument is read. ``_scope_checks`` now
            # consults it BEFORE the toggles, and ANY non-None value,
            # the empty string included, appends
            # ``scope_unreadable``, which is UNGATED and fails closed
            # unconditionally. Measured on this branch: None gives
            # [test_suite, typecheck, linter] and passed=True; "" gives
            # the same three plus scope_unreadable=False and
            # passed=False. In an advisory report over a checkout that
            # has no component scope and never had one, that verdict
            # would be invented rather than measured.
            allowed_paths_error=None,
            allowed_paths=None,
            # policy and adequacy read the same diff. Omitted rather than
            # disabled, because that is what their None defaults mean.
            config=verify_config,
            read_only=True,
        )
    except Exception as exc:  # noqa: BLE001 - an advisory report may not halt the flow
        detail = f"{type(exc).__name__}: {exc}"
        ui.err(f"verification: could not run ({detail})")
        # passed=False with an EMPTY checks tuple is the unambiguous
        # "nothing was measured and it did not succeed". It is never a
        # pass, and it cannot be mistaken for one by a reader doing
        # all(c.passed) over the names, because there are none.
        emit(
            VerificationResultEvent(
                component=component,
                passed=False,
                checks=(),
                failures=(detail,),
                duration_seconds=round(time.monotonic() - started, 2),
                phase=phase,
                advisory=True,
            )
        )
        return already_failing

    duration = round(time.monotonic() - started, 2)
    _narrate_verification(ui, result, already_failing)

    # The same event type the factory's Phase 1 emits for a result of
    # this same function, so `events.jsonl` carries one shape for one
    # measurement and existing readers (reducer, observability log) need
    # no new case. ``phase`` and ``advisory`` (#288) are what tell a
    # consumer which loop was measured and that nothing gated on the
    # answer; the ``phase="baseline"`` row is what lets it subtract
    # pre-existing breakage rather than read every failure as the
    # agent's. ``checks`` names what ran, and the suppressed checks are
    # ABSENT from it rather than recorded as passing skips: a machine
    # reader doing ``all(c.passed)`` must never see a check that measured
    # nothing counted as a pass.
    emit(
        VerificationResultEvent(
            component=component,
            passed=result.passed,
            checks=tuple(check.name for check in result.checks),
            failures=tuple(check.message for check in result.checks if not check.passed),
            duration_seconds=duration,
            phase=phase,
            advisory=True,
        )
    )
    return frozenset(check.name for check in result.checks if not check.passed)
