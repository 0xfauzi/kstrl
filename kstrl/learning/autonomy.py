"""R8.2 autonomy ladder: earned, bounded, revocable autonomy (L1-L4).

Autonomy today is a scatter of independent flags (``pause_before_pr_merge``,
``review_mode``, deploy). Any of them can be flipped in
isolation, so "how much is this factory allowed to do without me?" has no
single answer and no audit record. This module makes autonomy one ordered,
named level with a derived flag bundle, so the question has exactly one
answer at any moment and every change of that answer is journaled.

The shape is borrowed from continuous-authorization practice, not invented
here: autonomy is **earned** (entry criteria backed by evidence), **bounded**
(the R8.1 policy envelope defines what even L3 may touch), **continuously
monitored**, and **revocable** with automatic reversion to human-gated mode.

Three invariants carry the trust:

1. **Agents cannot promote themselves.** Promotion requires evidence, a
   named actor, an ack, AND an interactive terminal - a signal an
   unattended agent subprocess does not have (``--actor``/``--ack`` are
   just strings any caller could pass, so they prove nothing alone). The
   live state file lives outside the agent-reachable tree (XDG control
   dir, R8.9); the legacy in-tree path and this module remain in the
   R8.1 enforcement-machinery halt set so a diff cannot rewrite either.
   L3+ additionally refuses to proceed while control state still
   resolves in-tree (migration incomplete or ``XDG_STATE_HOME`` under
   the repo).
2. **Fast down, slow up.** Demotion is automatic and immediate on a trigger;
   re-promotion is locked for a cool-down period afterwards.
3. **The flag bundle is derived, never stored.** It is computed from the
   level at run start, so editing a flag by hand cannot silently grant
   autonomy the ladder never awarded - and a config that contradicts the
   level is recorded as a manual override rather than honored in silence.
   The rule is one-directional (#195). The bundle may WITHHOLD autonomy,
   and it may raise a gate over a value the operator never wrote; it may
   not remove a human merge gate the operator set explicitly.
   :func:`pause_gate_for` is the one place that asymmetry is written, and
   ``FactoryConfig.explicit_fields`` is how the run knows which of the
   two an incoming ``pause_before_pr_merge`` is.

Opt-in (``[autonomy] enabled = false``): L1 is stricter than today's
defaults (it forces the merge gate on), so enabling the ladder must be a
deliberate act rather than a surprise upgrade for existing repos.

**Every threshold in this module is an UNMEASURED PLACEHOLDER** (the R8
"no assumed thresholds" rule). ``kstrl.learning.autonomy_replay`` replays them
against historical run data and reports what would have fired; until that
output is recorded in ``docs/dark-factory-roadmap.md``, no threshold here
should be trusted to gate anything.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.io import (
    CONTROL_AUTONOMY,
    atomic_write_json,
    control_file,
    control_lock,
    ensure_control_state,
    legacy_control_refusal,
    legacy_control_remaining,
    read_json,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


STATE_SCHEMA_VERSION = 1


class AutonomyLevel(IntEnum):
    """Ordered autonomy levels, defined by the human's remaining role."""

    L1_SUPERVISED = 1  # human approves the plan AND the merge
    L2_GATED_MERGE = 2  # plans auto-accepted; human still gates merge
    L3_ENVELOPED_AUTO = 3  # merge auto when fully green AND inside envelope
    L4_DEPLOY = 4  # L3 plus the release stage (R8.7)

    @property
    def label(self) -> str:
        return {
            AutonomyLevel.L1_SUPERVISED: "L1 Supervised",
            AutonomyLevel.L2_GATED_MERGE: "L2 Gated-merge",
            AutonomyLevel.L3_ENVELOPED_AUTO: "L3 Enveloped auto-merge",
            AutonomyLevel.L4_DEPLOY: "L4 Deploy",
        }[self]


class DemotionTrigger(IntEnum):
    """Why a level was revoked. Each demotion drops exactly one level."""

    POLICY_VIOLATION = 1  # R8.1 envelope breach
    CALIBRATION_REGRESSION = 2  # adversarial detection rate fell
    HEALTH_BREACH = 3  # R8.4 control-limit breach
    HUMAN_REJECTED_AUTO_MERGE = 4  # a human rejected an L3 candidate
    MANUAL = 5  # operator demoted by hand

    @property
    def label(self) -> str:
        return self.name.lower()


#: The ``ks autonomy demote --trigger`` choices, one per trigger (#565).
DEMOTION_TRIGGER_LABELS: tuple[str, ...] = tuple(trigger.label for trigger in DemotionTrigger)


# ---------------------------------------------------------------------------
# Thresholds - ALL UNMEASURED PLACEHOLDERS (R8 "no assumed thresholds")
# ---------------------------------------------------------------------------
# Every number below is a guess taken from the roadmap table. None has been
# replayed against real run data yet, and with the data on hand (see
# `ks autonomy replay`) none can be. They live together, named, so the
# replay tool can report on them and a future measured value replaces one
# constant rather than a scattered literal.

#: Components that must merge cleanly at L1 before L2 is offered.
L2_MERGED_COMPONENTS_REQUIRED = 5
#: Consecutive L2 merges approved without human edits before L3 is offered.
L3_CLEAN_MERGES_REQUIRED = 15
#: Components merged while holding L3 before L4 is offered.
L4_MERGED_COMPONENTS_REQUIRED = 30
#: Decisive runs during which re-promotion is locked after any demotion.
#: "Fast down, slow up" - the cool-down is the slow part.
DEMOTION_COOLDOWN_RUNS = 10
#: Minimum decisive runs before ANY automatic transition may fire. Guards
#: against demotion flapping on small-sample noise.
MIN_DECISIVE_RUNS = 8

#: Every threshold constant, for the replay tool and `ks autonomy status`.
THRESHOLDS: dict[str, int] = {
    "L2_MERGED_COMPONENTS_REQUIRED": L2_MERGED_COMPONENTS_REQUIRED,
    "L3_CLEAN_MERGES_REQUIRED": L3_CLEAN_MERGES_REQUIRED,
    "L4_MERGED_COMPONENTS_REQUIRED": L4_MERGED_COMPONENTS_REQUIRED,
    "DEMOTION_COOLDOWN_RUNS": DEMOTION_COOLDOWN_RUNS,
    "MIN_DECISIVE_RUNS": MIN_DECISIVE_RUNS,
}


class AutonomyError(RuntimeError):
    """A ladder transition was refused (criteria unmet, cool-down active,
    missing ack). Raised rather than returned so no caller can ignore a
    refusal and proceed as though autonomy had been granted."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _warn_rejected_state(path: Path, reason: str) -> str:
    """Warn that a ladder state was rejected; return the same sentence.

    Returned as well as warned so ``AutonomyState.load`` can put the
    identical text on the transient ``degraded_reason`` field. One
    string, two consumers: a second copy of the wording could drift from
    the warning an operator actually sees.
    """
    message = f"autonomy: rejected ladder state {path} ({reason}); failing closed to L1 Supervised"
    warnings.warn(message, RuntimeWarning, stacklevel=3)
    return message


def _warn_refused_save(path: Path, reason: str) -> str:
    """Warn that a degraded state was NOT written back; return the sentence.

    Same shape as ``_warn_rejected_state``: one string, warned here and
    returned so a caller holding a ``UI`` can put the identical text on
    the surface an operator is actually watching. Nothing is written, so
    repeated runs cannot pile up a record of this; what makes it visible
    on every run is that the refusal is re-reported every time a save is
    attempted, keyed by the file and the cause an operator has to fix.
    """
    message = (
        f"autonomy: ladder state is degraded ({reason}); refusing to overwrite {path}, "
        "which is the only record an operator can repair"
    )
    warnings.warn(message, RuntimeWarning, stacklevel=3)
    return message


def _require_int(data: dict[str, Any], key: str, default: int) -> int:
    """Read an int field strictly: a wrong TYPE is a rejection, not a coerce.

    ``bool`` is excluded explicitly - it is an int subclass in Python, and
    silently reading ``true`` as ``1`` would launder a malformed record
    into a plausible-looking counter.
    """
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer, got {type(value).__name__}")
    return value


def _parse_history(raw: Any) -> list[Transition]:
    """Parse the transition history, rejecting any malformed entry."""
    if not isinstance(raw, list):
        raise ValueError("history must be an array")
    history: list[Transition] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("history entries must be objects")
        evidence = item.get("evidence", {}) or {}
        if not isinstance(evidence, dict):
            raise ValueError("history evidence must be an object")
        for text_key in ("at", "direction", "actor", "reason", "trigger"):
            if text_key in item and not isinstance(item[text_key], str):
                raise ValueError(f"history {text_key} must be a string")
        history.append(
            Transition(
                at=str(item.get("at", "")),
                from_level=_require_int(item, "from_level", 1),
                to_level=_require_int(item, "to_level", 1),
                direction=str(item.get("direction", "")),
                actor=str(item.get("actor", "")),
                reason=str(item.get("reason", "")),
                trigger=str(item.get("trigger", "")),
                evidence=evidence,
            )
        )
    return history


@dataclass(frozen=True)
class FlagBundle:
    """The permissions a level grants, derived fresh at run start.

    Never persisted: storing it would let the stored copy drift from the
    level that justified it. ``deploy_permitted`` is a ceiling the R8.7
    release config must also agree to - the ladder can only ever withhold
    permission, never grant something that gate denies.

    ``pause_before_pr_merge`` is the one flag with a ceiling on the OTHER
    side as well (#195): the bundle's False is a permission to auto-merge
    that an explicit operator request may refuse. Read it through
    :func:`pause_gate_for` rather than assigning it straight onto a
    config.
    """

    level: AutonomyLevel
    pause_before_pr_merge: bool
    review_mode: str
    auto_accept_plan: bool
    auto_merge_when_green: bool
    deploy_permitted: bool

    def describe(self) -> list[str]:
        return [
            f"merge gate: {'ON (human approves)' if self.pause_before_pr_merge else 'off'}",
            f"review mode: {self.review_mode}",
            f"plans: {'auto-accepted' if self.auto_accept_plan else 'human-approved'}",
            f"auto-merge when green: {'yes' if self.auto_merge_when_green else 'no'}",
            f"deploy: {'permitted' if self.deploy_permitted else 'blocked'}",
        ]


def flag_bundle_for(level: AutonomyLevel) -> FlagBundle:
    """Derive the flag bundle a level grants.

    L1 is deliberately stricter than the harness defaults (merge gate ON,
    hard review): the ladder's floor is "human approves everything", not
    "whatever the config happened to say".
    """
    if level is AutonomyLevel.L1_SUPERVISED:
        return FlagBundle(
            level=level,
            pause_before_pr_merge=True,
            review_mode="hard",
            auto_accept_plan=False,
            auto_merge_when_green=False,
            deploy_permitted=False,
        )
    if level is AutonomyLevel.L2_GATED_MERGE:
        return FlagBundle(
            level=level,
            pause_before_pr_merge=True,
            review_mode="hard",
            auto_accept_plan=True,
            auto_merge_when_green=False,
            deploy_permitted=False,
        )
    if level is AutonomyLevel.L3_ENVELOPED_AUTO:
        return FlagBundle(
            level=level,
            pause_before_pr_merge=False,
            review_mode="hard",
            auto_accept_plan=True,
            auto_merge_when_green=True,
            deploy_permitted=False,
        )
    return FlagBundle(
        level=AutonomyLevel.L4_DEPLOY,
        pause_before_pr_merge=False,
        review_mode="hard",
        auto_accept_plan=True,
        auto_merge_when_green=True,
        deploy_permitted=True,
    )


@dataclass(frozen=True)
class Transition:
    """One recorded level change. Append-only history."""

    at: str
    from_level: int
    to_level: int
    direction: str  # "promote" | "demote"
    actor: str  # human identity for promotions; "system" for auto
    reason: str
    trigger: str = ""  # DemotionTrigger label, demotions only
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class AutonomyState:
    """Persisted ladder state (``.kstrl/autonomy.json``).

    Counters are per-level and reset on every transition: evidence earned
    at one level does not carry into the next, and a demotion does not
    leave a half-full promotion counter behind.
    """

    level: int = int(AutonomyLevel.L1_SUPERVISED)
    since: str = field(default_factory=_utc_now_iso)
    components_merged_at_level: int = 0
    clean_merges_at_level: int = 0
    policy_violations_at_level: int = 0
    decisive_runs_at_level: int = 0
    #: Decisive runs still to elapse before re-promotion is allowed.
    cooldown_runs_remaining: int = 0
    last_promoted_by: str = ""
    history: list[Transition] = field(default_factory=list)
    #: Why ``load`` discarded the stored record and fell back to a fresh
    #: L1 state, or None when nothing was discarded. TRANSIENT: set by
    #: ``load`` on its fail-closed paths, never parsed from disk, and
    #: never written by ``save`` (whose payload is built key by key, not
    #: from ``asdict``). It exists so ``safemode.safe_mode_reasons`` can
    #: report the fallback without re-implementing the parser that
    #: detected it.
    degraded_reason: str | None = None

    @property
    def autonomy_level(self) -> AutonomyLevel:
        return AutonomyLevel(self.level)

    # -- persistence -------------------------------------------------------
    @classmethod
    def path_for(cls, root_dir: Path) -> Path:
        return control_file(root_dir, CONTROL_AUTONOMY)

    @classmethod
    def _first_run(cls, root_dir: Path, path: Path) -> AutonomyState:
        """The state for a control directory with no record.

        An in-tree record that kstrl no longer moves is NOT a first run:
        reading the empty control directory as one would drop an earned
        level without a word (#863).
        """
        if CONTROL_AUTONOMY not in legacy_control_remaining(root_dir):
            return cls()
        return cls(
            degraded_reason=_warn_rejected_state(
                path,
                legacy_control_refusal(root_dir, (CONTROL_AUTONOMY,)),
            ),
        )

    @classmethod
    def load(cls, root_dir: Path) -> AutonomyState:
        """Read state, failing CLOSED to L1 on anything unrecognizable.

        Every field is validated, not just the level: a syntactically
        valid file carrying ``"components_merged_at_level": "not-an-int"``
        must not crash `ks autonomy status` or an enabled factory run.
        Any malformed field, history entry, or out-of-range level
        discards the whole file and returns a fresh L1 state, because the
        safe direction for unknown autonomy is the LEAST autonomy - never
        a partially-trusted level assembled from a damaged record.

        The rejection is warned about rather than silent (mirroring
        ``knowledge.read_facts``): losing an earned level deserves a note.
        It is also recorded on the returned state's transient
        ``degraded_reason``, because a warning is only seen by whoever
        was watching the terminal at the time, and ``ks status`` needs to
        answer the question afterwards.

        A missing file is NOT a fallback: that is first run, and
        ``degraded_reason`` stays None.
        """
        ensure_control_state(root_dir)
        path = cls.path_for(root_dir)
        if not path.exists():
            return cls._first_run(root_dir, path)
        try:
            data = read_json(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return cls(
                degraded_reason=_warn_rejected_state(
                    path,
                    f"unreadable: {exc}",
                ),
            )
        except UnicodeDecodeError as exc:
            # Its own reason string, because this one is surfaced: it
            # reaches the warning AND ``ks status`` through
            # ``degraded_reason``, and "unreadable" would send the
            # operator to the file's permissions when the permissions
            # are fine and the bytes are not. Failing to L1 is unchanged;
            # only the explanation is.
            return cls(
                degraded_reason=_warn_rejected_state(
                    path,
                    f"not valid UTF-8: {exc}",
                ),
            )
        if not isinstance(data, dict):
            return cls(
                degraded_reason=_warn_rejected_state(
                    path,
                    "top-level value is not an object",
                ),
            )
        try:
            level = int(AutonomyLevel(_require_int(data, "level", 1)))
            history = _parse_history(data.get("history", []))
            since = data.get("since", "")
            if not isinstance(since, str):
                raise ValueError("since must be a string")
            promoted_by = data.get("last_promoted_by", "")
            if not isinstance(promoted_by, str):
                raise ValueError("last_promoted_by must be a string")
            state = cls(
                level=level,
                since=since or _utc_now_iso(),
                components_merged_at_level=_require_int(
                    data,
                    "components_merged_at_level",
                    0,
                ),
                clean_merges_at_level=_require_int(data, "clean_merges_at_level", 0),
                policy_violations_at_level=_require_int(
                    data,
                    "policy_violations_at_level",
                    0,
                ),
                decisive_runs_at_level=_require_int(data, "decisive_runs_at_level", 0),
                cooldown_runs_remaining=_require_int(
                    data,
                    "cooldown_runs_remaining",
                    0,
                ),
                last_promoted_by=promoted_by,
                history=history,
            )
        except (ValueError, TypeError) as exc:
            return cls(degraded_reason=_warn_rejected_state(path, str(exc)))
        return state

    def save(self, root_dir: Path) -> str | None:
        """Atomic write, through the one helper that owns the pattern (#291).

        Refuses when this state came back from a ``load`` that failed
        closed. Saving a fresh L1 over damaged bytes destroys the only
        thing an operator could have repaired, and the counters the save
        would carry were lost with the file they came from anyway. The
        next ``load`` would then find a clean file, so nothing would ever
        report the damage again.

        The refusal lives HERE, in the one function that writes
        ``autonomy.json``, rather than in the callers that remembered to
        ask: a branch-by-branch guard is only closed over the branches
        someone enumerated, and the branch an ordinary run takes was not
        one of them. This method is the module's only call to
        ``atomic_write_json`` and ``path_for`` its only path derivation,
        both checkable by grep.

        Returns None when the file was written, and the refusal sentence
        when it was not, so a caller holding a ``UI`` can report it on
        the surface the operator is watching rather than only as a
        ``RuntimeWarning``.
        """
        if self.degraded_reason is not None:
            return _warn_refused_save(self.path_for(root_dir), self.degraded_reason)
        ensure_control_state(root_dir)
        path = self.path_for(root_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "level": self.level,
            "since": self.since,
            "components_merged_at_level": self.components_merged_at_level,
            "clean_merges_at_level": self.clean_merges_at_level,
            "policy_violations_at_level": self.policy_violations_at_level,
            "decisive_runs_at_level": self.decisive_runs_at_level,
            "cooldown_runs_remaining": self.cooldown_runs_remaining,
            "last_promoted_by": self.last_promoted_by,
            "history": [asdict(h) for h in self.history],
        }
        with control_lock(root_dir):
            atomic_write_json(path, payload)
        return None

    # -- transitions -------------------------------------------------------
    def _reset_level_counters(self) -> None:
        self.components_merged_at_level = 0
        self.clean_merges_at_level = 0
        self.policy_violations_at_level = 0
        self.decisive_runs_at_level = 0

    def promotion_blockers(self, target: AutonomyLevel | None = None) -> list[str]:
        """Unmet criteria for the next level; empty means eligible.

        Returns human-readable reasons rather than a bool so `ks autonomy
        status` can show exactly what is missing and by how much.
        """
        current = self.autonomy_level
        target = target or AutonomyLevel(min(int(current) + 1, int(AutonomyLevel.L4_DEPLOY)))
        blockers: list[str] = []
        if int(target) <= int(current):
            blockers.append(f"already at {current.label}")
            return blockers
        if int(target) > int(current) + 1:
            blockers.append(f"cannot skip levels: {current.label} -> {target.label}")
        if self.cooldown_runs_remaining > 0:
            blockers.append(
                f"demotion cool-down active: {self.cooldown_runs_remaining} "
                "more decisive run(s) required"
            )
        if self.decisive_runs_at_level < MIN_DECISIVE_RUNS:
            blockers.append(
                f"insufficient evidence: {self.decisive_runs_at_level} decisive "
                f"run(s) at this level, need {MIN_DECISIVE_RUNS}"
            )
        if self.policy_violations_at_level:
            blockers.append(
                f"{self.policy_violations_at_level} policy violation(s) at this level; need zero"
            )
        if target is AutonomyLevel.L2_GATED_MERGE:
            if self.components_merged_at_level < L2_MERGED_COMPONENTS_REQUIRED:
                blockers.append(
                    f"{self.components_merged_at_level}/"
                    f"{L2_MERGED_COMPONENTS_REQUIRED} components merged at L1"
                )
        elif target is AutonomyLevel.L3_ENVELOPED_AUTO:
            if self.clean_merges_at_level < L3_CLEAN_MERGES_REQUIRED:
                blockers.append(
                    f"{self.clean_merges_at_level}/{L3_CLEAN_MERGES_REQUIRED} "
                    "consecutive merges approved without edits"
                )
        elif target is AutonomyLevel.L4_DEPLOY:
            if self.components_merged_at_level < L4_MERGED_COMPONENTS_REQUIRED:
                blockers.append(
                    f"{self.components_merged_at_level}/"
                    f"{L4_MERGED_COMPONENTS_REQUIRED} components merged while "
                    "holding L3"
                )
        return blockers

    def promote(
        self,
        actor: str,
        ack: str,
        *,
        signal_blockers: Sequence[str],
        force: bool = False,
        evidence: dict[str, Any] | None = None,
    ) -> Transition:
        """Raise one level. Requires a human actor AND an explicit ack.

        There is deliberately no unattended path into this method: an empty
        ``actor`` or ``ack`` raises. ``force`` records an override of unmet
        criteria - it still demands the ack, and the override is written
        into the transition's evidence so the audit trail shows the
        criteria were bypassed rather than met.

        ``signal_blockers`` are the unmet criteria no ladder counter holds,
        from :func:`entry_signal_blockers`. It has no default (#643): the
        calibration and health criteria had no reader because nothing
        asked for one, so every caller now states what it read, and
        ``autonomy_replay`` states that it read nothing.
        """
        if not actor.strip():
            raise AutonomyError("promotion requires an actor: agents cannot promote themselves")
        if not ack.strip():
            raise AutonomyError("promotion requires an explicit acknowledgement of the evidence")
        current = self.autonomy_level
        if current is AutonomyLevel.L4_DEPLOY:
            raise AutonomyError("already at the highest level (L4 Deploy)")
        target = AutonomyLevel(int(current) + 1)
        blockers = [*self.promotion_blockers(target), *signal_blockers]
        if blockers and not force:
            raise AutonomyError(
                f"cannot promote {current.label} -> {target.label}: " + "; ".join(blockers)
            )
        record = Transition(
            at=_utc_now_iso(),
            from_level=int(current),
            to_level=int(target),
            direction="promote",
            actor=actor,
            reason=ack,
            evidence={
                **(evidence or {}),
                "components_merged_at_level": self.components_merged_at_level,
                "clean_merges_at_level": self.clean_merges_at_level,
                "decisive_runs_at_level": self.decisive_runs_at_level,
                **({"forced_over_blockers": blockers} if blockers else {}),
            },
        )
        self.level = int(target)
        self.since = record.at
        self.last_promoted_by = actor
        self._reset_level_counters()
        self.history.append(record)
        return record

    def demote(
        self,
        trigger: DemotionTrigger,
        reason: str,
        *,
        actor: str = "system",
        evidence: dict[str, Any] | None = None,
    ) -> Transition | None:
        """Drop exactly one level and start the cool-down.

        Returns None at L1 (nothing below it) so a repeated trigger at the
        floor is a no-op rather than an error - the floor is already the
        safe state. Unlike promotion this needs no ack: revoking autonomy
        must never wait on a human.
        """
        current = self.autonomy_level
        if current is AutonomyLevel.L1_SUPERVISED:
            return None
        target = AutonomyLevel(int(current) - 1)
        record = Transition(
            at=_utc_now_iso(),
            from_level=int(current),
            to_level=int(target),
            direction="demote",
            actor=actor,
            reason=reason,
            trigger=trigger.label,
            evidence=evidence or {},
        )
        self.level = int(target)
        self.since = record.at
        self._reset_level_counters()
        self.cooldown_runs_remaining = DEMOTION_COOLDOWN_RUNS
        self.history.append(record)
        return record

    # -- evidence accumulation --------------------------------------------
    def record_decisive_run(self, count: int = 1) -> None:
        """Count a run that produced a verdict, and burn down the cool-down.

        Infra-aborted runs are NOT decisive: a run that died on a git push
        is not evidence about the factory's judgement, and counting it
        would let a string of broken runs unlock a promotion.
        """
        self.decisive_runs_at_level += count
        if self.cooldown_runs_remaining > 0:
            self.cooldown_runs_remaining = max(
                0,
                self.cooldown_runs_remaining - count,
            )

    def record_merged_component(self, *, human_edited: bool) -> None:
        """Count a merged component; edits break the clean-merge streak.

        ``human_edited`` has no default (#601): True when the commit
        GitHub merged is not the commit kstrl's gates judged, or either
        is unknown. Every caller states what it observed.
        """
        self.components_merged_at_level += 1
        if human_edited:
            self.clean_merges_at_level = 0
        else:
            self.clean_merges_at_level += 1

    def record_policy_violation(self, count: int = 1) -> None:
        self.policy_violations_at_level += count
