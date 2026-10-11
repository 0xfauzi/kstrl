"""R8.2 autonomy ladder: how the level changes.

The entry signals that no ladder counter holds, the authority a promotion
needs, the commit of one transition to the state file and both audit
streams, and the automatic demotion. The ladder itself (levels, triggers,
thresholds, state) is in :mod:`kstrl.autonomy`.
"""

from __future__ import annotations

import sys
import warnings
from typing import TYPE_CHECKING, Any

from kstrl.autonomy import MIN_DECISIVE_RUNS, AutonomyLevel, AutonomyState
from kstrl.statedir import ControlStateError

if TYPE_CHECKING:
    from pathlib import Path

    from kstrl.autonomy import DemotionTrigger, Transition
    from kstrl.events import EventBus
    from kstrl.ui.base import UI

#: The L4 entry criterion nothing can read yet (#643). A deploy target
#: belongs to the release stage, #154, which is deferred, so the criterion
#: is reported as unchecked rather than printed as met.
DEPLOY_TARGET_UNCHECKED = (
    "deploy target exists: not checked, because the release stage (#154) is not built"
)


def entry_signal_blockers(root_dir: Path, current: AutonomyLevel) -> list[str]:
    """Unmet entry criteria for the level above ``current`` that no ladder counter holds.

    ``AutonomyState.promotion_blockers`` reads the ladder's own counters.
    Three criteria in ``docs/dark-factory-roadmap.md`` R8.2 are not
    counters, and before #643 nothing read them, so `ks autonomy status`
    printed "Criteria met" with a calibration regression and a health
    breach open. Each is read here. A signal that cannot be read is a
    blocker, never a pass: this check CLEARS a promotion, so it is narrow.
    Empty at L4, which has no level above it.
    """
    if current is AutonomyLevel.L1_SUPERVISED:
        return _calibration_blockers(root_dir)
    if current is AutonomyLevel.L2_GATED_MERGE:
        return _health_blockers(root_dir)
    if current is AutonomyLevel.L3_ENVELOPED_AUTO:
        return [DEPLOY_TARGET_UNCHECKED]
    return []


def _calibration_blockers(root_dir: Path) -> list[str]:
    """L2's "calibration compare green": no undecided ``calibration_drift`` item.

    The measurement is ``python -m kstrl.calibration compare --root``, run
    by hand against kstrl's own baselines, and its one record in a project
    is the ``calibration_drift`` item ``kstrl.calibration_ladder`` opens on
    a regression. The absence of that item means something only while its
    writer is armed, which needs ``[autonomy]`` and ``[inbox]`` both
    enabled. A snoozed item is deferred, not decided, so it still blocks,
    and an unparseable inbox line could be one, so it blocks too.
    """
    from kstrl.autonomy_config import AutonomyConfig
    from kstrl.inbox import UNDECIDED, Inbox, InboxConfig, ItemKind

    try:
        ladder_enabled = AutonomyConfig.load(root_dir).enabled
        inbox_config = InboxConfig.load(root_dir)
    except Exception as exc:  # noqa: BLE001 - every unreadable config is the same refusal
        return [f"calibration compare: [autonomy] or [inbox] config could not be read ({exc})"]
    if not (ladder_enabled and inbox_config.enabled):
        return [
            "calibration compare: not recorded, because a regression opens a "
            "calibration_drift item only while [autonomy] and [inbox] are both enabled"
        ]
    inbox = Inbox(root_dir, inbox_config)
    scan = inbox.scan()
    if scan.unreadable:
        return [f"calibration compare: the inbox at {inbox.path} could not be read"]
    unparseable = scan.unparseable_count()
    if unparseable:
        return [
            f"calibration compare: {unparseable} line(s) of the inbox at {inbox.path} "
            "could not be parsed, and any of them could be a calibration_drift item"
        ]
    undecided = [
        item.id[:8]
        for item in scan.folded_items()
        if item.kind == ItemKind.CALIBRATION_DRIFT and item.status in UNDECIDED
    ]
    if undecided:
        return [
            f"calibration compare not green: {len(undecided)} undecided "
            f"calibration_drift inbox item(s): {', '.join(undecided)}"
        ]
    return []


def _health_blockers(root_dir: Path) -> list[str]:
    """L3's "health metrics inside limits", computed the way `ks health` computes it.

    From the recorded run history on every call, not from
    ``health_breach`` inbox items: an item records what one run saw, and
    says nothing about the history since. A metric with fewer than
    ``MIN_DECISIVE_RUNS`` decisive runs has no limits, so it blocks as
    unmeasured. A metric whose baseline never varied has no breach under
    the R8.4 rules, and `ks health` reports it the same way.
    """
    from kstrl.health import breach_lines, health_readings

    try:
        readings = health_readings(root_dir)
    except Exception as exc:  # noqa: BLE001 - every unreadable history is the same refusal
        return [f"health metrics: the recorded run history could not be read ({exc})"]
    unmeasured = [
        f"health metric {reading.metric} not measured: {len(reading.values)} "
        f"decisive run(s) record it, need {MIN_DECISIVE_RUNS}"
        for reading in readings
        if reading.mean is None
    ]
    breaches = [reading.breach for reading in readings if reading.breach is not None]
    outside = [
        f"health metric outside limits: {line.strip().removeprefix('- ')}"
        for line in breach_lines(breaches)
    ]
    return unmeasured + outside


def promotion_authority_error(*, force: bool) -> str | None:
    """Why this process may not authorize a promotion, or None if it may.

    ``--actor``/``--ack`` are strings any caller can supply, so on their
    own they prove nothing about who is asking: an unattended agent can
    pass ``--actor human``. The out-of-band signal is a controlling TTY -
    an interactive terminal the agent's subprocess does not have. Live
    autonomy state lives in the XDG control dir (R8.9); the legacy
    in-tree path remains in the R8.1 enforcement-machinery halt set so a
    diff cannot rewrite it. Stated plainly rather than overclaimed.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        detail = "forced promotions bypass evidence and " if force else ""
        return (
            f"promotion requires an interactive terminal ({detail}a "
            "caller-supplied --actor string does not establish human "
            "acknowledgement). Run this from a terminal; an unattended "
            "process cannot promote."
        )
    return None


def commit_transition(
    state: AutonomyState,
    record: Transition,
    root_dir: Path,
    *,
    bus: EventBus | None = None,
    run_id: str = "",
) -> None:
    """Persist a transition to state AND both audit streams, together.

    One function so the three writes cannot drift apart: before this, the
    CLI saved ``autonomy.json`` and nothing reached the journal, which made
    the documented "every transition is recorded" claim false. The state
    save happens first (it is the load-bearing one); audit-append failures
    are warned about, never fatal - losing the log must not strand the
    ladder in an unsaved state.

    ``bus`` is present only inside a factory run; CLI transitions have no
    run stream, so the evolution journal is their durable record.
    """
    if state.save(root_dir) is not None:
        # The state save is the load-bearing one, and it refuses to
        # overwrite a degraded file. Journalling and emitting a
        # transition the ladder did not take would record a level
        # nothing holds, which is the drift this function exists to
        # prevent. ``save`` has already warned, naming the file.
        return

    from kstrl.evolution import JOURNAL_SCHEMA_VERSION, EvolutionJournal
    from kstrl.evolution_config import EvolutionConfig

    entry = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "timestamp": record.at,
        "run_id": run_id,
        "event_type": "autonomy_transition",
        "direction": record.direction,
        "from_level": record.from_level,
        "to_level": record.to_level,
        "actor": record.actor,
        "trigger": record.trigger,
        "reason": record.reason,
        "evidence": record.evidence,
    }
    # load_or_none, not load: a typo in an [evolution] knob raises
    # ValueError, which this OSError guard does not catch, and the
    # ladder would then fail to record a transition it has already
    # SAVED - the exact drift this function exists to prevent.
    config = EvolutionConfig.load_or_none(
        root_dir,
        warn=lambda message: warnings.warn(message, RuntimeWarning, stacklevel=2),
    )
    # Through append_entries (the one writer of the line format, #312)
    # rather than a second raw open. EvolutionJournal is constructed
    # directly rather than through .open(), which would also gate on
    # config.enabled and stop recording transitions this function
    # records today.
    try:
        if config is not None:
            EvolutionJournal(config).append_entries([entry])
    except OSError as exc:
        warnings.warn(
            f"autonomy: journal append failed (non-fatal): {exc}",
            RuntimeWarning,
            stacklevel=2,
        )

    if bus is not None:
        from kstrl.event_catalog import AutonomyTransition

        bus.emit(
            AutonomyTransition(
                direction=record.direction,
                from_level=record.from_level,
                to_level=record.to_level,
                actor=record.actor,
                trigger=record.trigger,
                reason=record.reason,
            )
        )


def save_ladder_state(state: AutonomyState, root_dir: Path, ui: UI) -> None:
    """Persist ladder state, reporting a refused save on the run's surface.

    ``AutonomyState.save`` is what decides: it refuses to write over a
    file ``load`` already failed closed on, and warns through
    ``warnings.warn``, which nobody watching a run is reading. Putting
    the same sentence on the UI is the one thing every caller then has
    to remember, so it is here instead, in the one place both the
    demoting path and the ordinary path go through.
    """
    refused = state.save(root_dir)
    if refused is not None:
        ui.warn(refused)


def apply_demotion(
    root_dir: Path,
    trigger: DemotionTrigger,
    reason: str,
    *,
    evidence: dict[str, Any],
    run_id: str,
    ui: UI,
    bus: EventBus | None = None,
    state: AutonomyState | None = None,
) -> Transition | None:
    """Demote one level for ``trigger``, persist, emit, and open the notice.

    Returns None when already at the floor: L1 is the safe state, so a
    repeated trigger there is a no-op rather than an error. The state is
    still saved on that path, because the caller's own counters (a policy
    violation, say) were mutated before the demotion was attempted and
    they are what blocks the next promotion. One exception, decided by
    ``AutonomyState.save`` rather than here so that every caller gets it:
    a state that ``load`` already failed closed on is not written back,
    because saving a fresh L1 over damaged bytes destroys the only thing
    an operator could have repaired. The refusal is reported on this
    caller's ``ui`` as well as warned, because the operator is watching
    the run, not the warning stream.

    ``state`` is for callers that already hold a mutated, unsaved state.
    ``factory._record_autonomy_outcome`` counts the run's violations on an
    in-memory state and then demotes; re-loading here would silently drop
    that count. Callers with nothing pending pass nothing and this loads.

    Every automatic demotion goes through this one function so the four
    writes it performs - state, evolution journal, run event stream and
    inbox notice - cannot drift apart per trigger. An inbox failure is
    warned about, never fatal: losing the notice must not strand the
    ladder with a saved demotion and no record of why.
    """
    # Imported here rather than at module scope, matching
    # ``commit_transition`` above: the ladder state machine is imported
    # by config and safe-mode code that has no business pulling the
    # inbox in with it.
    from kstrl.inbox import Inbox, InboxConfig, ItemKind

    if state is None:
        state = AutonomyState.load(root_dir)
    trigger_text = trigger.label.replace("_", " ")
    record = state.demote(trigger, reason, evidence=evidence)
    if record is None:
        save_ladder_state(state, root_dir, ui)
        ui.warn(f"Autonomy: {trigger_text} recorded ({reason}); already at L1, nothing to revoke")
        return None
    commit_transition(state, record, root_dir, bus=bus, run_id=run_id)
    # R8.2 promised this and R8.3 delivers it: a demotion is exactly the
    # boundary condition an over-the-loop operator must see, and the
    # triggering evidence is perishable - it belongs on the item.
    try:
        inbox_config = InboxConfig.load(root_dir)
        if inbox_config.enabled:
            Inbox(root_dir, inbox_config).add(
                ItemKind.DEMOTION_NOTICE,
                f"Autonomy demoted L{record.from_level} -> L{record.to_level}",
                detail=record.reason,
                run_id=run_id,
                dedupe_key=f"demotion:{run_id}:{record.to_level}",
                evidence={
                    "trigger": record.trigger,
                    "from_level": record.from_level,
                    "to_level": record.to_level,
                    **record.evidence,
                },
            )
    except (OSError, TypeError, ValueError, ControlStateError) as exc:
        # The callee's surface, not an enumeration of believed causes.
        # ControlStateError is a RuntimeError, so the (OSError,
        # ValueError) pair every inbox site was written with does not
        # catch it - and every Inbox write takes the control lock, which
        # is where it comes from. TypeError is
        # InboxConfig.load's: it casts per key, so a TOML date or array
        # in [inbox] raises it. Escaping HERE is the worst of the
        # seven sites, because commit_transition has already saved the
        # demotion: the level would be revoked with no notice, no
        # DEMOTED line and a traceback in place of the caller's return.
        ui.warn(f"Inbox write failed (non-fatal): {exc}")
    ui.warn(
        f"Autonomy DEMOTED L{record.from_level} -> L{record.to_level} "
        f"({AutonomyLevel(record.to_level).label}) on {trigger_text}; cool-down "
        f"{state.cooldown_runs_remaining} decisive run(s)"
    )
    return record
