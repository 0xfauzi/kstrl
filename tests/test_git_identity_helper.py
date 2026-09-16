"""The identity helper, measured on a machine with no git identity (#367).

Real git, real temporary repositories, no mocks. The identity-free
machine is built with ``monkeypatch.setenv`` rather than by touching any
configuration file of the developer's: ``GIT_CONFIG_GLOBAL`` names a
file under ``tmp_path`` and ``GIT_CONFIG_NOSYSTEM`` drops
``/etc/gitconfig``. Every git subprocess these tests spawn, including
the ones inside :func:`tests.helpers.gitrepo.set_identity`, inherits
that environment, so a helper that wrote ``--global`` would write to the
temp file and be caught rather than corrupting the machine it runs on.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import make_review_repo
from tests.helpers import gitrepo

#: What a machine with no git identity looks like. ``useConfigOnly``
#: turns off git's guess from the account name and the hostname, and
#: that guess is the whole reason a developer machine disagrees with a
#: runner.
NO_IDENTITY_CONFIG = "[user]\n\tuseConfigOnly = true\n"


@pytest.fixture
def no_ambient_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every git subprocess in this test to an empty identity,
    and prove the redirection bites before returning.

    Returns the file ``GIT_CONFIG_GLOBAL`` names, so a test can assert
    the helper did not write to it. The proof is the control every test
    below used to carry separately: a scratch repository this fixture
    builds and a commit into it that this environment must refuse. A
    green suite under a fixture that stopped redirecting anything would
    look identical to one under a working fixture, so the refusal is
    checked once here rather than duplicated in five test bodies.
    """
    config = tmp_path / "gitconfig-global"
    config.write_text(NO_IDENTITY_CONFIG, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    probe = tmp_path / "identity-probe"
    probe.mkdir()
    gitrepo.git_in(probe, "init", "-q")
    refused = subprocess.run(
        ["git", "-C", str(probe), "commit", "--allow-empty", "-m", "x"],
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0, refused.stdout
    assert "no name was given" in refused.stderr or "no email was given" in refused.stderr

    return config


def _local(repo: Path, key: str) -> str:
    """What ``repo``'s OWN config holds for ``key``, empty when unset.

    ``--local`` on purpose: a helper that wrote the identity globally, or
    that exported ``GIT_AUTHOR_NAME``, would satisfy every commit in this
    file and leave this empty. Not routed through
    :func:`tests.helpers.gitrepo.git_in`, which always raises on a
    non-zero exit and returns nothing: an unset key exits 1 and this
    needs the (empty) stdout rather than an exception.
    """
    return subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get", key],
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fresh_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    gitrepo.git_in(repo, "init", "-q", "-b", "main")
    return repo


class TestSetIdentitySurvivesAMachineWithNoGitIdentity:
    def test_a_repo_commits_only_after_set_identity(
        self,
        tmp_path: Path,
        no_ambient_identity: Path,
    ) -> None:
        """set_identity is what lets a fresh repository commit, under the
        identity a commit's own log records.

        The fixture already proved this environment refuses an
        identity-less commit; what is left to show is that
        ``set_identity`` is what changes that for a repository, and that
        the identity it commits under is the one the helper names.
        """
        repo = _fresh_repo(tmp_path)
        (repo / "a.txt").write_text("x\n", encoding="utf-8")
        gitrepo.git_in(repo, "add", "-A")

        gitrepo.set_identity(repo)
        gitrepo.git_in(repo, "commit", "-qm", "base")

        name, email = gitrepo.IDENTITY.values()
        logged = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%an%n%ae"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert logged == f"{name}\n{email}"

    def test_set_identity_writes_to_the_repository_and_nowhere_else(
        self,
        tmp_path: Path,
        no_ambient_identity: Path,
    ) -> None:
        """A ``--global`` helper would also make the commit above pass.

        It would also rewrite whatever file ``GIT_CONFIG_GLOBAL`` names,
        which on a machine without this fixture is the developer's own
        configuration. Both halves are asserted: the keys are in the
        repository's own config, and the global file is byte-identical.
        """
        repo = _fresh_repo(tmp_path)

        gitrepo.set_identity(repo)

        # Both keys, always: a helper down to one key would still pass a
        # loop that only asserts what IDENTITY still holds. This pins the
        # count so a one-key IDENTITY dict is caught here rather than only
        # by the refusal tests elsewhere in this file.
        assert len(gitrepo.IDENTITY) == 2
        for key, value in gitrepo.IDENTITY.items():
            assert _local(repo, key) == value
        assert no_ambient_identity.read_text(encoding="utf-8") == NO_IDENTITY_CONFIG

    def test_a_worktree_of_that_repo_commits_too(
        self,
        tmp_path: Path,
        no_ambient_identity: Path,
    ) -> None:
        """The claim the helper's docstring makes about worktrees, measured."""
        repo = _fresh_repo(tmp_path)
        gitrepo.set_identity(repo)
        (repo / "a.txt").write_text("x\n", encoding="utf-8")
        gitrepo.git_in(repo, "add", "-A")
        gitrepo.git_in(repo, "commit", "-qm", "base")

        work = tmp_path / "wt"
        gitrepo.git_in(repo, "worktree", "add", "-q", str(work), "-b", "work")
        (work / "b.txt").write_text("y\n", encoding="utf-8")
        gitrepo.git_in(work, "add", "-A")
        gitrepo.git_in(work, "commit", "-qm", "work")

    def test_set_identity_refuses_a_path_that_is_not_a_repo(
        self,
        tmp_path: Path,
        no_ambient_identity: Path,
    ) -> None:
        """A helper that quietly does nothing on a wrong path is the same
        defect as the one this module exists to close.

        The directory exists but was never ``git init``-ed. ``git_in``
        runs with ``cwd=repo`` rather than ``-C repo``, so a path that
        does not exist at all fails one layer earlier, as a Python
        ``FileNotFoundError`` from the OS before git ever runs; a path
        that exists but is not a repository is what reaches git itself
        and is the case this helper has to refuse.
        """
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        with pytest.raises(subprocess.CalledProcessError):
            gitrepo.set_identity(not_a_repo)


class TestGitInEnforcesItsTimeout:
    def test_a_hanging_git_is_killed_rather_than_waited_on_forever(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The 30-second timeout is a fuse, not a comment.

        A stub named ``git`` put first on ``PATH`` stands in for a git
        that never returns in time. ``GIT_TIMEOUT_SECONDS`` is lowered
        so this test does not itself wait out the real 30 seconds to
        see the fuse fire; deleting ``timeout=GIT_TIMEOUT_SECONDS`` from
        ``git_in`` turns this from a fast failure into the subprocess
        outliving the test, which is the shape CLAUDE.md names as worse
        than a guard that merely goes silently green.
        """
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub_git = stub_dir / "git"
        stub_git.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
        stub_git.chmod(0o755)

        monkeypatch.setenv("PATH", str(stub_dir) + os.pathsep + os.environ["PATH"])
        monkeypatch.setattr(gitrepo, "GIT_TIMEOUT_SECONDS", 1)

        with pytest.raises(subprocess.TimeoutExpired):
            gitrepo.git_in(tmp_path, "status")


class TestTheSuitesOwnRepoBuilderNeedsNoAmbientIdentity:
    def test_make_review_repo_builds_on_a_machine_with_no_identity(
        self,
        tmp_path: Path,
        no_ambient_identity: Path,
    ) -> None:
        """End to end over the fixture the review tests build repos with.

        The environment's refusal without an identity is proved by the
        fixture now; this test is the positive half, that the suite's own
        repository builder configures one and can commit.
        """
        repo = make_review_repo(tmp_path / "repo")
        assert repo.stat.files >= 1
