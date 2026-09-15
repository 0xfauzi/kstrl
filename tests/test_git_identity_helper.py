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
    """Redirect every git subprocess in this test to an empty identity.

    Returns the file ``GIT_CONFIG_GLOBAL`` names, so a test can assert
    the helper did not write to it.
    """
    config = tmp_path / "gitconfig-global"
    config.write_text(NO_IDENTITY_CONFIG, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return config


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _local(repo: Path, key: str) -> str:
    """What ``repo``'s OWN config holds for ``key``, empty when unset.

    ``--local`` on purpose: a helper that wrote the identity globally, or
    that exported ``GIT_AUTHOR_NAME``, would satisfy every commit in this
    file and leave this empty. The key arrives as a constant from the
    helper rather than spelled here, so ``tests/test_git_identity.py``'s
    first census stays exactly the helper.
    """
    return _git(repo, "config", "--local", "--get", key, check=False).stdout.strip()


def _fresh_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    return repo


class TestSetIdentitySurvivesAMachineWithNoGitIdentity:
    def test_a_repo_commits_only_after_set_identity(
        self,
        tmp_path: Path,
        no_ambient_identity: Path,
    ) -> None:
        """The control and the claim in one function: either half alone
        proves nothing, because a green commit is also what an ambient
        identity gives."""
        repo = _fresh_repo(tmp_path)
        (repo / "a.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "-A")

        refused = _git(repo, "commit", "-qm", "base", check=False)
        assert refused.returncode != 0, refused.stdout
        assert "no name was given" in refused.stderr or "no email was given" in refused.stderr

        gitrepo.set_identity(repo)
        _git(repo, "commit", "-qm", "base")
        assert _git(repo, "log", "-1", "--format=%an%n%ae").stdout.strip() == (
            f"{gitrepo.IDENTITY_NAME}\n{gitrepo.IDENTITY_EMAIL}"
        )

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

        assert _local(repo, gitrepo.IDENTITY_KEY_NAME) == gitrepo.IDENTITY_NAME
        assert _local(repo, gitrepo.IDENTITY_KEY_EMAIL) == gitrepo.IDENTITY_EMAIL
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
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

        work = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", str(work), "-b", "work")
        (work / "b.txt").write_text("y\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "work")

    def test_set_identity_refuses_a_path_that_is_not_a_repo(
        self,
        tmp_path: Path,
        no_ambient_identity: Path,
    ) -> None:
        """A helper that quietly does nothing on a wrong path is the same
        defect as the one this module exists to close."""
        with pytest.raises(subprocess.CalledProcessError):
            gitrepo.set_identity(tmp_path / "not-a-repo")


class TestTheSuitesOwnRepoBuilderNeedsNoAmbientIdentity:
    def test_make_review_repo_builds_on_a_machine_with_no_identity(
        self,
        tmp_path: Path,
        no_ambient_identity: Path,
    ) -> None:
        """End to end over the fixture the review tests build repos with.

        The first half is the control: a repository this fixture did not
        build cannot commit at all in this environment, so a green second
        half is the fixture's doing and not the machine's.
        """
        bare = tmp_path / "bare"
        bare.mkdir()
        _git(bare, "init", "-q")
        refused = _git(bare, "commit", "--allow-empty", "-m", "x", check=False)
        assert refused.returncode != 0, refused.stdout

        repo = make_review_repo(tmp_path / "repo")
        assert repo.stat.files >= 1
