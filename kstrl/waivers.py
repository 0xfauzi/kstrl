"""An approved policy_exception item waives exactly that finding (#595).

A blocking ``policy_*`` finding fails Phase 1 and files an inbox item.
Approving the item tells the factory "this one is allowed". This module
is how a later run reads that decision.

- **The key.** :func:`waiver_key` is the one definition of "the same
  finding". The pipeline stores it in the item's evidence when it files
  the item, and the check recomputes it for every finding it raises. It
  covers the run's project, spec file and plan id, the component, and
  the finding's category, location and explanation. The explanation
  names every path, dependency, test and secret line the finding is
  about and embeds the configured limit, so an approval covers what the
  operator read and nothing else, and editing ``[policy]`` makes an old
  approval stop matching.
- **The change.** An approval also covers only the change it was taken
  on (#646). The item records ``diff_sha``, the sha256 of the diff Phase
  1 judged, and :meth:`ApprovalSnapshot.for_scope` refuses the approval
  for any other diff, so a regenerated change is asked again even when
  its finding reads the same. An item with no ``diff_sha`` was filed
  before this binding and is refused.
- **The retry.** ``ks retry`` keeps the failed branch when Phase 1
  failed on :data:`WAIVABLE_CHECKS` alone and an approved item was taken
  on the branch tip (:func:`approved_head`), so the change the operator
  approved is the change judged again, with no engineer. Anything else
  is regenerated, and the plan says why.
- **The read.** :func:`load_approvals` reads the inbox ONCE, when the run
  starts (``ComponentPipeline.snapshot_waivers``). A decision made
  mid-run does not change what a later attempt in that run is held to
  (#192), so an engineer that runs ``ks inbox approve`` inside its own
  run does not pass its own next attempt.
- **The effect.** :func:`apply_waivers` runs inside the check, before the
  check decides ``passed``, so the check stays the only place a finding
  becomes blocking. A matched finding is kept and re-emitted as
  ``advisory`` with a ``waiver:<item id>`` tag and an explanation suffix
  naming the approval. Not every refusal adds a tag: an unconsulted
  snapshot (the inbox itself could not be read) leaves a finding
  untagged and says so only in the check's message.
- **Fail closed.** Every path that cannot prove an approval covers a
  finding leaves the finding blocking, as it was before this module,
  and says why: a ``waiver_refused:<item id>`` tag on the finding and a
  reason in the check's message.

Only an APPROVED item grants anything, so ``ks inbox reject`` on an
approved item withdraws the waiver from the next run on.

The acceptance override (#700 owner decision 14) is read here too, the
same way and at the same time. When the operator's acceptance checks halt
a component, its halted_run item records the failing checks and the
commit (``evidence.check``, ``evidence.head_sha``). Approving that item,
then ``ks retry``, keeps that commit and judges it again with no engineer
(:func:`approved_head`), and the acceptance gate passes it when the
approval names every check that fails there (:func:`covering_override`).
The record and the PR body name the approval, who gave it and when.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.context import ACCEPTANCE_PHASE
from kstrl.findings import (
    POLICY_CATEGORY_PREFIX,
    WAIVER_TAG_PREFIX,
    Finding,
)
from kstrl.inbox import Inbox, InboxConfig, InboxError, InboxItem, ItemKind, ItemStatus
from kstrl.statedir import ControlStateError

if TYPE_CHECKING:
    from kstrl.manifest import Component

#: Bumped when the key's fields change. A stored key made under another
#: version never equals a recomputed one, so every older approval is
#: refused rather than reinterpreted.
WAIVER_KEY_VERSION = 1

#: The inbox kinds an approval of which waives a finding, each with the
#: finding-category prefix it may cover.
WAIVABLE: dict[ItemKind, str] = {
    ItemKind.POLICY_EXCEPTION: POLICY_CATEGORY_PREFIX,
}

#: The Phase 1 checks whose blocking findings are the waivable categories
#: above. ``ks retry`` keeps a failed head only when Phase 1 failed on
#: these checks and nothing else (#646).
WAIVABLE_CHECKS = frozenset({"policy_envelope"})

#: policy.py calls the enforcement-machinery halt non-overridable. An
#: approval of one is refused, never applied.
NON_WAIVABLE_CATEGORIES = frozenset({f"{POLICY_CATEGORY_PREFIX}enforcement_machinery"})

WAIVER_REFUSED_TAG_PREFIX = "waiver_refused:"

#: What rejecting a waivable item does, for the CLI and the TUI.
REJECTION_EFFECT = (
    "records the rejection with your reason. No waiver: the next run fails on this "
    "finding as before."
)

_HEX64 = re.compile(r"[0-9a-f]{64}")

#: A full commit id, sha-1 or sha-256.
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class WaiverScope:
    """Who a finding belongs to: the run's plan and the component."""

    project: str
    spec_file: str
    plan_id: str
    component: str

    def key(self, finding: Finding) -> str:
        return waiver_key(
            self,
            category=finding.category,
            location=finding.location,
            explanation=finding.explanation,
        )


def waiver_key(scope: WaiverScope, *, category: str, location: str, explanation: str) -> str:
    """sha256 of the canonical JSON of one finding in one scope."""
    payload = {
        "v": WAIVER_KEY_VERSION,
        "project": scope.project,
        "spec_file": scope.spec_file,
        "plan_id": scope.plan_id,
        "component": scope.component,
        "category": category,
        "location": location,
        "explanation": explanation,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Waiver:
    item_id: str
    category: str
    decided_by: str
    decided_at: str


@dataclass(frozen=True)
class Refusal:
    """An approved item for this component that is not applied, and why."""

    item_id: str
    category: str
    reason: str


@dataclass(frozen=True)
class Waivers:
    """The approvals that apply to one component in this run."""

    scope: WaiverScope
    by_key: Mapping[str, Waiver] = field(default_factory=dict)
    refused: tuple[Refusal, ...] = ()
    #: Non-empty when the inbox was not read: nothing is waived, and a
    #: finding that still blocks says so.
    unconsulted_reason: str = ""


@dataclass(frozen=True)
class ApprovalSnapshot:
    """Every approved waivable item, read once when the run starts."""

    approved: tuple[InboxItem, ...] = ()
    unconsulted_reason: str = ""
    #: The approved acceptance halts (:func:`acceptance_overrides`).
    overrides: tuple[InboxItem, ...] = ()
    #: The approved items that retire a carried check (#466).
    retirements: tuple[InboxItem, ...] = ()

    def for_scope(self, scope: WaiverScope, diff_sha: str) -> Waivers:
        """The approvals for ``scope`` that cover the change ``diff_sha`` (#646).

        ``diff_sha`` is the sha256 of the diff this attempt is judged on,
        "" when it could not be read, which no approval matches.
        """
        if self.unconsulted_reason:
            return Waivers(scope, unconsulted_reason=self.unconsulted_reason)
        by_key: dict[str, Waiver] = {}
        refused: list[Refusal] = []
        for item in self.approved:
            if item.component != scope.component:
                continue
            category = item.evidence.get("category")
            reason = _refusal(item, scope, diff_sha)
            if reason:
                refused.append(
                    Refusal(item.id, category if isinstance(category, str) else "", reason)
                )
                continue
            assert isinstance(category, str)
            key = item.evidence["waiver_key"]
            by_key[key] = Waiver(item.id, category, item.decided_by, item.decided_at)
        return Waivers(scope, by_key, tuple(refused))


def load_approvals(inbox: Inbox) -> ApprovalSnapshot:
    """One read of the inbox: every APPROVED item of a waivable kind.

    A line the fold cannot parse could be the rejection of an approved
    item, so any unparseable line means the approvals are not consulted.
    """
    scan = inbox.scan()
    if scan.unreadable:
        return ApprovalSnapshot(unconsulted_reason=f"the inbox at {inbox.path} could not be read")
    unparseable = scan.unparseable_count()
    if unparseable:
        return ApprovalSnapshot(
            unconsulted_reason=(
                f"{unparseable} line(s) of the inbox at {inbox.path} could not be parsed"
            )
        )
    items = scan.folded_items()
    approved = tuple(
        item for item in items if item.kind in WAIVABLE and item.status is ItemStatus.APPROVED
    )
    return ApprovalSnapshot(
        approved=approved,
        overrides=acceptance_overrides(items),
        retirements=carried_retirements(items),
    )


def carried_retirements(items: Sequence[InboxItem]) -> tuple[InboxItem, ...]:
    """The approved halted_run items that retire a carried check (#466)."""
    return tuple(
        item for item in items if item.status is ItemStatus.APPROVED and carried_halt(item)
    )


def carried_halt(item: InboxItem) -> bool:
    """Whether ``item`` is the halted_run of a carried check that does not
    pass on the base (#466), naming its plan, component and check."""
    carried = item.evidence.get("carried")
    return (
        item.kind is ItemKind.HALTED_RUN
        and isinstance(carried, dict)
        and all(isinstance(carried.get(k), str) for k in ("planId", "component", "check"))
    )


def acceptance_overrides(items: Sequence[InboxItem]) -> tuple[InboxItem, ...]:
    """The approved halted_run items an acceptance halt filed, each naming
    the full commit it was taken on (#700 owner decision 14)."""
    return tuple(
        item for item in items if item.status is ItemStatus.APPROVED and _acceptance_halt(item)
    )


def _acceptance_halt(item: InboxItem) -> bool:
    """Whether ``item`` is a halted_run an acceptance halt filed, naming the
    full commit it was taken on. It does not read the status, so
    :func:`approval_effect` can say what approving an open one does."""
    return (
        item.kind is ItemKind.HALTED_RUN
        and item.evidence.get("phase") == ACCEPTANCE_PHASE
        and _COMMIT.fullmatch(str(item.evidence.get("head_sha") or "")) is not None
    )


def covering_override(
    items: Sequence[InboxItem], head_sha: str, failing: Sequence[str]
) -> dict[str, str] | None:
    """The approval among ``items`` taken on ``head_sha`` that names every
    check in ``failing``, as an acceptance record keeps it; None when there
    is none. An approval never covers a check it does not name."""
    for item in items:
        named = {name.strip() for name in str(item.evidence.get("check", "")).split(",")}
        if failing and item.evidence.get("head_sha") == head_sha and set(failing) <= named:
            return {"item": item.id, "by": item.decided_by, "at": item.decided_at}
    return None


def approvals_at(root_dir: Path, inbox: InboxConfig | None = None) -> ApprovalSnapshot:
    """:func:`load_approvals` for a command outside a run (``ks retry``, #646).

    Fails closed the way ``ComponentPipeline.snapshot_waivers`` does: a
    disabled or unreadable inbox is an unconsulted snapshot, which covers
    nothing, and says why.
    """
    try:
        config = inbox if inbox is not None else InboxConfig.load(root_dir)
        if not config.enabled:
            return ApprovalSnapshot(unconsulted_reason="the inbox is disabled")
        return load_approvals(Inbox(root_dir, config))
    except (OSError, TypeError, ValueError, InboxError, ControlStateError) as exc:
        # The tuple ComponentPipeline.snapshot_waivers catches, for its reasons.
        return ApprovalSnapshot(unconsulted_reason=f"the inbox read failed: {exc}")


def approvals_on(snapshot: ApprovalSnapshot, component: str, head_sha: str) -> list[str]:
    """The ids of the approved items for ``component`` taken on commit ``head_sha``.

    Only items whose own evidence is valid (:func:`_evidence_refusal`):
    an item the gate refuses whatever the run is never a reason to keep
    its commit. ``head_sha`` "" matches nothing.
    """
    return [
        item.id
        for item in snapshot.approved
        if head_sha
        and item.component == component
        and item.evidence.get("head_sha") == head_sha
        and not _evidence_refusal(item)
    ]


def overrides_on(snapshot: ApprovalSnapshot, component: str, head_sha: str) -> list[str]:
    """The ids of the approved acceptance halts of ``component`` taken on
    commit ``head_sha`` (#700 owner decision 14). ``head_sha`` "" matches
    nothing."""
    return [
        item.id
        for item in snapshot.overrides
        if head_sha and item.component == component and item.evidence["head_sha"] == head_sha
    ]


def approved_head(snapshot: ApprovalSnapshot, comp: Component, tip: str) -> tuple[str, str]:
    """The commit ``ks retry`` keeps for ``comp`` and why, or "" and why it keeps none (#646).

    Read BEFORE ``Manifest.reset_for_retry``, which clears the failure
    fields this reads. ``tip`` is the failed branch's commit, "" when it is
    gone or git cannot read it. The head is kept only when Phase 1 failed,
    on waivable checks alone, and an approved item was taken on that exact
    commit. The kept head is judged again with no engineer; whatever it
    raises that no approval covers still fails.
    """
    if comp.failed_phase == ACCEPTANCE_PHASE:
        return _overridden_head(snapshot, comp, tip)
    if comp.failed_phase != "verify":
        return "", f"it failed in phase {comp.failed_phase or '(none recorded)'}, not Phase 1"
    other = [
        c or "(none recorded)" for c in comp.failed_check.split(", ") if c not in WAIVABLE_CHECKS
    ]
    if other:
        return "", f"Phase 1 also failed on {', '.join(other)}, which no approval covers"
    if not tip:
        return "", f"branch '{comp.branch_name}' does not exist"
    if snapshot.unconsulted_reason:
        return "", f"approvals were not consulted: {snapshot.unconsulted_reason}"
    ids = approvals_on(snapshot, comp.id, tip)
    if not ids:
        return "", f"no approved item was taken on commit {tip[:12]}, the branch tip"
    named = ", ".join(i[:8] for i in ids)
    return tip, f"approval {named} was taken on commit {tip[:12]}, the branch tip"


def _overridden_head(snapshot: ApprovalSnapshot, comp: Component, tip: str) -> tuple[str, str]:
    """:func:`approved_head` for a component its acceptance checks halted:
    the tip is kept when an approval of that halt was taken on it."""
    if not tip:
        return "", f"branch '{comp.branch_name}' does not exist"
    if snapshot.unconsulted_reason:
        return "", f"approvals were not consulted: {snapshot.unconsulted_reason}"
    ids = overrides_on(snapshot, comp.id, tip)
    if not ids:
        return "", f"no approved acceptance halt was taken on commit {tip[:12]}, the branch tip"
    named = ", ".join(i[:8] for i in ids)
    return tip, f"approval {named} was taken on commit {tip[:12]}, the branch tip"


def _evidence_refusal(item: InboxItem) -> str:
    """Why ``item``'s own evidence refuses a waiver, scope aside.

    Scope-free: everything :func:`_refusal` can decide without a run to
    check the key against. :func:`approval_effect` uses exactly this half
    - the shell and the TUI have no scope, only the item - so what they
    print and what the gate does are the same check, not two.
    """
    tag = f"approval {item.id[:8]}"
    evidence = item.evidence
    prefix = WAIVABLE[item.kind]
    category = evidence.get("category")
    if not isinstance(category, str) or not category.startswith(prefix):
        return f"{tag}: evidence.category {category!r} is not a {prefix}* category"
    if category in NON_WAIVABLE_CATEGORIES:
        return f"{tag}: {category} is non-overridable, so the approval is not applied"
    if "waiver_key" not in evidence:
        return f"{tag}: filed before an approval could waive a finding (no evidence.waiver_key)"
    key = evidence["waiver_key"]
    if not isinstance(key, str) or not _HEX64.fullmatch(key):
        return f"{tag}: evidence.waiver_key is not a 64-character hex digest"
    for name in ("location", "explanation"):
        if not isinstance(evidence.get(name), str):
            return f"{tag}: evidence.{name} is not a string"
    diff_sha = evidence.get("diff_sha")
    if diff_sha is None:
        return f"{tag}: filed before an approval was bound to its change (no evidence.diff_sha)"
    if not isinstance(diff_sha, str) or not _HEX64.fullmatch(diff_sha):
        return f"{tag}: evidence.diff_sha is not a 64-character hex digest, so it names no change"
    return ""


def _refusal(item: InboxItem, scope: WaiverScope, diff_sha: str) -> str:
    """Why an approved item is not applied, or "" when it covers ``scope`` and ``diff_sha``."""
    reason = _evidence_refusal(item)
    if reason:
        return reason
    tag = f"approval {item.id[:8]}"
    evidence = item.evidence
    recomputed = waiver_key(
        scope,
        category=evidence["category"],
        location=evidence["location"],
        explanation=evidence["explanation"],
    )
    if recomputed != evidence["waiver_key"]:
        return (
            f"{tag}: evidence.waiver_key does not match this run's project "
            f"{scope.project!r}, spec {scope.spec_file!r}, plan {scope.plan_id!r} and "
            f"component {scope.component!r}"
        )
    if evidence["diff_sha"] != diff_sha:
        judged = f"diff {diff_sha[:12]}" if diff_sha else "a diff that could not be read"
        return (
            f"{tag}: taken on diff {evidence['diff_sha'][:12]}, this attempt judges "
            f"{judged}; a regenerated change is asked again"
        )
    return ""


def _waivable(finding: Finding) -> bool:
    return finding.severity != "advisory" and finding.category.startswith(tuple(WAIVABLE.values()))


def apply_waivers(
    findings: Sequence[Finding], waivers: Waivers | None
) -> tuple[list[Finding], list[str], list[str]]:
    """Waive each blocking finding an approval covers exactly.

    Returns ``(findings, waived item ids, refusal reasons)``. ``None``
    (``ks check``, or a pipeline that took no snapshot) changes nothing.
    """
    if waivers is None:
        return list(findings), [], []
    out: list[Finding] = []
    waived: list[str] = []
    refusals: list[str] = []
    for finding in findings:
        if not _waivable(finding):
            out.append(finding)
            continue
        # NON_WAIVABLE_CATEGORIES is not rechecked here: _evidence_refusal
        # already keeps a non-overridable category out of by_key, in
        # for_scope, so a match can never be one.
        match = waivers.by_key.get(waivers.scope.key(finding))
        if match is not None:
            out.append(_waive(finding, match))
            waived.append(match.item_id)
            continue
        refused, reasons = _refuse(finding, waivers)
        out.append(refused)
        refusals.extend(reasons)
    return out, waived, list(dict.fromkeys(refusals))


def _waive(finding: Finding, match: Waiver) -> Finding:
    """The finding, kept, as advisory and naming the approval that covers it."""
    return replace(
        finding,
        severity="advisory",
        explanation=(
            f"{finding.explanation} [waived by inbox approval {match.item_id[:8]} "
            f"({match.decided_by} at {match.decided_at})]"
        ),
        tags=finding.tags + (f"{WAIVER_TAG_PREFIX}{match.item_id}",),
    )


def _refuse(finding: Finding, waivers: Waivers) -> tuple[Finding, list[str]]:
    """The still-blocking finding, tagged with every approval of its category not applied."""
    tags: list[str] = []
    reasons: list[str] = []
    for refusal in waivers.refused:
        if refusal.category in (finding.category, ""):
            tags.append(f"{WAIVER_REFUSED_TAG_PREFIX}{refusal.item_id}")
            reasons.append(refusal.reason)
    for other in waivers.by_key.values():
        if other.category == finding.category:
            tags.append(f"{WAIVER_REFUSED_TAG_PREFIX}{other.item_id}")
            reasons.append(
                f"approval {other.item_id[:8]} covers a different {finding.category} "
                "finding, so it is not applied"
            )
    if waivers.unconsulted_reason:
        reasons.append(f"approvals were not consulted: {waivers.unconsulted_reason}")
    return replace(finding, tags=finding.tags + tuple(tags)), reasons


def waiver_note(waived: Sequence[str], refusals: Sequence[str]) -> str:
    """The suffix a check appends to its message: what was waived and what was refused."""
    note = ""
    if waived:
        note += f"; {len(waived)} waived by inbox approval " + ", ".join(i[:8] for i in waived)
    for reason in refusals:
        note += f"; {reason}"
    return note


def approval_effect(item: InboxItem) -> str | None:
    """What approving ``item`` does, or None for a kind no step waives.

    #595 B3: the same check as the gate's, ``_evidence_refusal`` - this
    function adds no checks of its own, so the shell and the TUI cannot
    say "waives" about evidence the gate itself would refuse.
    """
    if _acceptance_halt(item):
        return (
            f"merges over the failing acceptance checks {item.evidence.get('check')} of "
            f"{item.component} on commit {str(item.evidence['head_sha'])[:12]} only: ks retry "
            f"{item.component} keeps that commit and judges it again with no engineer, and the "
            "PR body names this approval. A single-PR run, or a component built on unmerged "
            "dependency code, keeps no commit, so nothing is merged over. Any other failing "
            "check or commit still fails."
        )
    if carried_halt(item):
        carried = item.evidence["carried"]
        return (
            f"retires the acceptance check {carried['check']} of {carried['component']} in the "
            f"plan {carried['planId'][:12]}: no later run replays it, on the base or in Phase 3"
        )
    if item.kind not in WAIVABLE:
        return None
    reason = _evidence_refusal(item)
    if reason:
        return f"records approval only: {reason}"
    explanation = item.evidence["explanation"]
    head = item.evidence.get("head_sha")
    seen = (
        f"found on commit {head[:12]}"
        if isinstance(head, str) and head
        else "the item records no commit"
    )
    return (
        f"waives this one finding for {item.component}: {explanation!r} ({seen}). When Phase "
        f"1 failed on nothing an approval cannot cover, ks retry {item.component} keeps that "
        "commit and judges it again with no engineer, and the approval applies when that "
        "judgement reproduces it exactly (same category, location and explanation). A "
        "regenerated change is asked again. Any other finding still fails, and ks inbox "
        f"reject {item.id[:8]} withdraws the waiver."
    )
