"""One home for the AST walking the static guards in ``tests/`` do.

#324 is the record of what having eleven of them cost. Ten instances are
logged there, every one in the SKIP direction: a matcher could not resolve
a name or fold a value, so it silently did not look, and reported clean.
Two were holed a second time after being fixed once, and one was written
by an author explicitly briefed on the pattern, which is the evidence that
briefing is not a control.

So a shared resolver is not the whole job: on its own it would move eleven
holes into one. What this has to do as well is make the skip direction
LOUD, in five places the API will not let a caller leave out:
:func:`assert_census` requires a control, :func:`assert_sites` requires an
expectation for the undecided half, :class:`Clause` carries ``decided``,
:class:`Bindings` keeps ``opaque``, and :func:`blind_spot` is the body of
a disclosed limit's anti-vacuity test. Each says why on itself.

THE ONE EXCEPTION, stated here rather than left for a reader to find.
:func:`resolved_calls` returns the seen half as NODES, for a guard that
has to read a call's arguments, and a node-returning signature cannot
also force the undecided half. So that one is held by a static guard
instead: ``tests/test_astwalk.py``'s ``TestResolvedCallsIsNotUsableOnItsOwn``
fails any module in ``tests/`` that calls it and never names the other
half. Round 2 of #324 measured the hole first and then closed it, which
is the order that makes the claim above checkable rather than hopeful.

THE DISTINCTION THAT DRIVES THE SHAPE. ``EXPECTED_JOURNAL_PATH_SITES`` in
``tests/test_journal_one_writer.py`` inventories every place the resource
is OBTAINED, so it is closed by construction: you cannot add a writer
without adding a row. A ledger of places the walk gave up is closed only
over the shapes the walk already enumerates, and #324's instance 10 is the
proof: a well-built ledger with reasons per row still missed a producer,
because the producer's SHAPE was never enumerated. :func:`census` is that
closed form generalised. It enumerates no node types, and :func:`spells`
enumerates no FIELDS either, which is why it catches shapes nobody thought
of. Prefer it wherever the subject permits; where it does not, say so and
pin the residual with :func:`blind_spot`.
"""

from __future__ import annotations

from tests.helpers.astwalk.corpus import (
    KSTRL_PACKAGE,
    REPO_ROOT,
    TESTS_DIR,
    folded_str,
    label,
    module_name,
    package_sources,
    parse,
    parsed,
    test_sources,
)
from tests.helpers.astwalk.disclose import blind_spot
from tests.helpers.astwalk.net import (
    Keyed,
    Sees,
    Sites,
    assert_census,
    assert_sites,
    census,
    folds_containing,
    folds_to,
    spells,
)
from tests.helpers.astwalk.resolve import (
    Bindings,
    assignment_parts,
    bindings,
    calls_to,
    dotted,
    leaf_name,
    resolved_calls,
)
from tests.helpers.astwalk.scope import (
    Clause,
    declared_in,
    handler_clauses,
    own_nodes,
    scopes,
)

__all__ = [
    "KSTRL_PACKAGE",
    "REPO_ROOT",
    "TESTS_DIR",
    "Bindings",
    "Clause",
    "Keyed",
    "Sees",
    "Sites",
    "assert_census",
    "assert_sites",
    "assignment_parts",
    "bindings",
    "blind_spot",
    "calls_to",
    "census",
    "declared_in",
    "dotted",
    "folded_str",
    "folds_containing",
    "folds_to",
    "handler_clauses",
    "label",
    "leaf_name",
    "module_name",
    "own_nodes",
    "package_sources",
    "parse",
    "parsed",
    "resolved_calls",
    "scopes",
    "spells",
    "test_sources",
]
