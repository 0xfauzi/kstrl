"""Every action-required inbox kind either has a reader of its approval or says why not (#595).

Before #595 two kinds, policy_exception and test_adequacy, filed an item
whose approval nothing read: ``ks inbox approve`` recorded it and the
next run failed on the same finding. The fix reads them in
``kstrl/waivers.py``. This file stops the next action-required kind from
arriving without a reader, and stops a reader from moving without the
table below moving with it.

Three layers, each with a different reason to fail.

1. KINDS. Every action-required ``ItemKind`` is in exactly one of
   ``APPROVAL_READERS`` (the scope that acts on an approval of it) and
   ``RECORD_ONLY`` (why approving it only closes the item).
2. READERS. A census over ``kstrl/`` minus ``inbox.py`` of every
   expression that can test for the approved status: an attribute named
   ``APPROVED`` on any base, and the string ``"approved"`` inside a
   comparison, because ``ItemStatus`` is a ``StrEnum`` and compares equal
   to it. Counted per ``module::scope`` and pinned exactly. Every reader
   named in ``APPROVAL_READERS`` must be a row of it.
3. BINDING. ``waivers.WAIVABLE`` is exactly the set of kinds whose
   reader is ``waivers.py::load_approvals``.

The behaviour of each waivable kind is driven end to end in
``tests/test_inbox_waivers.py``, whose scenario table must name every
kind in ``WAIVABLE``.
"""

from __future__ import annotations

import ast

from kstrl.inbox import ItemKind
from kstrl.waivers import WAIVABLE
from tests.helpers import astwalk

#: The scope that acts on an approval of each kind, as the census keys it.
APPROVAL_READERS: dict[ItemKind, str] = {
    ItemKind.MERGE_GATE: "pipeline.py::ComponentPipeline.apply_merge_decisions",
    ItemKind.POLICY_EXCEPTION: "waivers.py::load_approvals",
    ItemKind.TEST_ADEQUACY: "waivers.py::load_approvals",
}

#: Action-required kinds whose approval no kstrl step reads, and why.
RECORD_ONLY: dict[ItemKind, str] = {
    ItemKind.HALTED_RUN: "the action is ks retry or ks inbox retry; approving only closes the item",
    ItemKind.BUDGET_OVERRUN: (
        "the cap is config; an approval must not raise a spend cap (no budget "
        "bypass without explicit opt-in)"
    ),
    ItemKind.SPEC_ESCALATION: (
        "resolved by a later clean decompose (decisions.py), which does not read the approval"
    ),
}

#: Every scope outside inbox.py that can test for an approved status, and
#: how many times. A new row is a new reader: name it in APPROVAL_READERS
#: or NOT_AN_INBOX_READER before adding it here.
EXPECTED_APPROVED_READS: dict[str, int] = {
    "pipeline.py::ComponentPipeline._checkpoint_refusal": 1,
    "pipeline.py::ComponentPipeline._phase_checkpoint": 1,
    "pipeline.py::ComponentPipeline.apply_merge_decisions": 1,
    "waivers.py::load_approvals": 1,
}

#: Rows of the census that read some other vocabulary's APPROVED.
NOT_AN_INBOX_READER: dict[str, str] = {
    "pipeline.py::ComponentPipeline._checkpoint_refusal": (
        "CheckpointDecision.APPROVED: the interactive pre-PR checkpoint's answer"
    ),
    "pipeline.py::ComponentPipeline._phase_checkpoint": (
        "CheckpointDecision.APPROVED: the interactive pre-PR checkpoint's answer"
    ),
}

CONTROL_ATTRIBUTE = "if item.status is ItemStatus.APPROVED:\n    pass\n"
CONTROL_COMPARE = 'if item.status == "approved":\n    pass\n'


def _reads_approved(node: ast.AST) -> list[ast.AST]:
    """The nodes under ``node`` that test for the approved status."""
    found: list[ast.AST] = []
    for child in astwalk.all_nodes(node):
        if isinstance(child, ast.Attribute) and child.attr == "APPROVED":
            found.append(child)
        elif isinstance(child, ast.Compare):
            for operand in (child.left, *child.comparators):
                if isinstance(operand, ast.Constant) and operand.value == "approved":
                    found.append(operand)
    return found


def approved_census() -> dict[str, int]:
    rows: dict[str, int] = {}
    for source in astwalk.package_sources():
        if astwalk.label(source) == "inbox.py":
            continue
        tree = astwalk.parsed(source)
        owner = astwalk.scope_of(tree)
        for node in _reads_approved(tree):
            key = f"{astwalk.label(source)}::{owner[id(node)]}"
            rows[key] = rows.get(key, 0) + 1
    return rows


def test_every_action_required_kind_is_classified_once() -> None:
    required = {kind for kind in ItemKind if kind.action_required}
    assert set(APPROVAL_READERS).isdisjoint(RECORD_ONLY)
    assert set(APPROVAL_READERS) | set(RECORD_ONLY) == required, (
        "an action-required inbox kind has no reader of its approval and no "
        "reason why not. Name the scope that acts on the approval in "
        "APPROVAL_READERS, or say in RECORD_ONLY why approving only closes it."
    )


def test_the_census_nets_fire() -> None:
    """One control per disjunct: a pinned census is also what a switched-off net returns."""
    for control in (CONTROL_ATTRIBUTE, CONTROL_COMPARE):
        assert _reads_approved(astwalk.parse(control)), control


def test_the_approved_reads_are_pinned() -> None:
    found = approved_census()
    assert found == EXPECTED_APPROVED_READS, (
        "kstrl/ tests for an approved status somewhere new, or a count moved. "
        "Name the scope in APPROVAL_READERS or NOT_AN_INBOX_READER. "
        f"Found: {found}"
    )


def test_every_row_is_a_reader_or_says_why_not() -> None:
    readers = set(APPROVAL_READERS.values())
    assert readers.isdisjoint(NOT_AN_INBOX_READER)
    assert readers | set(NOT_AN_INBOX_READER) == set(EXPECTED_APPROVED_READS)


def test_waivable_is_exactly_the_kinds_load_approvals_reads() -> None:
    read_by_waivers = {
        kind for kind, reader in APPROVAL_READERS.items() if reader == "waivers.py::load_approvals"
    }
    assert set(WAIVABLE) == read_by_waivers
