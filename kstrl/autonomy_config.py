"""R8.2 autonomy ladder: how a run resolves its autonomy.

The ``[autonomy]`` config, the runtime level under the policy envelope, the
control-state placement refusal, and the flag bundle that a run uses. The
ladder itself (levels, triggers, thresholds, state) is in
:mod:`kstrl.autonomy`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from kstrl.autonomy import AutonomyError, AutonomyLevel, AutonomyState, FlagBundle
from kstrl.config_numbers import check_numbers
from kstrl.config_section import ConfigSection
from kstrl.statedir import (
    control_is_external,
    control_untrusted_reason,
    ensure_control_state,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def strict_bool(section: Mapping[str, Any], key: str, default: bool) -> bool:
    """Read one governance boolean, refusing anything that is not one.

    ``bool("false")`` is True, so the ``bool(section[key])`` reading this
    package uses everywhere else arms a switch the operator wrote
    ``"false"`` against. Every key that uses this one arms or revokes
    autonomy, and a typo that ARMS a safety switch is worse than one that
    disarms it, so a non-boolean is named and refused rather than
    coerced.

    Six keys, not the whole repo. Three are ``[autonomy]``'s: the two
    revocation switches and ``enabled``, which is the switch that arms
    them and can therefore only fail in the arming direction. The fourth
    is ``[factory] pause_before_pr_merge``, added by #195 for a reason
    the other three do not have: since #195 that key OUTRANKS the ladder
    when the operator set it, so a coerced ``"false"`` would manufacture
    an explicit request nobody wrote and then keep the gate up at every
    level. The fifth is ``[factory] integration_review`` (#482): a
    coerced ``"false"`` would read as True and spend a reviewer call the
    operator switched off. The sixth is ``[factory] integration_blocking``
    (#483): a coerced ``"false"`` would read as True and let open findings
    build a fix component the operator never enabled. The remaining
    coercion is repo-wide (29
    ``bool(section[...])`` sites in ``kstrl/``, counted by grep) and
    tightening the rest changes how existing configs load, which is its
    own change with its own guard.

    The message carries no section prefix. ``config_preflight`` puts the
    section label in front of whatever the loader raises, and each of
    these keys is unique to its own section, so each identifies itself on
    the one surface that prints the exception bare (``python -m
    kstrl.calibration compare``).

    It also names the type it actually READ rather than a cause the
    operator may not have hit. The first draft explained that a quoted
    value is a string and every non-empty string reads as true, which is
    the wrong sentence for ``pause_before_pr_merge = 0`` - a plausible
    boolean spelling, and this key is far more hand-written than
    ``[autonomy] enabled``. Same rule as the TOML readers one module
    over: report individually only the causes you can name.
    """
    if key not in section:
        return default
    from kstrl.config import ConfigError

    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(
            f"{key} must be a boolean, written unquoted as true or false. "
            f"Got {value!r}, which TOML reads as {type(value).__name__}."
        )
    return value


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
    #: Demote one level when ``python -m kstrl.calibration compare`` finds
    #: a regression. Advisory first (#232): a regression always opens a
    #: ``calibration_drift`` inbox item while the ladder is enabled, and
    #: revokes a level only when this is true. Off by default because
    #: every ladder threshold is still an unmeasured placeholder.
    demote_on_calibration_regression: bool = False
    #: Demote one level on an R8.4 health control-limit breach. Advisory
    #: first for the same reason, and additionally suppressed while a
    #: cool-down is running: a breach is a windowed trend, so it persists
    #: across runs and would otherwise cost a level per run. Run
    #: ``ks health`` first; the measured false-alarm rate at the n floor
    #: is in roadmap R8.4.
    demote_on_health_breach: bool = False

    @classmethod
    def from_env(cls) -> AutonomyConfig:
        defaults = cls()
        enabled_raw = os.environ.get("KSTRL_AUTONOMY_ENABLED")
        max_raw = os.environ.get("KSTRL_AUTONOMY_MAX_LEVEL")
        calibration_raw = os.environ.get("KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION")
        health_raw = os.environ.get("KSTRL_AUTONOMY_DEMOTE_ON_HEALTH")
        return cls(
            enabled=defaults.enabled if enabled_raw is None else enabled_raw == "1",
            max_level=defaults.max_level if max_raw is None else int(max_raw),
            demote_on_calibration_regression=(
                defaults.demote_on_calibration_regression
                if calibration_raw is None
                else calibration_raw == "1"
            ),
            demote_on_health_breach=(
                defaults.demote_on_health_breach if health_raw is None else health_raw == "1"
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> AutonomyConfig:
        """Precedence: env > toml > defaults; reads ``[autonomy]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        section = load_toml_section(resolve_config_file(root_dir), "autonomy")
        defaults = cls()
        enabled = strict_bool(section, "enabled", defaults.enabled)
        max_level = int(section["max_level"]) if "max_level" in section else defaults.max_level
        demote_calibration = strict_bool(
            section,
            "demote_on_calibration_regression",
            defaults.demote_on_calibration_regression,
        )
        demote_health = strict_bool(
            section, "demote_on_health_breach", defaults.demote_on_health_breach
        )
        if "KSTRL_AUTONOMY_ENABLED" in os.environ:
            enabled = os.environ["KSTRL_AUTONOMY_ENABLED"] == "1"
        if "KSTRL_AUTONOMY_MAX_LEVEL" in os.environ:
            max_level = int(os.environ["KSTRL_AUTONOMY_MAX_LEVEL"])
        if "KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION" in os.environ:
            demote_calibration = os.environ["KSTRL_AUTONOMY_DEMOTE_ON_CALIBRATION"] == "1"
        if "KSTRL_AUTONOMY_DEMOTE_ON_HEALTH" in os.environ:
            demote_health = os.environ["KSTRL_AUTONOMY_DEMOTE_ON_HEALTH"] == "1"
        return check_numbers(
            cls(
                enabled=enabled,
                max_level=max_level,
                demote_on_calibration_regression=demote_calibration,
                demote_on_health_breach=demote_health,
            )
        )

    def __post_init__(self) -> None:
        valid = {int(level) for level in AutonomyLevel}
        if self.max_level not in valid:
            raise AutonomyError(
                f"invalid max_level {self.max_level}; expected one of {sorted(valid)}"
            )


AUTONOMY_SECTION = ConfigSection(("autonomy",), AutonomyConfig)


def envelope_ceiling(policy_enabled: bool) -> AutonomyLevel:
    """The highest level defensible given the R8.1 envelope's state.

    L3 is *Enveloped* auto-merge: the envelope is the boundary that makes
    unattended merging defensible at all. With ``[policy] enabled=false``
    there is no boundary, so "auto-merge inside the envelope" would mean
    auto-merge inside nothing. L2 (human gates the merge) is then the
    ceiling regardless of the level the ladder has awarded.
    """
    return AutonomyLevel.L4_DEPLOY if policy_enabled else AutonomyLevel.L2_GATED_MERGE


def resolve_runtime_level(
    state: AutonomyState,
    config: AutonomyConfig,
    *,
    policy_enabled: bool,
    root_dir: Path,
) -> tuple[AutonomyLevel, list[str]]:
    """The level a run may actually use, plus why it was clamped.

    Independent ceilings apply, and the LOWEST wins: the operator's
    ``max_level``, the envelope ceiling, and (R8.9) the control-state
    relocation gate. Returned rather than raised so the run can proceed
    at the safe level while saying loudly what it withheld.
    """
    notes: list[str] = []
    level = state.autonomy_level
    if int(level) > config.max_level:
        level = AutonomyLevel(config.max_level)
        notes.append(
            f"clamped to L{config.max_level} by [autonomy] max_level "
            f"(earned level L{state.level} retained)"
        )
    ceiling = envelope_ceiling(policy_enabled)
    if int(level) > int(ceiling):
        notes.append(
            f"clamped to {ceiling.label}: L3+ requires the R8.1 policy "
            "envelope, but [policy] enabled=false - there is no envelope "
            "to auto-merge inside"
        )
        level = ceiling
    ensure_control_state(root_dir)
    if int(level) >= int(AutonomyLevel.L3_ENVELOPED_AUTO) and not control_is_external(root_dir):
        notes.append(
            "clamped to L2 Gated-merge: L3+ requires control state outside "
            "the agent-reachable tree (R8.9); leftover `.kstrl/` control "
            "files or an XDG_STATE_HOME under the repo block unattended "
            "auto-merge"
        )
        level = AutonomyLevel.L2_GATED_MERGE
    return level, notes


def control_relocation_error(
    root_dir: Path,
    *,
    target_level: AutonomyLevel,
) -> str | None:
    """Why L3+ is refused for control-state placement, or None if allowed."""
    if int(target_level) < int(AutonomyLevel.L3_ENVELOPED_AUTO):
        return None
    ensure_control_state(root_dir)
    reason = control_untrusted_reason(root_dir)
    if reason is None:
        return None
    return (
        "L3+ requires control state outside the agent-reachable tree "
        f"(R8.9): {reason}. Correct it, or point XDG_STATE_HOME outside the repo, then retry."
    )


def pause_gate_for(bundle: FlagBundle, *, configured: bool, explicit: bool) -> bool:
    """The merge gate this run actually uses. One asymmetry, written once.

    ``bundle.pause_before_pr_merge or (explicit and configured)``: the
    ladder may RAISE the gate, and it may not lower one the operator set.

    Both halves are needed and they are not the same rule.

    - The bundle's True stands whatever the config says. That is the
      ladder withholding auto-merge, which it may always do, and it is
      the direction PR #174 correction 1 closed: a hand-edited flag must
      not grant autonomy the ladder never awarded. So an explicit
      ``false`` at L1 or L2 still pauses.
    - The bundle's False is a PERMISSION to auto-merge, not an
      instruction to. At L3 and L4 it used to be assigned straight onto
      the config, which removed a human gate the operator had asked for
      in writing and logged it as an override ignored (#195). An
      operator's explicit request now survives every level.

    ``explicit`` is provenance, never a value comparison: it is true when
    the key was present in ``kstrl.toml``, the env var was set, or the
    CLI flag was passed. A value that merely EQUALS the default is not a
    request (an env var set to ``0`` compares equal to the default, which
    is how ``config_report`` mislabels it), and a defaulted True is not
    one either, which is why a configured-but-not-explicit gate is still
    dropped at L3.
    """
    return bundle.pause_before_pr_merge or (explicit and configured)


def resolved_flag_bundle(bundle: FlagBundle, *, configured: bool, explicit: bool) -> FlagBundle:
    """The bundle the run USES, with every dependent flag rebuilt.

    ``flag_bundle_for`` returns what a LEVEL awards. An explicit operator
    gate can raise ``pause_before_pr_merge`` above that (see
    :func:`pause_gate_for`), and the flags that depend on it have to move
    with it or the audit record contradicts itself. Round 1 of #195
    rebound one field with ``replace`` and shipped an
    ``AutonomyLevelApplied`` reading ``merge gate: ON (human approves)``
    beside ``auto-merge when green: yes``: a run that stops at a human at
    every component and also merges without one.

    ``auto_merge_when_green`` is the dependent flag, and only in the
    withholding direction: a retained gate takes it away, and nothing
    here may hand it back, because the ladder's False is a permission
    never granted.
    """
    pause = pause_gate_for(bundle, configured=configured, explicit=explicit)
    return replace(
        bundle,
        pause_before_pr_merge=pause,
        auto_merge_when_green=bundle.auto_merge_when_green and not pause,
    )


def manual_override_notes(
    bundle: FlagBundle,
    *,
    configured_pause_before_pr_merge: bool | None = None,
    configured_review_mode: str | None = None,
    pause_before_pr_merge_explicit: bool,
) -> list[str]:
    """Config values that contradict the level's bundle, and what won.

    Named rather than silently honored: the roadmap's stale-ladder failure
    mode is a hand-edited flag granting autonomy the ladder never awarded.
    These notes exist so the divergence is visible in the run log and the
    transition record.

    The bundle wins in every case but one, and the exception is #195: an
    explicit ``pause_before_pr_merge = true`` at L3 or L4 is honoured.
    Which case a run is in is not restated here; the note is chosen from
    what :func:`pause_gate_for` actually returned, so the record cannot
    disagree with the decision.

    Each note carries its own verdict, because two of them now have
    different verdicts and a caller cannot prefix them with one label.

    ``pause_before_pr_merge_explicit`` has no default on purpose. The
    claim above holds only while it travels with
    ``configured_pause_before_pr_merge``, and a default is exactly what
    lets the two separate: a caller that passed the value and forgot the
    provenance would get "bundle wins" for a gate the run in fact kept.
    Required, so the compiler asks instead of the reader.
    """
    notes: list[str] = []
    if (
        configured_pause_before_pr_merge is not None
        and configured_pause_before_pr_merge != bundle.pause_before_pr_merge
    ):
        resolved = pause_gate_for(
            bundle,
            configured=configured_pause_before_pr_merge,
            explicit=pause_before_pr_merge_explicit,
        )
        if resolved == configured_pause_before_pr_merge:
            notes.append(
                f"[factory] pause_before_pr_merge="
                f"{configured_pause_before_pr_merge} was set explicitly and "
                f"{bundle.level.label} (bundle: {bundle.pause_before_pr_merge}) "
                f"may withhold autonomy but not remove a gate the operator "
                f"asked for; gate retained by explicit request"
            )
        else:
            notes.append(
                f"Manual override ignored: [factory] pause_before_pr_merge="
                f"{configured_pause_before_pr_merge} contradicts "
                f"{bundle.level.label} "
                f"(bundle: {bundle.pause_before_pr_merge}); bundle wins"
            )
    if configured_review_mode is not None and configured_review_mode != bundle.review_mode:
        notes.append(
            f"Manual override ignored: [factory] review_mode="
            f"{configured_review_mode!r} contradicts "
            f"{bundle.level.label} (bundle: {bundle.review_mode!r}); bundle wins"
        )
    return notes
