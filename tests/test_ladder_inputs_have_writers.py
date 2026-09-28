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
neither does a call reached through an alias or ``**kwargs``. ``apply_demotion``
is a free function, matched by MODULE RESOLUTION
(``kstrl.autonomy.apply_demotion``) rather than by its leaf name, so a local
function also called ``apply_demotion`` cannot clear an input it never
writes (measured: a no-op shadow of that name cleared every trigger before
this). ``.demote`` is a bound method whose receiver this walker cannot
resolve to a class, so it still matches by leaf name alone. What neither
rule can check is that a non-constant value really varies with what
happened, or that the writer it found is called by anything else in
``kstrl/`` - a dead function with a literal trigger or a varied keyword
clears the same as a live one; the end-to-end tests in
``tests/test_ladder_merge_evidence.py`` carry the first, and nothing here
carries the second.
"""

from __future__ import annotations

import ast
import inspect

from kstrl.autonomy import AutonomyState, DemotionTrigger
from tests.helpers.astwalk import (
    all_nodes,
    calls_to,
    label,
    leaf_name,
    module_name,
    package_sources,
    parse,
    parsed,
    resolved_calls,
)

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


def _production_trees() -> list[tuple[str, ast.Module, str]]:
    return [
        (label(path), parsed(path), module_name(path))
        for path in package_sources()
        if path.name not in NOT_WRITERS
    ]


def _calls(
    trees: list[tuple[str, ast.Module, str]], names: set[str]
) -> list[tuple[str, ast.Call, str]]:
    """Every call in ``trees`` whose callee LEAF is one of ``names``, with its site and callee.

    Leaf-matched only: every caller of this is a bound method
    (``state.record_*``) whose receiver varies per site, which this
    walker cannot resolve to a class, the same narrowing ``.demote``
    keeps in :func:`literal_trigger_writers`.
    """
    return [
        (f"{where}:{node.lineno}", node, str(leaf_name(node.func)))
        for where, tree, _module in trees
        for node in all_nodes(tree)
        if isinstance(node, ast.Call) and leaf_name(node.func) in names
    ]


def varied_bool_writers(trees: list[tuple[str, ast.Module, str]]) -> dict[str, list[str]]:
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


#: The one free function in the trigger position: matched by module
#: resolution (see :func:`literal_trigger_writers`), never by leaf name.
_APPLY_DEMOTION = "kstrl.autonomy.apply_demotion"


def _trigger_names(node: ast.Call, callee: str) -> list[str]:
    index, keyword_name = TRIGGER_POSITION[callee]
    candidates = node.args[index : index + 1] + [
        k.value for k in node.keywords if k.arg == keyword_name
    ]
    return list(filter(None, map(_literal_trigger, candidates)))


def literal_trigger_writers(
    trees: list[tuple[str, ast.Module, str]],
) -> tuple[dict[str, list[str]], list[str]]:
    """Trigger name -> the sites that fire it as a literal ``DemotionTrigger.<NAME>``,
    and every ``apply_demotion`` call this walker could not resolve.

    ``apply_demotion`` calls are resolved by MODULE (``_APPLY_DEMOTION``),
    not by leaf name: a same-named local function (a no-op shadow, say)
    used to clear every trigger without writing any of them before this.
    An unresolved call - the second element this returns - is deliberately
    NOT counted as a writer either: narrow, because this rule CLEARS an
    input rather than flagging one, and a trigger it names stays reported
    as unwritten rather than being cleared by a call nobody could confirm
    fires it. ``.demote`` keeps the leaf match: it is a bound method whose
    receiver varies per call site, which this walker cannot resolve to a
    class.
    """
    found: dict[str, list[str]] = {}
    undecided: list[str] = []
    for where, tree, module in trees:
        undecided.extend(calls_to(tree, {_APPLY_DEMOTION}, where=where, module=module).undecided)
        for node, _origin in resolved_calls(tree, {_APPLY_DEMOTION}, module=module):
            _add_trigger_sites(found, f"{where}:{node.lineno}", node, "apply_demotion")
    for site, node, callee in _calls(trees, {"demote"}):
        _add_trigger_sites(found, site, node, callee)
    return found, undecided


def _add_trigger_sites(found: dict[str, list[str]], site: str, node: ast.Call, callee: str) -> None:
    for name in _trigger_names(node, callee):
        found.setdefault(name, []).append(site)


def _unwritten(trees: list[tuple[str, ast.Module, str]]) -> list[str]:
    bools = {f"{name}.{p.name}" for name, params in _bool_params().items() for p in params}
    automatic = {m.name for m in DemotionTrigger if m is not DemotionTrigger.MANUAL}
    triggers, _undecided = literal_trigger_writers(trees)
    return sorted(
        (bools - set(varied_bool_writers(trees)))
        | {f"DemotionTrigger.{name}" for name in automatic - set(triggers)}
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
            for node in all_nodes(parsed(cli))
            if isinstance(node, ast.FunctionDef) and node.name == "autonomy_demote"
        ]
        assert any(
            isinstance(node, ast.Call) and leaf_name(node.func) == "demote"
            for node in all_nodes(function)
        )


#: Every probe below matches a real production call site's shape, imports
#: included: an unimported bare ``apply_demotion(...)`` cannot be resolved
#: to ``_APPLY_DEMOTION`` (measured: it lands in ``calls_to``'s undecided
#: half), which would make ``test_literal_writers_clear_every_input`` pass
#: for the wrong reason - a walker that cannot see the import either way.
_APPLY_DEMOTION_IMPORT = "from kstrl.autonomy import apply_demotion\n"


class TestTheWalkerDetects:
    """The walker fed source it must flag and source it must clear."""

    def test_a_trigger_passed_as_a_name_is_not_a_writer(self) -> None:
        tree = parse("state.demote(chosen, 'reason')\n")
        assert "DemotionTrigger.HUMAN_REJECTED_AUTO_MERGE" in _unwritten([("probe", tree, "")])

    def test_a_constant_bool_is_not_a_writer(self) -> None:
        tree = parse("state.record_merged_component(human_edited=False)\n")
        assert "record_merged_component.human_edited" in _unwritten([("probe", tree, "")])

    def test_a_same_named_local_function_is_not_a_writer(self) -> None:
        """A shadow named ``apply_demotion`` must not clear a trigger it never writes.

        Reproduces the #601 review finding: before module resolution, a
        no-op function sharing the free function's name cleared every
        trigger by leaf name alone.
        """
        tree = parse(
            "def apply_demotion(*a, **k):\n"
            "    pass\n"
            + "".join(
                f"apply_demotion(root, DemotionTrigger.{m.name}, 'r')\n" for m in DemotionTrigger
            )
        )
        automatic = {m.name for m in DemotionTrigger if m is not DemotionTrigger.MANUAL}
        assert {f"DemotionTrigger.{name}" for name in automatic} <= set(
            _unwritten([("probe", tree, "")])
        )

    def test_literal_writers_clear_every_input(self) -> None:
        tree = parse(
            "state.record_merged_component(human_edited=seen)\n"
            + _APPLY_DEMOTION_IMPORT
            + "".join(
                f"apply_demotion(root, DemotionTrigger.{m.name}, 'r')\n" for m in DemotionTrigger
            )
        )
        assert _unwritten([("probe", tree, "tests.probe")]) == []


def test_the_corpus_is_the_package() -> None:
    labels = {where for where, _tree, _module in _production_trees()}
    assert "factory.py" in labels and "autonomy.py" not in labels, sorted(labels)[:5]
