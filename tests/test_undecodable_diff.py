"""#416: git produces the bytes, kstrl decodes them as utf-8, and a diff it
cannot decode is a diff it could not obtain, which is what ``GitDiffError``
already documents.

One byte that is not valid utf-8, in a diff's CONTENT or in a diff's PATH,
used to make ``UnicodeDecodeError`` (a ``ValueError``) pass straight through
``except git.GitDiffError`` and crash two blocking gates and the mechanical
verifier with a raw traceback instead of a verdict. This file drives the real
git subprocess against a real temporary repository, through the real reader
functions in ``kstrl/git.py`` and the real gates in ``kstrl/verify.py``, and
proves the failure is now a fail-closed ``CheckResult``/``GitDiffError``
rather than an escaped exception.
"""

from __future__ import annotations

import ast
import builtins
import subprocess
from pathlib import Path

import pytest

import kstrl
from kstrl import git, verify
from kstrl.adequacy import AdequacyConfig
from kstrl.policy import PolicyConfig
from tests.helpers import gitrepo
from tests.helpers.astwalk import (
    Bindings,
    all_nodes,
    bindings,
    calls_to,
    handler_clauses,
    label,
    module_name,
    parse,
    parsed,
    resolved_calls,
)
from tests.helpers.astwalk.scope import own_nodes, try_body_nodes
from tests.helpers.encodingspawn import SPAWN_TARGETS, text_mode

#: The one bad byte, as a whole word rather than a split-off fragment, so
#: codespell reads a French noun instead of a typo for a young cow. See
#: [tool.codespell] in pyproject.toml.
BAD_NAME: bytes = "café.py".encode("latin-1")
BAD_CONTENT: bytes = "# café comment\nvalue = 2\n".encode("latin-1")


def _repo(tmp_path: Path, *, quotepath: str | None = None) -> Path:
    """A repository on branch `work`, cut from a one-commit `main`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    gitrepo.git_in(repo, "init", "-q", "-b", "main", ".")
    gitrepo.set_identity(repo)
    if quotepath is not None:
        gitrepo.git_in(repo, "config", "core.quotepath", quotepath)
    (repo / "base.py").write_text("x = 1\n", encoding="utf-8")
    gitrepo.git_in(repo, "add", "-A")
    gitrepo.git_in(repo, "commit", "-qm", "base")
    gitrepo.git_in(repo, "checkout", "-qb", "work")
    return repo


def _commit_undecodable_content(repo: Path) -> None:
    """A file whose CONTENT holds one latin-1 byte. Reaches get_diff_content."""
    (repo / "legacy.py").write_bytes(BAD_CONTENT)
    gitrepo.git_in(repo, "add", "-A")
    gitrepo.git_in(repo, "commit", "-qm", "legacy")


def _commit_undecodable_path(repo: Path) -> None:
    """A commit whose PATH holds one latin-1 byte, built through the index.

    The working tree cannot hold this name on APFS (Errno 92), and
    `git reset --hard` onto this commit exits 128 for the same reason, so
    neither is used. argv is BYTES so the raw path reaches git unchanged.

    `gitrepo.git_in` is NOT used here and must not be used on this
    repository afterwards: it runs git with ``text=True`` and no
    ``encoding=``, so any git command that prints this path raises
    ``UnicodeDecodeError`` inside the test helper rather than inside the
    code under test (read tests/helpers/gitrepo.py:59-68).
    """
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=b"value = 2\n",
            capture_output=True,
            check=True,
            timeout=30,
        )
        .stdout.decode("ascii")
        .strip()
    )
    subprocess.run(
        [
            b"git",
            b"update-index",
            b"--add",
            b"--cacheinfo",
            b"100644," + blob.encode("ascii") + b"," + BAD_NAME,
        ],
        cwd=repo,
        capture_output=True,
        check=True,
        timeout=30,
    )
    tree = (
        subprocess.run(["git", "write-tree"], cwd=repo, capture_output=True, check=True, timeout=30)
        .stdout.decode("ascii")
        .strip()
    )
    parent = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True, timeout=30
        )
        .stdout.decode("ascii")
        .strip()
    )
    commit = (
        subprocess.run(
            ["git", "commit-tree", tree, "-p", parent, "-m", "a path git cannot decode"],
            cwd=repo,
            capture_output=True,
            check=True,
            timeout=30,
        )
        .stdout.decode("ascii")
        .strip()
    )
    subprocess.run(
        ["git", "update-ref", "refs/heads/work", commit],
        cwd=repo,
        capture_output=True,
        check=True,
        timeout=30,
    )


# --- 1: get_diff_content -----------------------------------------------


def test_get_diff_content_refuses_a_diff_it_cannot_decode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit_undecodable_content(repo)

    with pytest.raises(git.GitDiffError) as excinfo:
        git.get_diff_content("main", repo)

    assert "not valid utf-8" in str(excinfo.value)
    assert "0xe9" in str(excinfo.value)
    assert "position" in str(excinfo.value)


# --- 2: get_diff_name_status --------------------------------------------


def test_get_diff_name_status_refuses_a_path_it_cannot_decode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit_undecodable_path(repo)

    with pytest.raises(git.GitDiffError) as strict_info:
        git.get_diff_name_status("main", repo, strict=True)
    assert "0xe9" in str(strict_info.value)
    assert "position" in str(strict_info.value)

    # The D2 half: the LENIENT call must not return [], which would assert
    # that a diff known to carry changes carries none.
    with pytest.raises(git.GitDiffError):
        git.get_diff_name_status("main", repo)


# --- 3: get_diff_names ----------------------------------------------------


def test_get_diff_names_refuses_a_path_it_cannot_decode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit_undecodable_path(repo)

    with pytest.raises(git.GitDiffError):
        git.get_diff_names("main", repo, strict=True)
    with pytest.raises(git.GitDiffError):
        git.get_diff_names("main", repo)


# --- 4/5: get_diff_numstat and its quotepath asymmetry --------------------


def test_get_diff_numstat_refuses_a_path_it_cannot_decode(tmp_path: Path) -> None:
    """``--numstat`` C-quotes a non-ASCII path to pure ASCII unless
    ``core.quotepath`` is off, while ``--name-status -z`` always emits the
    raw bytes (measured). So this fixture, unlike test 2's, must turn
    quoting off to reach the same decode failure."""
    repo = _repo(tmp_path, quotepath="false")
    _commit_undecodable_path(repo)

    with pytest.raises(git.GitDiffError):
        git.get_diff_numstat("main", repo, strict=True)
    with pytest.raises(git.GitDiffError):
        git.get_diff_numstat("main", repo)


#: What git writes for BAD_NAME while core.quotepath is on: the ASCII
#: prefix of the word, then the one byte as backslash-octal, quoted.
QUOTED_NAME = '"{}\\{:o}.py"'.format("café"[:3], 0xE9)


def test_the_numstat_reader_is_reached_only_with_quotepath_off(tmp_path: Path) -> None:
    """The control for test 4's fixture choice: with quotepath at its
    default (on), the same undecodable path is C-quoted to pure ASCII
    before it ever reaches the decode, so the strict reader RETURNS a row
    instead of raising."""
    repo = _repo(tmp_path)
    _commit_undecodable_path(repo)

    rows = git.get_diff_numstat("main", repo, strict=True)

    assert [path for _, _, path in rows] == [QUOTED_NAME]


# --- 6/7: the two gates that read the diff directly -----------------------


def test_check_test_adequacy_fails_closed_on_a_diff_it_cannot_decode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit_undecodable_content(repo)

    result = verify.check_test_adequacy(repo, "main", AdequacyConfig())

    assert result.passed is False
    assert result.measured is False
    assert "could not read the diff" in result.message
    assert len(result.findings) == 1
    assert "not valid utf-8" in result.findings[0].explanation


def test_check_policy_envelope_fails_closed_on_a_diff_it_cannot_decode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit_undecodable_content(repo)

    result = verify.check_policy_envelope(repo, "main", PolicyConfig())

    assert result.passed is False
    assert result.measured is False
    assert "policy envelope could not read the diff" in result.message
    assert len(result.findings) == 1
    assert "not valid utf-8" in result.findings[0].explanation


# --- 8/9: check_diff_scope -------------------------------------------------


def test_check_diff_scope_fails_closed_on_a_path_it_cannot_decode(tmp_path: Path) -> None:
    """The gate the issue did not name (measured M4): the lenient
    ``get_diff_names`` call used to sit outside any try in this function."""
    repo = _repo(tmp_path, quotepath="false")
    _commit_undecodable_path(repo)

    result = verify.check_diff_scope(repo, "main", ["base.py"])

    assert result.passed is False
    assert "failing closed" in result.message


def test_check_diff_scope_still_passes_vacuously_on_an_empty_diff(tmp_path: Path) -> None:
    """The control for test 8: an ordinary empty diff must still pass
    vacuously, so the new handler did not swallow that path."""
    repo = _repo(tmp_path)

    result = verify.check_diff_scope(repo, "main", ["base.py"])

    assert result.passed is True
    assert result.measured is False


# --- 9b: the altitude the issue actually complains about -------------------


def test_the_mechanical_verifier_returns_a_verdict_on_a_diff_it_cannot_decode(
    tmp_path: Path,
) -> None:
    """ "A run that has already spent money dies with a traceback instead
    of a verdict" - the issue's own words. Drives the real entry point."""
    repo = _repo(tmp_path)
    _commit_undecodable_content(repo)

    result = verify.run_mechanical_verification(
        repo,
        prd_path=None,
        base_branch="main",
        allowed_paths=None,
        config=verify.VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            subprocess_timeout=30.0,
        ),
        adequacy_config=AdequacyConfig(enabled=True),
    )

    adequacy_rows = [c for c in result.checks if c.name == "test_adequacy"]
    assert len(adequacy_rows) == 1
    assert adequacy_rows[0].passed is False
    assert adequacy_rows[0].measured is False


# --- 10: Guard 1 ------------------------------------------------------------


def _catching_names(target: type[BaseException]) -> frozenset[str]:
    """Every BUILTIN name whose class catches ``target``.

    Derived by asking CPython, in the house style of ``encodingrules``: a
    list of spellings would be wrong the first time somebody wrote `except
    ValueError` instead of `except UnicodeDecodeError`.
    """
    found = set()
    for name in dir(builtins):
        obj = getattr(builtins, name)
        if isinstance(obj, type) and issubclass(obj, BaseException) and issubclass(target, obj):
            found.add(name)
    return frozenset(found)


def _guarded_by(tree: ast.Module, node: ast.AST, names: frozenset[str], table: Bindings) -> bool:
    """Is ``node`` in the BODY of a try whose clauses catch one of ``names``?

    ``try_body_nodes`` is the boundary, so a handler cannot be credited with
    guarding a call that lives in a function defined in its body.
    ``clause.decided`` is required: a clause this walk cannot name reads as
    "catches nothing", and clearing on it would be the skip direction.
    """
    for candidate in all_nodes(tree):
        if not isinstance(candidate, ast.Try | ast.TryStar):
            continue
        if not any(body is node for body in try_body_nodes(candidate)):
            continue
        for clause in handler_clauses(candidate, table):
            if clause.decided and clause.names & names:
                return True
    return False


def _raises_git_diff_error(fn: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Raise)
        and isinstance(n.exc, ast.Call)
        and getattr(n.exc.func, "id", None) == "GitDiffError"
        for n in own_nodes(fn)
    )


def scan_git_source(source: str) -> tuple[dict[str, int], list[str]]:
    """``(census, reported)`` for one module's source text.

    TEXT rather than a path so the planted shapes below exercise the same
    code the real sweep runs.
    """
    tree = parse(source)
    table = bindings(tree, module="kstrl.git")
    names = _catching_names(UnicodeDecodeError)
    census: dict[str, int] = {}
    reported: list[str] = []
    for fn in all_nodes(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not _raises_git_diff_error(fn):
            continue
        owned = own_nodes(fn)
        for node, _origin in resolved_calls(tree, SPAWN_TARGETS, module="kstrl.git"):
            if not any(child is node for child in owned) or text_mode(node) is not True:
                continue
            census[fn.name] = census.get(fn.name, 0) + 1
            if not _guarded_by(tree, node, names, table):
                reported.append(f"{fn.name}:{node.lineno}")
    return census, reported


_PLANTED_WITHOUT_HANDLER = """
import subprocess


class GitDiffError(RuntimeError):
    pass


def get_diff_authors(base_ref, cwd=None, timeout=30.0):
    try:
        result = subprocess.run(
            ["git", "log", "--format=%an", f"{base_ref}...HEAD"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitDiffError("timed out") from exc
    return result.stdout.splitlines()
"""

_PLANTED_WITH_HANDLER = _PLANTED_WITHOUT_HANDLER.replace(
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n',
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n'
    "    except UnicodeDecodeError as exc:\n"
    '        raise GitDiffError("not valid utf-8") from exc\n',
)

_PLANTED_HANDLER_ON_ANOTHER_TRY = (
    _PLANTED_WITHOUT_HANDLER
    + """

def unrelated():
    try:
        pass
    except UnicodeDecodeError:
        pass
"""
)


class TestEveryStrictReaderConvertsADecodeFailure:
    """Closed by construction over the property that DEFINES a strict
    reader: a function in ``kstrl/git.py`` whose own body raises
    ``GitDiffError``. Measured today: exactly four, one text-mode spawn
    each. A new reader is in the subject the moment it raises, so there is
    no ledger to go stale."""

    def test_the_walk_reports_what_it_could_not_decide(self) -> None:
        """The census above is over every spawn in the file rather than
        over the ones the resolver happened to place."""
        path = Path(kstrl.__file__).parent / "git.py"
        tree = parsed(path)

        found = calls_to(tree, SPAWN_TARGETS, where=label(path), module=module_name(path))

        assert found.undecided == ()

    def test_the_subject_census_is_pinned(self) -> None:
        path = Path(kstrl.__file__).parent / "git.py"
        census, _reported = scan_git_source(path.read_text(encoding="utf-8"))

        assert census == {
            "get_diff_content": 1,
            "get_diff_name_status": 1,
            "get_diff_numstat": 1,
            "resolve_base_sha": 1,
        }

    def test_every_subject_spawn_converts_a_decode_failure(self) -> None:
        path = Path(kstrl.__file__).parent / "git.py"
        _census, reported = scan_git_source(path.read_text(encoding="utf-8"))

        assert reported == []

    def test_a_new_reader_without_the_handler_is_reported(self) -> None:
        _census, reported = scan_git_source(_PLANTED_WITHOUT_HANDLER)

        assert reported == ["get_diff_authors:11"]

    def test_a_reader_whose_handler_is_on_another_try_is_reported(self) -> None:
        """The pair: a handler on a SIBLING try does not count, and the
        identical handler on the RIGHT try clears - proving the guard is
        narrow rather than merely present."""
        _with_census, with_reported = scan_git_source(_PLANTED_WITH_HANDLER)
        _sibling_census, sibling_reported = scan_git_source(_PLANTED_HANDLER_ON_ANOTHER_TRY)

        assert with_reported == []
        assert sibling_reported == ["get_diff_authors:11"]
