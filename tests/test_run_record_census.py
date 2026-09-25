"""Every record kstrl writes about a run carries the kstrl version (#451).

A run record is a record whose own identity key is ``run_id`` or
``runId``. The guard has two layers.

LAYER 1 is the net. ``run_record_sites`` counts every container in
``kstrl/`` that stores a run id: a dict display with the key, a
subscript store of the key, or a tuple, list or set holding it as an
element (a header row). Counted per module and enclosing scope, so a new
record writer anywhere changes the inventory and fails
``test_the_set_of_run_record_writers_is_pinned`` until someone looks.

LAYER 2 is the rule. ``unstamped_run_records`` names every layer-1 site
that does not also store the stamp key in the same container (for a
subscript store, anywhere in the same scope). Each such site must be in
``NOT_STAMPED_HERE`` with the reason it is not stamped at the container.
Exact equality, so a new unstamped writer fails and a row that becomes
stamped fails until its exemption is deleted.

Layer 2 checks the KEY, not the value. The e2e tests in
``tests/test_run_record_version.py`` check that the value written is
``kstrl_version()``.

WHAT THIS CANNOT SEE, pinned below with ``blind_spot``: a record that
names its run under another key (``last_run_id``, ``created_run_id``) or
builds it without a string key (``dict(run_id=...)``,
``dataclasses.asdict``). Those are not run records by the definition
above.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers.astwalk import (
    assert_census,
    blind_spot,
    folded_str,
    label,
    package_sources,
    parse,
    parsed,
)

RUN_ID_KEYS = frozenset({"run_id", "runId"})
STAMP_KEYS = frozenset({"kstrl_version", "kstrlVersion"})

Scoped = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _holds(node: ast.AST, keys: frozenset[str]) -> bool:
    """Does this container store one of ``keys``?"""
    if isinstance(node, ast.Dict):
        return any(key is not None and folded_str(key) in keys for key in node.keys)
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return any(folded_str(element) in keys for element in node.elts)
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
        return folded_str(node.slice) in keys
    return False


def stores_run_id(node: ast.AST) -> bool:
    return _holds(node, RUN_ID_KEYS)


def _scopes(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every def and class in ``tree`` with its dotted name."""
    found: list[tuple[str, ast.AST]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, Scoped):
                name = f"{prefix}{child.name}"
                found.append((name, child))
                visit(child, f"{name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def _enclosing(tree: ast.Module, node: ast.AST) -> tuple[str, ast.AST]:
    """The innermost def or class around ``node``, or the module."""
    line = getattr(node, "lineno", 0)
    around = [
        (name, scope)
        for name, scope in _scopes(tree)
        if scope.lineno <= line <= (scope.end_lineno or scope.lineno)
    ]
    if not around:
        return "<module>", tree
    return max(around, key=lambda pair: pair[1].lineno)


def _site_key(source_file: Path, node: ast.AST) -> str:
    return f"{label(source_file)}: {_enclosing(parsed(source_file), node)[0]}"


def _stamped(tree: ast.Module, node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return _holds(node, STAMP_KEYS)
    _name, scope = _enclosing(tree, node)
    return any(_holds(inner, STAMP_KEYS) for inner in ast.walk(scope))


def unstamped_in(tree: ast.Module, where: str) -> list[str]:
    """Layer 2 over one module: the scopes holding an unstamped run record."""
    return sorted(
        {
            f"{where}: {_enclosing(tree, node)[0]}"
            for node in ast.walk(tree)
            if stores_run_id(node) and not _stamped(tree, node)
        }
    )


def unstamped_run_records() -> list[str]:
    return sorted(
        row
        for source_file in package_sources()
        for row in unstamped_in(parsed(source_file), label(source_file))
    )


#: Layer 1. Re-derive by running the census; never edit a count by hand.
EXPECTED_RUN_RECORD_SITES: dict[str, int] = {
    "autonomy.py: commit_transition": 1,
    "events.py: <module>": 1,
    "events.py: _envelope_kwargs": 1,
    "events.py: Event.to_dict": 1,
    "evolution.py: <module>": 1,
    "evolution.py: _role_usage_entries": 1,
    "evolution.py: EvolutionJournal.record_run": 1,
    "evolution.py: EvolutionJournal.carry_superseded": 1,
    "factory.py: _run_factory_locked._record_contract_event": 1,
    "factory.py: _record_health_breaches": 1,
    "factory.py: _record_autonomy_outcome": 1,
    "inbox.py: InboxItem.to_dict": 1,
    "integration_phase.py: _not_run": 1,
    "integration_phase.py: _review_evidence": 1,
    "integration_state.py: _history": 1,
    "integration_state.py: add_fix": 1,
    "integration_state.py: add_stop": 1,
    "launch_record.py: write_launch_record": 1,
    "manifest.py: Manifest.save": 1,
    # The tuple of string-typed top-level keys the schema check walks. A
    # vocabulary rather than a record, and it names kstrlVersion too.
    "manifest.py: Manifest.validate_schema": 1,
    "observability.py: ProgressLog.emit": 1,
    "observability.py: ProgressLog._repair_event": 1,
    "pipeline.py: ComponentPipeline.journal_integration_result": 1,
    "pipeline.py: ComponentPipeline.journal_superseded_findings": 1,
    "reducer.py: upconvert_v1": 1,
    "workqueue.py: Queue.await_approval": 1,
    "workqueue.py: Queue.relink_run": 1,
}

_JOURNAL = (
    "an evolution journal row: EvolutionJournal.append_entries writes every "
    "row through _journal_line, which stamps it"
)
_DEMOTION = (
    "demotion evidence nested inside the autonomy transition row that "
    "commit_transition writes through the journal, whose line is stamped"
)
_V1 = (
    "progress.jsonl is the v1 projection whose line format V1CompatSink holds "
    "still; the same run's events.jsonl, written by the same bus, is stamped"
)

_QUEUE_LINK = (
    "a queue item's last_run_id points at the run that parked it (#464); the "
    "item is a work item with its own id, not a record of the run"
)

#: Layer 2. Each row is a run-record site that is not stamped at the
#: container, and why.
NOT_STAMPED_HERE: dict[str, str] = {
    "autonomy.py: commit_transition": _JOURNAL,
    "evolution.py: _role_usage_entries": _JOURNAL,
    "evolution.py: EvolutionJournal.record_run": _JOURNAL,
    "evolution.py: EvolutionJournal.carry_superseded": _JOURNAL,
    "factory.py: _run_factory_locked._record_contract_event": _JOURNAL,
    "pipeline.py: ComponentPipeline.journal_superseded_findings": _JOURNAL,
    "pipeline.py: ComponentPipeline.journal_integration_result": _JOURNAL,
    "factory.py: _record_health_breaches": _DEMOTION,
    "factory.py: _record_autonomy_outcome": _DEMOTION,
    "inbox.py: InboxItem.to_dict": (
        "an operator inbox item that points at the run it came from; it is a "
        "work item with its own id, not a record of the run"
    ),
    "reducer.py: upconvert_v1": (
        "read side: lifts a v1 line that was already written into an event for the fold"
    ),
    "observability.py: ProgressLog.emit": _V1,
    "observability.py: ProgressLog._repair_event": _V1,
    "workqueue.py: Queue.await_approval": _QUEUE_LINK,
    "workqueue.py: Queue.relink_run": _QUEUE_LINK,
}


class TestEveryRunRecordCarriesTheVersion:
    def test_the_set_of_run_record_writers_is_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=stores_run_id,
            expected=EXPECTED_RUN_RECORD_SITES,
            key=_site_key,
            control=(
                'record = {"run_id": rid}\n',
                'row["runId"] = rid\n',
                'COLUMNS = ("run_id", "ts")\n',
            ),
            message=(
                "The set of places kstrl stores a run id changed. A new run record must "
                'carry the kstrl version (#451): add "kstrl_version": kstrl_version() '
                '("kstrlVersion" in a camelCase document) to the same container, then '
                "re-derive this inventory by running it. If it is not a record of the "
                "run, add it to NOT_STAMPED_HERE with the reason."
            ),
        )

    def test_every_run_record_is_stamped_or_says_why_not(self) -> None:
        assert unstamped_run_records() == sorted(NOT_STAMPED_HERE), (
            "A run record without a kstrl_version key, or an exemption for a site "
            "that is now stamped. Stamp the record with kstrl_version() from "
            "kstrl/version.py, or give the site a reason in NOT_STAMPED_HERE."
        )


class TestTheGuardDetects:
    """Layer 2 fed source it must flag and source it must clear."""

    def test_it_flags_a_dict_record_without_the_stamp(self) -> None:
        tree = parse('def write(rid):\n    return {"run_id": rid, "ts": 1}\n')
        assert unstamped_in(tree, "m.py") == ["m.py: write"]

    def test_it_clears_a_dict_record_with_the_stamp(self) -> None:
        tree = parse('def write(rid, v):\n    return {"run_id": rid, "kstrl_version": v}\n')
        assert unstamped_in(tree, "m.py") == []

    def test_it_flags_a_subscript_record_without_the_stamp(self) -> None:
        tree = parse('def emit(rid):\n    e = {}\n    e["run_id"] = rid\n    return e\n')
        assert unstamped_in(tree, "m.py") == ["m.py: emit"]

    def test_it_clears_a_subscript_record_stamped_in_the_same_scope(self) -> None:
        tree = parse(
            'def emit(rid, v):\n    e = {"kstrl_version": v}\n    e["run_id"] = rid\n    return e\n'
        )
        assert unstamped_in(tree, "m.py") == []

    def test_it_flags_a_header_row_without_the_stamp_column(self) -> None:
        tree = parse('HEADER = ("run_id", "timestamp")\n')
        assert unstamped_in(tree, "m.py") == ["m.py: <module>"]

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_record_built_with_keyword_arguments_is_not_seen(self) -> None:
        blind_spot(
            lambda source: any(stores_run_id(node) for node in ast.walk(parse(source))),
            "record = dict(run_id=rid, ts=now)\n",
        )
