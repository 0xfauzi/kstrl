"""The merge gate of one queue item, from the autonomy, policy and factory config (#776).

Moved from ``kstrl/serve.py`` with no change to a body. ``serve_cycle``
reads :func:`resolve_merge_gate` before it launches a run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from kstrl.workqueue import MergeDisposition, QueueItem

# ---------------------------------------------------------------------------
# Merge disposition - the human gate must survive continuous intake
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeGate:
    """The merge-gate decision for one item.

    ``refusal`` non-empty means the item must NOT run: this run would
    pause at a human merge gate and this repo cannot honour one, so
    silently proceeding would be precisely the governance erosion R8.6
    lists as a failure mode. Until #195 the producer was the autonomy
    ladder dropping the gate at L3; the ladder no longer does that, and
    the producer is now a repo whose config makes the checkpoint
    unreachable.

    Built only by :func:`_merge_gate` and
    :func:`_unreadable_config_gate`, which is what makes the refusal key
    on the RESOLVED gate rather than on the item's disposition.

    ``unreadable_section`` names the ``kstrl.toml`` section a config read
    could not complete, and empty means the refusal is about the repo's
    resolved configuration rather than about reading it. The distinction
    decides whether the item is POISONED or WAITS, and it is a fact about
    the refusal's cause rather than a new outcome: a ``create_prs =
    false`` repo will not stop conflicting with a promised human gate
    until somebody changes a policy, so that item is terminal, while a
    quoted boolean is a typo that clears the moment the file is fixed.
    Poisoning on the typo would cost one terminal item per poll for as
    long as the operator took to notice, and poison is documented as the
    state that is never retried automatically.
    """

    pause_before_pr_merge: bool
    notes: tuple[str, ...] = ()
    refusal: str = ""
    unreadable_section: str = ""


def _unreadable_section_reason(
    section: str, exc: BaseException, reason: str, next_step: str
) -> str:
    """The one phrasing an unreadable config section resolves to (#364).

    Shared by :func:`_unreadable_config_gate` (`[factory]`, `[autonomy]`,
    `[policy]`, #361) and ``serve_cycle``'s own `[queue]`/`[serve]`
    refusal, so the two do not drift onto two different sentences for
    the same fact. ``section`` is bare; the brackets are added HERE, at
    format time. ``str(exc).rstrip(".")`` because the cause is quoted
    mid-sentence and its own messages already end with a full stop,
    which would otherwise read as "...as str.. The item...". ``reason``
    is what cannot be done because the section could not be read;
    ``next_step`` is what the operator does about it.
    """
    detail = str(exc).rstrip(".")
    return f"[{section}] cannot be read, so {reason}: {detail}. {next_step}"


def _unreadable_config_gate(
    section: str,
    exc: BaseException,
    notes: tuple[str, ...] = (),
) -> MergeGate:
    """The gate a config read that could not complete resolves to.

    A config read on the poll path may REFUSE and it may not CLEAR, and
    above all it may not ESCAPE. ``serve``'s ``_cycle`` has no handler
    and neither does ``serve_cycle``, so an exception here leaves
    ``serve()`` and stops the daemon; under launchd it is relaunched on
    ``LAUNCHD_THROTTLE_SECONDS`` and dies again on the same key. That is
    the failure ``check_open_pr_bound`` already carries a paragraph
    about, and #318's rule stated for this module.

    Fail-closed by construction: the gate is on and the refusal is
    non-empty, so an unreadable section can never be the thing that lets
    an item run. ``unreadable_section`` then makes it a WAIT rather than
    a poison; see :class:`MergeGate`.

    The caught set is the whole surface and not an enumeration, for the
    reason ``check_open_pr_bound`` gives: every outcome that is not a
    config means one thing here, and a list of the types believed
    reachable is the defect rather than the precaution. What that would
    have to enumerate, measured through the real loaders rather than
    reasoned about: ``ConfigError`` from a quoted boolean, from a
    document that will not parse and from one that is not UTF-8; a plain
    ``ValueError`` from an ``int()`` cast on a string; and a ``TypeError``
    from the same cast on a TOML date, which is NOT a ``ValueError`` and
    is the type an author writing that list by inspection leaves out.
    ``OSError`` is reachable too, because ``load_toml_section`` does not
    normalise it.
    """
    return MergeGate(
        pause_before_pr_merge=True,
        notes=notes,
        unreadable_section=section,
        refusal=_unreadable_section_reason(
            section,
            exc,
            "this item's merge gate cannot be resolved",
            "The item waits; fix the section and the next poll picks it up.",
        ),
    )


def _merge_gate(
    root_dir: Path,
    *,
    pause_before_pr_merge: bool,
    wants_gate: bool,
    notes: tuple[str, ...] = (),
) -> MergeGate:
    """The single exit of :func:`resolve_merge_gate`.

    The unreachable-checkpoint refusal keys on the gate about to be
    RETURNED, never on the item's own disposition. Round 1 of #195 asked
    the disposition and so cleared the other way a gate arises: an
    ``AUTO_MERGE`` item that L1 or L2 downgrades is promised a human gate
    by the LADDER, and that gate is exactly as unreachable in a
    ``create_prs = false`` repo. Measured at f4356cf, that case returned
    ``pause=True, refusal=''``: the daemon logged "merge gate on", passed
    ``--pause-before-pr-merge`` to the child, and the child never reached
    ``_phase_checkpoint``. A control that CLEARS must be narrow
    (CLAUDE.md guard-design rule 3), and asking the disposition cleared a
    case it had not proved compliant.

    Every ``MergeGate`` this module returns is built here, so a fifth
    exit cannot skip the probe by construction.

    ``merge_gate_unreachable_warning`` is the PREDICATE and not the text.
    Its own sentences are written for ``run_factory``'s already-resolved
    config and read as a second subject when pasted into this one (they
    also tell the operator that ``ks serve`` honours the gate, inside the
    ``ks serve`` message explaining why it cannot). The probe forces
    ``pause_before_pr_merge=True`` and ``single_pr=False``, which leaves
    ``create_prs`` as the only input that can still produce a warning, so
    naming that key here is correct by construction;
    ``tests/test_serve.py::TestTheRefusalNamesTheRightKey`` pins the set
    of config fields the predicate reads so a fourth arm cannot make this
    sentence wrong in silence.
    """
    from kstrl.factory import FactoryConfig, merge_gate_unreachable_warning

    if not pause_before_pr_merge:
        return MergeGate(pause_before_pr_merge=False, notes=notes)
    try:
        probe = replace(
            FactoryConfig.load(root_dir),
            pause_before_pr_merge=True,
            single_pr=False,
        )
    except Exception as exc:  # noqa: BLE001 - anything but a config is the same answer
        return _unreadable_config_gate("factory", exc, notes)
    unreachable = merge_gate_unreachable_warning(probe)
    if unreachable is None:
        return MergeGate(pause_before_pr_merge=True, notes=notes)
    subject = (
        "the item requires a human merge gate"
        if wants_gate
        else "the autonomy level requires a human merge gate"
    )
    remedy = (
        "Set the item to --auto-merge deliberately, or turn create_prs on."
        if wants_gate
        else "Raise the autonomy level to one that permits auto-merge, or turn create_prs on."
    )
    return MergeGate(
        pause_before_pr_merge=True,
        notes=notes,
        refusal=(
            f"{subject}, but [factory] create_prs = false means this repo "
            f"creates no PR, so the checkpoint never runs. {remedy}"
        ),
    )


def resolve_merge_gate(item: QueueItem, root_dir: Path) -> MergeGate:
    """Reconcile the item's merge disposition with the autonomy ladder.

    Two directions, and they are not symmetric:

    - The item asks for AUTO_MERGE and the ladder withholds it: downgrade
      to a human gate. This is the ladder doing its job - it may always
      withhold a permission.
    - The item asks for STOP_AT_PR and the ladder's bundle would
      auto-merge (L3+): the item wins. ``STOP_AT_PR`` reaches the child
      as ``--pause-before-pr-merge``, which is an EXPLICIT request, and
      since #195 ``run_factory`` keeps an explicit gate at every level.
      Nothing to refuse; this used to be the refusal below.

    The refusal that remains is a different hole, and it is the one this
    function can still see: a gate that will be honoured by nobody
    because the checkpoint it runs at is unreachable. ``ks factory``
    reaches ``_phase_checkpoint`` only when it creates per-component PRs,
    so a repo whose ``[factory] create_prs = false`` turns a promised
    human gate into a silent auto-merge (#207's failure-open shape,
    arriving through intake). Refused rather than run.

    Every exit goes through :func:`_merge_gate`, which applies that
    refusal to the gate this function RESOLVED. Both ways a gate arises
    are covered: the item asking for one, and the ladder withholding
    auto-merge from an item that did not. Checked whether or not the
    ladder is enabled: the checkpoint is unreachable for a config reason,
    not a level reason.
    """
    from kstrl.autonomy import AutonomyConfig, AutonomyState, flag_bundle_for, resolve_runtime_level
    from kstrl.policy import PolicyConfig

    wants_gate = item.merge_disposition is MergeDisposition.STOP_AT_PR

    try:
        config = AutonomyConfig.load(root_dir)
    except Exception as exc:  # noqa: BLE001 - anything but a config is the same answer
        return _unreadable_config_gate("autonomy", exc)
    if not config.enabled:
        # No ladder: the item's own disposition is authoritative.
        return _merge_gate(root_dir, pause_before_pr_merge=wants_gate, wants_gate=wants_gate)

    try:
        policy = PolicyConfig.load(root_dir)
    except Exception as exc:  # noqa: BLE001 - anything but a config is the same answer
        return _unreadable_config_gate("policy", exc)
    level, clamps = resolve_runtime_level(
        AutonomyState.load(root_dir),
        config,
        policy_enabled=policy.enabled,
        root_dir=root_dir,
    )
    bundle = flag_bundle_for(level)
    notes = list(clamps)

    if not wants_gate and not bundle.auto_merge_when_green:
        notes.append(
            f"item requested auto-merge; {bundle.level.label} withholds it, "
            "so the PR waits for a human"
        )
        return _merge_gate(
            root_dir,
            pause_before_pr_merge=True,
            wants_gate=wants_gate,
            notes=tuple(notes),
        )

    if wants_gate and not bundle.pause_before_pr_merge:
        # #195: the item's explicit request outranks the bundle's
        # permission to auto-merge. Recorded, not refused, and the child
        # is told in the same words run_factory will use.
        notes.append(
            f"item requires a human merge gate; {bundle.level.label} would "
            "auto-merge, but an explicit request outranks the ladder, so "
            "the gate is retained"
        )
        return _merge_gate(
            root_dir,
            pause_before_pr_merge=True,
            wants_gate=wants_gate,
            notes=tuple(notes),
        )

    return _merge_gate(
        root_dir,
        pause_before_pr_merge=bundle.pause_before_pr_merge,
        wants_gate=wants_gate,
        notes=tuple(notes),
    )
