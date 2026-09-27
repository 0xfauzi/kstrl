"""Every autonomy ladder input has a production writer that can set each of its values (#601).

The defect this guards: the ladder promoted on inputs no production code
could set truthfully. ``record_merged_component(human_edited=False)`` had a
default and no caller ever passed the argument, so the value True was
unreachable and every merge counted as clean. ``HUMAN_REJECTED_AUTO_MERGE``
had no automatic writer, so the demotion it names never fired.

The inputs are read from ``kstrl/autonomy.py`` at test time: every
parameter of every ``AutonomyState.record_*`` method, and every
``DemotionTrigger`` member. ``EXPECTED_LADDER_INPUTS`` pins that set, so a
new input fails here by name until someone decides what writes it.

Three rules, walked over ``kstrl/`` minus ``autonomy.py`` (the ladder
itself) and ``autonomy_replay.py`` (a simulation over recorded runs, not a
writer).

1. A ``bool`` parameter of a ``record_*`` method has no default, so every
   caller states what it observed.
2. A ``bool`` parameter is passed by keyword, at one production call site
   or more, with a value that is not a literal constant.
3. Every trigger but ``MANUAL`` appears as ``DemotionTrigger.<NAME>`` in
   the trigger position of an ``apply_demotion`` call (argument 1 or
   ``trigger=``) or a ``.demote`` call (argument 0 or ``trigger=``).
   ``MANUAL`` is the operator's own act: its writer is ``autonomy_demote``
   in ``kstrl/cli.py``, which must call ``.demote``.

Rules 2 and 3 CLEAR an input when they find a site, so they are narrow: a
name in the trigger position (``demote(chosen, ...)``) clears nothing, and
neither does a call reached through an alias or ``**kwargs``. What they
cannot check is that a non-constant value really varies with what
happened; the end-to-end tests in ``tests/test_ladder_merge_evidence.py``
carry that.
"""

from __future__ import annotations

import ast
import inspect

from kstrl.autonomy import AutonomyState, DemotionTrigger
from tests.helpers.astwalk import label, package_sources, parse, parsed

#: Re-derive by running ``ladder_inputs()``; never edit by hand to make it pass.
EXPECTED_LADDER_INPUTS = frozenset(
    {
        "record_decisive_run.count",
        "record_merged_component.human_edited",
        "record_policy_violation.count",
        "DemotionTrigger.POLICY_VIOLATION",
        "DemotionTrigger.CALIBRATION_REGRESSION",
        "DemotionTrigger.HEALTH_BREACH",
        "DemotionTrigger.HUMAN_REJECTED_AUTO_MERGE",
        "DemotionTrigger.MANUAL",
    }
)

NOT_WRITERS = frozenset({"autonomy.py", "autonomy_replay.py"})

#: Where each call shape carries its trigger: (positional index, keyword).
TRIGGER_POSITION = {"apply_demotion": (1, "trigger"), "demote": (0, "trigger")}


def _record_methods() -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(method)
        for name, method in inspect.getmembers(AutonomyState, inspect.isfunction)
        if name.startswith("record_")
    }


def _bool_params() -> dict[str, list[inspect.Parameter]]:
    """Each record_* method's bool parameters (annotations are strings here)."""
    return {
        name: [p for p in sig.parameters.values() if p.annotation in ("bool", bool)]
        for name, sig in _record_methods().items()
    }


def ladder_inputs() -> frozenset[str]:
    params = {
        f"{name}.{param}"
        for name, sig in _record_methods().items()
        for param in sig.parameters
        if param != "self"
    }
    return frozenset(params | {f"DemotionTrigger.{member.name}" for member in DemotionTrigger})


def _production_trees() -> list[tuple[str, ast.Module]]:
    return [
        (label(path), parsed(path)) for path in package_sources() if path.name not in NOT_WRITERS
    ]


def _callee(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _calls(trees: list[tuple[str, ast.Module]], names: set[str]) -> list[tuple[str, ast.Call, str]]:
    """Every call in ``trees`` whose callee is one of ``names``, with its site and callee."""
    return [
        (f"{where}:{node.lineno}", node, str(_callee(node)))
        for where, tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee(node) in names
    ]


def varied_bool_writers(trees: list[tuple[str, ast.Module]]) -> dict[str, list[str]]:
    """``method.param`` -> the sites passing it by keyword with a non-constant value."""
    wanted = {name: [p.name for p in params] for name, params in _bool_params().items() if params}
    found: dict[str, list[str]] = {}
    for site, node, method in _calls(trees, set(wanted)):
        for keyword in node.keywords:
            varied = not isinstance(keyword.value, ast.Constant)
            if keyword.arg in wanted[method] and varied:
                found.setdefault(f"{method}.{keyword.arg}", []).append(site)
    return found


def _literal_trigger(node: ast.expr) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "DemotionTrigger"
    ):
        return node.attr
    return None


def literal_trigger_writers(trees: list[tuple[str, ast.Module]]) -> dict[str, list[str]]:
    """Trigger name -> the sites that fire it as a literal ``DemotionTrigger.<NAME>``."""
    found: dict[str, list[str]] = {}
    for site, node, callee in _calls(trees, set(TRIGGER_POSITION)):
        index, keyword_name = TRIGGER_POSITION[callee]
        candidates = node.args[index : index + 1] + [
            k.value for k in node.keywords if k.arg == keyword_name
        ]
        for name in filter(None, map(_literal_trigger, candidates)):
            found.setdefault(name, []).append(site)
    return found


def _unwritten(trees: list[tuple[str, ast.Module]]) -> list[str]:
    bools = {f"{name}.{p.name}" for name, params in _bool_params().items() for p in params}
    automatic = {m.name for m in DemotionTrigger if m is not DemotionTrigger.MANUAL}
    return sorted(
        (bools - set(varied_bool_writers(trees)))
        | {f"DemotionTrigger.{name}" for name in automatic - set(literal_trigger_writers(trees))}
    )


class TestEveryLadderInputHasAWriter:
    def test_the_set_of_ladder_inputs_is_pinned(self) -> None:
        assert ladder_inputs() == EXPECTED_LADDER_INPUTS, (
            "The autonomy ladder gained or lost an input. Decide which production code "
            "sets each of its values, then re-derive EXPECTED_LADDER_INPUTS by running "
            "ladder_inputs()."
        )

    def test_no_bool_ladder_input_has_a_default(self) -> None:
        defaulted = [
            f"{name}.{p.name}"
            for name, params in _bool_params().items()
            for p in params
            if p.default is not inspect.Parameter.empty
        ]
        assert defaulted == [], (
            f"{defaulted}: a default lets every caller leave the value unstated, which is how "
            "human_edited=False became the only value any merge ever recorded (#601)"
        )

    def test_every_ladder_input_has_a_production_writer(self) -> None:
        assert _unwritten(_production_trees()) == [], (
            "A ladder input no production code in kstrl/ can set to each of its values. "
            "Pass a bool input by keyword with an observed value; fire a trigger as a "
            "literal DemotionTrigger.<NAME> through apply_demotion or .demote."
        )

    def test_manual_is_written_by_the_operator_command(self) -> None:
        cli = next(p for p in package_sources() if label(p) == "cli.py")
        (function,) = [
            node
            for node in ast.walk(parsed(cli))
            if isinstance(node, ast.FunctionDef) and node.name == "autonomy_demote"
        ]
        assert any(
            isinstance(node, ast.Call) and _callee(node) == "demote" for node in ast.walk(function)
        )


class TestTheWalkerDetects:
    """The walker fed source it must flag and source it must clear."""

    def test_a_trigger_passed_as_a_name_is_not_a_writer(self) -> None:
        tree = parse("state.demote(chosen, 'reason')\n")
        assert "DemotionTrigger.HUMAN_REJECTED_AUTO_MERGE" in _unwritten([("probe", tree)])

    def test_a_constant_bool_is_not_a_writer(self) -> None:
        tree = parse("state.record_merged_component(human_edited=False)\n")
        assert "record_merged_component.human_edited" in _unwritten([("probe", tree)])

    def test_literal_writers_clear_every_input(self) -> None:
        tree = parse(
            "state.record_merged_component(human_edited=seen)\n"
            + "".join(
                f"apply_demotion(root, DemotionTrigger.{m.name}, 'r')\n" for m in DemotionTrigger
            )
        )
        assert _unwritten([("probe", tree)]) == []


def test_the_corpus_is_the_package() -> None:
    labels = {where for where, _tree in _production_trees()}
    assert "factory.py" in labels and "autonomy.py" not in labels, sorted(labels)[:5]
