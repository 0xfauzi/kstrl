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
siblings would also work for a plain subprocess, and it would be a smaller
change. It is not what this does, because `kstrl.verify.scrubbed_subprocess_env`
passes an allowlisted environment to its children and none of the four names is
on the allowlist, so an environment-carried identity does not reach the commit
`kstrl/verify.py` makes for the dead-code cleanup. Repository configuration
reaches every caller, including a worktree, which shares its repository's
config. `tests/test_git_identity_helper.py` measures the worktree half.

BOTH KEYS, ALWAYS. git refuses the commit for a missing name and for a missing
email separately, and the second message only appears once the first is fixed,
so a half-fix reads as a fix until CI says otherwise.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: What every temp repository in this suite commits as. `.invalid` is the
#: reserved TLD from RFC 2606, so the address cannot reach anybody.
IDENTITY_NAME = "kstrl tests"
IDENTITY_EMAIL = "kstrl@test.invalid"

#: The config keys themselves. Named constants so the two spellings this
#: file is pinned at by `tests/test_git_identity.py` are these two lines
#: and nothing else, and so a test can ask for a key without spelling it.
IDENTITY_KEY_NAME = "user.name"
IDENTITY_KEY_EMAIL = "user.email"


def set_identity(repo: Path) -> None:
    """Set both identity keys on ``repo``.

    Raises `subprocess.CalledProcessError` if `repo` is not a repository,
    because a helper that quietly does nothing on a wrong path is the same
    defect as the one this module exists to close.
    """
    for key, value in (
        (IDENTITY_KEY_NAME, IDENTITY_NAME),
        (IDENTITY_KEY_EMAIL, IDENTITY_EMAIL),
    ):
        subprocess.run(
            ["git", "-C", str(repo), "config", key, value],
            check=True,
            capture_output=True,
            text=True,
        )
