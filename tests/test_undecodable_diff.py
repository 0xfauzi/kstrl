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

The static-analysis guard over every strict reader in ``kstrl/git.py``
lives in ``tests/test_undecodable_diff_guard.py`` (#423): it was split out
of this file to keep this one under the repo's 800-line ratchet. Nothing
about the guard's behaviour changed in the split.
"""

from __future__ import annotations

import io
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl import doctor, git, guards, loop, verify
from kstrl.adequacy import AdequacyConfig
from kstrl.breaker import compute_diff_hash
from kstrl.config import KstrlConfig
from kstrl.policy import PolicyConfig
from kstrl.ui import PlainUI
from tests.helpers import gitrepo

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


def _guard_config(repo: Path) -> KstrlConfig:
    """A config with ALLOWED_PATHS set, and the two files KstrlConfig needs on
    disk. Written under `scripts/`, which is inside allowed_paths, so the
    fixture's own files are never the violation under test."""
    kstrl_dir = repo / "scripts" / "kstrl"
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
    """``--numstat`` used to C-quote a non-ASCII path to pure ASCII unless
    ``core.quotepath`` was off, so this fixture had to turn quoting off to
    reach the decode at all. #423 put ``-z`` on the reader, which emits the
    raw bytes whatever ``core.quotepath`` says, so the plain fixture now
    reaches it and the explicit ``quotepath="false"`` is gone."""
    repo = _repo(tmp_path)
    _commit_undecodable_path(repo)

    with pytest.raises(git.GitDiffError):
        git.get_diff_numstat("main", repo, strict=True)
    with pytest.raises(git.GitDiffError):
        git.get_diff_numstat("main", repo)


def test_quotepath_off_does_not_change_the_numstat_reader(tmp_path: Path) -> None:
    """The pair to the test above, and the row that records what #423
    changed. Before it, quotepath at its default C-quoted the undecodable
    path to pure ASCII and the strict reader RETURNED a mangled row; only
    with quoting off did it refuse. With ``-z`` the setting is inert, so
    both fixtures now refuse and the reader fails closed on the config an
    operator actually has."""
    repo = _repo(tmp_path, quotepath="false")
    _commit_undecodable_path(repo)

    with pytest.raises(git.GitDiffError):
        git.get_diff_numstat("main", repo, strict=True)


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


# --- 10: check_bad_patterns (#414, the call site #416 left uncovered) ------


def test_check_bad_patterns_fails_closed_on_a_path_it_cannot_decode(tmp_path: Path) -> None:
    """The last uncovered reader call site in ``kstrl/``: ``check_bad_patterns``
    called the lenient ``get_diff_names`` OUTSIDE its try, so a path git
    cannot decode left this blocking gate as a traceback rather than a
    verdict. Measured as a handoff on PR #419."""
    repo = _repo(tmp_path, quotepath="false")
    _commit_undecodable_path(repo)

    result = verify.check_bad_patterns(repo, "main")

    assert result.passed is False
    assert result.measured is False
    assert "failing closed" in result.message
    assert len(result.findings) == 1
    assert "not valid utf-8" in result.findings[0].explanation


def test_check_bad_patterns_still_passes_vacuously_on_an_empty_diff(tmp_path: Path) -> None:
    """The control for the test above: an ordinary empty diff still passes
    vacuously and measures nothing, so the widened try did not swallow it."""
    repo = _repo(tmp_path)

    result = verify.check_bad_patterns(repo, "main")

    assert result.passed is True
    assert result.measured is False


#: A legal Python file whose bytes are not utf-8: `BAD_CONTENT` above with a
#: PEP 263 declaration in front of it. py_compile honours the declaration,
#: so only a SECOND decode outside py_compile can fail on this, which is
#: what the scan used to do (#414). Built from the existing constant rather
#: than written out again (#425 simplify pass F2), so the file's three
#: latin-1 fixtures stay one spelling and no codespell suppression is
#: needed ([tool.codespell] in pyproject.toml).
LATIN1_SOURCE: bytes = b"# -*- coding: latin-1 -*-\n" + BAD_CONTENT


def test_check_bad_patterns_does_not_crash_on_a_renamed_latin_1_source_file(
    tmp_path: Path,
) -> None:
    """A rename-only diff decodes fine, so the scan reaches the file itself, and
    the old ``read_text(encoding='utf-8')`` raised ``UnicodeDecodeError`` out of a
    blocking Phase 1 gate. Measured on main at 6a354cc."""
    repo = _repo(tmp_path)
    gitrepo.git_in(repo, "checkout", "-q", "main")
    (repo / "legacy.py").write_bytes(LATIN1_SOURCE)
    gitrepo.git_in(repo, "add", "-A")
    gitrepo.git_in(repo, "commit", "-qm", "a latin-1 source file on main")
    gitrepo.git_in(repo, "checkout", "-qB", "work")
    (repo / "base.py").write_text("x = 2\n", encoding="utf-8")
    gitrepo.git_in(repo, "add", "-A")
    gitrepo.git_in(repo, "mv", "legacy.py", "moved.py")
    gitrepo.git_in(repo, "commit", "-qm", "rename the latin-1 file")

    result = verify.check_bad_patterns(repo, "main")

    assert result.passed is True
    assert result.measured is True
    assert result.details == []


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


# --- 11: the readers -z reaches (#423) ------------------------------------


def test_get_changed_files_refuses_a_path_it_cannot_decode(tmp_path: Path) -> None:
    """#423 put ``-z`` on this reader, so git no longer C-quotes the bad
    byte into pure ASCII before kstrl sees it. Returning a set with the
    path silently missing would be a vacuous pass on the scope gate, so
    the reader refuses, exactly as the four #416 readers do."""
    repo = _repo(tmp_path)
    _commit_undecodable_path(repo)

    with pytest.raises(git.GitDiffError) as excinfo:
        git.get_changed_files(repo)

    assert "not valid utf-8" in str(excinfo.value)


def test_the_allowed_paths_guard_fails_closed_on_a_path_it_cannot_decode(
    tmp_path: Path,
) -> None:
    """The real entry point: the refusal is a verdict, not a traceback."""
    repo = _repo(tmp_path)
    config = _guard_config(repo)
    _commit_undecodable_path(repo)
    out = io.StringIO()

    ok, violations = guards.enforce_allowed_paths(config, PlainUI(no_color=True, file=out), repo)

    assert (ok, violations) == (False, [])
    assert "not valid utf-8" in out.getvalue()


def test_the_breaker_cannot_measure_a_path_it_cannot_decode(tmp_path: Path) -> None:
    """The breaker fails OPEN by contract: None means "cannot measure",
    and the caller skips the stall count for that iteration."""
    repo = _repo(tmp_path)
    _commit_undecodable_path(repo)

    assert compute_diff_hash(repo) is None


def test_the_allowed_paths_guard_fails_closed_with_a_baseline_it_cannot_read(
    tmp_path: Path,
) -> None:
    """The baseline branch of the same guard, which goes through
    ``get_changed_files_since`` -> ``_committed_since`` and not through
    ``get_changed_files`` at all. ``run_loop`` always captures a baseline
    when ALLOWED_PATHS is set, so this is the path a real run takes. The
    baseline head is the commit that CARRIES the undecodable path: a head
    from before it never puts that name in the diff and the test would
    pass vacuously."""
    repo = _repo(tmp_path)
    config = _guard_config(repo)
    _commit_undecodable_path(repo)
    head = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True, timeout=30
        )
        .stdout.decode("ascii")
        .strip()
    )
    out = io.StringIO()

    ok, violations = guards.enforce_allowed_paths(
        config,
        PlainUI(no_color=True, file=out),
        repo,
        baseline=git.WorkspaceBaseline(head=head, dirty=frozenset()),
    )

    assert (ok, violations) == (False, [])
    assert "not valid utf-8" in out.getvalue()


def test_doctor_reports_a_working_tree_it_cannot_read(tmp_path: Path) -> None:
    """``ks doctor`` must report a verdict, not traceback. Without the try in
    check_git_clean this raises once get_changed_files carries ``-z``."""
    repo = _repo(tmp_path)
    _commit_undecodable_path(repo)

    status, message, _remedy = doctor.check_git_clean(repo)

    assert status == doctor.STATUS_WARN
    assert "not valid utf-8" in message


def test_the_loop_refuses_before_paying_for_an_iteration(tmp_path: Path) -> None:
    """The guard baseline is taken before iteration 1. A baseline that cannot
    be read is a refusal there, where nothing has been spent, and not a
    traceback after an agent has run."""

    class NeverRuns:
        def __init__(self) -> None:
            self.runs = 0

        @property
        def name(self) -> str:
            return "never"

        def run(
            self, prompt: str, cwd: Path | None = None, timeout: float | None = None
        ) -> Iterator[str]:
            self.runs += 1
            yield "line"

        @property
        def final_message(self) -> str | None:
            return None

        @property
        def usage_records(self) -> list[object]:
            return []

    repo = _repo(tmp_path)
    config = _guard_config(repo)
    _commit_undecodable_path(repo)
    agent = NeverRuns()
    out = io.StringIO()

    result = loop.run_loop(config, PlainUI(no_color=True, file=out), agent, repo)

    assert agent.runs == 0
    assert (result.completed, result.exit_code) == (False, 1)
    assert "not valid utf-8" in out.getvalue()
