"""The git and gh operations.

The base branch, the diff, the worktree files, the PR state and the
version stamp.
"""

from __future__ import annotations

from kstrl.git.git import get_head_sha, get_origin_slug, git_write_paths
from kstrl.git.git_base import (
    detect_base_branch,
    fetch_base_branch,
    resolve_base_branch,
    resolve_base_ref,
)
from kstrl.git.pr_state import GH_POLL_TIMEOUT, fetch_pr_state, pr_mergeable
from kstrl.git.version import kstrl_version, stamp_label

__all__ = [
    "GH_POLL_TIMEOUT",
    "detect_base_branch",
    "fetch_base_branch",
    "fetch_pr_state",
    "get_head_sha",
    "get_origin_slug",
    "git_write_paths",
    "kstrl_version",
    "pr_mergeable",
    "resolve_base_branch",
    "resolve_base_ref",
    "stamp_label",
]
