"""Git operations for Ralph - branch management and path enforcement."""

from __future__ import annotations

import subprocess
from pathlib import Path


def is_git_repo(cwd: Path | None = None) -> bool:
    """Check if the current directory is inside a git work tree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def current_branch(cwd: Path | None = None) -> str:
    """Return the current branch name, or empty string if detached/not a repo."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def branch_exists(branch: str, cwd: Path | None = None) -> bool:
    """Check if a local branch exists."""
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        cwd=cwd,
    )
    return result.returncode == 0


def checkout_branch(branch: str, create: bool = False, cwd: Path | None = None) -> str:
    """Switch to a branch, optionally creating it. Returns output message."""
    if not branch:
        return "No branch specified, skipping checkout"

    cur = current_branch(cwd)
    if cur == branch:
        return f"Already on {branch}"

    if branch_exists(branch, cwd):
        cmd = ["git", "checkout", branch]
        action = f"Switched to existing branch {branch}"
    elif create:
        cmd = ["git", "checkout", "-b", branch]
        action = f"Created and switched to branch {branch}"
    else:
        return f"Branch {branch} does not exist (use create=True to create)"

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        return f"Failed to checkout {branch}: {result.stderr.strip()}"
    return action


def get_changed_files(cwd: Path | None = None) -> list[str]:
    """Return all changed files: unstaged + staged + untracked."""
    files: set[str] = set()

    for cmd in [
        ["git", "diff", "--name-only"],
        ["git", "diff", "--name-only", "--cached"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                if line.strip():
                    files.add(line.strip())

    return sorted(files)


def path_is_allowed(path: str, allowed_paths: list[str]) -> bool:
    """Check if a file path is allowed by the ALLOWED_PATHS rules.

    Rules:
    - Exact file match: "scripts/ralph/codebase_map.md"
    - Directory prefix (ending with /): "scripts/ralph/"
    """
    for allowed in allowed_paths:
        allowed = allowed.strip()
        if not allowed:
            continue
        # Directory prefix rule
        if allowed.endswith("/"):
            if path.startswith(allowed):
                return True
            continue
        # Exact match rule
        if path == allowed:
            return True
    return False


def find_disallowed_files(
    allowed_paths: list[str], cwd: Path | None = None
) -> list[str]:
    """Return list of changed files that are not in the allowed paths."""
    if not allowed_paths:
        return []
    changed = get_changed_files(cwd)
    return [f for f in changed if not path_is_allowed(f, allowed_paths)]


def revert_files(files: list[str], cwd: Path | None = None) -> list[str]:
    """Revert disallowed file changes. Returns list of revert messages."""
    messages: list[str] = []
    for f in files:
        # Check if file is tracked
        check = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", f],
            capture_output=True,
            cwd=cwd,
        )
        if check.returncode == 0:
            # Tracked file: restore it
            subprocess.run(
                ["git", "restore", "--staged", "--worktree", "--", f],
                capture_output=True,
                cwd=cwd,
            )
            messages.append(f"Reverted {f}")
        else:
            # Untracked file: delete it
            file_path = Path(cwd or ".") / f
            if file_path.exists():
                file_path.unlink()
                messages.append(f"Removed {f}")

    return messages
