"""Every registered event type is either folded by the reducer or listed as ignored (#448).

``component_scope_resolved`` was registered, emitted for every component
of every factory run, and never folded, so a component a run carried
from an earlier run rendered as pending. Nothing failed, because nothing
required the reducer to decide what that event means.

This census closes the set. ``kstrl.events._REGISTRY`` is every event
type the stream can carry. The reducer's handled set is read from its
source: every ``isinstance(event, <alias>.<Class>)`` in the module-level
``apply()`` function of ``kstrl/reducer.py``, where ``<alias>`` is the
name ``kstrl.events`` is imported as. A type in neither set fails, so a
new event is a decision somebody writes down rather than a silent drop.

"Folded" means named in an ``isinstance`` check in the test of an ``if``
or ``elif`` inside ``apply()``. The census proves a type was DECIDED
about there, not that the decision reads its status field.

Why only ``apply()`` and not the whole module: ``_infer_phase`` also
names ``ComponentStarted``, ``ComponentCompleted`` and ``ComponentFailed``
(it only infers ``comp.phase``, a display field, never ``comp.status``).
Walking the whole module let it stand in for apply()'s real dispatch, so
deleting apply()'s ``ComponentCompleted`` branch (or ``ComponentFailed``,
or ``ComponentStarted``) left this census green: the type read as
"folded" through a branch that never touched status (measured with a
scratch mutation runner: all three STILL GREEN with their real branch
gone). A clearing guard that can be satisfied by a branch other than the
one it exists to prove is not narrow enough to clear anything.

It fails red, not blind. If the walk stops seeing a module-level
``apply`` (renamed, or no longer defined once, at module scope), it
raises rather than silently finding zero branches. If the ``events``
import is renamed or the checks change shape, the handled set shrinks,
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
    "log": (
        "operator narration, rendered by the activity feed; _note_error_log keeps the "
        "latest error lines as RunState.error_block text (#433) and moves no status"
    ),
    "phase_skipped": "recorded in the findings stream; the component's status is unchanged",
    "review_divergence": "a blocking divergence fails the component, which is folded",
    "review_result": "phase only: _infer_phase maps it to a phase string; status is not folded",
    "verification_result": (
        "phase only: _infer_phase maps it to a phase string, and _fold_gate_detail "
        "copies its failures and gate_logs into phase_history (#433); status is not folded"
    ),
}


def _events_alias(tree: ast.Module) -> str:
    """The name ``kstrl.events`` is bound to in the reducer module."""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "kstrl":
            for alias in node.names:
                if alias.name == "events":
                    return alias.asname or alias.name
    raise AssertionError("kstrl/reducer.py no longer imports kstrl.events as a module")


def _apply_function(tree: ast.Module) -> ast.FunctionDef:
    """The one module-level ``apply`` function.

    ``len(fns) != 1`` raises rather than returning nothing, so a rename
    (or a split into two functions) fails this census red instead of
    silently walking zero branches and reading as "nothing to fold".
    """
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "apply"]
    if len(fns) != 1:
        raise AssertionError("kstrl/reducer.py no longer defines exactly one module-level apply()")
    return fns[0]


def _isinstance_calls_in_if_tests(tree: ast.AST) -> list[ast.Call]:
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
    """Event types ``apply()`` names in an ``isinstance`` branch test."""
    tree = ast.parse(Path(reducer.__file__).read_text(encoding="utf-8"))
    alias = _events_alias(tree)
    apply_fn = _apply_function(tree)
    handled: set[str] = set()
    for node in _isinstance_calls_in_if_tests(apply_fn):
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
