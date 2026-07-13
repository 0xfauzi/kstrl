"""Git operations for Ralph."""

from __future__ import annotations

import re as _re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ralph_py.ui.base import UI

DEFAULT_TIMEOUT = 30.0

# Network fetches get a longer budget than local plumbing calls.
FETCH_TIMEOUT = 120.0


def resolve_base_ref(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Resolve the ref that worktree cuts and diffs measure against.

    Returns ``origin/<base_branch>`` when that remote-tracking ref
    exists, else ``base_branch`` unchanged (local-only repos). This is
    the single place that decides the base ref (R0.2): squash merges
    rewrite SHAs, so a stale local base produces phantom diffs via
    ``base...HEAD``; cutting AND diffing against ``origin/<base>``
    removes the class. Never mutates any ref or the checkout.
    """
    if base_branch.startswith("origin/"):
        return base_branch
    try:
        result = subprocess.run(
            [
                "git", "rev-parse", "--verify", "--quiet",
                f"refs/remotes/origin/{base_branch}",
            ],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return f"origin/{base_branch}"
    except (subprocess.TimeoutExpired, OSError):
        pass
    return base_branch


def fetch_base_branch(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = FETCH_TIMEOUT,
) -> str | None:
    """Update ``refs/remotes/origin/<base_branch>`` via ``git fetch``.

    Replaces the old ``git pull`` (R0.2/H-1): fetch touches only the
    remote-tracking ref, never the operator's checked-out branch or the
    local base branch. Returns an error message, or None on success.
    """
    try:
        # "--" keeps a crafted base branch out of option position (R0.6).
        result = subprocess.run(
            ["git", "fetch", "--", "origin", base_branch],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"git fetch origin {base_branch} timed out after {timeout}s"
    if result.returncode != 0:
        return (
            result.stderr.strip()
            or f"git fetch origin {base_branch} failed"
        )
    return None


def is_git_repo(path: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def get_repo_root(path: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> Path | None:
    """Get the root directory of the git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


def branch_exists(
    branch: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Check if a branch exists."""
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def checkout_branch(
    branch: str,
    ui: UI,
    cwd: Path | None = None,
    source: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Checkout or create a branch.

    Returns True on success, False on failure.
    """
    source_suffix = f" ({source})" if source else ""

    try:
        if branch_exists(branch, cwd, timeout=timeout):
            ui.info(f"Branch: checking out existing branch {branch}{source_suffix}")
            # Trailing "--" pins the argument as a ref, never a pathspec
            # (R0.6). Note "--" cannot stop git from parsing a leading
            # "-" ref as an option here; that shape is rejected upstream
            # by manifest.validate_branch_name.
            result = subprocess.run(
                ["git", "checkout", branch, "--"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            ui.info(f"Branch: creating branch {branch}{source_suffix}")
            result = subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        ui.err(f"Branch checkout timed out after {timeout}s")
        return False

    output = result.stdout + result.stderr
    for line in output.strip().splitlines():
        if line:
            ui.stream_line("GIT", line)

    return result.returncode == 0


def get_changed_files(
    cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
) -> set[str]:
    """Get all changed files (staged, unstaged, and untracked).

    Returns paths relative to repo root.
    """
    files: set[str] = set()

    try:
        # Unstaged changes
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            files.update(
                line.strip() for line in result.stdout.splitlines() if line.strip()
            )

        # Staged changes
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            files.update(
                line.strip() for line in result.stdout.splitlines() if line.strip()
            )

        # Untracked files
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            files.update(
                line.strip() for line in result.stdout.splitlines() if line.strip()
            )
    except subprocess.TimeoutExpired:
        pass

    return files


def restore_file(
    file: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Restore a tracked file (staged and working tree)."""
    try:
        result = subprocess.run(
            ["git", "restore", "--staged", "--worktree", "--", file],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def delete_untracked(file: str, cwd: Path | None = None) -> bool:
    """Delete an untracked file."""
    try:
        path = Path(cwd or ".") / file
        if path.exists():
            if path.is_dir():
                import shutil
                shutil.rmtree(path)
            else:
                path.unlink()
        return True
    except Exception:
        return False


def is_file_tracked(
    file: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Check if a file is tracked by git."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", file],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def get_diff_names(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[str]:
    """Get list of changed file names compared to a base branch.

    The base is resolved through :func:`resolve_base_ref` so diffs
    measure against ``origin/<base>`` whenever a remote exists (R0.2).
    """
    base_ref = resolve_base_ref(base_branch, cwd, timeout)
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD", "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return [
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip()
            ]
    except subprocess.TimeoutExpired:
        pass
    return []


def get_diff_content(
    base_branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Get full diff content compared to a base branch.

    The base is resolved through :func:`resolve_base_ref` so diffs
    measure against ``origin/<base>`` whenever a remote exists (R0.2).
    """
    base_ref = resolve_base_ref(base_branch, cwd, timeout)
    try:
        result = subprocess.run(
            ["git", "diff", f"{base_ref}...HEAD", "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout
    except subprocess.TimeoutExpired:
        pass
    return ""


# Shared budget for diff content injected into LLM prompts. Centralized
# here so review / security / knowledge prompts truncate to the same
# limit; if the LLM context window changes, edit one place.
DEFAULT_PROMPT_DIFF_CHAR_LIMIT = 50_000


def truncate_diff_for_prompt(
    diff_content: str, limit: int = DEFAULT_PROMPT_DIFF_CHAR_LIMIT,
) -> str:
    """Truncate a diff string for inclusion in an LLM prompt.

    Appends a single trailing line noting the truncation so the reviewer
    knows it isn't seeing the full diff.
    """
    if len(diff_content) <= limit:
        return diff_content
    return diff_content[:limit] + f"\n... (diff truncated at {limit // 1000}KB)"


# E2: regex matches a Self-Critique block in a diff. Used by the
# reviewer-prep step to remove the engineer's self-reported failure
# modes from what the reviewer sees, so the reviewer is not biased
# toward "the implementer already thought of that" and skips checking.
_SELF_CRITIQUE_BLOCK_RE = _re.compile(
    r"""
    \+\#{2,3}\s+Self[-\s]Critique[\s\S]*?       # heading + content
    (?=                                          # stop before:
        ^\+\#{1,6}\s                             #   any other heading
      | ^\+---\s*$                               #   ---  separator
      | ^[^+]                                    #   non-add line
      | \Z                                       #   end of string
    )
    """,
    _re.MULTILINE | _re.VERBOSE | _re.IGNORECASE,
)


def strip_self_critique_from_diff(diff_content: str) -> str:
    """Remove the engineer's Self-Critique block from a diff before
    showing it to the reviewer.

    The Self-Critique block is the engineer's self-reported list of
    failure modes (verify.py mandates >=3 bullets). If the reviewer
    sees it inline in progress.txt's diff, the reviewer is biased to
    think those failure modes are already handled. The reviewer should
    arrive at its concerns independently.

    Returns the diff with the block stripped; if no block is found,
    returns the input unchanged.
    """
    return _SELF_CRITIQUE_BLOCK_RE.sub("", diff_content)


def merge_branch(
    branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Merge a branch into the current branch (no-edit)."""
    try:
        # "--" makes a crafted branch value an invalid ref instead of a
        # git option (R0.6).
        result = subprocess.run(
            ["git", "merge", "--no-edit", "--", branch],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def create_branch_from(
    branch_name: str,
    base: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Create and checkout a new branch from a base ref."""
    try:
        # Trailing "--" pins *base* as a ref, never a pathspec (R0.6).
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name, base, "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def delete_branch(
    branch_name: str,
    cwd: Path | None = None,
    force: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Delete a local branch."""
    flag = "-D" if force else "-d"
    try:
        # "--" makes a crafted branch value an unknown ref instead of a
        # git option (R0.6).
        result = subprocess.run(
            ["git", "branch", flag, "--", branch_name],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def checkout_existing(
    branch: str,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Checkout an existing branch without creating it."""
    try:
        # Trailing "--" pins the argument as a ref, never a pathspec
        # (R0.6); leading "-" shapes are rejected upstream by
        # manifest.validate_branch_name.
        result = subprocess.run(
            ["git", "checkout", branch, "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
