"""#247: two site inventories over ``kstrl/``.

The record of which phases produced a reading is only as good as its
coverage. Two things can silently un-fix this: a skippable phase with no
recording site, and a third writer of the retry context that does not
merge the record in. Neither shows up as a failing behaviour test,
because both are ABSENCES.

Both censuses FLAG rather than CLEAR, per the guard-direction rule in
CLAUDE.md: over-matching costs a false positive somebody reads, while a
clearing guard that over-matches deletes the mechanism. Neither proves
the merge is correct - the behaviour tests in ``tests/test_pipeline.py``
do that, one per writer, and this file's docstrings name them so the
scope cannot be misread.

Two rows in the first census are keyed by LOCATION rather than by phase,
because their phase is a variable. ``test_the_only_unkeyable_recording_
sites_are_the_two_fan_out_points`` measures that those two strings still
name what they claim to, so a stale row cannot sit in the table watching
nothing. The writer net's one disclosed miss carries a strict-xfail
``blind_spot`` row for the same reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kstrl.context import SKIPPABLE_PHASES
from tests.helpers.astwalk import (
    all_nodes,
    assert_census,
    blind_spot,
    folded_str,
    label,
    leaf_name,
    package_sources,
    parse,
    parsed,
    scopes,
)

#: The two names a phase reading can be written through: the pipeline's
#: private recorder and the context's own public method. Both, because
#: a census that watched only the private one would clear the dangerous
#: mutation: a reading for a phase that measured nothing can be written
#: straight onto the context with ``ctx.add_phase_reading(...)`` and
#: never touch the recorder. Measured in round 1 of review: with only
#: the private name in this set, that planted writer left the file 2
#: passed.
_READING_WRITERS = frozenset({"_note_phase_reading", "add_phase_reading"})

#: A call recording a phase reading, with a phase argument to key on.
_RECORDING_CALL = """
self._note_phase_reading(comp, "review", review.produced_a_reading)
"""

#: The public spelling, which takes the phase FIRST. ONE CONTROL PER
#: DISJUNCT: the net is a membership test over two names, and a single
#: control is a scalar proof over the whole predicate.
_PUBLIC_RECORDING_CALL = """
ctx.add_phase_reading("review", attempt=2)
"""

#: The sites that write a reading whose phase is not a literal, keyed by
#: WHERE they are rather than by a phase the walk cannot read.
#:
#: Both are fan-out points rather than observations: the pipeline's
#: per-attempt merge replays what ``_note_phase_reading`` already
#: recorded, and the deserialiser replays what a previous process
#: serialised. Neither can be keyed by phase, and neither may be
#: dropped from the inventory: an unkeyable site left out is exactly the
#: absence this census exists to make loud. A new one is a NEW ROW here,
#: not a bigger anonymous bucket, so the failure names the function.
_FAN_OUT_SITES: dict[str, int] = {
    "context.py::IterationContext.from_json": 1,
    "pipeline.py::ComponentPipeline._merge_phase_readings": 1,
}

#: A write to the retry context, in the shape both writers use.
_CONTEXT_WRITE = """
self.component_contexts[comp.id] = ctx.to_json()
"""

#: The same write with no subscript on the left. ONE CONTROL PER
#: DISJUNCT again, and this pair is not decoration: round 1 of review
#: deleted the ``update``/``setdefault`` branch of the predicate below
#: and the file stayed 2 passed, because the only control was a
#: subscript assign and the branch matches nothing in ``kstrl/`` today.
#: ``assert_census`` proves each string separately and names the one
#: that went quiet.
_CONTEXT_UPDATE = """
self.component_contexts.update({comp.id: ctx.to_json()})
"""
_CONTEXT_SETDEFAULT = """
self.component_contexts.setdefault(comp.id, ctx.to_json())
"""

#: The disclosed miss, pinned by ``test_a_write_through_a_local_alias_
#: is_a_known_miss`` below rather than left as a sentence.
_ALIAS_WRITE = """
contexts = self.component_contexts
contexts[comp.id] = ctx.to_json()
"""


def _records_a_reading(node: ast.AST) -> bool:
    """Every call that writes a reading, whatever shape it takes.

    Deliberately not conditioned on arity or on the phase being
    readable. A conjunct like ``len(node.args) >= 2`` would make a site
    spelled ``_note_phase_reading(comp, phase="review", ...)`` invisible
    and leave the census green, which is the second-site case the tests
    below exist to catch, failing in the skip direction.
    """
    return isinstance(node, ast.Call) and leaf_name(node.func) in _READING_WRITERS


def _writes_the_retry_context(node: ast.AST) -> bool:
    """A statement that installs a context under a component's key.

    Three shapes, because the guard FLAGS: a subscript assignment, and
    the ``update``/``setdefault`` calls that do the same job without
    one. Widening a flagging guard costs a false positive somebody
    reads; leaving a shape out costs a writer that never merges.

    Disclosed miss: a write through a local alias
    (``m = self.component_contexts; m[cid] = ...``) is invisible, because
    resolving that binding needs a parent map the top-down walk in
    ``tests/helpers/astwalk`` does not keep.
    """
    if isinstance(node, ast.Assign):
        return any(
            isinstance(target, ast.Subscript) and leaf_name(target.value) == "component_contexts"
            for target in node.targets
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in {"update", "setdefault"} and (
            leaf_name(node.func.value) == "component_contexts"
        )
    return False


def _phase_argument_node(node: ast.AST) -> ast.AST:
    """The expression a recording site passes as the phase, or the call
    itself when it passes none (which never folds, so it is unkeyable
    and lands in a location row)."""
    assert isinstance(node, ast.Call)
    named = [kw.value for kw in node.keywords if kw.arg == "phase"]
    first_arg = leaf_name(node.func) == "add_phase_reading"
    written = named or list(node.args[:1] if first_arg else node.args[1:2])
    return written[0] if written else node


def _phase_argument(source_file: Path, node: ast.AST) -> str:
    """Key a recording site by the phase it names, or by where it sits.

    A phase this cannot read is its own row, named for the function the
    call is in, rather than an absence: a site the walk cannot key must
    fail the inventory instead of quietly not counting. That covers the
    positional spelling each of the two entry points uses, the keyword
    spelling, and a call whose arguments are a splat.

    The two entry points put the phase in different places, so the
    position is read from the callee: ``add_phase_reading(phase,
    attempt=...)`` takes it first, ``_note_phase_reading(comp, phase,
    produced)`` second. Keying both off ``args[1:2]`` would file
    ``ctx.add_phase_reading("security", attempt=1)`` under the location
    row instead of under ``security``, which still FAILS the census but
    reports the wrong thing.
    """
    return folded_str(_phase_argument_node(node)) or _enclosing_site(source_file, node)


def _enclosing_site(source_file: Path, node: ast.AST) -> str:
    """``file.py::Class.function`` for the innermost scope holding it.

    Located by line range over :func:`scopes` rather than by a parent
    map, which the top-down walk does not keep. A call at module level
    keys to ``<module>``, which is also a row and also fails.
    """
    line = getattr(node, "lineno", 0)
    innermost = ""
    best = -1
    for scope, qualified in scopes(parsed(source_file)):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if scope.lineno <= line <= (scope.end_lineno or scope.lineno) and scope.lineno > best:
            innermost, best = qualified, scope.lineno
    return f"{label(source_file)}::{innermost or '<module>'}"


def test_the_only_unkeyable_recording_sites_are_the_two_fan_out_points() -> None:
    """``_FAN_OUT_SITES`` says WHERE, and this proves it.

    The census below pins two rows whose phase the walk cannot fold. A
    location pinned in a table is a claim about the tree, so it is
    measured here rather than trusted: each named site really does hold
    exactly the number of unkeyable writers the table gives it, and
    ``_enclosing_site`` really does resolve to that qualified name. A
    typo in either row would otherwise sit in the table forever, failing
    nothing, while the row it was meant to pin went unwatched.
    """
    found: dict[str, int] = {}
    for source_file in package_sources():
        for node in all_nodes(parsed(source_file)):
            if _records_a_reading(node) and not folded_str(_phase_argument_node(node)):
                row = _enclosing_site(source_file, node)
                found[row] = found.get(row, 0) + 1

    assert found == _FAN_OUT_SITES, (
        "the sites that write a reading with a variable phase moved. "
        "_FAN_OUT_SITES names them by function, and the census keys on "
        f"the same strings, so a stale row watches nothing. Found: {found}"
    )


def test_every_skippable_phase_has_exactly_one_recording_site() -> None:
    """One reading-writing call per skippable phase, plus the two
    fan-out points, and nothing else.

    The per-phase expectation is DERIVED from ``SKIPPABLE_PHASES``
    rather than written out, so widening that set without adding a
    recording site fails here: the new phase's entries would then never
    be retired by an observed pass, which is the silent half of the
    defect. A second site for a phase that has one fails too, because
    two sites is how one of them ends up on a path that did not measure
    anything.

    A phase OUTSIDE the set fails as an unexpected row. That is
    deliberate: the rank rule already infers those phases ran, so a
    record for one is a second, overlapping source of truth.

    BOTH ENTRY POINTS are watched, not just the private recorder. The
    mutation this exists to catch is a reading recorded for a phase that
    measured nothing, and the public ``IterationContext.add_phase_
    reading`` writes one without going anywhere near
    ``_note_phase_reading``. Round 1 of review planted exactly that in
    ``kstrl/pipeline.py`` and this file stayed 2 passed; it now lands as
    a second row for that phase.
    """
    assert_census(
        sources=package_sources(),
        sees=_records_a_reading,
        key=_phase_argument,
        expected={phase: 1 for phase in sorted(SKIPPABLE_PHASES)} | _FAN_OUT_SITES,
        control=[_RECORDING_CALL, _PUBLIC_RECORDING_CALL],
        message=(
            "the phase-reading recording sites no longer match "
            "SKIPPABLE_PHASES plus the two fan-out points. A phase in "
            "that set with no site is a finding that an observed pass "
            "can never retire; a phase outside it with a site "
            "duplicates the rank rule; a new location row is a writer "
            "whose phase nobody can read from the source."
        ),
    )


def test_the_retry_context_has_exactly_two_writers_and_both_are_in_the_pipeline() -> None:
    """The retry context is written twice, both times in ``pipeline.py``.

    Before #247 the two writers were ``pipeline.retry_or_fail`` and the
    contract-breaker reset in ``factory.py``, and only one of them could
    merge the phase readings. Moving the contract write into
    ``ComponentPipeline.record_contract_failure`` puts both behind
    ``_merge_phase_readings``.

    WHAT THIS DOES AND DOES NOT SAY. It flags a third writer in any of
    the shapes ``_writes_the_retry_context`` sees, and it flags either
    of the two leaving ``pipeline.py``. It does not prove the two merge:
    a writer could be added inside ``pipeline.py`` that ignores the
    record and this stays green. Nor does it see a write through a local
    alias, which that predicate records as a disclosed miss. The proof
    that each writer merges is behavioural and lives one per writer -
    ``TestPhaseReadingsRetireSkippableFindings::test_a_review_that_ran_
    and_passed_retires_its_own_earlier_finding`` for ``retry_or_fail``
    and ``::test_the_contract_gate_retires_a_review_that_passed`` for
    ``record_contract_failure``. Saying so explicitly is the point: a
    guard whose scope is misread is how a clearing guard gets written by
    accident.
    """
    assert_census(
        sources=package_sources(),
        sees=_writes_the_retry_context,
        key=lambda source_file, _node: label(source_file),
        expected={"pipeline.py": 2},
        control=[_CONTEXT_WRITE, _CONTEXT_UPDATE, _CONTEXT_SETDEFAULT],
        message=(
            "the retry context gained or lost a writer. Every writer "
            "must merge the attempt's phase readings through "
            "ComponentPipeline._merge_phase_readings, or a failure at "
            "that gate re-raises a finding an earlier phase cleared."
        ),
    )


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_write_through_a_local_alias_is_a_known_miss() -> None:
    """The limit ``_writes_the_retry_context`` discloses, with a test
    behind it instead of a sentence.

    Resolving ``contexts = self.component_contexts`` needs a parent map
    the top-down walk does not keep, so the second statement is
    invisible. The row passes only while that holds: the day astwalk
    grows the map, or somebody widens the predicate, this XPASSes and
    ``strict=True`` turns it into a failure, so the disclosure has to be
    edited in the same diff. A disclosure with no test behind it rots
    silently, and the guard's docstring goes on claiming a limit that is
    no longer there.
    """
    blind_spot(_sees_a_context_write, _ALIAS_WRITE)


def _sees_a_context_write(source: str) -> bool:
    """Whether the writer net fires anywhere in one snippet."""
    return any(_writes_the_retry_context(node) for node in all_nodes(parse(source)))
