"""The chokepoint every check name has to pass through, inventoried.

Split out of ``tests/test_check_name_enrolment.py`` on #339 review,
and the seam is a difference in GUARANTEE rather than in subject.

That file walks ``kstrl/`` for four producer SHAPES, resolves the check
names out of them and asserts the category table carries what it finds.
Everything it knows is bounded by a shape somebody enumerated, so a
producer written in an unimagined shape is silence: not resolved, and
not recorded in its ``BLIND_SITES`` ledger either. That ledger lists the
places the walk gave up, which is a strictly smaller set than the places
a check name can be written, and #339 review found ``_fail_pr_flow``
living in the gap between the two for two review rounds.

This file has the other guarantee, the one
``tests/test_journal_one_writer.py`` gets from
``EXPECTED_JOURNAL_PATH_SITES``: "code cannot write to a file whose path
it never obtained." The same sentence holds here. A check name cannot
reach ``evolution.category_for_check`` without passing through
``pipeline.component_failure_signatures``, so every producer must touch
one of the five names below, whatever shape it is written in. No matcher
to widen, no shape to recognise, nothing to go blind.

Measured both ways on #339. The day ``_fail_pr_flow`` was written,
``self._record_failure_signatures`` went from two occurrences to three
and this pin would have gone red. And a new writer reaching the mapping
through a local alias with a runtime-built signature - the shape nothing
else here can see - fails this pin and no other.

Until #324 gives every guard in this repo one shared AST walker, this is
the part of the check-name enrolment story that is closed by
construction.
"""

from __future__ import annotations

import ast

from tests.helpers.astfold import parsed_modules
from tests.test_check_name_enrolment import SIGNATURE_CONTAINER_SUFFIX

#: EVERY mention of a ``*failure_signatures`` name in ``kstrl/``, as
#: ``(module, spelling) -> occurrences``. The chokepoint inventory, and
#: the only thing in this file that is closed BY CONSTRUCTION.
#:
#: The distinction #339 review drew, and it is the one that matters here.
#: :data:`BLIND_SITES` lists the walk's own resolution FAILURES, and
#: ``tests/test_check_name_shapes.py`` lists the shapes its author
#: thought of. Both are bounded by what somebody already knew. This is
#: not: a check name cannot reach ``category_for_check`` without passing
#: through ``pipeline.component_failure_signatures``, which is the same
#: sentence ``tests/test_journal_one_writer.py`` makes its
#: ``EXPECTED_JOURNAL_PATH_SITES`` out of ("code cannot write to a file
#: whose path it never obtained"), so every producer must touch one of
#: these names whatever shape it is written in.
#:
#: Measured against the defect this PR is downstream of: the day
#: ``_fail_pr_flow`` was written, ``self._record_failure_signatures``
#: went from 2 occurrences to 3 and this pin would have gone red, with
#: no matcher to widen and no shape to recognise. It was instead
#: invisible for as long as it took two review rounds to find it.
#:
#: Counts rather than a set, because a THIRD caller of the recorder is
#: the event worth catching and the module already had two. The counts
#: are expected to move: the diff that moves one is where somebody says
#: whether the new site's check name is censused.
EXPECTED_SIGNATURE_CONTAINER_SITES: dict[tuple[str, str], int] = {
    # The recorder itself, and its three callers: fail, retry_or_fail
    # and _fail_pr_flow.
    ("kstrl/pipeline.py", "self._record_failure_signatures"): 3,
    # The mapping, under all three spellings it is reached by.
    ("kstrl/pipeline.py", "self.component_failure_signatures"): 9,
    ("kstrl/pipeline.py", "component_failure_signatures"): 1,
    ("kstrl/factory.py", "component_failure_signatures"): 5,
    # The reader: record_run takes the mapping as an argument and writes
    # each component's list into the journal entry.
    ("kstrl/evolution.py", "failure_signatures"): 4,
}


def signature_container_sites() -> dict[tuple[str, str], int]:
    """``(module, spelling) -> occurrences`` for the chokepoint names.

    Names and attributes only. A string literal ``"failure_signatures"``
    is the journal's COLUMN rather than the mapping, and counting it
    would make this inventory move on an unrelated edit to a dict key.
    """
    found: dict[tuple[str, str], int] = {}
    for rel, tree in parsed_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id.endswith(SIGNATURE_CONTAINER_SUFFIX):
                spelling = node.id
            elif isinstance(node, ast.Attribute) and node.attr.endswith(SIGNATURE_CONTAINER_SUFFIX):
                spelling = ast.unparse(node)
            else:
                continue
            found[(rel, spelling)] = found.get((rel, spelling), 0) + 1
    return found


class TestTheChokepointIsInventoried:
    """The closed-by-construction half, in the shape of
    ``tests/test_journal_one_writer.py``'s ``EXPECTED_JOURNAL_PATH_SITES``.

    Every other guard in this file and its two siblings is bounded by a
    shape somebody enumerated, so a producer written in an unimagined
    shape is silence rather than a red test. This one is not: a check
    name reaches the journal through
    ``pipeline.component_failure_signatures`` or it does not reach it at
    all, so the census of that name's mentions moves whenever a producer
    is added, whatever the producer looks like.
    """

    def test_the_chokepoint_is_touched_in_exactly_these_places(self) -> None:
        """The message names the action, because this pin fires on
        ordinary refactors too and a pin whose message is 'update the
        number' teaches nothing."""
        measured = signature_container_sites()
        pinned = EXPECTED_SIGNATURE_CONTAINER_SITES
        grown = sorted(key for key, n in measured.items() if pinned.get(key) != n)
        shrunk = sorted(key for key, n in pinned.items() if measured.get(key) != n)
        assert measured == pinned, (
            f"the places kstrl/ touches a *{SIGNATURE_CONTAINER_SUFFIX} name "
            f"have moved. Added or grown: {grown}. Gone or shrunk: {shrunk}. "
            f"If a new site WRITES a component's signatures, make sure the "
            f"check name it writes is censused: check_names() must contain "
            f"it, or BLIND_SITES must say why it cannot be read. Then "
            f"update the count here."
        )

    def test_the_recorder_has_the_callers_the_walk_assumes(self) -> None:
        """Named separately from the count above because it is the
        specific claim ``PHASE_FALLBACK_CALLS`` rests on: keying the
        producer on ``fail`` and ``retry_or_fail`` was correct only
        while those were the recorder's ONLY callers, and it stopped
        being correct without anything going red. Three now: the two
        entry points and ``_fail_pr_flow``."""
        callers = EXPECTED_SIGNATURE_CONTAINER_SITES[
            ("kstrl/pipeline.py", "self._record_failure_signatures")
        ]
        assert callers == 3, (
            "the failure-signature recorder has a different number of "
            "call sites. A new one files its failure under its PHASE "
            "unless it passes signatures=, so census that phase."
        )
