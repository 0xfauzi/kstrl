"""Every registered event type is either folded by the reducer or listed as ignored (#448).

``component_scope_resolved`` was registered, emitted for every component
of every factory run, and never folded, so a component a run carried
from an earlier run rendered as pending. Nothing failed, because nothing
required the reducer to decide what that event means.

This census closes the set. ``kstrl.events._REGISTRY`` is every event
type the stream can carry. The reducer's handled set is read from its
source: every ``isinstance(event, <alias>.<Class>)`` in ``kstrl/reducer.py``,
where ``<alias>`` is the name ``kstrl.events`` is imported as. A type in
neither set fails, so a new event is a decision somebody writes down
rather than a silent drop.

"Folded" means named in an ``isinstance`` check in the test of an ``if``
or ``elif`` anywhere in the module, including ``_infer_phase``, which only
infers a phase. The census proves a type was DECIDED about, not that
the decision reads its status field.

It fails red, not blind. If the walk stops seeing handled types (the
import is renamed, or the checks change shape), the handled set shrinks,
the registered types it no longer sees are in neither set, and the
equality fails.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kstrl import events as ev
from kstrl import reducer

#: Registered event types the reducer deliberately does not fold, each
#: with the reason. None of them changes a component's status.
DELIBERATELY_IGNORED: dict[str, str] = {
    "adversarial_agent_selected": "run-level audit of which reviewer ran; no component state",
    "autonomy_level_applied": "run-level policy record; no component state",
    "autonomy_transition": "run-level policy record; no component state",
    "diff_fetch_failed": "the diff phase then retries or fails the component, which is folded",
    "distill_result": "knowledge written after the component finished; no status meaning",
    "fact_utilization_measured": "knowledge measurement; no status meaning",
    "iteration_completed": "iteration_started already carries the counter the board shows",
    "journal_repair": "a marker that the journal was repaired; describes the file",
    "log": "operator narration, rendered by the activity feed, not the reducer",
    "phase_skipped": "recorded in the findings stream; the component's status is unchanged",
    "review_divergence": "a blocking divergence fails the component, which is folded",
}


def _events_alias(tree: ast.Module) -> str:
    """The name ``kstrl.events`` is bound to in the reducer module."""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "kstrl":
            for alias in node.names:
                if alias.name == "events":
                    return alias.asname or alias.name
    raise AssertionError("kstrl/reducer.py no longer imports kstrl.events as a module")


def _isinstance_calls_in_if_tests(tree: ast.Module) -> list[ast.Call]:
    """Every ``isinstance(...)`` call inside the test of an ``if`` or ``elif``.

    Only branch tests count. ``apply`` also has the line
    ``comp.carried = isinstance(event, ev.ComponentScopeResolved)``, an
    assignment every event passes through. Counting it would mark
    ``component_scope_resolved`` folded even with its dispatch branch
    deleted, so this census CLEARS a type only on a branch that decides
    what to do with it.
    """
    calls: list[ast.Call] = []
    for branch in ast.walk(tree):
        if isinstance(branch, ast.If):
            calls.extend(
                node
                for node in ast.walk(branch.test)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
            )
    return calls


def _handled_types() -> set[str]:
    """Event types the reducer names in an ``isinstance`` branch test."""
    tree = ast.parse(Path(reducer.__file__).read_text(encoding="utf-8"))
    alias = _events_alias(tree)
    handled: set[str] = set()
    for node in _isinstance_calls_in_if_tests(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) == 2
        ):
            continue
        target = node.args[1]
        names = target.elts if isinstance(target, ast.Tuple) else [target]
        for name in names:
            if (
                isinstance(name, ast.Attribute)
                and isinstance(name.value, ast.Name)
                and name.value.id == alias
            ):
                cls = getattr(ev, name.attr)
                handled.add(cls.type)
    return handled


class TestReducerEventCensus:
    def test_every_registered_type_is_folded_or_listed(self) -> None:
        registered = set(ev._REGISTRY)
        handled = _handled_types()
        ignored = set(DELIBERATELY_IGNORED)
        unaccounted = registered - handled - ignored
        assert not unaccounted, (
            f"event types the reducer neither folds nor lists as ignored: "
            f"{sorted(unaccounted)}. Fold them in kstrl/reducer.py apply(), or add "
            f"them to DELIBERATELY_IGNORED with the reason they carry no component state."
        )

    def test_the_ignore_list_names_only_unhandled_registered_types(self) -> None:
        registered = set(ev._REGISTRY)
        handled = _handled_types()
        ignored = set(DELIBERATELY_IGNORED)
        assert ignored <= registered, sorted(ignored - registered)
        assert not (ignored & handled), sorted(ignored & handled)

    def test_the_scope_event_is_folded(self) -> None:
        """#448 itself, named rather than left to the set arithmetic."""
        assert ev.ComponentScopeResolved.type in _handled_types()
