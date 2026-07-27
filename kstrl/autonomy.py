"""R8.2 autonomy ladder: earned, bounded, revocable autonomy (L1-L4).

Autonomy today is a scatter of independent flags (``pause_before_pr_merge``,
``review_mode``, ``deps_allow_new``, deploy). Any of them can be flipped in
isolation, so "how much is this factory allowed to do without me?" has no
single answer and no audit record. This module makes autonomy one ordered,
named level with a derived flag bundle, so the question has exactly one
answer at any moment and every change of that answer is journaled.

The shape is borrowed from continuous-authorization practice, not invented
here: autonomy is **earned** (entry criteria backed by evidence), **bounded**
(the R8.1 policy envelope defines what even L3 may touch), **continuously
monitored**, and **revocable** with automatic reversion to human-gated mode.

Three invariants carry the trust:

1. **Agents cannot promote themselves.** Promotion requires evidence AND a
   recorded human ack naming an actor. There is no code path that raises a
   level without one.
2. **Fast down, slow up.** Demotion is automatic and immediate on a trigger;
   re-promotion is locked for a cool-down period afterwards.
3. **The flag bundle is derived, never stored.** It is computed from the
   level at run start, so editing a flag by hand cannot silently grant
   autonomy the ladder never awarded - and a config that contradicts the
   level is recorded as a manual override rather than honored in silence.

Opt-in (``[autonomy] enabled = false``): L1 is stricter than today's
defaults (it forces the merge gate on), so enabling the ladder must be a
deliberate act rather than a surprise upgrade for existing repos.

**Every threshold in this module is an UNMEASURED PLACEHOLDER** (the R8
"no assumed thresholds" rule). ``kstrl.autonomy_replay`` replays them
against historical run data and reports what would have fired; until that
output is recorded in ``docs/dark-factory-roadmap.md``, no threshold here
should be trusted to gate anything.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

STATE_FILENAME = ".kstrl/autonomy.json"
STATE_SCHEMA_VERSION = 1


class AutonomyLevel(IntEnum):
    """Ordered autonomy levels, defined by the human's remaining role."""

    L1_SUPERVISED = 1        # human approves the plan AND the merge
    L2_GATED_MERGE = 2       # plans auto-accepted; human still gates merge
    L3_ENVELOPED_AUTO = 3    # merge auto when fully green AND inside envelope
    L4_DEPLOY = 4            # L3 plus the release stage (R8.7)

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

    POLICY_VIOLATION = 1          # R8.1 envelope breach
    CALIBRATION_REGRESSION = 2    # adversarial detection rate fell
    HEALTH_BREACH = 3             # R8.4 control-limit breach
    HUMAN_REJECTED_AUTO_MERGE = 4  # a human rejected an L3 candidate
    MANUAL = 5                    # operator demoted by hand

    @property
    def label(self) -> str:
        return self.name.lower()


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


@dataclass(frozen=True)
class FlagBundle:
    """The permissions a level grants, derived fresh at run start.

    Never persisted: storing it would let the stored copy drift from the
    level that justified it. ``deps_allow_new_permitted`` and
    ``deploy_permitted`` are ceilings the R8.1 envelope and R8.7 release
    config must also agree to - the ladder can only ever withhold
    permission, never grant something those gates deny.
    """

    level: AutonomyLevel
    pause_before_pr_merge: bool
    review_mode: str
    auto_accept_plan: bool
    deps_allow_new_permitted: bool
    auto_merge_when_green: bool
    deploy_permitted: bool

    def describe(self) -> list[str]:
        return [
            f"merge gate: {'ON (human approves)' if self.pause_before_pr_merge else 'off'}",
            f"review mode: {self.review_mode}",
            f"plans: {'auto-accepted' if self.auto_accept_plan else 'human-approved'}",
            f"new dependencies: {'permitted' if self.deps_allow_new_permitted else 'blocked'}",
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
            deps_allow_new_permitted=False,
            auto_merge_when_green=False,
            deploy_permitted=False,
        )
    if level is AutonomyLevel.L2_GATED_MERGE:
        return FlagBundle(
            level=level,
            pause_before_pr_merge=True,
            review_mode="hard",
            auto_accept_plan=True,
            deps_allow_new_permitted=False,
            auto_merge_when_green=False,
            deploy_permitted=False,
        )
    if level is AutonomyLevel.L3_ENVELOPED_AUTO:
        return FlagBundle(
            level=level,
            pause_before_pr_merge=False,
            review_mode="hard",
            auto_accept_plan=True,
            deps_allow_new_permitted=True,
            auto_merge_when_green=True,
            deploy_permitted=False,
        )
    return FlagBundle(
        level=AutonomyLevel.L4_DEPLOY,
        pause_before_pr_merge=False,
        review_mode="hard",
        auto_accept_plan=True,
        deps_allow_new_permitted=True,
        auto_merge_when_green=True,
        deploy_permitted=True,
    )


@dataclass(frozen=True)
class Transition:
    """One recorded level change. Append-only history."""

    at: str
    from_level: int
    to_level: int
    direction: str           # "promote" | "demote"
    actor: str               # human identity for promotions; "system" for auto
    reason: str
    trigger: str = ""        # DemotionTrigger label, demotions only
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

    @property
    def autonomy_level(self) -> AutonomyLevel:
        return AutonomyLevel(self.level)

    def flag_bundle(self) -> FlagBundle:
        return flag_bundle_for(self.autonomy_level)

    # -- persistence -------------------------------------------------------
    @classmethod
    def path_for(cls, root_dir: Path) -> Path:
        return root_dir / STATE_FILENAME

    @classmethod
    def load(cls, root_dir: Path) -> AutonomyState:
        """Read state, defaulting to a fresh L1 state when absent.

        A corrupt or unreadable file falls back to L1 rather than raising:
        the safe direction for unknown autonomy is the least autonomy.
        """
        path = cls.path_for(root_dir)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        raw_level = data.get("level", int(AutonomyLevel.L1_SUPERVISED))
        try:
            level = int(AutonomyLevel(int(raw_level)))
        except (ValueError, TypeError):
            level = int(AutonomyLevel.L1_SUPERVISED)
        history = [
            Transition(
                at=str(h.get("at", "")),
                from_level=int(h.get("from_level", 1)),
                to_level=int(h.get("to_level", 1)),
                direction=str(h.get("direction", "")),
                actor=str(h.get("actor", "")),
                reason=str(h.get("reason", "")),
                trigger=str(h.get("trigger", "")),
                evidence=h.get("evidence", {}) or {},
            )
            for h in data.get("history", [])
            if isinstance(h, dict)
        ]
        return cls(
            level=level,
            since=str(data.get("since", "")) or _utc_now_iso(),
            components_merged_at_level=int(data.get("components_merged_at_level", 0)),
            clean_merges_at_level=int(data.get("clean_merges_at_level", 0)),
            policy_violations_at_level=int(data.get("policy_violations_at_level", 0)),
            decisive_runs_at_level=int(data.get("decisive_runs_at_level", 0)),
            cooldown_runs_remaining=int(data.get("cooldown_runs_remaining", 0)),
            last_promoted_by=str(data.get("last_promoted_by", "")),
            history=history,
        )

    def save(self, root_dir: Path) -> None:
        """Atomic write (mkstemp + os.replace), mirroring manifest.py."""
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
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=".autonomy-",
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

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
            blockers.append(
                f"cannot skip levels: {current.label} -> {target.label}"
            )
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
                f"{self.policy_violations_at_level} policy violation(s) at "
                "this level; need zero"
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
        force: bool = False,
        evidence: dict[str, Any] | None = None,
    ) -> Transition:
        """Raise one level. Requires a human actor AND an explicit ack.

        There is deliberately no unattended path into this method: an empty
        ``actor`` or ``ack`` raises. ``force`` records an override of unmet
        criteria - it still demands the ack, and the override is written
        into the transition's evidence so the audit trail shows the
        criteria were bypassed rather than met.
        """
        if not actor.strip():
            raise AutonomyError(
                "promotion requires an actor: agents cannot promote themselves"
            )
        if not ack.strip():
            raise AutonomyError(
                "promotion requires an explicit acknowledgement of the evidence"
            )
        current = self.autonomy_level
        if current is AutonomyLevel.L4_DEPLOY:
            raise AutonomyError("already at the highest level (L4 Deploy)")
        target = AutonomyLevel(int(current) + 1)
        blockers = self.promotion_blockers(target)
        if blockers and not force:
            raise AutonomyError(
                f"cannot promote {current.label} -> {target.label}: "
                + "; ".join(blockers)
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
                0, self.cooldown_runs_remaining - count,
            )

    def record_merged_component(self, *, human_edited: bool = False) -> None:
        """Count a merged component; edits break the clean-merge streak."""
        self.components_merged_at_level += 1
        if human_edited:
            self.clean_merges_at_level = 0
        else:
            self.clean_merges_at_level += 1

    def record_policy_violation(self, count: int = 1) -> None:
        self.policy_violations_at_level += count


@dataclass(frozen=True)
class AutonomyConfig:
    """``[autonomy]`` config. Opt-in, like the R8.1 envelope.

    Off by default because L1 is STRICTER than the harness defaults (it
    forces the merge gate on): switching the ladder on must be deliberate,
    never a silent behavioural upgrade for an existing repo. When enabled,
    the level in ``.kstrl/autonomy.json`` derives the flag bundle at run
    start and any config flag that contradicts it is logged as a manual
    override rather than quietly honored.
    """

    enabled: bool = False
    #: Refuse to run above this level regardless of stored state. A hard
    #: local ceiling for operators who want the ladder's bookkeeping
    #: without its upper levels.
    max_level: int = int(AutonomyLevel.L4_DEPLOY)

    @classmethod
    def from_env(cls) -> AutonomyConfig:
        defaults = cls()
        enabled_raw = os.environ.get("KSTRL_AUTONOMY_ENABLED")
        max_raw = os.environ.get("KSTRL_AUTONOMY_MAX_LEVEL")
        return cls(
            enabled=defaults.enabled if enabled_raw is None else enabled_raw == "1",
            max_level=defaults.max_level if max_raw is None else int(max_raw),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> AutonomyConfig:
        """Precedence: env > toml > defaults; reads ``[autonomy]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "autonomy")
        defaults = cls()
        enabled = (
            bool(section["enabled"]) if "enabled" in section else defaults.enabled
        )
        max_level = (
            int(section["max_level"])
            if "max_level" in section
            else defaults.max_level
        )
        if "KSTRL_AUTONOMY_ENABLED" in os.environ:
            enabled = os.environ["KSTRL_AUTONOMY_ENABLED"] == "1"
        if "KSTRL_AUTONOMY_MAX_LEVEL" in os.environ:
            max_level = int(os.environ["KSTRL_AUTONOMY_MAX_LEVEL"])
        return cls(enabled=enabled, max_level=max_level)

    def __post_init__(self) -> None:
        valid = {int(level) for level in AutonomyLevel}
        if self.max_level not in valid:
            raise AutonomyError(
                f"invalid max_level {self.max_level}; expected one of "
                f"{sorted(valid)}"
            )


def effective_level(state: AutonomyState, config: AutonomyConfig) -> AutonomyLevel:
    """The level actually in force: stored level clamped by ``max_level``.

    Clamping here (rather than rewriting state) keeps the earned level
    intact when an operator temporarily lowers the ceiling.
    """
    return AutonomyLevel(min(state.level, config.max_level))


def manual_override_notes(
    bundle: FlagBundle,
    *,
    configured_pause_before_pr_merge: bool | None = None,
    configured_review_mode: str | None = None,
) -> list[str]:
    """Config values that contradict the level's bundle.

    Named rather than silently honored: the roadmap's stale-ladder failure
    mode is a hand-edited flag granting autonomy the ladder never awarded.
    The bundle still wins; these notes exist so the divergence is visible
    in the run log and the transition record.
    """
    notes: list[str] = []
    if (
        configured_pause_before_pr_merge is not None
        and configured_pause_before_pr_merge != bundle.pause_before_pr_merge
    ):
        notes.append(
            f"[factory] pause_before_pr_merge={configured_pause_before_pr_merge} "
            f"contradicts {bundle.level.label} "
            f"(bundle: {bundle.pause_before_pr_merge}); bundle wins"
        )
    if (
        configured_review_mode is not None
        and configured_review_mode != bundle.review_mode
    ):
        notes.append(
            f"[factory] review_mode={configured_review_mode!r} contradicts "
            f"{bundle.level.label} (bundle: {bundle.review_mode!r}); bundle wins"
        )
    return notes
