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
import subprocess
from pathlib import Path
from typing import TypeGuard

import pytest

import kstrl
from kstrl import git, verify
from kstrl.adequacy import AdequacyConfig
from kstrl.policy import PolicyConfig
from tests.helpers import gitrepo
from tests.helpers.astwalk import (
    all_nodes,
    bindings,
    calls_to,
    guarded_by,
    label,
    module_name,
    package_sources,
    parse,
    parsed,
    resolved_calls,
)
from tests.helpers.astwalk.scope import own_nodes
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
    """A change whose PATH holds one latin-1 byte, built through the index.

    The working tree cannot hold this name on APFS (Errno 92), and
    `git reset --hard` onto this ref exits 128 for the same reason, so
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

    # The lenient half: the LENIENT call must not return [], which would
    # assert that a diff known to carry changes carries none.
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
    raw bytes (measured). So this fixture, unlike
    ``test_get_diff_name_status_refuses_a_path_it_cannot_decode``'s, must
    turn quoting off to reach the same decode failure."""
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
    """The control for
    ``test_get_diff_numstat_refuses_a_path_it_cannot_decode``'s fixture
    choice: with quotepath at its default (on), the same undecodable path
    is C-quoted to pure ASCII before it ever reaches the decode, so the
    strict reader RETURNS a row instead of raising."""
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
    """The gate the issue did not name (#416, measured): the lenient
    ``get_diff_names`` call used to sit outside any try in this function."""
    repo = _repo(tmp_path, quotepath="false")
    _commit_undecodable_path(repo)

    result = verify.check_diff_scope(repo, "main", ["base.py"])

    assert result.passed is False
    assert result.measured is False
    assert "failing closed" in result.message
    assert len(result.findings) == 1
    assert "not valid utf-8" in result.findings[0].explanation


def test_check_diff_scope_still_passes_vacuously_on_an_empty_diff(tmp_path: Path) -> None:
    """The control for
    ``test_check_diff_scope_fails_closed_on_a_path_it_cannot_decode``: an
    ordinary empty diff must still pass vacuously, so the new handler did
    not swallow that path."""
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

#: The exact spelling only. The round-two review of #416 measured that the
#: CPython-derived set (``BaseException``, ``Exception``, ``UnicodeDecodeError``,
#: ``UnicodeError``, ``ValueError``) and this one narrow name give byte-identical
#: censuses on the real ``kstrl/git.py`` (``{"get_diff_content": 1,
#: "get_diff_name_status": 1, "get_diff_numstat": 1, "resolve_base_sha": 1}``,
#: ``reported == []`` either way), so the wider set bought nothing but a
#: clearing hole: ``except Exception: return []`` cleared under the derived
#: set - a diff known to carry changes reported as carrying none, the
#: vacuous pass this fix exists to prevent, verbatim. A guard that CLEARS
#: must be narrow (CLAUDE.md).
_ACCEPTED_NAMES = frozenset({"UnicodeDecodeError"})


def _raises_git_diff_error(fn: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Raise)
        and isinstance(n.exc, ast.Call)
        and getattr(n.exc.func, "id", None) == "GitDiffError"
        for n in own_nodes(fn)
    )


def _handler_raises_git_diff_error(handler: ast.ExceptHandler) -> bool:
    """Does this handler's own body raise ``GitDiffError``?

    ``guarded_by`` alone proves only that a clause NAMES
    ``UnicodeDecodeError``; a clause that swallows it (``pass``), returns
    something unrelated, or bare re-raises it clears there but converts
    nothing - a diff known to carry changes handled as though it were
    empty, exactly the vacuous pass ``check_diff_scope``'s fail-closed
    handling exists to prevent (measured by #416's round-two review: all
    three shapes cleared before this predicate was added). This is the
    stronger question ``guarded_by``'s ``handler_converts`` hook exists
    for: the handler that names the decode must itself raise the
    converted error.
    """
    return any(
        isinstance(n, ast.Raise)
        and isinstance(n.exc, ast.Call)
        and getattr(n.exc.func, "id", None) == "GitDiffError"
        for n in own_nodes(handler)
    )


def _fn_spawns(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    spawns: list[tuple[ast.Call, str]],
) -> list[ast.Call]:
    """The text-mode spawns from ``spawns`` that belong to ``fn``'s own
    scope, extracted so :func:`scan_git_source` stays under the repo's
    cognitive-complexity ratchet (measured: this loop, inlined, was the
    difference between a passing and a refused commit)."""
    owned_ids = {id(n) for n in own_nodes(fn)}
    return [node for node, _origin in spawns if id(node) in owned_ids and text_mode(node) is True]


def scan_git_source(source: str) -> tuple[dict[str, int], list[str]]:
    """``(census, reported)`` for one module's source text.

    TEXT rather than a path so the planted shapes below exercise the same
    code the real sweep runs.
    """
    tree = parse(source)
    table = bindings(tree, module="kstrl.git")
    census: dict[str, int] = {}
    reported: list[str] = []
    spawns = list(resolved_calls(tree, SPAWN_TARGETS, module="kstrl.git"))
    for fn in all_nodes(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not _raises_git_diff_error(fn):
            continue
        for node in _fn_spawns(fn, spawns):
            census[fn.name] = census.get(fn.name, 0) + 1
            if not guarded_by(
                tree, node, _ACCEPTED_NAMES, table, handler_converts=_handler_raises_git_diff_error
            ):
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

#: The ``clause.decided`` conjunct's own mutation test. The other planted
#: sources all have fully resolvable clauses, so ``decided`` is True
#: everywhere and the conjunct never fires; none of them can tell
#: ``clause.decided and clause.names & names`` apart from
#: ``clause.names & names`` alone. This one can: its clause's decidable
#: half (``UnicodeDecodeError`` itself, the exact accepted name - #416's
#: round-two review narrowed this from ``ValueError``, which the narrowed
#: accepted set no longer contains) overlaps ``names``, but its other half
#: (``shim.Whatever``) cannot be named because the module never binds
#: ``shim``, so the clause as a whole is undecided. With the conjunct the
#: site is still reported; drop the conjunct and it clears, because a
#: bare name-overlap check does not require the WHOLE clause to be known.
_PLANTED_UNDECIDABLE_HANDLER = _PLANTED_WITHOUT_HANDLER.replace(
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n',
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n'
    "    except (UnicodeDecodeError, shim.Whatever) as exc:\n"
    '        raise GitDiffError("not valid utf-8") from exc\n',
)

#: A clause that NAMES the target and then does something other than
#: convert it. Each clears under a guard that only asks what a clause
#: CATCHES; #416's round-two review measured all three clearing before
#: ``handler_converts`` was added.
_PLANTED_CLEARS_TO_EMPTY = _PLANTED_WITHOUT_HANDLER.replace(
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n',
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n'
    "    except UnicodeDecodeError:\n"
    "        return []\n",
)

_PLANTED_SWALLOWS = _PLANTED_WITHOUT_HANDLER.replace(
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n',
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n'
    "    except UnicodeDecodeError:\n"
    "        pass\n",
)

_PLANTED_BARE_RERAISE = _PLANTED_WITHOUT_HANDLER.replace(
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n',
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n'
    "    except UnicodeDecodeError:\n"
    "        raise\n",
)

#: The exact shape #416's round-two review measured clearing under the
#: CPython-derived accepted set: a clause naming ``Exception``, not the
#: exact target, that returns ``[]``. This clause is reported by the
#: ``handler_converts`` check regardless of the accepted set: it returns
#: ``[]`` instead of raising ``GitDiffError``, so it never converts, and
#: that stays true even when ``_ACCEPTED_NAMES`` is widened to include
#: ``Exception`` (measured). It is therefore not evidence that the
#: exact-spelling accepted set (``_ACCEPTED_NAMES``) is load-bearing on its
#: own; ``_PLANTED_WIDE_BUT_CONVERTING`` below is the source that only the
#: narrowing can report.
_PLANTED_UNRELATED_EXCEPTION = _PLANTED_WITHOUT_HANDLER.replace(
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n',
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n'
    "    except Exception:\n"
    "        return []\n",
)

#: The missing sole-killer for the exact-spelling accepted set
#: (``_ACCEPTED_NAMES``). This clause names ``ValueError``, a WIDER name
#: that at runtime really does catch every ``UnicodeDecodeError``
#: (``UnicodeDecodeError`` is a ``ValueError`` subclass), and it converts:
#: it raises ``GitDiffError`` exactly like the compliant handler does, so
#: the ``handler_converts`` check says yes. Only the exact-spelling
#: accepted set can still report this site, because ``clause.names`` is
#: ``{"ValueError"}``, which does not overlap ``{"UnicodeDecodeError"}``.
#: Widen ``_ACCEPTED_NAMES`` to include ``ValueError`` and this is the one
#: test in the file that fails (measured, #416 round three).
_PLANTED_WIDE_BUT_CONVERTING = _PLANTED_WITHOUT_HANDLER.replace(
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n',
    "    except subprocess.TimeoutExpired as exc:\n"
    '        raise GitDiffError("timed out") from exc\n'
    "    except ValueError as exc:\n"
    '        raise GitDiffError("not valid utf-8") from exc\n',
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
        with_census, with_reported = scan_git_source(_PLANTED_WITH_HANDLER)
        _sibling_census, sibling_reported = scan_git_source(_PLANTED_HANDLER_ON_ANOTHER_TRY)

        assert with_census == {"get_diff_authors": 1}
        assert with_reported == []
        assert sibling_reported == ["get_diff_authors:11"]

    def test_a_partially_undecidable_handler_is_reported(self) -> None:
        """``clause.decided`` is what stops the skip direction. Without it,
        a clause whose decidable half overlaps the target names is treated
        as guarding the site even though its other half (``shim.Whatever``,
        which the module never binds) could not be named - the exact "half
        of a promise" a fail-closed reader must not accept."""
        _census, reported = scan_git_source(_PLANTED_UNDECIDABLE_HANDLER)

        assert reported == ["get_diff_authors:11"]

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(_PLANTED_CLEARS_TO_EMPTY, id="returns-empty-list"),
            pytest.param(_PLANTED_SWALLOWS, id="swallows"),
            pytest.param(_PLANTED_BARE_RERAISE, id="bare-reraise"),
        ],
    )
    def test_a_clause_that_names_the_target_and_does_not_convert_it_is_reported(
        self, source: str
    ) -> None:
        """Naming ``UnicodeDecodeError`` is not conversion. A clause that
        returns ``[]`` (a diff known to carry changes reported as carrying
        none, the vacuous pass verbatim), one that swallows the decode,
        and one that bare re-raises it all NAME the target and all three
        cleared under a guard that asked only that question (#416's
        round-two review, measured). ``handler_converts`` requiring an
        actual ``raise GitDiffError(...)`` is what reports them."""
        _census, reported = scan_git_source(source)

        assert reported == ["get_diff_authors:11"]

    def test_a_clause_naming_an_unrelated_broad_exception_is_reported(self) -> None:
        """The exact shape #416's round-two review measured clearing under
        the CPython-derived accepted set: ``except Exception: return []``.
        This clause is reported by the ``handler_converts`` check
        regardless of the accepted set: it returns ``[]`` instead of
        raising ``GitDiffError``, so it never converts, and widening
        ``_ACCEPTED_NAMES`` to include ``Exception`` does not clear it
        (measured). See
        ``test_a_wider_name_that_still_converts_is_reported_only_by_the_narrowing``
        for the source that only the exact-spelling accepted set can
        report."""
        _census, reported = scan_git_source(_PLANTED_UNRELATED_EXCEPTION)

        assert reported == ["get_diff_authors:11"]

    def test_a_wider_name_that_still_converts_is_reported_only_by_the_narrowing(
        self,
    ) -> None:
        """A clause naming ``ValueError`` catches every decode failure
        (``UnicodeDecodeError`` is a ``ValueError``) and converts it
        exactly like the compliant handler does - the ``handler_converts``
        check says yes. Only the exact-spelling accepted set
        (``_ACCEPTED_NAMES``) reports this site, because ``clause.names``
        (``{"ValueError"}``) does not overlap ``{"UnicodeDecodeError"}``.
        This is the test that fails when the narrowing is reverted
        (measured, #416 round three): none of the other planted-source
        tests in this class do."""
        _census, reported = scan_git_source(_PLANTED_WIDE_BUT_CONVERTING)

        assert reported == ["get_diff_authors:11"]


def _names_git_diff_error(node: ast.Raise) -> bool:
    if not isinstance(node.exc, ast.Call):
        return False
    func = node.exc.func
    if isinstance(func, ast.Name):
        return func.id == "GitDiffError"
    return isinstance(func, ast.Attribute) and func.attr == "GitDiffError"


def _binds_git_diff_error(node: ast.AST) -> TypeGuard[ast.ImportFrom]:
    if not isinstance(node, ast.ImportFrom):
        return False
    return any((alias.asname or alias.name) == "GitDiffError" for alias in node.names)


class TestGitDiffErrorHasOneHome:
    """Guard 1's closure is over ``kstrl/git.py`` and its subject predicate
    keys on a bare ``ast.Name`` ``GitDiffError``. Neither fact was pinned
    before #416's round-two review: a raise elsewhere spelled
    ``git.GitDiffError(...)``, through an aliased import, or in another
    module entirely dropped its whole function out of every census above
    with nothing failing, which is the mirror of
    ``test_the_error_name_has_one_home`` in
    ``tests/test_undecodable_child_output.py`` for this file's error."""

    def test_every_raise_of_the_name_is_in_git_py(self) -> None:
        census: dict[str, int] = {}
        for source_file in package_sources():
            where = label(source_file)
            tree = parse(source_file.read_text(encoding="utf-8"))
            for node in all_nodes(tree):
                if isinstance(node, ast.Raise) and _names_git_diff_error(node):
                    census[where] = census.get(where, 0) + 1

        assert census == {"git.py": 14}

    def test_no_importfrom_outside_git_py_binds_the_name(self) -> None:
        offenders = [
            f"{label(source_file)}:{node.lineno}"
            for source_file in package_sources()
            if label(source_file) != "git.py"
            for node in all_nodes(parse(source_file.read_text(encoding="utf-8")))
            if _binds_git_diff_error(node)
        ]

        assert offenders == []

    def test_the_class_is_defined_only_in_git_py(self) -> None:
        homes = [
            label(source_file)
            for source_file in package_sources()
            if any(
                isinstance(node, ast.ClassDef) and node.name == "GitDiffError"
                for node in all_nodes(parse(source_file.read_text(encoding="utf-8")))
            )
        ]

        assert homes == ["git.py"]

    def test_a_raise_of_the_name_in_another_module_is_reported(self) -> None:
        """The mutation: plant ``raise git.GitDiffError("x")`` in a source
        text and confirm the census sees it as a new file, rather than the
        walk silently ignoring it because it is not the bare ``Name``
        spelling ``kstrl/git.py`` happens to use everywhere today."""
        tree = parse('raise git.GitDiffError("x")\n')
        census: dict[str, int] = {}
        for node in all_nodes(tree):
            if isinstance(node, ast.Raise) and _names_git_diff_error(node):
                census["planted.py"] = census.get("planted.py", 0) + 1

        assert census == {"planted.py": 1}
