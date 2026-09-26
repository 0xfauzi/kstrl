"""Every ``git worktree remove`` in ``kstrl/`` kills what runs there first (#528).

#461 swept the four component-worktree removals and left three others
unswept, and nothing counted removal sites, so a new one was unswept by
default. This file counts them.

TWO LAYERS.

LAYER 1, the net, is :func:`astwalk.assert_census` over every expression in
``kstrl/`` that folds to exactly ``"remove"`` or to a string containing
``"worktree remove"``. It enumerates no node types, so any spelling of a
removal, an argv list, a tuple passed to a wrapper, or a shell string, moves
a module's count. The prose hits in ``contract.py`` are its error messages,
which tell the operator the manual command.

LAYER 2, :func:`removal_sites`, reads every argv whose ``"remove"`` follows
``"worktree"`` and CLEARS the site only when the same scope calls
``kstrl.worktree_sweep.sweep_worktree`` on the same path in a simple
statement that runs on every path to the removal: earlier in the same
statement list, or in a list enclosing it. A sweep inside a branch the
removal is outside does not clear. Resolution goes through
``astwalk.bindings`` and a guessed origin does not clear, because this
layer clears (see ``astwalk.Origin``). A ``"remove"`` that is not in such an
argv is undecided, which fails rather than passes.

DISCLOSED LIMIT. A worktree deleted by ``shutil.rmtree`` alone, with no git
call, spells neither token and neither layer sees it. It is pinned by
:func:`test_an_rmtree_alone_is_not_seen` as a strict xfail.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers import astwalk

SWEEP = "kstrl.worktree_sweep.sweep_worktree"


def _spells_a_removal(node: ast.AST) -> bool:
    folded = astwalk.folded_str(node)
    return folded is not None and (folded == "remove" or "worktree remove" in folded)


#: Every module spelling a removal, and how many times. contract.py is one
#: argv and six prose pieces of its manual-removal messages.
EXPECTED_REMOVAL_SPELLINGS = {
    "contract.py": 7,
    "factory.py": 4,
    "retry_plan.py": 1,
}

#: Every removal argv in kstrl/, by scope, and whether it is swept.
EXPECTED_REMOVAL_SITES = (
    "contract.py::_remove_temp_worktree swept",
    "factory.py::_cleanup_worktree swept",
    "factory.py::_prune_stale_worktrees swept",
    "factory.py::_prune_stale_worktrees swept",
    "factory.py::_setup_worktree swept",
    "retry_plan.py::prepare_retry swept",
)

SWEPT = """\
import subprocess
from kstrl.worktree_sweep import sweep_worktree
def remove(path):
    sweep_worktree(path)
    subprocess.run(["git", "worktree", "remove", "--force", str(path)])
"""


def test_every_removal_spelling_is_counted() -> None:
    astwalk.assert_census(
        sources=astwalk.package_sources(),
        sees=_spells_a_removal,
        expected=EXPECTED_REMOVAL_SPELLINGS,
        control=[
            'run(["git", "worktree", "remove", path])',
            'run(f"git worktree remove --force {path}", shell=True)',
        ],
        message=(
            "A module's count of 'remove' spellings moved. A new git worktree "
            "removal must call kstrl.worktree_sweep.sweep_worktree on the same "
            "path first, in the same function, and be added to "
            "EXPECTED_REMOVAL_SITES; re-derive this pin by running it."
        ),
    )


def removal_sites(tree: ast.Module, where: str, module: str) -> astwalk.Sites:
    """Every removal argv in ``tree``: swept or UNSWEPT, or undecided."""
    holders = _sequence_holding(tree)
    owner = astwalk.scope_of(tree)
    scope_nodes = {qualified: node for node, qualified in astwalk.scopes(tree)}
    table = astwalk.bindings(tree, module=module)
    seen: list[str] = []
    undecided: list[str] = []
    for node in astwalk.all_nodes(tree):
        if astwalk.folded_str(node) != "remove":
            continue
        scope = owner.get(id(node), "<module>")
        argv = _worktree_remove_argv(node, holders.get(id(node)))
        if argv is None:
            undecided.append(f"{where}::{scope} line {getattr(node, 'lineno', 0)}")
            continue
        swept = _swept_before(scope_nodes[scope], argv, table)
        seen.append(f"{where}::{scope} {'swept' if swept else 'UNSWEPT'}")
    return astwalk.Sites(tuple(sorted(seen)), tuple(sorted(undecided)))


def _sequence_holding(tree: ast.Module) -> dict[int, ast.List | ast.Tuple]:
    """Each list or tuple element's id, mapped to the literal holding it."""
    holders: dict[int, ast.List | ast.Tuple] = {}
    for node in astwalk.all_nodes(tree):
        if not isinstance(node, ast.List | ast.Tuple):
            continue
        for element in node.elts:
            holders[id(element)] = node
    return holders


def _worktree_remove_argv(
    node: ast.AST, holder: ast.List | ast.Tuple | None
) -> ast.List | ast.Tuple | None:
    """``holder`` when ``node`` is its ``"remove"`` after ``"worktree"``
    with a path after it, else None."""
    if holder is None:
        return None
    index = next(i for i, element in enumerate(holder.elts) if element is node)
    if index < 1 or index == len(holder.elts) - 1:
        return None
    return holder if astwalk.folded_str(holder.elts[index - 1]) == "worktree" else None


def _path_key(node: ast.expr) -> str:
    """The path expression with ``str(...)`` and ``Path(...)`` taken off."""
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("str", "Path")
        and len(node.args) == 1
    ):
        node = node.args[0]
    return ast.dump(node)


def _swept_before(scope: ast.AST, argv: ast.List | ast.Tuple, table: astwalk.Bindings) -> bool:
    """Whether a sweep of ``argv``'s path runs on every path to ``argv``.

    The sweep must be a simple statement that comes before the statement
    holding ``argv``, in the same statement list or in a list enclosing it.
    A sweep inside an ``if``, a loop, a ``with`` or a ``try`` does not clear
    a removal outside it, because one branch skips it.
    """
    body = getattr(scope, "body", None)
    if not isinstance(body, list):
        return False
    return _dominated(body, argv, _path_key(argv.elts[-1]), table)


def _dominated(
    body: list[ast.stmt], argv: ast.List | ast.Tuple, path: str, table: astwalk.Bindings
) -> bool:
    swept = False
    for statement in body:
        if not any(node is argv for node in ast.walk(statement)):
            swept = swept or _sweeps(statement, path, table)
            continue
        if swept:
            return True
        return any(_dominated(block, argv, path, table) for block in _blocks(statement))
    return False


def _sweeps(statement: ast.stmt, path: str, table: astwalk.Bindings) -> bool:
    """Whether ``statement`` is a simple statement calling the sweep on ``path``."""
    if not isinstance(statement, ast.Expr | ast.Assign | ast.AnnAssign):
        return False
    for node in astwalk.own_nodes(statement):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        origin = table.origin_of(node.func)
        if origin is None or origin.guessed or origin.dotted != SWEEP:
            continue
        if _path_key(node.args[0]) == path:
            return True
    return False


def _blocks(statement: ast.stmt) -> list[list[ast.stmt]]:
    """The statement lists directly inside a compound statement."""
    if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return []
    blocks = [getattr(statement, name, None) for name in ("body", "orelse", "finalbody")]
    blocks += [handler.body for handler in getattr(statement, "handlers", [])]
    blocks += [case.body for case in getattr(statement, "cases", [])]
    return [block for block in blocks if isinstance(block, list)]


def _package_removal_sites() -> astwalk.Sites:
    found = astwalk.Sites()
    for source in astwalk.package_sources():
        found += removal_sites(
            astwalk.parsed(source), astwalk.label(source), astwalk.module_name(source)
        )
    return found.sorted()


def test_every_worktree_removal_is_swept_first() -> None:
    astwalk.assert_sites(
        _package_removal_sites(),
        seen=EXPECTED_REMOVAL_SITES,
        undecided=(),
        message=(
            "A git worktree removal is UNSWEPT or new. Call "
            "kstrl.worktree_sweep.sweep_worktree(path) earlier in the same "
            "function, on the same path, so a process left in the worktree is "
            "killed and named before the worktree goes (#528)."
        ),
    )


def _sites(source: str) -> astwalk.Sites:
    return removal_sites(astwalk.parse(source), "planted.py", "planted")


#: The shape of ``contract._remove_temp_worktree``: the sweep, then the
#: removal inside a ``try`` that follows it.
SWEPT_THEN_TRY = """\
import subprocess
from kstrl.worktree_sweep import sweep_worktree
def remove(path):
    sweep_worktree(path)
    try:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)])
    except OSError:
        pass
"""


@pytest.mark.parametrize("source", [SWEPT, SWEPT_THEN_TRY], ids=["same-block", "enclosing-block"])
def test_the_sweep_clears_a_removal_that_follows_it(source: str) -> None:
    assert _sites(source) == astwalk.Sites(("planted.py::remove swept",), ())


@pytest.mark.parametrize(
    "source",
    [
        SWEPT.replace("    sweep_worktree(path)\n", ""),
        SWEPT.replace("    sweep_worktree(path)\n", "    sweep_worktree(other)\n"),
        SWEPT.replace("    sweep_worktree(path)\n", "") + "    sweep_worktree(path)\n",
        SWEPT.replace("from kstrl.worktree_sweep import sweep_worktree", "sweep_worktree = print"),
        SWEPT.replace("    sweep_worktree(path)\n", "    if path:\n        sweep_worktree(path)\n"),
    ],
    ids=["no-sweep", "another-path", "sweep-after", "not-the-sweep", "sweep-in-a-branch"],
)
def test_a_removal_without_its_own_sweep_is_unswept(source: str) -> None:
    assert _sites(source) == astwalk.Sites(("planted.py::remove UNSWEPT",), ())


def test_a_remove_outside_a_worktree_argv_is_undecided() -> None:
    source = 'import subprocess\ndef f(p):\n    subprocess.run(["git", "branch", "remove", p])\n'
    assert _sites(source) == astwalk.Sites((), ("planted.py::f line 3",))


def _net_sees(source: str) -> bool:
    return any(_spells_a_removal(node) for node in astwalk.all_nodes(astwalk.parse(source)))


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_an_rmtree_alone_is_not_seen() -> None:
    astwalk.blind_spot(_net_sees, "import shutil\nshutil.rmtree(worktree_path)\n")


def test_the_census_reads_this_repository() -> None:
    """Anti-vacuity for the corpus: the pinned sites are real files here."""
    for row in EXPECTED_REMOVAL_SITES:
        assert (astwalk.KSTRL_PACKAGE / Path(row.split("::")[0])).is_file(), row
