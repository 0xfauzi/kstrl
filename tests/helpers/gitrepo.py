"""The git identity every temp repository in this suite commits under (#367).

A repository created by `git init` has neither identity key of its own.
On a developer machine git supplies both, from `~/.gitconfig` or
failing that from the account name and the hostname, so a test that commits
into such a repository passes locally and fails on a runner, which has
neither. CI on PR #357 is where that was first seen. The fix there was two
`git config` lines in one test; this module is the same fix written once
instead of forty-two times, and `tests/test_git_identity.py` is what keeps it
that way.

ON THE REPOSITORY, NOT IN THE ENVIRONMENT. `GIT_AUTHOR_NAME` and its three
siblings would also work for a plain subprocess, and setting them once,
session-wide, would be a smaller change. It is rejected because a
session-wide ambient identity restores exactly the masking that caused
#357: every repository would commit whether or not it went through
`set_identity`, so no test would depend on the repository carrying its own
identity and there is nowhere left for a guard to stand. `HOME` is in fact
ON `kstrl.verify.scrubbed_subprocess_env`'s allowlist and does reach the
commit `kstrl/verify.py` makes for the dead-code cleanup: pointing `HOME`
at a config that carries an identity makes that commit succeed, measured
directly. Repository configuration reaches every caller too, including a
worktree, which shares its repository's config, without masking anything:
`tests/test_git_identity_helper.py` measures the worktree half.

BOTH KEYS, ALWAYS. git refuses the commit for a missing name and for a
missing email separately, and the second message only appears once the
first is fixed, so a half-fix reads as a fix until CI says otherwise.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: What every temp repository in this suite commits as. `.invalid` is the
#: reserved TLD from RFC 2606, so the address cannot reach anybody. One
#: mapping rather than four constants and a tuple, so the pair a commit
#: needs travels together everywhere it is used.
IDENTITY: dict[str, str] = {
    "user.name": "kstrl tests",
    "user.email": "kstrl@test.invalid",
}

#: How long a single git call this helper makes is allowed to run before it
#: is killed. A module constant rather than a literal inside `git_in`, so a
#: test can lower it with `monkeypatch.setattr` against a slow git stub and
#: prove the fuse still fires, without adding a `timeout=` parameter to
#: `git_in` that would touch every one of its existing callers.
GIT_TIMEOUT_SECONDS = 30


def git_in(repo: Path, *args: str) -> None:
    """Run ``git <args>`` with ``repo`` as the working directory.

    The one git runner the suite's fixtures share, moved here from
    ``tests/conftest.py`` so the identity helper and the repository
    builders that call it live in one module. Re-exported from
    ``tests.conftest`` unchanged, so its existing callers do not move.
    """
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def set_identity(repo: Path) -> None:
    """Set both identity keys on ``repo``.

    Raises `subprocess.CalledProcessError` if `repo` is not a repository,
    because a helper that quietly does nothing on a wrong path is the same
    defect as the one this module exists to close.
    """
    for key, value in IDENTITY.items():
        git_in(repo, "config", key, value)
