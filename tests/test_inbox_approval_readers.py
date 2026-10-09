"""Every action-required inbox kind either has a reader of its approval or says why not (#595).

Before #595 two kinds, policy_exception and test_adequacy (which #696
slice 8 removed), filed an item
whose approval nothing read: ``ks inbox approve`` recorded it and the
next run failed on the same finding. The fix reads them in
``kstrl/waivers.py``, the same shape ``kstrl/plan_gate.py`` already used
for a plan park (#602) and ``kstrl/pipeline.py`` for a merge-gate park
(#465). This file stops the next action-required kind from arriving
without a reader, and stops a reader from moving without the table
below moving with it.

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
APPROVAL_READERS: dict[ItemKind, tuple[str, ...]] = {
    ItemKind.MERGE_GATE: ("pipeline.py::ComponentPipeline.apply_merge_decisions",),
    ItemKind.POLICY_EXCEPTION: ("waivers.py::load_approvals",),
    # #602: the L1 plan gate reads its item at the start of the next run.
    ItemKind.PLAN_GATE: ("plan_gate.py::run_plan_gate",),
    # #696: the newest approved stack item is what confirms a [stack].
    ItemKind.STACK_CONFIRMATION: ("stack.py::_latest_approval",),
    # #639 slice 4: an approval's comment is the owner's answer, which the
    # next decompose of that spec appends to the architect's input.
    ItemKind.SPEC_ESCALATION: ("owner_answers.py::read_owner_answers",),
    # #700 decision 14: an approved halt of the acceptance checks lets ks
    # retry merge over the checks it names on the commit it names. #466: an
    # approved halt of a carried check retires that check. Every other
    # halted_run approval still only closes the item.
    ItemKind.HALTED_RUN: ("waivers.py::acceptance_overrides", "waivers.py::carried_retirements"),
}

#: Action-required kinds whose approval no kstrl step reads, and why.
RECORD_ONLY: dict[ItemKind, str] = {
    ItemKind.BUDGET_OVERRUN: (
        "the cap is config; an approval must not raise a spend cap (no budget "
        "bypass without explicit opt-in)"
    ),
}

#: Every scope outside inbox.py that can test for an approved status, and
#: how many times. A new row is a new reader: name it in APPROVAL_READERS
#: or NOT_AN_INBOX_READER before adding it here.
EXPECTED_APPROVED_READS: dict[str, int] = {
    "cli.py::_decide_parked_merge_if_parked": 1,
    "factory.py::_emit_stack_confirmation": 1,
    "owner_answers.py::read_owner_answers": 1,
    "pipeline.py::<module>": 1,
    "pipeline.py::ComponentPipeline._checkpoint_refusal": 1,
    "pipeline.py::ComponentPipeline._phase_checkpoint": 1,
    "pipeline.py::ComponentPipeline.apply_merge_decisions": 1,
    "plan_gate.py::_settle": 2,
    "plan_gate.py::run_plan_gate": 5,
    "stack.py::_latest_approval": 2,
    "waivers.py::acceptance_overrides": 1,
    "waivers.py::carried_retirements": 1,
    "waivers.py::load_approvals": 1,
}

#: Rows of the census that read (or, for the module row, define) some
#: other vocabulary's APPROVED.
NOT_AN_INBOX_READER: dict[str, str] = {
    "factory.py::_emit_stack_confirmation": (
        'CheckpointResolved(decision="approved"): the event a run records for a stack '
        "confirmed_stack already confirmed, not a read of item.status"
    ),
    "cli.py::_decide_parked_merge_if_parked": (
        '{"approve": "approved", "reject": "rejected"}[action]: the past-tense verb '
        "for the log line, not a read of item.status"
    ),
    "pipeline.py::<module>": (
        'CheckpointDecision.APPROVED = "approved": the enum member definition itself, '
        "not a read of it"
    ),
    "pipeline.py::ComponentPipeline._checkpoint_refusal": (
        "CheckpointDecision.APPROVED: the interactive pre-PR checkpoint's answer"
    ),
    "pipeline.py::ComponentPipeline._phase_checkpoint": (
        "CheckpointDecision.APPROVED: the interactive pre-PR checkpoint's answer"
    ),
    "plan_gate.py::_settle": (
        'decision == "approved"/"rejected": tests the local string run_plan_gate '
        "already decided, not a fresh read of item.status"
    ),
}

CONTROL_ATTRIBUTE = "if item.status is ItemStatus.APPROVED:\n    pass\n"
CONTROL_COMPARE = 'if item.status == "approved":\n    pass\n'


def _reads_approved(node: ast.AST) -> list[ast.AST]:
    """The nodes under ``node`` that could test for the approved status.

    Over-matches on purpose. The narrower version - only a ``.APPROVED``
    attribute or a lowercase ``"approved"`` constant, and only as a
    direct operand of an ``ast.Compare`` - missed an ``in (...)`` tuple,
    a ``match``/``case`` pattern, a ``.name ==`` comparison against the
    uppercase member name, and a bare ``ItemStatus("approved")`` call: a
    clearing guard must flag when it is not sure a site is unrelated
    (CLAUDE.md), not require the exact shape of a comparison. So this
    matches any ``APPROVED`` attribute access and any string constant
    equal to "approved" in either case, wherever each sits.
    """
    found: list[ast.AST] = []
    for child in astwalk.all_nodes(node):
        if isinstance(child, ast.Attribute) and child.attr == "APPROVED":
            found.append(child)
        elif (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value.lower() == "approved"
        ):
            found.append(child)
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
    readers = {reader for scopes in APPROVAL_READERS.values() for reader in scopes}
    assert readers.isdisjoint(NOT_AN_INBOX_READER)
    assert readers | set(NOT_AN_INBOX_READER) == set(EXPECTED_APPROVED_READS)


def test_waivable_is_exactly_the_kinds_load_approvals_reads() -> None:
    read_by_waivers = {
        kind for kind, scopes in APPROVAL_READERS.items() if "waivers.py::load_approvals" in scopes
    }
    assert set(WAIVABLE) == read_by_waivers
