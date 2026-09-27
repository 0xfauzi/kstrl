"""Every ``run_loop`` call in ``kstrl/`` states where its context comes from.

#599. ``ks feature`` ran three engineer loops and handed none of them a
``context_prefix``, so its engineer read no knowledge fact and neither
operator file (``scripts/kstrl/golden-patterns.md``,
``scripts/kstrl/memory.md``), while every factory engineer read all
three. Nothing failed: ``context_prefix`` is an optional keyword, and a
call that leaves it out looks like every other call. So this file names
every ``run_loop`` call site in ``kstrl/`` and the expression its prefix
comes from, and the next entry point that calls ``run_loop`` without
stating one fails here instead of shipping.

TWO LAYERS, in the split ``tests/test_operator_files_reach_the_prompt.py``
uses.

LAYER 1 is a census of the NAME ``run_loop`` that enumerates no node
types. A new call site, an import, an alias or a re-export moves it
whatever shape it takes.

LAYER 2 resolves each call to ``kstrl.loop.run_loop`` and reads its
``context_prefix`` keyword back as source text, keyed by
``<file>::<scope>``. It FLAGS rather than clears: a prefix passed
positionally or through a ``**`` splat is a row that cannot match the
pinned table, never an absence, and a call the walk could not resolve is
counted in the undecided half and pinned. A call with no prefix at all
reads as ``None`` and must carry a stated reason in
``NO_CONTEXT_REASONS``.

One assembler: every prefix handed to an engineer loop is built by
``factory.engineer_context_prefix``. Every node that spells its name is
pinned by scope, so a caller that stops calling it moves the census; a
second copy of the prompt order would also need ``load_operator_file``,
which ``tests/test_operator_files_reach_the_prompt.py`` pins.

``TestTheWalkStillFires`` runs the extractor over source built for it,
so a walk that stopped looking fails there rather than agreeing with an
empty answer.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from tests.helpers.astwalk import (
    KSTRL_PACKAGE,
    all_nodes,
    assert_census,
    bound_names,
    calls_to,
    label,
    module_name,
    package_sources,
    parse,
    parsed,
    resolved_calls,
    scope_of,
    spells,
)

RUN_LOOP = "kstrl.loop.run_loop"

#: ``run_loop(config, ui, agent, cwd, context_prefix, ...)``: a fifth
#: positional argument IS the prefix.
PREFIX_POSITION = 4

#: What the extractor reports for a prefix it cannot read back by name.
#: Both are rows that match no expected source, so they fail.
POSITIONAL = "<positional: pass context_prefix by keyword>"
SPLAT = "<** splat: context_prefix cannot be read back>"

FEATURE_SOURCE = "_feature_context_prefix(params, base_config, root_dir, ui)"

#: Every place in ``kstrl/`` that spells ``run_loop``: its definition in
#: ``loop.py``, the import plus one call in ``cli.py`` and ``factory.py``,
#: and the import plus three calls in ``feature_cmd.py``.
EXPECTED_RUN_LOOP_SPELLINGS: dict[str, int] = {
    "cli.py": 2,
    "factory.py": 2,
    "feature_cmd.py": 4,
    "loop.py": 1,
}

#: Every resolved ``run_loop`` call, by ``<file>::<scope>``, and the
#: source text of its ``context_prefix`` keyword in line order. ``None``
#: is a call that passes no prefix.
EXPECTED_CONTEXT_SOURCES: dict[str, tuple[str | None, ...]] = {
    "cli.py::_understand_core": (None,),
    "factory.py::_run_component": ("context_prefix",),
    "feature_cmd.py::run_feature": (FEATURE_SOURCE,) * 3,
}

#: Calls the walk could not decide, per file. All four have a callee with
#: no identifier (``TABLE[key](...)``, ``helper(...)(...)``), which
#: ``calls_to`` reports as a candidate for every target; none of them is
#: a ``run_loop`` call.
EXPECTED_UNDECIDED: dict[str, int] = {"gateparse.py": 2, "tui/app.py": 2}

#: The call sites that pass no prefix, and why. Exactly the ``None`` rows.
NO_CONTEXT_REASONS: dict[str, str] = {
    "cli.py::_understand_core": (
        "ks understand writes the codebase map; it is not an engineer loop and "
        "reads no operator context by decision (item 6 of #596)"
    ),
}

#: Every node that spells the assembler's name, by ``<file>::<scope>``:
#: its definition and its call in ``factory.py``, and the import and the
#: call in ``feature_cmd.py``. Counted by NAME rather than resolved,
#: because ``astwalk.bindings`` deliberately does not bind a local
#: ``def``, so the call inside ``factory.py`` resolves to nothing.
EXPECTED_ASSEMBLER_SPELLINGS: dict[str, int] = {
    "factory.py::<module>": 1,
    "factory.py::_run_component": 1,
    "feature_cmd.py::<module>": 1,
    "feature_cmd.py::_feature_context_prefix": 1,
}


def context_source(node: ast.Call) -> str | None:
    """The source text of one call's ``context_prefix``, or None if absent.

    A prefix this cannot read back by name comes out as a row that
    matches nothing, never as None: None is what a call with no prefix
    returns, and the two must not be spelled the same way.
    """
    for keyword in node.keywords:
        if keyword.arg == "context_prefix":
            return ast.unparse(keyword.value)
    if len(node.args) > PREFIX_POSITION:
        return POSITIONAL
    if any(keyword.arg is None for keyword in node.keywords):
        return SPLAT
    return None


def context_sources(
    tree: ast.Module, module: str, where: str
) -> tuple[dict[str, tuple[str | None, ...]], tuple[str, ...]]:
    """``(prefix source per call site, calls the walk could not decide)``."""
    found = calls_to(tree, {RUN_LOOP}, where=where, module=module)
    owner = scope_of(tree)
    nodes = sorted(
        (node for node, _origin in resolved_calls(tree, {RUN_LOOP}, module=module)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    sources: dict[str, list[str | None]] = {}
    for node in nodes:
        site = f"{where}::{owner.get(id(node), '<module>')}"
        sources.setdefault(site, []).append(context_source(node))
    return {site: tuple(rows) for site, rows in sources.items()}, found.undecided


def package_context_sources() -> tuple[dict[str, tuple[str | None, ...]], Counter[str]]:
    """Layer 2 over every module in ``kstrl/``."""
    sources: dict[str, tuple[str | None, ...]] = {}
    undecided: Counter[str] = Counter()
    for source_file in package_sources():
        where = label(source_file)
        here, rows = context_sources(parsed(source_file), module_name(source_file), where)
        sources.update(here)
        undecided.update(row.split(":", 1)[0] for row in rows)
    return sources, undecided


def by_scope(source_file: Path, node: ast.AST) -> str:
    """``<file>::<scope>`` for one node, the census key of the assembler."""
    return f"{label(source_file)}::{scope_of(parsed(source_file)).get(id(node), '<module>')}"


class TestEveryRunLoopCallStatesItsContext:
    """The guard itself."""

    def test_nothing_new_gets_hold_of_run_loop(self) -> None:
        """Layer 1, the net. Closed by construction: it counts every node
        that spells the name, so a new caller moves it in any shape."""
        assert_census(
            sources=package_sources(),
            sees=spells("run_loop"),
            expected=EXPECTED_RUN_LOOP_SPELLINGS,
            control="result = run_loop(config, ui, agent)\n",
            message=(
                "The set of places that name run_loop changed. A new engineer loop "
                "must pass context_prefix built by factory.engineer_context_prefix, "
                "and its call site must be added to EXPECTED_CONTEXT_SOURCES in "
                "tests/test_run_loop_callers_state_context.py."
            ),
        )

    def test_every_run_loop_call_states_its_context_source(self) -> None:
        """Layer 2. #599's defect is a row here reading ``None`` where the
        engineer should have read the operator's files."""
        sources, _undecided = package_context_sources()

        assert sources == EXPECTED_CONTEXT_SOURCES, (
            "a run_loop call's context_prefix changed. An engineer loop takes its "
            "prefix from factory.engineer_context_prefix, built at the call; a call "
            "with no prefix needs a reason in NO_CONTEXT_REASONS. "
            f"Found: {sources}"
        )

    def test_the_walk_reports_what_it_could_not_decide(self) -> None:
        """The undecided half of layer 2, pinned per file, so a call the
        walk cannot resolve is a count that moves rather than an absence."""
        _sources, undecided = package_context_sources()

        assert dict(undecided) == EXPECTED_UNDECIDED

    def test_a_call_with_no_context_has_a_stated_reason(self) -> None:
        sources, _undecided = package_context_sources()
        without = {site for site, rows in sources.items() if None in rows}

        assert set(NO_CONTEXT_REASONS) == without
        assert all(reason.strip() for reason in NO_CONTEXT_REASONS.values())


class TestOneAssemblerBuildsTheContext:
    """The prompt order is written once, in ``factory.engineer_context_prefix``."""

    def test_every_prefix_is_built_by_the_one_assembler(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=spells("engineer_context_prefix"),
            expected=EXPECTED_ASSEMBLER_SPELLINGS,
            control="prefix = engineer_context_prefix(root_dir, retry_block='')\n",
            key=by_scope,
            message=(
                "factory.engineer_context_prefix is the one assembly of the engineer's "
                "context, called once by factory._run_component and once by "
                "feature_cmd._feature_context_prefix. A caller that builds the blocks "
                "another way writes a second prompt order."
            ),
        )

    def test_the_factory_binds_its_prefix_once_from_the_assembler(self) -> None:
        """``_run_component`` passes ``context_prefix=context_prefix``, so the
        local has to be the assembler's answer and nothing else. Counted as
        every stored ``Name`` in the scope, which covers every binding form."""
        source = KSTRL_PACKAGE / "factory.py"
        tree = parsed(source)
        owner = scope_of(tree)
        in_scope = [node for node in all_nodes(tree) if owner.get(id(node)) == "_run_component"]
        stores = [
            node
            for node in in_scope
            if isinstance(node, ast.Name)
            and node.id == "context_prefix"
            and isinstance(node.ctx, ast.Store)
        ]
        values = [
            value
            for node in in_scope
            for names, value in [bound_names(node)]
            if "context_prefix" in names
        ]

        assert len(stores) == 1, f"context_prefix is stored {len(stores)} times in _run_component"
        assert len(values) == 1 and isinstance(values[0], ast.Call)
        assert isinstance(values[0].func, ast.Name)
        assert values[0].func.id == "engineer_context_prefix"
        assert any(
            isinstance(node, ast.FunctionDef) and node.name == "engineer_context_prefix"
            for node in tree.body
        ), "factory.py no longer defines engineer_context_prefix at module level"


#: Control sources, each small enough to read.
FROM_ASSEMBLER = (
    "from kstrl.factory import engineer_context_prefix\n"
    "from kstrl.loop import run_loop\n"
    "def go(config, ui, agent, root):\n"
    "    return run_loop(config, ui, agent, root, "
    "context_prefix=engineer_context_prefix(root, retry_block=''))\n"
)
NO_PREFIX = (
    "from kstrl.loop import run_loop\n"
    "def go(config, ui, agent):\n"
    "    return run_loop(config, ui, agent)\n"
)
POSITIONAL_PREFIX = (
    "from kstrl.loop import run_loop\n"
    "def go(config, ui, agent, root, block):\n"
    "    return run_loop(config, ui, agent, root, block)\n"
)
SPLAT_PREFIX = (
    "from kstrl.loop import run_loop\n"
    "def go(config, ui, agent, options):\n"
    "    return run_loop(config, ui, agent, **options)\n"
)
ALIASED = (
    "from kstrl.loop import run_loop as loop_once\n"
    "def go(config, ui, agent):\n"
    "    return loop_once(config, ui, agent)\n"
)


class TestTheWalkStillFires:
    """Each extractor run over source built for it. An empty answer fails."""

    def test_a_prefix_from_the_assembler_is_read_back(self) -> None:
        sources, undecided = context_sources(parse(FROM_ASSEMBLER), "control", "control.py")

        assert sources == {
            "control.py::go": ("engineer_context_prefix(root, retry_block='')",),
        }
        assert undecided == ()

    def test_a_call_without_a_prefix_is_read_as_none(self) -> None:
        sources, _undecided = context_sources(parse(NO_PREFIX), "control", "control.py")

        assert sources == {"control.py::go": (None,)}

    def test_a_positional_prefix_is_a_row_not_an_absence(self) -> None:
        sources, _undecided = context_sources(parse(POSITIONAL_PREFIX), "control", "control.py")

        assert sources == {"control.py::go": (POSITIONAL,)}

    def test_a_splat_is_a_row_not_an_absence(self) -> None:
        sources, _undecided = context_sources(parse(SPLAT_PREFIX), "control", "control.py")

        assert sources == {"control.py::go": (SPLAT,)}

    def test_an_aliased_import_is_still_a_call_site(self) -> None:
        sources, _undecided = context_sources(parse(ALIASED), "control", "control.py")

        assert sources == {"control.py::go": (None,)}
