"""One home for the AST walking the static guards in ``tests/`` do.

#324 is the record of what having eleven of them cost. Ten instances are
logged there, every one in the SKIP direction: a matcher could not resolve
a name or fold a value, so it silently did not look, and reported clean.
Two were holed a second time after being fixed once, and one was written
by an author explicitly briefed on the pattern, which is the evidence that
briefing is not a control.

So a shared resolver is not the whole job: on its own it would move eleven
holes into one. What this has to do as well is make the skip direction
LOUD. Round 2 of #324 audited that claim one item at a time, and this
paragraph is the audit rather than the intention, because two of the five
places the first draft named turned out to hold nothing.

THREE ARE SIGNATURES, which a caller cannot route around.
:func:`assert_census` requires a control AND a non-empty corpus,
:func:`assert_sites` requires an expectation for the undecided half, and
:func:`calls_to` has no third bucket, so an unresolved callee is a row
rather than an absence. That last one is total over CANDIDACY and not
over the corpus, which is a weaker claim than the first draft made and
the one that survives measurement; :class:`Sites` says why.

TWO ARE STATIC GUARDS in ``tests/test_astwalk.py``, because no signature
reaches them. :func:`resolved_calls` returns the seen half as NODES, for
a guard that has to read a call's arguments, and a node-returning
signature cannot also force the undecided half;
``TestResolvedCallsIsNotUsableOnItsOwn`` fails any module in ``tests/``
that calls it and never names the other half. :func:`blind_spot` needs
``@pytest.mark.xfail(strict=True, raises=AssertionError)`` on its caller,
which the helper cannot apply for it; ``TestEveryDisclosedLimitCanFail``
fails any call site missing either keyword, which is #328's measurement
turned into a check.

ONE IS A CONVENTION AND SAYS SO: :class:`Clause` carries ``decided``, and
one of its three consumers reads it. The other two are safe for a
different reason, that an unnameable handler yields empty ``names`` and an
empty set intersects nothing.

AND ONE IS AN ASYMMETRY, because a single rule was wrong for a third of
the guards. :class:`Origin` carries ``guessed``, true for the one step
that answers for a receiver it never saw. Resolving in order to FLAG and
resolving in order to CLEAR are opposite directions, and thirteen of the
sixteen migrated guards flag while three clear. Round 3 of review
measured all three going quiet on four lines of ordinary-looking code, so
a guessed origin IN the target set stays a hit and a guessed origin
outside it is undecided. :meth:`Bindings.resolve` still returns the bare
string, which is safe for a membership test and unsafe for a decision,
and its docstring names the two call sites that must ask
:meth:`Bindings.origin_of` instead.

AND ONE WAS REMOVED. The first draft named ``Bindings.opaque``. Nothing
read it, forcing it empty changed no test, and it contradicted
``origins``. See :class:`Bindings` for the measurement.

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
    all_nodes,
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
    Origin,
    assignment_parts,
    bindings,
    bound_names,
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
    "Origin",
    "Clause",
    "Sees",
    "Sites",
    "assert_census",
    "all_nodes",
    "assert_sites",
    "assignment_parts",
    "bindings",
    "bound_names",
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
