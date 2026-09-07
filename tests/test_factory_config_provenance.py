"""Static half of #195: every place ``kstrl/`` touches the merge-gate flag.

The behaviour is in ``tests/test_explicit_merge_gate.py``. What that file
cannot do is notice a FIFTH source of ``pause_before_pr_merge``. Four
exist today (kstrl.toml, the env var, the ``ks factory`` flag, and the
autonomy ladder), and a fifth that forgot to record provenance would set
the value, resolve as not-explicit, and be dropped at L3 with the run
reporting a manual override ignored. That is the defect this file is
about, and it is invisible to any test that does not go looking for new
code.

TWO LAYERS, in the two directions #324 separates.

LAYER 1 is the NET. It counts every node in ``kstrl/`` whose folded value
IS the string ``pause_before_pr_merge``, per module, and pins the
inventory. It enumerates no node types and no field names, so a new
source has to appear in it whatever shape it takes: an assignment, a
keyword argument, a dict key, a ``getattr``, a parameter name. A module
appearing, disappearing, or changing count is an unexplained delta the
author has to account for.

LAYER 2 is the MESSAGE. It enumerates assignment shapes and names the
enclosing function, so the failure can say what to do rather than only
that a number moved. It is strictly weaker than layer 1 and exists for
the sentence it prints.

BOTH FLAG, neither CLEARS. A census that over-matches costs a false
positive somebody reads; a clearing guard that over-matches deletes the
mechanism (CLAUDE.md, guard-design rule 3). Nothing here decides that a
site is compliant, only that the set of sites is the pinned one.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

import pytest

from tests.helpers.astwalk import (
    all_nodes,
    assert_census,
    blind_spot,
    label,
    own_nodes,
    package_sources,
    parse,
    parsed,
    scopes,
    spells,
)

#: The flag every row here is about.
FLAG = "pause_before_pr_merge"

#: Layer 1. How many times each module in ``kstrl/`` spells the flag.
#:
#: The numbers are not individually meaningful and are not meant to be
#: read as a specification; the DELTA is the signal. What each module is
#: doing with it, so a reviewer can judge a change rather than only see
#: one:
#:
#: - ``factory.py`` declares the field, loads it from toml and env, and
#:   resolves it against the ladder.
#: - ``autonomy.py`` carries it on ``FlagBundle``, decides it in
#:   ``pause_gate_for`` and rebuilds the run's bundle around it in
#:   ``resolved_flag_bundle``.
#: - ``cli.py`` is the ``--pause-before-pr-merge`` flag and its wiring.
#: - ``serve.py`` is the merge disposition of a queued item, the single
#:   exit ``_merge_gate`` that every ``MergeGate`` is built at, and the
#:   child's command line.
#: - ``config_report.py`` and ``pipeline.py`` READ the resolved value;
#:   neither is a source.
EXPECTED_FLAG_SPELLINGS = {
    "autonomy.py": 11,
    "cli.py": 7,
    "config_report.py": 1,
    "factory.py": 16,
    "pipeline.py": 1,
    "serve.py": 22,
}

#: Layer 2. Every ``<something>.pause_before_pr_merge = ...`` in
#: ``kstrl/``, keyed by module and enclosing scope. Each of these four
#: writes a resolved value onto a config object, and each had to decide
#: whether it is an operator request:
#:
#: - ``factory.py::FactoryConfig.load`` twice, toml then env: both record
#:   provenance, because the key's PRESENCE is the operator's act.
#: - ``cli.py::factory``: the CLI flag, likewise.
#: - ``factory.py::_run_factory_locked``: the ladder's own resolution,
#:   which reads provenance and must never add it.
EXPECTED_FLAG_WRITES = {
    "cli.py::factory": 1,
    "factory.py::FactoryConfig.load": 2,
    "factory.py::_run_factory_locked": 1,
}


def _writes_the_flag(node: ast.AST) -> bool:
    """An assignment whose target is ``<expr>.pause_before_pr_merge``."""
    if isinstance(node, ast.Assign):
        targets: Iterable[ast.expr] = node.targets
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        targets = [node.target]
    else:
        return False
    return any(isinstance(t, ast.Attribute) and t.attr == FLAG for t in targets)


def _scope_of(source_file: Path, node: ast.AST) -> str:
    """``module.py::Qualified.scope`` for the INNERMOST scope holding it.

    ``own_nodes`` stops at a nested function, so a helper defined inside
    another one is credited to itself rather than to its parent.
    """
    tree = parsed(source_file)
    innermost = "<module>"
    for scope_node, qualified in scopes(tree):
        if any(child is node for child in own_nodes(scope_node)):
            innermost = qualified
    return f"{label(source_file)}::{innermost}"


class TestTheNet:
    def test_flag_spellings_are_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=spells(FLAG),
            expected=EXPECTED_FLAG_SPELLINGS,
            control="config.pause_before_pr_merge = True\n",
            message=(
                "the set of places kstrl/ names pause_before_pr_merge changed. "
                "If this is a new SOURCE for the flag (a config surface an "
                "operator can write), it must also record provenance in "
                "FactoryConfig.explicit_fields, or the autonomy ladder will "
                "drop the gate at L3 (#195), and tests/"
                "test_explicit_merge_gate.py needs a row for it. If it is a "
                "new reader, update the pin."
            ),
        )

    def test_the_net_does_not_fold_prose(self) -> None:
        """Equality, not substring: a docstring mentioning the flag is not
        a spelling, so the pin stays about code."""
        tree = parse('"""Prose about pause_before_pr_merge in a sentence."""\n')
        assert not any(spells(FLAG)(node) for node in all_nodes(tree))

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_runtime_attribute_name_is_not_seen(self) -> None:
        """Disclosed limit: a name only the interpreter can produce.

        ``folded_str`` decides literals, adjacent literals, concatenated
        literals and f-strings, so ``setattr(config, "pause_" +
        "before_pr_merge", True)`` IS seen; that was this row's first
        draft and it XPASSed, which is the mechanism working. What stays
        invisible is a name assembled at run time, from a loop variable
        or a mapping, because deciding it needs the interpreter. There
        are none in ``kstrl/``. If this row ever XPASSes the walk got
        stronger and the disclosure has to be edited in the same diff.
        """
        blind_spot(
            lambda source: any(spells(FLAG)(node) for node in all_nodes(parse(source))),
            "for key in keys:\n    setattr(config, key, True)\n",
        )


class TestTheMessage:
    def test_flag_writes_are_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=_writes_the_flag,
            expected=EXPECTED_FLAG_WRITES,
            control="config.pause_before_pr_merge = bundle.pause_before_pr_merge\n",
            key=_scope_of,
            message=(
                "a new write to pause_before_pr_merge. Decide which kind it "
                "is. An OPERATOR-set value must also add the key to "
                "FactoryConfig.explicit_fields in the same place, so the "
                "ladder cannot lower it at L3+. A DERIVED value (the ladder's "
                "own resolution) must not, and must go through "
                "autonomy.pause_gate_for rather than assigning a bundle "
                "value straight on."
            ),
        )

    def test_an_augmented_assignment_counts(self) -> None:
        """``config.pause_before_pr_merge |= x`` is a write too.

        A guard that enumerates ``ast.Assign`` alone reads clean on it,
        which is the skip direction this repo has been holed in eleven
        times.
        """
        tree = parse("config.pause_before_pr_merge |= other\n")
        assert any(_writes_the_flag(node) for node in all_nodes(tree))

    def test_a_read_is_not_a_write(self) -> None:
        tree = parse("if config.pause_before_pr_merge:\n    pass\n")
        assert not any(_writes_the_flag(node) for node in all_nodes(tree))
