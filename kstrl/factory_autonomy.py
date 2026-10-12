"""The autonomy ladder at the two ends of a run.

This module sets the start level. After the run it records the merges,
the rejections, the health breaches and the demotions.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from kstrl.autonomy import (
    AutonomyLevel,
    AutonomyState,
    DemotionTrigger,
    FlagBundle,
    flag_bundle_for,
)
from kstrl.autonomy_config import (
    AutonomyConfig,
    manual_override_notes,
    resolve_runtime_level,
    resolved_flag_bundle,
)
from kstrl.autonomy_transitions import apply_demotion, save_ladder_state
from kstrl.event_catalog import AutonomyLevelApplied
from kstrl.events import EventBus
from kstrl.factory_config import (
    FactoryConfig,
)
from kstrl.factory_result import (
    FactoryResult,
)
from kstrl.findings import POLICY_CATEGORY_PREFIX
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.manifest import (
    Manifest,
)
from kstrl.pipeline_merge import HITL_REJECT_CHECK, PR_CLOSED_CHECK
from kstrl.runenvelope import RunEnvelope
from kstrl.statedir import ControlStateError

if TYPE_CHECKING:
    from kstrl.factory_run import RunContext
    from kstrl.ui.base import UI


class _HealthBreach(Protocol):
    """The R8.4 breach record this seam reads and #151 will supply.

    Structural rather than imported: ``kstrl/health.py`` does not exist
    yet, so the factory must not take a hard dependency on it. These five
    attributes are the whole contract between #232 and #151; anything
    else the eventual dataclass carries is invisible here.

    READ-ONLY members, declared as properties rather than as the plain
    ``metric: str`` a Protocol usually carries. A plain variable member
    demands a SETTABLE attribute, and the record #151 is contracted to
    supply is a ``@dataclass(frozen=True)``, whose attributes are
    read-only: written the obvious way, this Protocol rejects the one
    implementation it exists to describe, and would have done so at
    exactly the moment the two changes met.
    :func:`_health_breach_seam_accepts_the_contract` below is the guard
    for that: it hands mypy the mandated frozen shape where a
    ``_HealthBreach`` is expected, so ``uv run mypy kstrl/ --strict``
    fails here rather than in #151.
    """

    @property
    def metric(self) -> str: ...

    @property
    def rule(self) -> str: ...

    @property
    def value(self) -> float: ...

    @property
    def limit(self) -> float: ...

    @property
    def window_runs(self) -> int: ...


if TYPE_CHECKING:

    @dataclass(frozen=True)
    class _HealthBreachContract:
        """The record #151 is contracted to supply, spelled as the issue
        and ``docs/dark-factory-roadmap.md`` spell it."""

        metric: str
        rule: str
        value: float
        limit: float
        window_runs: int

    def _health_breach_seam_accepts_the_contract(
        breach: _HealthBreachContract,
    ) -> _HealthBreach:
        """A static assertion, and the only guard this seam can have.

        The seam reads a module ``importlib`` returns as ``Any``, so
        nothing about the two sides is checked where they meet. This
        function is checked: it hands mypy the FROZEN dataclass the
        contract mandates where a ``_HealthBreach`` is expected, so a
        Protocol that dataclass cannot satisfy fails
        ``uv run mypy kstrl/ --strict`` here rather than in #151.
        ``TYPE_CHECKING`` only - nothing calls it and nothing is
        constructed.
        """
        return breach


def _open_health_breach_items(
    root_dir: Path,
    breaches: list[_HealthBreach],
    *,
    run_id: str,
    ui: UI,
) -> None:
    """Write one advisory item per breach, non-fatally.

    Honours ``[inbox] enabled``; a failed write warns and never fails the
    run, because losing the notice must not also lose the demotion the
    caller goes on to apply.

    One guard PER BREACH, not one around the loop: a failure on the third
    of five would otherwise drop the remaining two as well, under a
    single warning that named none of them. The warning names the breach
    it lost, so the operator can tell which observation is missing.
    """
    try:
        inbox_config = InboxConfig.load(root_dir)
    except (OSError, TypeError, ValueError, ControlStateError) as exc:
        # TypeError is this callee's own surface: InboxConfig.load casts
        # per key, so `[inbox] open_item_cap = 1979-05-27` (a valid TOML
        # date) raises it rather than a ValueError, and a raw traceback
        # from a seam whose whole contract is "bookkeeping cannot fail a
        # run" is the outcome this guard exists to prevent.
        ui.warn(f"Inbox write failed (non-fatal): {exc}")
        return
    if not inbox_config.enabled:
        return
    inbox = Inbox(root_dir, inbox_config)
    for breach in breaches:
        try:
            inbox.add(
                ItemKind.HEALTH_BREACH,
                f"Health breach: {breach.metric} {breach.rule}",
                detail=(
                    f"value {breach.value} beyond limit {breach.limit} "
                    f"over {breach.window_runs} run(s)"
                ),
                run_id=run_id,
                dedupe_key=f"health:{breach.metric}:{breach.rule}",
                evidence={
                    "metric": breach.metric,
                    "rule": breach.rule,
                    "value": breach.value,
                    "limit": breach.limit,
                    "window_runs": breach.window_runs,
                },
            )
        except (OSError, ValueError, ControlStateError) as exc:
            # ControlStateError is a RuntimeError, raised by the control
            # lock every Inbox write takes.
            ui.warn(f"Inbox write failed for {breach.metric} {breach.rule} (non-fatal): {exc}")


def _record_health_breaches(
    root_dir: Path,
    state: AutonomyState,
    autonomy_config: AutonomyConfig,
    *,
    run_id: str,
    ui: UI,
    bus: EventBus,
) -> None:
    """Open an inbox item per R8.4 control-limit breach, and maybe demote.

    Inert until ``kstrl/health.py`` exists (#151). The import guard
    swallows exactly one thing: THAT module not being importable at all,
    which is what ``exc.name != "kstrl.health"`` decides. A
    ``kstrl.health`` that exists and fails on a dependency the operator
    has not installed raises ``ModuleNotFoundError`` naming that
    dependency, and must reach the caller's "Autonomy state update
    failed" warning rather than read as "#151 has not landed". A
    ``kstrl.health`` that exists and has renamed ``health_breaches``
    raises ``AttributeError`` and must do the same, so the attribute
    lookup is INSIDE the guarded block where widening the clause can
    swallow it and a test can see that it did. A seam that disarms itself
    silently is worse than one that is loudly broken, and a bare
    ``except ImportError`` cannot tell the two cases apart.

    Advisory first: the item is always written, the demotion happens only
    under ``[autonomy] demote_on_health_breach``. It is additionally
    suppressed while a cool-down is running, because a breach is a
    WINDOWED TREND rather than an event: it persists across runs, so an
    ungated trigger would take one level per run down to L1 before the
    operator had read the first notice. The suppression is announced, not
    silent: a windowed trend can hold for the whole cool-down, and an
    unexplained still level is the failure this ladder is meant to make
    visible.

    ``autonomy_config`` is the run's own snapshot, taken once at run
    start, rather than a second read here: a run must not have its
    permissions decided by one resolution of ``[autonomy]`` and its
    demotion by another.

    The false-alarm arithmetic #151 must choose its rule set against is
    in ``docs/dark-factory-roadmap.md`` under R8.4, with the cost of a
    false alarm attached. It is written there once rather than here as
    well: two copies of an arithmetic nothing keeps in step is how one of
    them goes wrong.
    """
    try:
        health = importlib.import_module("kstrl.health")
        breaches_of = health.health_breaches
    except ModuleNotFoundError as exc:
        if exc.name != "kstrl.health":
            raise
        return
    # OUTSIDE the guard on purpose: a ModuleNotFoundError raised by
    # #151's own code, for its own missing dependency, is that module
    # being broken and not this one being absent.
    breaches: list[_HealthBreach] = list(breaches_of(root_dir))
    if not breaches:
        return
    _open_health_breach_items(root_dir, breaches, run_id=run_id, ui=ui)
    if not autonomy_config.demote_on_health_breach:
        return
    if state.cooldown_runs_remaining > 0:
        ui.warn(
            f"Autonomy: {len(breaches)} health breach(es) recorded; not demoting during "
            f"cool-down ({state.cooldown_runs_remaining} decisive run(s) remaining)"
        )
        return
    first = breaches[0]
    apply_demotion(
        root_dir,
        DemotionTrigger.HEALTH_BREACH,
        f"{first.metric}: {first.rule}",
        evidence={
            "breaches": [
                {
                    "metric": breach.metric,
                    "rule": breach.rule,
                    "value": breach.value,
                    "limit": breach.limit,
                    "window_runs": breach.window_runs,
                }
                for breach in breaches
            ],
            "run_id": run_id,
        },
        run_id=run_id,
        ui=ui,
        bus=bus,
        state=state,
    )


#: The failed_check values that say a human turned a merge candidate
#: down: rejected at the merge gate (interactively or with ``ks inbox
#: reject``), or its PR closed without merging while kstrl waited (#601).
#: Read from ``kstrl.pipeline``, the one place that spells them, so a
#: rename there cannot silently switch off the L3 rejection demotion.
HUMAN_REJECTION_CHECKS = (HITL_REJECT_CHECK, PR_CLOSED_CHECK)


def _classify_merges(manifest: Manifest, merged: dict[str, str]) -> dict[str, str]:
    """One verdict per merged component id: ``"clean"``, ``"edited"`` or ``"head unknown"``.

    A merge is clean only when the PR head GitHub merged is the commit
    the diff phase judged (``Component.judged_sha``). A commit pushed to
    the PR, or made in the worktree while the checkpoint prompt was open,
    makes them differ. When either commit is unknown the merge counts as
    edited: a promotion input must not be satisfied by what kstrl could
    not observe (#601).

    Pure (no state mutation) so ``kstrl.evolution``'s journal writer can
    call it to size the ``merged``/``clean_merged`` TSV columns from the
    same classification the live ladder uses, rather than a second
    definition of "clean" that could drift from this one.
    """
    judged = {comp.id: comp.judged_sha for comp in manifest.components}
    result: dict[str, str] = {}
    for comp_id, head_sha in merged.items():
        judged_sha = judged.get(comp_id, "")
        if not judged_sha or not head_sha:
            result[comp_id] = "head unknown"
        elif judged_sha == head_sha:
            result[comp_id] = "clean"
        else:
            result[comp_id] = "edited"
    return result


def _record_merges(
    state: AutonomyState, manifest: Manifest, merged: dict[str, str]
) -> dict[str, list[str]]:
    """Count each merge this run confirmed; return the parts by verdict."""
    by_id = _classify_merges(manifest, merged)
    verdicts: dict[str, list[str]] = {"clean": [], "edited": [], "head unknown": []}
    for comp_id, verdict in by_id.items():
        state.record_merged_component(human_edited=verdict != "clean")
        verdicts[verdict].append(comp_id)
    return verdicts


def _human_rejections(
    manifest: Manifest, factory_result: FactoryResult, run_level: AutonomyLevel
) -> list[str]:
    """The parts a human turned down in a run operating at L3 or above.

    ``run_level`` is the run's clamped level, not the stored one: a
    stored L3 clamped to L2 ran an L2 gate, and a rejection there is the
    gate working, not evidence against L3 (#601). Required: the only
    caller runs inside ``if ladder is not None:``, and the ladder is None
    only when autonomy is disabled, so a resolved level always exists
    here.
    """
    if run_level < AutonomyLevel.L3_ENVELOPED_AUTO:
        return []
    by_id = {comp.id: comp for comp in manifest.components}
    return sorted(
        {
            comp_id
            for comp_id in factory_result.failed
            if comp_id in by_id and by_id[comp_id].failed_check in HUMAN_REJECTION_CHECKS
        }
    )


def _demote_once(
    root_dir: Path,
    state: AutonomyState,
    *,
    violations: list[str],
    rejected: list[str],
    run_id: str,
    ui: UI,
    bus: EventBus,
) -> None:
    """Fire at most one demotion for this run.

    A policy violation wins and carries any human rejection on its
    evidence, so one run never drops two levels (#601). Whether one fires
    is always ``bool(violations or rejected)``, the caller's own inputs,
    so this returns nothing rather than a bool that would only restate
    them back.
    """
    if violations:
        evidence: dict[str, Any] = {"components": violations, "run_id": run_id}
        if rejected:
            evidence["human_rejected"] = rejected
        apply_demotion(
            root_dir,
            DemotionTrigger.POLICY_VIOLATION,
            f"policy violation in {', '.join(violations)}",
            evidence=evidence,
            run_id=run_id,
            ui=ui,
            bus=bus,
            state=state,
        )
        return
    if rejected:
        apply_demotion(
            root_dir,
            DemotionTrigger.HUMAN_REJECTED_AUTO_MERGE,
            f"a human rejected {', '.join(rejected)} at L3 or above",
            evidence={"components": rejected, "run_id": run_id},
            run_id=run_id,
            ui=ui,
            bus=bus,
            state=state,
        )
        return


def _would_demote(violations: list[str], rejected: list[str]) -> bool:
    """Whether ``_demote_once`` would fire: either input on its own is enough."""
    return bool(violations or rejected)


def _evidence_line(
    state: AutonomyState, verdicts: dict[str, list[str]], factory_result: FactoryResult
) -> str:
    """The "Autonomy evidence" line: the counters, then this run's merges by name."""
    parts = [
        f"L{state.level}: {state.decisive_runs_at_level} decisive run(s), "
        f"{state.components_merged_at_level} merged, "
        f"{state.clean_merges_at_level} clean in a row"
    ]
    parts.extend(f"{verdict}: {', '.join(ids)}" for verdict, ids in verdicts.items() if ids)
    unmerged = [c for c in factory_result.completed if c not in factory_result.merged]
    if unmerged:
        parts.append(f"completed without a merge: {', '.join(unmerged)}")
    return "; ".join(parts)


def _record_autonomy_outcome(
    *,
    root_dir: Path,
    manifest: Manifest,
    factory_result: FactoryResult,
    autonomy_config: AutonomyConfig,
    run_level: AutonomyLevel,
    bus: EventBus,
    run_id: str,
    ui: UI,
) -> None:
    """Fold one run's terminal outcome into the persisted ladder state.

    Evidence accrues only from what the run actually demonstrated:

    - **Decisive run**: at least one component reached a terminal verdict
      that was actually ABOUT the factory's judgement. A run that produced
      nothing, or whose components died on infrastructure (git diff
      failure, timeout kill, agent crash), says nothing about whether the
      factory can be trusted with more autonomy - counting those would let
      a string of broken runs burn down a cool-down and accrue promotion
      evidence. This mirrors the replay tool's decisive-run definition;
      the two must agree or the replay predicts nothing.
    - **Merged components**: the parts whose merge GitHub confirmed in
      this run (``factory_result.merged``), never the completed set: a
      part completed with no PR, in single-PR mode or without ``gh``
      merged nothing. Each is clean or edited by ``_record_merges``.
    - **Human rejections** (#601): in a run operating at L3 or above, a
      part rejected at the merge gate or whose PR was closed unmerged
      demotes with ``HUMAN_REJECTED_AUTO_MERGE``, once per run: when a
      policy violation already demotes, the rejection goes on that
      transition's evidence.
    - **Policy violations**: any component carrying an R8.1 policy finding.
      These both block promotion AND fire an immediate demotion, because a
      breach of the envelope is the clearest evidence that the current
      level is not warranted.
    - **Health breaches** (R8.4, seam only until #151): control-limit
      breaches over run metrics, opened as inbox items on every path and
      demoting only under an explicit switch. Read from ``kstrl.health``
      if that module exists; see ``_record_health_breaches``.

    Automatic demotion happens here rather than mid-run: demoting while
    components are still executing would change the flag bundle underneath
    them, and the run's own PRs were already gated by the level in force
    when it started.
    """
    state = AutonomyState.load(root_dir)

    by_id = {comp.id: comp for comp in manifest.components}

    def _infra_casualty(comp_id: str) -> bool:
        comp = by_id.get(comp_id)
        if comp is None:
            return False
        return any(f.is_infrastructure_error for f in comp.findings)

    judged_failures = [comp_id for comp_id in factory_result.failed if not _infra_casualty(comp_id)]
    decisive = bool(factory_result.completed or judged_failures)
    if decisive:
        state.record_decisive_run()
    verdicts = _record_merges(state, manifest, factory_result.merged)

    violations = [
        comp.id
        for comp in manifest.components
        if any(
            finding.category.startswith(POLICY_CATEGORY_PREFIX) and finding.severity != "advisory"
            for finding in comp.findings
        )
    ]
    if violations:
        state.record_policy_violation(len(violations))
    rejected = _human_rejections(manifest, factory_result, run_level)
    demoted = _would_demote(violations, rejected)
    _demote_once(
        root_dir,
        state,
        violations=sorted(violations),
        rejected=rejected,
        run_id=run_id,
        ui=ui,
        bus=bus,
    )
    if not demoted:
        # Through the same save every other path uses, which is what
        # refuses to overwrite a file ``load`` failed closed on. This
        # branch is the one an ordinary run takes, and it was the branch
        # a guard placed in the demotion path could not see.
        save_ladder_state(state, root_dir, ui)
        if decisive:
            ui.kv("Autonomy evidence", _evidence_line(state, verdicts, factory_result))

    # The health seam runs on every path, the demoting one included: the
    # cool-down that demotion just set is what then stops a second
    # revocation inside one run.
    _record_health_breaches(root_dir, state, autonomy_config, run_id=run_id, ui=ui, bus=bus)


@dataclass(frozen=True)
class _LadderOutcome:
    """What the R8.2 ladder decided, split from what it PRINTS (#192).

    The decision has to happen before ``ComponentPipeline`` is built,
    because the bundle can clamp the policy envelope and the pipeline
    must be handed the clamped one rather than have it assigned 155
    lines later. The reporting has to happen where it always did: the
    event bus and its file sinks do not exist yet at the decision point,
    and ``_adversarial_phase_gates`` runs in between and must keep
    reading the CONFIGURED ``review_mode`` rather than the bundle's.
    Splitting them is what lets both be true at once.
    """

    level: AutonomyLevel
    bundle: FlagBundle
    #: Why ``resolve_runtime_level`` lowered the stored level. A tuple,
    #: because ``frozen=True`` over a ``list`` is a claim the class does
    #: not keep: ``hash()`` raised ``TypeError`` and ``.append()``
    #: succeeded straight through the frozen dataclass.
    clamps: tuple[str, ...]
    #: Configured flags the bundle overruled.
    overrides: tuple[str, ...]


def _resolve_ladder(
    run_envelope: RunEnvelope,
    factory_config: FactoryConfig,
    root_dir: Path,
) -> tuple[RunEnvelope, _LadderOutcome | None]:
    """The run's clamped envelope, and the ladder decision behind it.

    ``None`` when ``[autonomy]`` is disabled, which is the default: the
    config's own flags stand and the envelope is returned untouched.

    Reads the stored state off the envelope rather than loading it
    again. The second ``AutonomyState.load`` this replaces cost a
    measured 17.2 ms, discarded its result, ensured the control state a
    second time, and left a window in which a concurrent `ks autonomy
    promote` made the two reads disagree.
    """
    state = run_envelope.autonomy_state
    # Two spellings of one condition, and that is deliberate: the
    # envelope loads the stored state only when the ladder is on, so
    # ``state is None`` and ``not enabled`` are the same fact. The
    # second is the half a type checker can see, and writing it as an
    # assert instead would put a raise on the default path.
    if not run_envelope.autonomy.enabled or state is None:
        return run_envelope, None
    level, clamps = resolve_runtime_level(
        state,
        run_envelope.autonomy,
        policy_enabled=run_envelope.policy.enabled,
        root_dir=root_dir,
    )
    # #192: the level the run OPERATES at. Phase 1 and the claim
    # gate re-read the RAW stored level: measured, a run clamped to L1
    # had Phase 1 judging at the stored L4. No verdict changes at either
    # level today (both consumers test only >= 1); it goes live the
    # moment either threshold becomes level-graded.
    clamped = replace(run_envelope, autonomy_level=int(level))
    bundle = flag_bundle_for(level)
    pause_explicit = "pause_before_pr_merge" in factory_config.explicit_fields
    overrides = manual_override_notes(
        bundle,
        configured_pause_before_pr_merge=factory_config.pause_before_pr_merge,
        configured_review_mode=factory_config.review_mode,
        pause_before_pr_merge_explicit=pause_explicit,
    )
    # #195: the bundle may RAISE this gate and may not lower one the
    # operator set. `pause_gate_for` is the only place that rule is
    # written; the notes above ask it what it returned rather than
    # keeping a second copy.
    #
    # Rebound to the bundle the run USES, not the one the level awarded,
    # because the event this feeds is what makes a run's permissions
    # auditable: `bundle.describe()` would otherwise record "merge gate:
    # off" for a run that pauses at every component, and rebinding that
    # ONE field would leave "auto-merge when green: yes" beside "merge
    # gate: ON". `resolved_flag_bundle` moves every dependent flag
    # together. Ordered after `manual_override_notes`, which compares the
    # CONFIGURED value against what the LEVEL awarded.
    bundle = resolved_flag_bundle(
        bundle,
        configured=factory_config.pause_before_pr_merge,
        explicit=pause_explicit,
    )
    return clamped, _LadderOutcome(
        level=level, bundle=bundle, clamps=tuple(clamps), overrides=tuple(overrides)
    )


def _record_autonomy(ctx: RunContext) -> None:
    """Fold the run's outcome into the ladder, when the run has a ladder."""
    # R8.2: fold this run's outcome into the ladder. Without this the
    # counters never move, promotion is unreachable except by --force,
    # and the promised automatic demotion never fires - the state machine
    # would be real but inert. Runs INSIDE the factory lock, before it is
    # released, so two runs cannot interleave read-modify-write.
    if ctx.ladder is not None:
        try:
            _record_autonomy_outcome(
                root_dir=ctx.root_dir,
                manifest=ctx.manifest,
                factory_result=ctx.factory_result,
                autonomy_config=ctx.autonomy_config,
                run_level=ctx.ladder.level,
                bus=ctx.bus,
                run_id=ctx.run_id,
                ui=ctx.ui,
            )
        except Exception as exc:  # noqa: BLE001 - never fail a run on bookkeeping
            ctx.ui.warn(f"Autonomy state update failed (non-fatal): {exc}")


def _report_ladder(
    ladder: _LadderOutcome,
    factory_config: FactoryConfig,
    bus: EventBus,
    ui: UI,
) -> None:
    """Apply the bundle of the level to the config and tell the operator."""
    factory_config.pause_before_pr_merge = ladder.bundle.pause_before_pr_merge
    factory_config.review_mode = ladder.bundle.review_mode
    bus.emit(
        AutonomyLevelApplied(
            level=int(ladder.level),
            label=ladder.level.label,
            flags=tuple(ladder.bundle.describe()),
            overrides=tuple(ladder.clamps + ladder.overrides),
        )
    )
    ui.kv("Autonomy", f"L{int(ladder.level)} - {ladder.level.label}")
    for note in ladder.clamps:
        ui.warn(f"  {note}")
    # Each note carries its own verdict since #195: one of them
    # reports a gate the run RETAINED, and a blanket "Manual override
    # ignored" prefix would have contradicted it.
    for note in ladder.overrides:
        ui.warn(f"  {note}")
