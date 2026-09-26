"""What each inbox choice changes, derived from the code that reads it (#433 Q4, Q5).

The inbox detail listed an item and offered approve, reject and snooze
with no word on what any of them does. What they do depends on who reads
the decision, and for most kinds nobody does:

- A merge-gate PARK (dedupe key ``manifest.park_dedupe_key``) is read by
  ``Pipeline.apply_merge_decisions`` at the start of the next
  ``ks factory`` run. Approved: the branch is pushed and merged if its
  head is still the commit the gate parked (``evidence.head_sha``),
  otherwise the component fails and nothing is pushed. Rejected: the
  component fails (check ``hitl_reject``) and its dependents are skipped.
  Anything else leaves it parked. ``ks serve`` admits no new work while
  a park exists. The decision acts only while the component is still
  AWAITING_APPROVAL; for any other status neither approve nor reject has
  an effect, so neither is offered.
- Every other kind is record-only: a grep for the approved and rejected
  statuses outside ``kstrl/inbox.py`` finds only the park consumer.
  Approve and reject close the item and nothing else.
- Snooze hides any item until ``snooze_hours`` pass; it returns by the
  clock (``InboxItem.is_open``), and nothing else changes.

The TUI records the decision only. ``ks inbox approve`` and
``ks inbox reject`` on a park also start ``ks factory``; this screen does
not, and the approve sentence says so first (#433 advice 2.2).
"""

from __future__ import annotations

from dataclasses import dataclass

from kstrl.inbox import InboxItem, ItemKind
from kstrl.manifest import MERGE_GATE_PARK_KEY, ComponentStatus

APPROVE = "approve"
REJECT = "reject"
SNOOZE = "snooze"


@dataclass(frozen=True)
class Consequences:
    #: (choice, sentence) for each choice offered, in key order.
    offered: tuple[tuple[str, str], ...]
    #: Why a choice is not offered, "" when all three are.
    withheld: str = ""
    #: What this screen does not do that the CLI does, "" when nothing.
    note: str = ""

    def allows(self, choice: str) -> bool:
        return any(name == choice for name, _ in self.offered)


def kind_label(kind: str) -> str:
    """``merge gate``, not ``merge_gate`` (#433 G6)."""
    return str(kind).replace("_", " ")


def _hours(snooze_hours: float) -> str:
    return f"{snooze_hours:g}h"


def _snooze(item: InboxItem, snooze_hours: float, still: str) -> tuple[str, str]:
    return (
        SNOOZE,
        f"hides this item for {_hours(snooze_hours)}; it returns to the list after that. {still}",
    )


def _is_park(item: InboxItem) -> bool:
    return item.kind is ItemKind.MERGE_GATE and item.dedupe_key.startswith(MERGE_GATE_PARK_KEY)


def _park(item: InboxItem, component_status: str | None, snooze_hours: float) -> Consequences:
    cid = item.component or item.dedupe_key.removeprefix(MERGE_GATE_PARK_KEY)
    head = str(item.evidence.get("head_sha") or "")
    parked = component_status == ComponentStatus.AWAITING_APPROVAL.value
    snooze = _snooze(
        item,
        snooze_hours,
        f"{cid} stays parked and ks serve admits no new work meanwhile."
        if parked
        else "Nothing else changes.",
    )
    if not parked:
        why = (
            "the manifest could not be read, so the effect is unknown"
            if component_status is None
            else f"{cid} is {component_status}, not parked, so nothing reads the decision"
        )
        return Consequences(
            offered=(snooze,), withheld=f"approve and reject are not offered: {why}"
        )
    approve = (
        "records approval only; nothing merges until the next ks factory run. That run "
        f"pushes and merges {cid}'s branch if its head is still {head[:12]}; otherwise "
        f"{cid} fails and nothing is pushed. From the shell, ks inbox approve also starts that run."
        if head
        else "records approval only. The next ks factory run fails "
        f"{cid}: the park recorded no commit to merge, so nothing is pushed."
    )
    return Consequences(
        offered=(
            (APPROVE, approve),
            (
                REJECT,
                "records rejection with your reason only. The next ks factory run marks "
                f"{cid} failed as rejected by a person and skips its dependents.",
            ),
            snooze,
        ),
    )


def consequences(
    item: InboxItem,
    component_status: str | None,
    snooze_hours: float,
) -> Consequences:
    """The choices to offer for ``item`` and what each one changes.

    ``component_status`` is the manifest status of ``item.component``, or
    None when the manifest could not be read.
    """
    if _is_park(item):
        return _park(item, component_status, snooze_hours)
    record_only = f"closes this item; no kstrl step reads a {kind_label(item.kind)} decision."
    return Consequences(
        offered=(
            (APPROVE, record_only),
            (REJECT, f"{record_only} It asks for your reason."),
            _snooze(item, snooze_hours, "Nothing else changes."),
        ),
    )
