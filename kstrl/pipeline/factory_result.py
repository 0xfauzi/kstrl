"""The records one run returns, and the usage snapshot of one attempt (#776)."""

from __future__ import annotations

import warnings
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.events import UsageTotals
from kstrl.io import atomic_write_json, read_json
from kstrl.plan import COMPONENT_STATUS_VALUES, ComponentStatus, Manifest
from kstrl.proc import WorktreeSweep

if TYPE_CHECKING:
    from kstrl.pipeline.pipeline import ComponentPipeline
    from kstrl.ui import UI


@dataclass
class ComponentResult:
    """Result from running a single component."""

    component_id: str
    success: bool
    iterations: int = 0
    error: str | None = None
    duration_seconds: float = 0.0
    context_json: str | None = None
    # R3.1: engineer-loop usage aggregated by the worker; pickled back
    # across the ProcessPoolExecutor boundary. None means the worker
    # predates the meter or crashed before the loop started.
    usage: UsageTotals | None = None
    # R7.5: the no-progress circuit breaker halted the loop. Routed by
    # the pipeline to a direct FAILED transition (retrying the same
    # prompt against the same state is the exact spend the breaker
    # exists to stop) with a distinct journal event.
    no_progress: bool = False
    # The write guard's gate stopped the engineer session. Routed to a
    # direct FAILED transition, like ``no_progress``, under its own check.
    session_stopped: bool = False
    # R8: the engineer loop halted ITSELF on a run-level ceiling
    # (max_total_tokens or max_cost_usd, between iterations). Routed to
    # pipeline.fail_for_budget so the audit trail is identical to a
    # breach caught at a phase boundary; ``error`` carries WHICH ceiling
    # and which condition fired. The typed pair below carries the same
    # facts structurally (R8 review #180) so the pipeline never re-derives
    # the identity from its own totals.
    budget_exceeded: bool = False
    budget_halt_condition: str = ""
    budget_halt_ceilings: tuple[str, ...] = ()
    # Files the in-loop scope guard rejected. Carried so the pipeline can
    # file the halt under the retry context's verification-failures
    # section, where R0.4 established the retry agent looks for scope
    # guidance - catching the violation earlier must not relocate it.
    guard_violations: tuple[str, ...] = ()
    # #461: processes the attempt left running in its worktree, killed
    # before the result was returned. The pipeline records one finding each.
    worktree_sweep: WorktreeSweep = field(default_factory=WorktreeSweep)


@dataclass
class FactoryResult:
    """Overall result from the factory run."""

    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # R0.2: components whose PR merge was initiated but not confirmed.
    # Not failed - a factory re-run re-polls them - but their dependents
    # were not scheduled, so the run is incomplete (nonzero exit code).
    merge_pending: list[str] = field(default_factory=list)
    # #465: components the merge gate parked (every gate passed, nobody
    # to ask). Not failed, but their dependents were not scheduled, so
    # the run is incomplete (nonzero exit code), the same as merge_pending.
    awaiting_approval: list[str] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)
    # #601: components whose merge ComponentPipeline._record_merge
    # confirmed in this run, each mapped to the PR head GitHub merged
    # ("" when GitHub reported none). The autonomy ladder counts merges
    # from this, never from ``completed``: a part completed with no PR
    # merged nothing.
    merged: dict[str, str] = field(default_factory=dict)
    # R0.3: unresolved contract failures (one human-readable line per
    # failed check). Non-empty forces a nonzero exit code even when no
    # single component could be blamed.
    contract_failures: list[str] = field(default_factory=list)
    # Components this run actually handed to the executor, in launch
    # order (a retried component appears once per attempt). The counters
    # above cannot stand in for this: they are all empty both when the
    # scheduler found nothing to do and when there was nothing left to
    # do, and only the first of those is a failure (#263).
    scheduled: list[str] = field(default_factory=list)
    exit_code: int = 0


def run_is_clean(
    result: FactoryResult,
    manifest: Manifest,
    *,
    stopped: bool,
) -> bool:
    """Whether a finished run counts as clean. One predicate, two callers
    (#154 fix round, A1).

    Before this, the R8.7 release gate computed its own `run_clean` from
    three of these five terms and missed `stopped` and the #263
    unfinished-components term, so an operator-stopped run with one
    component merged and the rest PENDING recorded
    `release_withheld="no_driver"` (fully permitted) while
    `resolve_exit_code` on the identical run returned 130 - the #260
    class: two definitions of one verdict, and the weaker one is the one
    a gate consults. `resolve_exit_code` and the release block in
    `_run_factory_locked` both call this now, so they cannot diverge
    again. Pure: no UI side effect, so the nothing-scheduled diagnostic
    stays in `resolve_exit_code`, the one caller that reports it.
    """
    if stopped:
        return False
    if result.failed or result.contract_failures:
        return False
    if result.merge_pending:
        # Incomplete, not failed: unconfirmed merges blocked their
        # dependents.
        return False
    if result.awaiting_approval:
        # Incomplete, not failed: parked merges hold their dependents (#465).
        return False
    if result.skipped and not result.completed:
        return False
    # #263: components the manifest ends the run short of COMPLETED. Read
    # from the manifest rather than the run counters, because the counters
    # are equally empty whether the scheduler found nothing it COULD do or
    # nothing it NEEDED to do, and only the first of those is a failure.
    # Same reasoning the merge_pending list above is rebuilt from the
    # manifest rather than accumulated during the run.
    unfinished = [c.id for c in manifest.components if c.status != ComponentStatus.COMPLETED.value]
    # A component an earlier run left FAILED or SKIPPED is still a part of
    # the feature that did not get built: a retry of another component that
    # completed does not make the feature whole (a user run exited 0 with
    # two failed components in the manifest).
    left = {ComponentStatus.FAILED.value, ComponentStatus.SKIPPED.value}
    if any(c.status in left for c in manifest.components):
        return False
    return not (not result.scheduled and unfinished)


def resolve_exit_code(
    factory_result: FactoryResult,
    manifest: Manifest,
    ui: UI,
    *,
    stopped: bool,
) -> int:
    """Map a finished run's state to its process exit code.

    Emits the diagnostic for the nothing-was-scheduled case, which is the
    only branch whose cause the summary counters do not already name.
    """
    if stopped:
        return 130
    if factory_result.failed or factory_result.contract_failures:
        return 1
    if factory_result.merge_pending:
        # Incomplete, not failed: unconfirmed merges blocked their
        # dependents. Nonzero so automation notices; a re-run re-polls.
        return 1
    if factory_result.awaiting_approval:
        # Incomplete, not failed (#465): `ks serve` reads the manifest to
        # tell this apart from a failure, so the code only has to be
        # nonzero so `ks factory && deploy` does not deploy half a run.
        return 1
    if factory_result.skipped and not factory_result.completed:
        return 1

    # Delegates to run_is_clean for the verdict; at this point stopped,
    # failed/contract_failures, merge_pending and skipped-only are all
    # already known False, so this reduces to the #263 unfinished check
    # below, but goes through the one shared predicate rather than a
    # second copy of it.
    if run_is_clean(factory_result, manifest, stopped=False):
        return 0

    # An off-enum status, a component left FAILED or SKIPPED by an
    # earlier run, or a future scheduling bug: the manifest held work and
    # the scheduler launched none of it, or this run's work completed and
    # an earlier run's did not. Reported as a failure so `ks factory &&
    # deploy` cannot deploy a feature with a part missing.
    unfinished = [c.id for c in manifest.components if c.status != ComponentStatus.COMPLETED.value]
    if factory_result.scheduled:
        retryable = manifest.retryable_component_ids()
        ui.err(
            "This run's components completed, and the feature is not whole: "
            + ", ".join(f"{c.id} is {c.status}" for c in manifest.components if c.id in unfinished)
            + (f". `ks retry {retryable[0]}` runs the first of them again" if retryable else "")
        )
    else:
        _report_nothing_scheduled(manifest, unfinished, ui)
    return 1


def ids_in_status(manifest: Manifest, status: ComponentStatus) -> list[str]:
    """The ids of the components in ``status``, in manifest order."""
    return [c.id for c in manifest.components if c.status == status.value]


def report_awaiting_approval(factory_result: FactoryResult, ui: UI) -> None:
    """Name the components the merge gate parked, and what moves them (#465)."""
    if not factory_result.awaiting_approval:
        return
    ui.kv("Awaiting approval", str(len(factory_result.awaiting_approval)))
    ui.warn(
        "Parked at the merge gate, nothing pushed: "
        + ", ".join(factory_result.awaiting_approval)
        + ". `ks inbox ls` lists them; `ks inbox approve <id>` merges one and "
        "continues the run, `ks inbox reject <id> --comment ...` fails it."
    )


def _report_nothing_scheduled(
    manifest: Manifest,
    unfinished: list[str],
    ui: UI,
) -> None:
    """Say what the run did not do, and name an action that will work.

    The remedy has to be executable for the state it names, so the retry
    targets come from ``Manifest.retryable_component_ids`` - the same
    definition ``reset_for_retry`` enforces - rather than from a second
    reading of the statuses here. A skipped component is never named:
    it becomes runnable by retrying the failure that cascaded onto it,
    and `ks retry <skipped-id>` exits 2.
    """
    ui.err(
        f"No component was scheduled from {len(manifest.components)} in the "
        f"manifest, and {len(unfinished)} did not complete: "
        f"{', '.join(unfinished)}"
    )
    retryable = manifest.retryable_component_ids()
    if retryable:
        advice = (
            f"  `ks retry <component-id>` resets a failed component to "
            f"'{ComponentStatus.PENDING.value}', and with it every dependent "
            f"it cascade-skipped. Failed here: {', '.join(retryable)}."
        )
    else:
        advice = (
            "  Check each component's status against ComponentStatus "
            f"({', '.join(COMPONENT_STATUS_VALUES)}). `ks retry` accepts only "
            f"a component in '{ComponentStatus.FAILED.value}', and none is."
        )
    ui.info(advice)


def write_partial_usage(path: Path, totals: UsageTotals) -> None:
    """Publish the engineer loop's usage-so-far, atomically (R8).

    The worker owns its UsageRecords in memory; the abort path
    (``_abort_inflight``) SIGKILLs the process, so without this file the
    spend of a killed worker is simply lost. ``atomicio`` owns the
    atomic-write convention (#291) and is what makes the file safe to
    read from a process that may be killing the writer: a reader sees the
    previous complete snapshot or the new one, never a torn one.

    Accounting only - every failure is swallowed. A worker must not die
    because its usage file could not be written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, totals.to_dict())
    except (OSError, TypeError, ValueError):
        return


def _read_partial_usage(path: Path) -> UsageTotals | None:
    """Rehydrate a killed worker's last usage snapshot (R8).

    Returns None when the file is absent, unreadable, or not a usage
    dict - the abort path then records nothing rather than guessing.
    ``unreported_calls``, ``tokenless_calls`` and ``costless_calls`` are
    derived properties and are deliberately not read back.

    A snapshot without ``token_calls`` or ``cost_calls`` predates the
    per-axis counts (R8). A warning names the file, and the spend IS
    recorded with each missing count at 0, so the run total reads as a
    lower bound (#863). Dropping the snapshot would remove the worker's
    spend from the meter, and a budget must not read money as zero. The
    scheduler deletes the slot before each submission
    (``clear_partial_usage``), so only this kstrl's writer, which
    always writes both counts, fills it.
    """
    try:
        data = read_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    missing = [name for name in ("token_calls", "cost_calls") if name not in data]
    if missing:
        warnings.warn(
            f"usage snapshot {path} has no {' or '.join(missing)}, so it was written before "
            "the per-axis counts existed; the spend of the killed worker is recorded as a "
            "lower bound",
            RuntimeWarning,
            stacklevel=2,
        )
    totals = UsageTotals()
    int_fields = (
        "calls",
        "known_calls",
        "token_calls",
        "cost_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "total_tokens",
    )
    for name in int_fields:
        value = data.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            setattr(totals, name, value)
    for name in ("cost_usd", "duration_seconds"):
        value = data.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            setattr(totals, name, float(value))
    if totals.calls == 0:
        return None
    return totals


def clear_partial_usage(path: Path) -> bool:
    """Retire the previous attempt's usage snapshot (R8 review, P2-c).

    The snapshot is keyed by run + component only, and a normal result
    leaves the file on disk, so without this the NEXT attempt inherits
    it: a retry cancelled before its own first iteration boundary got
    salvaged with the previous attempt's tokens, which
    ``process_result`` had already recorded - a clean double count.

    Called by the scheduler in the PARENT immediately before every
    submission, not by the worker, because the window that matters is
    exactly the one where the worker never starts (cancelled or killed
    during pool startup).

    Returns True when the slot is provably clean - deleted, or already
    absent. Returns False when a snapshot may still be on disk, and the
    caller must then refuse disk salvage for that attempt: deletion IS
    the attempt-scoping invariant, so swallowing the error left the
    previous attempt's totals addressable as the current one and
    re-counted them (review finding on 22e99b4). A worker that cannot
    delete the file generally cannot overwrite it either, so "we failed
    to clear it" and "what is there is stale" travel together.

    Never fails the component: an accounting risk is not a reason to
    stop work, but it IS a reason to distrust the number.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return True  # nothing to retire; the slot is clean
    except OSError:
        return False
    return True


def salvage_aborted_usage(
    future: Future[ComponentResult],
    comp_id: str,
    pipeline: ComponentPipeline,
) -> None:
    """Record what an aborted worker spent before it was stopped (R8).

    Organic failures carry usage back on the ComponentResult
    (``pipeline.process_result`` records it before the success branch -
    "failed attempts cost real tokens too"). The abort path used to lose
    it: the worker is killed and its future is cancelled without anyone
    reading a result, so the tokens vanished from the meter and the run
    under-reported its own spend.

    Two recovery routes, deliberately exclusive so nothing is counted
    twice:
    - The future already carries a result (inline executor, or a pool
      worker that finished as the stop landed): that result is
      authoritative. ``result()`` cannot block here - the future is
      done.
    - Otherwise the worker was killed mid-loop: fall back to the
      iteration-boundary snapshot it wrote to disk. That file is
      rewritten atomically, so reading it while the writer is being
      killed yields a complete (if slightly stale) snapshot. Spend
      inside the interrupted iteration is NOT recoverable - it was never
      reported to anyone before the kill.

    The snapshot is attempt-scoped by the scheduler, which deletes it
    immediately before every submission (``clear_partial_usage``). That
    is what makes this route safe: without it a completed attempt left
    its file behind and a later attempt aborted before its own first
    iteration boundary salvaged the PREVIOUS attempt's tokens on top of
    the ones ``process_result`` had already recorded (review finding
    P2-c, reproduced as a clean 2x double count).

    Reads ``pipeline.usage_paths``, not ``run_paths``: accounting
    storage exists even when progress logging is off (P2-d).
    """
    if future.done() and not future.cancelled():
        try:
            result = future.result(timeout=0)
        except Exception:  # noqa: BLE001 - a crashed worker reported nothing
            return
        if result is None:
            return
        pipeline.record_worktree_sweep(comp_id, result.worktree_sweep, "engineer")
        if result.usage is not None:
            pipeline.record_engineer_usage(comp_id, result.usage)
        return
    usage_paths = pipeline.usage_paths
    if usage_paths is None:
        return
    if not pipeline.usage_salvage_is_safe(comp_id):
        return
    totals = _read_partial_usage(usage_paths.engineer_usage(comp_id))
    if totals is not None:
        pipeline.record_engineer_usage(comp_id, totals)
