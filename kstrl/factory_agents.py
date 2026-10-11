"""The agent CLI and model of each adversarial role and the engineer, and the warnings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kstrl.autonomy import AutonomyLevel, flag_bundle_for
from kstrl.autonomy_config import AutonomyConfig
from kstrl.config import KstrlConfig
from kstrl.factory_config import FactoryConfig, review_enabled, security_enabled
from kstrl.review import ReviewMode
from kstrl.sandbox import SandboxConfig

if TYPE_CHECKING:
    from kstrl.agents.liveness import ProbeResult
    from kstrl.ui.base import UI


# R7.1: cross-model review rotation. Self-preference bias means a
# same-family reviewer systematically misses the bug classes its own
# family produces, so when no explicit reviewer config is given the
# review and security phases default to the OPPOSITE model family from
# the engineer (user decision 2: the OpenAI family via the codex CLI
# reviews Claude-engineered code; a codex engineer flips the default to
# claude-code). The engineer always keeps the primary family.
_CROSS_FAMILY_TYPE: dict[str, str] = {
    "claude-code": "codex",
    "codex": "claude-code",
}


def _cli_family(
    agent_cmd: str | None,
    agent_type: str | None,
    claude_available: bool,
) -> str | None:
    """Which MODEL family a (cmd, type) config resolves to, mirroring
    ``agents.get_agent`` dispatch exactly: a custom command is an
    unknown family (None); "claude-sdk" is the Claude family through
    the SDK transport (R7.6 - without this branch it would fall through
    to codex and INVERT the R7.1 rotation for SDK engineers);
    "auto"/None auto-detects claude-code first.

    Canonicalizes through the SAME table ``get_agent`` uses, and that is
    load-bearing rather than tidiness. This function decides which family
    the ENGINEER belongs to, and the R7.1 rotation then picks a reviewer
    from the other family. If the two disagree - as they did when
    ``get_agent`` learned the "claude" alias and this mirror did not -
    a Claude engineer is recorded as codex, the rotation "cross-family"
    reviewer resolves to claude-code, and the run gets SAME-family review
    while reporting the opposite. Correlated blind spots are exactly what
    R7.1 exists to avoid, so a silent mismatch here is worse than a
    crash: the safety property fails while the audit trail says it held.

    An unrecognized type raises, matching ``get_agent`` - a config that
    cannot construct an agent must not first be assigned a family.
    """
    # Imported here, matching this module's lazy-agent-import pattern.
    from kstrl.agents import (
        VALID_AGENT_TYPES,
        UnknownAgentTypeError,
        canonical_agent_type,
    )

    if agent_cmd:
        return None
    if agent_type is None:
        # Unset means auto-detect, NOT unknown. canonical_agent_type
        # returns None for both, so they must be split before the
        # unknown-type check or the ordinary "no [agent] type
        # configured" case would raise.
        return "claude-code" if claude_available else "codex"
    canonical = canonical_agent_type(agent_type)
    if canonical is None:
        raise UnknownAgentTypeError(
            f"unknown agent type {agent_type!r}; expected one of {', '.join(VALID_AGENT_TYPES)}"
        )
    if canonical in ("claude-code", "claude-sdk"):
        return "claude-code"
    if canonical == "auto":
        return "claude-code" if claude_available else "codex"
    if canonical == "custom":
        return None
    return "codex"


def _agent_identity(
    agent_cmd: str | None,
    agent_type: str | None,
    model: str | None,
    claude_available: bool,
) -> str:
    """Reviewing-model identity for a configuration, matching the agent
    adapters' ``name`` property ("codex (gpt-5)", "claude-code",
    "claude-sdk (haiku)", "custom (<cmd>)") so findings attributed
    before an agent exists match what a live run stamps on its
    results. "claude-sdk" keeps its own identity label (the adapter
    name is the transport, distinct from its claude-code FAMILY used
    for rotation)."""
    if agent_cmd:
        return f"custom ({agent_cmd})"
    if agent_type == "claude-sdk":
        label = "claude-sdk"
    else:
        label = _cli_family(agent_cmd, agent_type, claude_available) or "unknown"
    if model:
        return f"{label} ({model})"
    return label


@dataclass(frozen=True)
class AdversarialAgentSelection:
    """Resolved agent configuration for one adversarial phase (R7.1).

    ``source`` records how the resolution went: "explicit" (operator
    config always wins), "cross-family-default" (the R7.1 rotation
    default), or "same-family-fallback" (heterogeneity unavailable;
    ``warning`` then carries the homogeneity risk statement to print).
    """

    phase: str
    agent_cmd: str | None
    agent_type: str | None
    model: str | None
    reasoning: str | None
    source: str
    identity: str
    warning: str | None = None


@dataclass(frozen=True)
class _AdversarialGates:
    """What this run's config permits the adversarial phases to do."""

    review: bool
    security: bool
    may_dispatch: bool


def _ladder_can_force_review() -> bool:
    """Could any autonomy level turn a skip-mode run back into a
    reviewing one? Computed rather than asserted in prose, so it cannot
    quietly stop being true when a bundle changes."""
    return any(
        flag_bundle_for(level).review_mode != ReviewMode.SKIP.value for level in AutonomyLevel
    )


def _adversarial_phase_gates(
    factory_config: FactoryConfig,
    autonomy_config: AutonomyConfig,
) -> _AdversarialGates:
    """Which adversarial phases are on, and may any of them dispatch.

    ``may_dispatch`` is a spend decision (#262 review): resolving the
    reviewer is free, but the cross-family LIVENESS PROBE costs a real
    CLI turn, so it must not fire for a run that will never dispatch an
    adversarial call.

    ``review_mode = "skip"`` is only proof of that when the autonomy
    ladder cannot put review back, and the ladder resolves further down
    ``_run_factory_locked``, after the pipeline is already holding this
    selection.
    """
    review = review_enabled(factory_config)
    security = security_enabled(factory_config)
    may_dispatch = review or security or (autonomy_config.enabled and _ladder_can_force_review())
    return _AdversarialGates(review=review, security=security, may_dispatch=may_dispatch)


def _resolve_cross_family(
    cross_type: str | None,
    *,
    claude_available: bool,
    codex_available: bool,
    may_dispatch_adversarial: bool,
) -> ProbeResult | None:
    """The opposite family's liveness, or None when there is none to ask.

    Liveness is deliberately NOT folded into
    ``claude_available``/``codex_available``: those two also decide which
    family the ENGINEER belongs to, and a claude CLI that is installed
    but dead must still be recorded as a claude engineer, because
    ``get_agent`` will construct it from PATH regardless. Mixing
    liveness in there would invert the rotation while the audit trail
    claimed it held - the exact failure ``_cli_family`` warns about.

    The probe sits behind the installed check, so a machine with one CLI
    pays nothing and only the family this run would actually dispatch to
    is ever probed. ``probe_family`` caches per process, so the review
    and security resolutions share one probe, and it is inert when
    ``KSTRL_AGENT_PROBE=0``.

    ``may_dispatch_adversarial=False`` resolves on PATH alone, exactly
    as this did before #262: the answer cannot change a run that will
    never call a reviewer, so buying it with a CLI turn would be spend
    for nothing.
    """
    if cross_type is None:
        return None
    installed = codex_available if cross_type == "codex" else claude_available
    if not installed:
        return None
    from kstrl.agents.liveness import UNPROBED, probe_family

    return probe_family(cross_type) if may_dispatch_adversarial else UNPROBED


def _homogeneity_warning(
    phase: str,
    engineer_family: str | None,
    cross_type: str | None,
    cross: ProbeResult | None,
) -> str:
    """The self-preference risk statement for a same-family fallback.

    Three causes, three remedies: an unknowable engineer family, a
    cross CLI that is not installed, and (#262) one that is installed
    but cannot run. The last quotes the CLI's own refusal, because
    "install codex" is useless advice to someone who already has it.
    """
    tail = (
        "Self-preference bias means a same-family reviewer systematically "
        "misses the bug classes its own family produces. "
    )
    if engineer_family is None:
        return (
            "Homogeneity risk (R7.1): the engineer runs a custom agent "
            "command, so its model family is unknown and the "
            f"cross-family default cannot be applied; the {phase} "
            f"reviewer falls back to the same configuration. {tail}"
            f"Set an explicit {phase} agent config on a different model "
            "family to restore cross-family review."
        )
    same_family = (
        f"so the {phase} reviewer runs on the same model family as the "
        f"engineer ({engineer_family}). {tail}"
    )
    if cross is not None:
        because = f" ({cross.detail})" if cross.detail else ""
        return (
            f"Homogeneity risk (R7.1): the {cross_type} CLI is installed "
            f"but cannot run a turn{because}, {same_family}"
            f"Fix the {cross_type} CLI (authentication, quota, config) for "
            f"cross-family review, or set an explicit {phase} agent config "
            "to accept the risk silently."
        )
    return (
        f"Homogeneity risk (R7.1): the {cross_type} CLI is not available, "
        f"{same_family}"
        f"Install the {cross_type} CLI for cross-family review, or set an "
        f"explicit {phase} agent config to accept the risk silently."
    )


def resolve_adversarial_selection(
    phase: str,
    *,
    explicit_cmd: str | None,
    explicit_type: str | None,
    explicit_model: str | None,
    fallback_cmd: str | None,
    fallback_type: str | None,
    fallback_model: str | None,
    fallback_reasoning: str | None,
    engineer_cmd: str | None,
    engineer_type: str | None,
    claude_available: bool | None = None,
    codex_available: bool | None = None,
    may_dispatch_adversarial: bool = True,
) -> AdversarialAgentSelection:
    """Resolve which agent reviews this run's diffs (R7.1).

    Precedence:
    1. Any explicit field (cmd/type/model) makes the whole selection
       explicit: each unset field falls back to the phase's historical
       fallback, exactly as before R7.1. No warning - an operator who
       pins a same-family reviewer has decided so deliberately.
    2. Otherwise, when the engineer's family is known and the opposite
       family's CLI is both installed and able to complete a turn, the
       reviewer defaults to that family (adapter-default model;
       reasoning deliberately not inherited - effort strings do not
       transfer across families).
    3. Otherwise the reviewer falls back to the same configuration as
       today (same family as the engineer) and ``warning`` names the
       self-preference risk.

    ``claude_available``/``codex_available`` default to asking PATH;
    tests inject both.

    Step 2 also LIVENESS-PROBES the cross family (#262), at most once
    per process and at most twice per family - an installed CLI that
    cannot authenticate or has exhausted its quota used to be selected
    here, and the run then paid the whole engineer bill before finding
    out. A dead cross CLI takes the SAME path a missing one always took:
    downgrade to same-family review with the homogeneity warning, never
    a hard failure. ``KSTRL_AGENT_PROBE=0`` switches the probe off
    globally; ``may_dispatch_adversarial=False`` switches it off for one
    resolution whose answer cannot change the run.
    """
    from kstrl.agents import ClaudeCodeAgent, CodexAgent

    if claude_available is None:
        claude_available = ClaudeCodeAgent.is_available()
    if codex_available is None:
        codex_available = CodexAgent.is_available()

    explicit = any(v is not None for v in (explicit_cmd, explicit_type, explicit_model))
    if explicit:
        cmd = explicit_cmd if explicit_cmd is not None else fallback_cmd
        agent_type = explicit_type if explicit_type is not None else fallback_type
        model = explicit_model if explicit_model is not None else fallback_model
        return AdversarialAgentSelection(
            phase=phase,
            agent_cmd=cmd,
            agent_type=agent_type,
            model=model,
            reasoning=fallback_reasoning,
            source="explicit",
            identity=_agent_identity(cmd, agent_type, model, claude_available),
        )

    engineer_family = _cli_family(engineer_cmd, engineer_type, claude_available)
    cross_type = _CROSS_FAMILY_TYPE.get(engineer_family) if engineer_family else None
    cross = _resolve_cross_family(
        cross_type,
        claude_available=claude_available,
        codex_available=codex_available,
        may_dispatch_adversarial=may_dispatch_adversarial,
    )
    if cross is not None and cross.live:
        return AdversarialAgentSelection(
            phase=phase,
            agent_cmd=None,
            agent_type=cross_type,
            model=None,
            reasoning=None,
            source="cross-family-default",
            identity=_agent_identity(None, cross_type, None, claude_available),
        )

    warning = _homogeneity_warning(phase, engineer_family, cross_type, cross)
    return AdversarialAgentSelection(
        phase=phase,
        agent_cmd=fallback_cmd,
        agent_type=fallback_type,
        model=fallback_model,
        reasoning=fallback_reasoning,
        source="same-family-fallback",
        identity=_agent_identity(
            fallback_cmd,
            fallback_type,
            fallback_model,
            claude_available,
        ),
        warning=warning,
    )


def _warn_unsandboxable_reviewers(
    ui: UI,
    review_selection: AdversarialAgentSelection | None,
    security_selection: AdversarialAgentSelection | None,
) -> None:
    """#266: say so when a reviewer cannot be held read-only.

    ``get_agent`` returns ``CustomAgent`` BEFORE any adapter branch, so a
    custom command drops the read-only posture as well as the sandbox.
    Both reviewer selections fall back to the engineer's ``agent_cmd``
    when no reviewer command is configured (the code reviewer since
    #498), so ``[agent] command`` alone reaches them. The read-only
    guarantee is the one an operator is most likely to assume holds
    unconditionally, which is exactly why it is worth saying out loud
    when it does not.
    """
    for role, selection in (
        ("review", review_selection),
        ("security", security_selection),
    ):
        if selection is not None and selection.agent_cmd:
            ui.warn(
                f"  the {role} reviewer is a custom command; it CANNOT be "
                "sandboxed or held read-only, so it may write the worktree "
                "it is judging (#266)"
            )


def _engineer_sandbox(sandbox: SandboxConfig, base_config: KstrlConfig) -> str:
    """The harness whose sandbox confines the engineer, or "" (#700).

    The adapter ``get_agent`` picks for the engineer, by the same rule
    (``_agent_identity``); a custom command or ``enabled = false`` is "".
    """
    from kstrl.agents import ClaudeCodeAgent

    if not sandbox.enabled or base_config.agent_cmd:
        return ""
    return _agent_identity(None, base_config.agent_type, None, ClaudeCodeAgent.is_available())
