"""#435's static guard: one precedence rule turns a base branch into a ref.

LAYER 1 pins every git argv in ``kstrl/`` RENDERED WITH THE EXPRESSION
behind each operand. It enumerates no subcommand and no flag; the net is
``tests.test_git_path_spelling.is_git_argv``, the same one that file's own
layer 1 uses, so there is one definition of "a git argv" in the suite
rather than two. What this layer adds over ``EXPECTED_GIT_ARGVS`` is the
operand: that file renders every non-literal element as ``?``, which
cannot tell ``base`` from ``base_ref``. This one can, which is the whole
subject of #435.

LAYER 2 pins every string expression in ``kstrl/`` whose literal skeleton
STARTS WITH ``origin/`` or ``refs/remotes/origin/``. Precedence between
the remote-tracking ref and the local one can only be written by spelling
``origin/<something>``, so a second precedence rule cannot be added
without adding a row here. ``kstrl/git.py::resolve_ref`` was exactly such
a row before this change.

The skeleton is folded with ``tests.helpers.astfold.fold``, not a
hand-rolled template renderer: an earlier version of this layer stopped
at an ``ast.Name``, so a rule spelled with the module constant
(``f"{_ORIGIN_REF_PREFIX}{ref}"`` or ``_ORIGIN_REF_PREFIX + ref``)
contributed no row - only the literal ``f"origin/{ref}"`` did, and
``kstrl/git.py`` writes its own precedence rule with the constant
(``_BASE_BRANCH_REFS``). ``fold`` resolves an unambiguous module-level
string constant by name (its ``own`` argument is ``{}`` here, so
resolution falls through to the package-wide pool), which is what makes
the module-constant and concatenation spellings visible.

WHAT NEITHER LAYER SEES: an argv assembled at run time, and a ref name
built from a piece ``fold`` cannot decide - a call, a subscript, a
``%``-format or a ``.join`` - which is disclosed as an
``astwalk.blind_spot`` below rather than left as an unstated limit. Both
layers flag on everything else, so an over-match costs a reader one
line, which is the direction a guard of this kind is allowed to be wrong
in (CLAUDE.md).

WHAT NEITHER LAYER CAN DO IS CATCH THE AUTHOR OF THIS CHANGE. A census is
re-derived from the tree it is pinned against, so it agrees with whatever
the author did. It catches change N+1, not change N. The behaviour tests
in ``tests/test_stale_base_ref.py`` are what catch change N.

A constant part of an f-string is its own row as well as part of the
f-string's row, because ``astwalk.all_nodes`` visits it. That is why
``git.py 'origin/'`` reads its own count separately from the f-strings
that also start with it. Stated, not silenced.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers import astfold
from tests.helpers.astwalk import (
    all_nodes,
    assert_census,
    blind_spot,
    folded_str,
    label,
    package_sources,
    parse,
)
from tests.test_git_path_spelling import _literal_of, is_git_argv


def render(node: ast.AST) -> str:
    """The argv as ``git <token> ...``, every element that does not fold to a
    literal rendered as its SOURCE EXPRESSION."""
    found = _literal_of(node)
    assert found is not None
    literal, prefix = found
    tokens: list[str] = []
    for element in literal.elts:
        folded = folded_str(element)
        tokens.append(folded if folded is not None else ast.unparse(element))
    return " ".join([*prefix, *tokens])


def _argv_row(source_file: Path, node: ast.AST) -> str:
    return f"{label(source_file)} {render(node)}"


_ORIGIN_PREFIXES = ("origin/", "refs/remotes/origin/")


def spells_an_origin_ref(node: ast.AST) -> bool:
    """Does this expression fold to a string starting with an origin
    prefix? Folded with ``tests.helpers.astfold.fold`` rather than a
    hand-rolled template renderer that stops at an ``ast.Name`` - see
    the module docstring for what that missed."""
    folded = astfold.fold(node, {})
    return folded is not None and folded.text.startswith(_ORIGIN_PREFIXES)


def _origin_row(source_file: Path, node: ast.AST) -> str:
    return f"{label(source_file)} {ast.unparse(node)}"


#: DERIVED BY RUNNING: set to ``{}``, run the file, copy the ``Found:``
#: dict out of the failure. A new git call anywhere in ``kstrl/`` is an
#: unexplained row; a site that stops resolving its base moves an
#: existing row. After #435's fix: ``contract.py``'s row reads
#: ``base_ref`` in place of ``base`` (the resolved name replaces the bare
#: one in the argv), and one ``git.py`` row drops from 2 to 1 (the
#: deleted ``resolve_ref`` took one of its two identical spawns with it).
#: After the #435 fix-round's A0 (hoisting ``verify._merge_base_ref`` into
#: ``kstrl/git.py`` as the one owner of the merge-base anchor): the
#: ``git merge-base base_label HEAD`` row moves from ``verify.py`` to
#: ``git.py``, count unchanged.
EXPECTED_REF_OPERAND_ARGVS: dict[str, int] = {
    "breaker.py git status --porcelain -uall -z": 1,
    "breaker.py git rev-parse HEAD": 1,
    "breaker.py git diff HEAD": 1,
    "breaker.py git *args": 1,
    "config_report.py git [('branch', 'kstrl_branch'), ('auto_checkout', 'auto_checkout')]": 1,
    "contract.py git worktree add --detach str(worktree_path) base_ref": 1,
    "contract.py git merge --abort": 1,
    "contract.py git worktree remove --force str(worktree_path)": 1,
    "contract.py git worktree prune": 1,
    "doctor.py git ls-files -z": 1,
    "doctor.py git check-ignore -q -- probe": 1,
    "factory.py git worktree remove --force str(worktree_path)": 2,
    "factory.py git worktree add str(worktree_path) -b branch_name base_ref": 1,
    "factory.py git worktree prune": 1,
    "factory.py git rev-parse --verify --quiet f'refs/heads/{branch}'": 1,
    "factory.py git merge-base --is-ancestor branch base_ref": 1,
    "factory.py git branch -D branch_name": 1,
    "factory.py git worktree add str(worktree_path) branch_name": 1,
    "factory.py git worktree remove --force str(entry)": 1,
    "factory.py git branch -D branch": 1,
    "factory.py git worktree remove --force str(wt)": 1,
    "git.py git for-each-ref --format=%(refname)%09%(symref) "
    "_ORIGIN_HEAD_REF *_BASE_BRANCH_REFS": 1,
    "git.py git rev-parse --verify --quiet f'refs/remotes/origin/{base_branch}'": 1,
    "git.py git fetch -- origin base_branch": 1,
    "git.py git rev-parse --is-inside-work-tree": 1,
    "git.py git rev-parse --show-toplevel": 1,
    "git.py git show-ref --verify --quiet f'refs/heads/{branch}'": 1,
    "git.py git rev-parse --abbrev-ref HEAD": 1,
    "git.py git diff --name-only -z": 1,
    "git.py git diff --name-only --cached -z": 1,
    "git.py git ls-files --others --exclude-standard -z": 1,
    "git.py git rev-parse --verify --quiet HEAD": 1,
    "git.py git config --get remote.origin.url": 1,
    "git.py git diff --name-status -z ref --": 1,
    "git.py git restore f'--source={ref}' --staged --worktree -- file": 1,
    "git.py git add -- file": 1,
    "git.py git check-ignore -v -- file": 1,
    "git.py git rm --cached --ignore-unmatch -q -- file": 1,
    "git.py git restore --staged --worktree -- file": 1,
    "git.py git ls-files --error-unmatch -- file": 1,
    "git.py git diff --name-status -z -M -C f'{base_ref}...HEAD' --": 1,
    "git.py git diff f'{base_ref}...HEAD' --": 1,
    "git.py git diff --numstat -z f'{base_ref}...HEAD' --": 1,
    "git.py git merge --no-edit -- branch": 1,
    "git.py git merge-base base_label HEAD": 1,
    "git.py git checkout -b branch_name base --": 1,
    "git.py git branch flag -- branch_name": 1,
    "git.py git checkout branch --": 2,
    "git.py git checkout -b branch": 1,
    "git.py git rev-parse --verify --quiet f'{candidate}^{{commit}}'": 1,
    "pr.py git push -u -- origin branch": 1,
    "pr.py git push --delete -- origin branch": 2,
    "retry_plan.py git worktree remove --force evidence_worktree": 1,
    "retry_plan.py git worktree prune": 1,
    "retry_plan.py git rev-parse --verify --quiet f'refs/heads/{failed_branch}'": 1,
    "retry_plan.py git branch -D failed_branch": 1,
    "statedir.py git -C str(root_dir) remote get-url origin": 1,
    "tui/screens/home.py git rev-parse --abbrev-ref HEAD": 1,
    "verify.py git show f'{base_commit}:{path}'": 1,
    "verify.py git add -A -- . f':(exclude){STATE_DIR_NAME}'": 1,
}

#: DERIVED BY RUNNING, same procedure as above. After #435's fix:
#: ``f'origin/{ref}'`` (``resolve_ref``'s body) is gone, and ``'origin/'``
#: drops from 5 to 4 (that body's f-string also held the constant part
#: ``astwalk.all_nodes`` counts separately). After the #435 fix-round's
#: layer 2 rewrite (folding with ``tests.helpers.astfold`` instead of a
#: template renderer that stopped at an ``ast.Name``): four new rows,
#: all of them ``_ORIGIN_REF_PREFIX`` - the module constant a template
#: renderer cannot resolve. ``"_ORIGIN_REF_PREFIX"`` (5) is every bare
#: reference to it (its own assignment target counts, because ``fold``
#: does not distinguish a Store from a Load); the other three are the
#: two f-strings that interpolate it (``_ORIGIN_HEAD_REF`` and the
#: candidate-refs comprehension) and the ``FormattedValue`` node each of
#: those carries as a piece of its own JoinedStr, visited separately by
#: ``astwalk.all_nodes`` the same way a JoinedStr's constant piece
#: already is. Measured: a strict superset of the previous four rows,
#: none lost.
EXPECTED_ORIGIN_SPELLINGS: dict[str, int] = {
    "git.py 'refs/remotes/origin/'": 2,
    "git.py 'origin/'": 4,
    "git.py f'origin/{base_branch}'": 2,
    "git.py f'refs/remotes/origin/{base_branch}'": 1,
    "git.py _ORIGIN_REF_PREFIX": 5,
    "git.py f'{_ORIGIN_REF_PREFIX}HEAD'": 1,
    "git.py f'{_ORIGIN_REF_PREFIX}{name}'": 1,
    "git.py {_ORIGIN_REF_PREFIX}": 2,
}


def test_every_git_argv_names_the_expression_behind_each_operand() -> None:
    assert_census(
        sources=package_sources(),
        sees=is_git_argv,
        key=_argv_row,
        expected=EXPECTED_REF_OPERAND_ARGVS,
        control='subprocess.run(["git", "worktree", "add", "--detach", str(p), base])',
        message="The git argv inventory moved. Re-derive it by running this test.",
    )


def test_origin_is_spelled_only_where_precedence_is_decided() -> None:
    # ONE CONTROL PER SPELLING (per assert_census's own docstring): a
    # literal f-string, the module-constant f-string, and the
    # module-constant concatenation. A single control proves only the
    # disjunct it exercises; #435's own defect was the literal-only
    # control passing while the module-constant spelling went unseen.
    assert_census(
        sources=package_sources(),
        sees=spells_an_origin_ref,
        key=_origin_row,
        expected=EXPECTED_ORIGIN_SPELLINGS,
        control=(
            'x = f"origin/{ref}"',
            'x = f"{_ORIGIN_REF_PREFIX}{ref}"',
            "x = _ORIGIN_REF_PREFIX + ref",
        ),
        message="An origin-ref spelling moved. Re-derive it by running this test.",
    )


def test_a_bare_base_and_a_resolved_one_are_different_rows() -> None:
    """This is what makes layer 1 a control on #435 and not a restatement
    of ``EXPECTED_GIT_ARGVS``, where both of those render identically as
    ``git worktree add --detach ? ?``."""
    bare = parse('subprocess.run(["git", "worktree", "add", "--detach", str(p), base])')
    fixed = parse('subprocess.run(["git", "worktree", "add", "--detach", str(p), base_ref])')
    assert [render(n) for n in all_nodes(bare) if is_git_argv(n)] == [
        "git worktree add --detach str(p) base"
    ]
    assert [render(n) for n in all_nodes(fixed) if is_git_argv(n)] == [
        "git worktree add --detach str(p) base_ref"
    ]


def test_a_second_precedence_rule_is_a_new_row() -> None:
    """The planted source is ``resolve_ref``'s own body, the rule this
    change deleted."""
    planted = parse('for candidate in (ref, f"origin/{ref}"):\n    pass\n')
    rows = sorted(ast.unparse(n) for n in all_nodes(planted) if spells_an_origin_ref(n))
    assert rows == ["'origin/'", "f'origin/{ref}'"]


def test_a_second_precedence_rule_spelled_with_the_module_constant_is_a_new_row() -> None:
    """The shape ``_template`` missed entirely (#435 fix-round finding A):
    the identical rule, spelled the way ``kstrl/git.py`` spells every
    other remote-tracking ref, with the module constant rather than the
    literal. Plant P12 is this shape re-added to ``resolve_ref``."""
    planted = parse('for candidate in (ref, f"{_ORIGIN_REF_PREFIX}{ref}"):\n    pass\n')
    rows = sorted(ast.unparse(n) for n in all_nodes(planted) if spells_an_origin_ref(n))
    assert rows == [
        "_ORIGIN_REF_PREFIX",
        "f'{_ORIGIN_REF_PREFIX}{ref}'",
        "{_ORIGIN_REF_PREFIX}",
    ]


class TestLayerTwoBlindSpot:
    """Anti-vacuity for the residue LAYER 2's docstring discloses: a ref
    name built from a piece ``astfold.fold`` cannot decide. Under a
    strict xfail, so the day the resolver gets stronger this XPASSes and
    the disclosure has to move into the caught set in the same diff
    (CLAUDE.md guard-design rule 2)."""

    @staticmethod
    def _spelled_anywhere(source: str) -> bool:
        return any(spells_an_origin_ref(n) for n in all_nodes(parse(source)))

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_joined_precedence_rule_is_a_disclosed_miss(self) -> None:
        blind_spot(self._spelled_anywhere, 'x = "/".join(["origin", ref])')
