"""Which git argvs in ``kstrl/`` print a PATH, and whether they carry -z (#423).

Git C-quotes any pathname holding a non-ASCII byte, a tab, a double quote
or a backslash. ``-z`` switches that off at the source and NUL-separates
the records, which is why the fix for #423 is ``-z`` on three readers
rather than an unquoting function on their output.

LAYER 1 pins every git argv in ``kstrl/``, rendered. It enumerates no
subcommand and no flag: a list literal whose first element folds to
``"git"`` is a git argv, and so is a list literal handed to a helper that
prefixes ``"git"`` to one of its own parameters (``breaker._git``, found
by walking rather than by being named here). A new git command anywhere in
``kstrl/`` is therefore an unexplained row, which is what makes layer 2's
marker set safe to be a list: a path-printing command nobody enumerated
still has to be declared HERE before it can reach layer 2 unclassified.

LAYER 2 is the rule: a path-printing argv carries ``-z``. Its exceptions
are three, each measured rather than assumed, and each pinned by row.

WHAT NEITHER LAYER SEES. An argv assembled at run time, and an argv
holding a ``*splat``: the splat can carry any flag, so those rows are
UNDECIDED and pinned as such rather than cleared. Both layers FLAG, so an
over-match costs a reader one line, which is the direction a guard of this
kind is allowed to be wrong in (CLAUDE.md). A third residual, a run-time
marker that folds to no literal string at all, is disclosed and pinned
below with a strict xfail (`test_a_runtime_marker_is_a_known_miss`).
"""

from __future__ import annotations

import ast
import io
from dataclasses import dataclass
from pathlib import Path

import pytest

from kstrl import git, guards, verify
from kstrl.breaker import BreakerConfig, NoProgressBreaker
from kstrl.config import KstrlConfig
from kstrl.policy import PolicyConfig, count_diff_size
from kstrl.ui import PlainUI
from tests.helpers import gitrepo
from tests.helpers.astwalk import (
    all_nodes,
    assert_census,
    blind_spot,
    census,
    folded_str,
    label,
    leaf_name,
    own_nodes,
    package_sources,
    parse,
    parsed,
)

PATH_PRINTING: frozenset[str] = frozenset(
    {
        "--name-only",
        "--name-status",
        "--numstat",
        "--raw",
        "--stat",
        "--porcelain",
        "ls-files",
        "ls-tree",
        "diff-tree",
        "diff-index",
        "diff-files",
        "check-ignore",
    }
)


@dataclass(frozen=True)
class Argv:
    where: str
    render: str
    decided: frozenset[str]
    splat: bool

    @property
    def row(self) -> str:
        return f"{self.where} {self.render}"


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = fn.args
    return {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}


def _prefixes_git(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    params = _params(fn)
    for node in own_nodes(fn):
        if not isinstance(node, ast.List | ast.Tuple) or len(node.elts) != 2:
            continue
        head, tail = node.elts
        if folded_str(head) != "git":
            continue
        if (
            isinstance(tail, ast.Starred)
            and isinstance(tail.value, ast.Name)
            and tail.value.id in params
        ):
            return True
    return False


def prefix_helpers(sources: list[Path]) -> dict[str, str]:
    found: dict[str, str] = {}
    for source in sources:
        for node in all_nodes(parsed(source)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _prefixes_git(node):
                found[node.name] = label(source)
    return found


HELPERS: dict[str, str] = prefix_helpers(package_sources())

EXPECTED_HELPERS: dict[str, str] = {"_git": "breaker.py"}


def _literal_of(node: ast.AST) -> tuple[ast.List | ast.Tuple, tuple[str, ...]] | None:
    if isinstance(node, ast.List | ast.Tuple) and node.elts:
        return (node, ()) if folded_str(node.elts[0]) == "git" else None
    if (
        isinstance(node, ast.Call)
        and leaf_name(node.func) in HELPERS
        and node.args
        and isinstance(node.args[0], ast.List | ast.Tuple)
    ):
        return node.args[0], ("git",)
    return None


def is_git_argv(node: ast.AST) -> bool:
    return _literal_of(node) is not None


def argv_at(where: str, node: ast.AST) -> Argv:
    found = _literal_of(node)
    assert found is not None
    literal, prefix = found
    tokens: list[str | None] = []
    splat = False
    for element in literal.elts:
        if isinstance(element, ast.Starred):
            splat = True
            tokens.append(None)
        else:
            tokens.append(folded_str(element))
    render = " ".join([*prefix, *(t if t is not None else "?" for t in tokens)])
    decided = frozenset([*prefix, *(t for t in tokens if t is not None)])
    return Argv(where, render, decided, splat)


def argvs_in(tree: ast.Module, where: str) -> list[Argv]:
    return [argv_at(where, node) for node in all_nodes(tree) if is_git_argv(node)]


def classify(rows: list[Argv]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    with_z: set[str] = set()
    without_z: set[str] = set()
    undecided: set[str] = set()
    for row in rows:
        if row.splat:
            undecided.add(row.row)
        elif row.decided & PATH_PRINTING:
            (with_z if "-z" in row.decided else without_z).add(row.row)
    return tuple(sorted(with_z)), tuple(sorted(without_z)), tuple(sorted(undecided))


def package_argvs() -> list[Argv]:
    rows: list[Argv] = []
    for source in package_sources():
        rows.extend(argvs_in(parsed(source), label(source)))
    return rows


EXPECTED_WITH_Z: tuple[str, ...] = (
    "breaker.py git status --porcelain -uall -z",
    "doctor.py git ls-files -z",
    "git.py git diff --name-only --cached -z",
    "git.py git diff --name-only -z",
    "git.py git diff --name-status -z -M -C ? --",
    "git.py git diff --name-status -z ? --",
    "git.py git diff --numstat -z ? --",
    "git.py git ls-files --others --exclude-standard -z",
)

EXPECTED_WITHOUT_Z: tuple[str, ...] = (
    # -q means git prints nothing at all, so there is no path in the
    # output to spell. Measured: `git check-ignore -q -- foo.py` exits
    # with nothing on stdout.
    "doctor.py git check-ignore -q -- ?",
    # git.ignore_source keeps only result.stdout.split("\t")[0], the
    # rule's source:line:pattern, and discards the pathname half. -z is
    # also refused here: measured, `git check-ignore -z -v -- foo.py`
    # exits 128 with "fatal: -z only makes sense with --stdin".
    "git.py git check-ignore -v -- ?",
    # git.is_file_tracked names no encoding= on this spawn, so it runs in
    # BYTES mode and reads only result.returncode == 0. No path decoded.
    "git.py git ls-files --error-unmatch -- ?",
)

EXPECTED_UNDECIDED: tuple[str, ...] = (
    # breaker._git itself: the helper every breaker argv goes through.
    # Undecided by construction.
    "breaker.py git ?",
    # Carries *_BASE_BRANCH_REFS, so it is splat-undecided. It prints REF
    # names, not paths, but the splat is what puts it here and the splat
    # is what is pinned.
    "git.py git for-each-ref --format=%(refname)%09%(symref) ? ?",
)

EXPECTED_GIT_ARGVS: dict[str, int] = {
    "breaker.py git ?": 1,
    "breaker.py git diff HEAD": 1,
    "breaker.py git rev-parse HEAD": 1,
    "breaker.py git status --porcelain -uall -z": 1,
    "config_report.py git ?": 1,
    "contract.py git merge --abort": 1,
    "contract.py git worktree add --detach ? ?": 1,
    "contract.py git worktree prune": 1,
    "contract.py git worktree remove --force ?": 1,
    "doctor.py git check-ignore -q -- ?": 1,
    "doctor.py git ls-files -z": 1,
    "factory.py git branch -D ?": 2,
    "factory.py git merge-base --is-ancestor ? ?": 1,
    "factory.py git rev-parse --verify --quiet ?": 1,
    "factory.py git worktree add ? -b ? ?": 1,
    "factory.py git worktree add ? ?": 1,
    "factory.py git worktree prune": 1,
    "factory.py git worktree remove --force ?": 4,
    "git.py git add -- ?": 1,
    "git.py git branch ? -- ?": 1,
    "git.py git check-ignore -v -- ?": 1,
    "git.py git checkout -b ?": 1,
    "git.py git checkout -b ? ? --": 1,
    "git.py git checkout ? --": 2,
    "git.py git config --get remote.origin.url": 1,
    "git.py git diff --name-only --cached -z": 1,
    "git.py git diff --name-only -z": 1,
    "git.py git diff --name-status -z -M -C ? --": 1,
    "git.py git diff --name-status -z ? --": 1,
    "git.py git diff --numstat -z ? --": 1,
    "git.py git diff ? --": 1,
    "git.py git fetch -- origin ?": 1,
    "git.py git for-each-ref --format=%(refname)%09%(symref) ? ?": 1,
    "git.py git ls-files --error-unmatch -- ?": 1,
    "git.py git ls-files --others --exclude-standard -z": 1,
    "git.py git merge --no-edit -- ?": 1,
    "git.py git restore --staged --worktree -- ?": 1,
    "git.py git restore ? --staged --worktree -- ?": 1,
    "git.py git rev-parse --abbrev-ref HEAD": 1,
    "git.py git rev-parse --is-inside-work-tree": 1,
    "git.py git rev-parse --show-toplevel": 1,
    "git.py git rev-parse --verify --quiet ?": 3,
    "git.py git rev-parse --verify --quiet HEAD": 1,
    "git.py git rm --cached --ignore-unmatch -q -- ?": 1,
    "git.py git show-ref --verify --quiet ?": 1,
    "pr.py git push --delete -- origin ?": 2,
    "pr.py git push -u -- origin ?": 1,
    "retry_plan.py git branch -D ?": 1,
    "retry_plan.py git rev-parse --verify --quiet ?": 1,
    "retry_plan.py git worktree prune": 1,
    "retry_plan.py git worktree remove --force ?": 1,
    "statedir.py git -C ? remote get-url origin": 1,
    "tui/screens/home.py git rev-parse --abbrev-ref HEAD": 1,
    "verify.py git add -A -- . ?": 1,
}


_PLANTED_DIRECT = """
import subprocess

subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=cwd)
"""

_PLANTED_HELPER = """
def caller(cwd):
    return _git(["ls-files", "--others"], cwd)
"""

_PLANTED_SPLAT = """
import subprocess

subprocess.run(["git", "diff", "--numstat", *flags], cwd=cwd)
"""

_PLANTED_NEW_COMMAND = """
import subprocess

subprocess.run(["git", "cat-file", "-p", "HEAD"], cwd=cwd)
"""


class TestLayerOneInventory:
    def test_the_git_prefix_helpers_are_pinned(self) -> None:
        assert HELPERS == EXPECTED_HELPERS

    def test_the_git_argv_inventory_is_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=is_git_argv,
            expected=EXPECTED_GIT_ARGVS,
            control=(
                'import subprocess\nsubprocess.run(["git", "diff", "--name-only"], cwd=cwd)',
                "def f(cwd):\n    return _git(['status', '--porcelain'], cwd)",
            ),
            message="the git argv inventory moved.",
            key=lambda source, node: argv_at(label(source), node).row,
        )

    def test_a_new_git_command_is_a_new_row(self, tmp_path: Path) -> None:
        built = census(
            [_written(tmp_path, _PLANTED_NEW_COMMAND)],
            is_git_argv,
            key=lambda source, node: argv_at("planted.py", node).row,
        )
        assert built == {"planted.py git cat-file -p HEAD": 1}


class TestLayerTwoTheRule:
    def test_every_path_printing_argv_is_classified(self) -> None:
        with_z, without_z, undecided = classify(package_argvs())

        assert with_z == EXPECTED_WITH_Z
        assert without_z == EXPECTED_WITHOUT_Z
        assert undecided == EXPECTED_UNDECIDED

    def test_a_direct_argv_without_z_is_reported(self) -> None:
        _, without_z, _ = classify(argvs_in(parse(_PLANTED_DIRECT), "planted.py"))

        assert without_z == ("planted.py git diff --name-only HEAD",)

    def test_a_helper_argv_without_z_is_reported(self) -> None:
        _, without_z, _ = classify(argvs_in(parse(_PLANTED_HELPER), "planted.py"))

        assert without_z == ("planted.py git ls-files --others",)

    def test_a_splat_argv_is_undecided_not_cleared(self) -> None:
        with_z, without_z, undecided = classify(argvs_in(parse(_PLANTED_SPLAT), "planted.py"))

        assert (with_z, without_z) == ((), ())
        assert undecided == ("planted.py git diff --numstat ?",)


def _classifies_in_source(source: str) -> bool:
    """`classify` applied to one parsed source, for `blind_spot`'s probe shape."""
    with_z, without_z, undecided = classify(argvs_in(parse(source), "planted.py"))
    return bool(with_z or without_z or undecided)


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_runtime_marker_is_a_known_miss() -> None:
    """The disclosed residual. `classify` reads the FOLDED value of each
    element, so an argv whose path-printing marker is a run-time name is
    neither with_z, without_z nor undecided: it falls out of every bucket.
    Layer 1 still sees it as a row, which is what keeps this from being a
    hole with nothing behind it. Under `strict=True` so that teaching
    `classify` to treat an unfoldable element as undecided XPASSes here and
    forces this disclosure to be edited in the same diff."""
    blind_spot(
        _classifies_in_source, 'import subprocess\nsubprocess.run(["git", "diff", marker])\n'
    )


def _written(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "planted.py"
    path.write_text(source, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The behaviour: what the readers return for names git would have quoted.
# --------------------------------------------------------------------------

#: Every shape git C-quotes, plus the two whitespace shapes a `.strip()`
#: eats, plus a plain control.
TRICKY: tuple[str, ...] = (
    "café.py",
    "tab\there.py",
    # codespell:ignore-next-line
    'quo"te.py',
    "back\\slash.py",
    " lead.py",
    "trail .py",
    "plain.py",
)


def _repo_with_tricky_names(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    gitrepo.git_in(repo, "init", "-q", "-b", "main", ".")
    gitrepo.set_identity(repo)
    (repo / "base.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "ré.py").write_text("z = 3\n", encoding="utf-8")
    gitrepo.git_in(repo, "add", "-A")
    gitrepo.git_in(repo, "commit", "-qm", "base")
    gitrepo.git_in(repo, "checkout", "-qb", "work")
    for name in TRICKY:
        (repo / name).write_text("y = 2\n", encoding="utf-8")
    return repo


class TestEveryReaderReturnsTheFilesOwnName:
    def test_the_working_tree_readers_do(self, tmp_path: Path) -> None:
        repo = _repo_with_tricky_names(tmp_path)

        assert git.get_untracked_files(repo) == set(TRICKY)
        assert git.get_changed_files(repo) == set(TRICKY)

    def test_the_diff_readers_do(self, tmp_path: Path) -> None:
        repo = _repo_with_tricky_names(tmp_path)
        gitrepo.git_in(repo, "add", "-A")
        gitrepo.git_in(repo, "commit", "-qm", "work")

        assert set(git.get_diff_names("main", repo, strict=True)) == set(TRICKY)
        assert {p for _s, p in git.get_diff_name_status("main", repo, strict=True)} == set(TRICKY)
        assert {p for _a, _r, p in git.get_diff_numstat("main", repo, strict=True)} == set(TRICKY)

    def test_a_rename_of_a_non_ascii_file_lands_on_the_destination(self, tmp_path: Path) -> None:
        repo = _repo_with_tricky_names(tmp_path)
        gitrepo.git_in(repo, "mv", "ré.py", "naïve.py")
        gitrepo.git_in(repo, "add", "-A")
        gitrepo.git_in(repo, "commit", "-qm", "work")

        rows = git.get_diff_numstat("main", repo, strict=True)

        assert {p for _a, _r, p in rows} == set(TRICKY) | {"naïve.py"}
        assert (0, 0, "naïve.py") in rows

    def test_a_binary_file_still_reports_none_counts(self, tmp_path: Path) -> None:
        repo = _repo_with_tricky_names(tmp_path)
        (repo / "bin.dat").write_bytes(b"\x00\x01\x02\x03")
        gitrepo.git_in(repo, "add", "-A")
        gitrepo.git_in(repo, "commit", "-qm", "work")

        rows = git.get_diff_numstat("main", repo, strict=True)

        assert (None, None, "bin.dat") in rows


class TestTheGuardsThatConsumeThem:
    def test_allowed_paths_passes_a_non_ascii_file_in_scope(self, tmp_path: Path) -> None:
        repo = _repo_with_tricky_names(tmp_path)
        (repo / "src" / "café.py").write_text("y = 2\n", encoding="utf-8")
        for name in TRICKY:
            (repo / name).unlink()
        config = _guard_config(repo)

        ok, violations = guards.enforce_allowed_paths(
            config, PlainUI(no_color=True, file=io.StringIO()), repo
        )

        assert (ok, violations) == (True, [])

    def test_changed_and_numstat_agree_about_a_non_ascii_file(self, tmp_path: Path) -> None:
        repo = _repo_with_tricky_names(tmp_path)
        gitrepo.git_in(repo, "add", "-A")
        gitrepo.git_in(repo, "commit", "-qm", "work")

        changed = set(git.get_diff_names("main", repo, strict=True))
        numstat = {p for _a, _r, p in git.get_diff_numstat("main", repo, strict=True)}

        assert changed == numstat

    def test_the_policy_gate_does_not_count_a_lockfile_under_a_non_ascii_directory(
        self, tmp_path: Path
    ) -> None:
        """The real R8.1 gate, not `count_diff_size`. On main the numstat row
        renders as `"vendé/uv.lock"`, whose basename ends in a double quote, so
        the lockfile exclusion misses it and its 500 lines are charged against
        the size cap. Measured: passed=False on main, passed=True here."""
        repo = _repo_with_tricky_names(tmp_path)
        for name in TRICKY:
            (repo / name).unlink()
        (repo / "vendé").mkdir()
        (repo / "vendé" / "uv.lock").write_text("lock\n" * 500, encoding="utf-8")
        gitrepo.git_in(repo, "add", "-A")
        gitrepo.git_in(repo, "commit", "-qm", "work")

        result = verify.check_policy_envelope(
            repo, "main", PolicyConfig(enabled=True, max_lines_changed=10)
        )

        assert result.passed is True, result.details
        assert count_diff_size(git.get_diff_numstat("main", repo, strict=True)) == (0, 0)

    def test_the_breaker_does_not_report_a_stall_after_a_real_edit(self, tmp_path: Path) -> None:
        """The real entry point. `no_progress_iterations=1` trips on the first
        stalled iteration, and `test_command` is unset so the probe is a
        constant and no subprocess runs. On main the untracked `café.py` is
        listed under a quoted name that names no file on disk, its content is
        never hashed, and a real edit reads as a stall."""
        repo = _repo_with_tricky_names(tmp_path)
        breaker = NoProgressBreaker(repo, BreakerConfig(no_progress_iterations=1))
        (repo / "café.py").write_text("y = 2 and then some more\n", encoding="utf-8")

        assert breaker.enabled is True
        assert breaker.record_iteration() is False
        assert breaker.stall_count == 0


def _guard_config(root: Path) -> KstrlConfig:
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True, exist_ok=True)
    (kstrl_dir / "prompt.md").write_text("p", encoding="utf-8")
    (kstrl_dir / "prd.json").write_text('{"branchName": "t", "userStories": []}', encoding="utf-8")
    return KstrlConfig(
        max_iterations=1,
        prompt_file=kstrl_dir / "prompt.md",
        prd_file=kstrl_dir / "prd.json",
        sleep_seconds=0,
        interactive=False,
        kstrl_branch="",
        kstrl_branch_explicit=True,
        allowed_paths=["src/", "scripts/"],
    )
