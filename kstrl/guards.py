"""ALLOWED_PATHS enforcement for kstrl."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from kstrl import git
from kstrl.interaction import (
    InteractionChannel,
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)

if TYPE_CHECKING:
    from pathlib import Path

    from kstrl.config import KstrlConfig
    from kstrl.ui.base import UI


def path_is_allowed(path: str, allowed_paths: list[str]) -> bool:
    """Check if a path is allowed.

    Matching rules (from shell script):
    - Exact match: "foo/bar.txt" matches only that file
    - Directory prefix: "foo/" matches anything under foo/
    """
    for allowed in allowed_paths:
        if allowed.endswith("/"):
            # Directory prefix
            if path.startswith(allowed) or path + "/" == allowed:
                return True
        else:
            # Exact match
            if path == allowed:
                return True
    return False


def scope_entry_hazard(entry: str) -> str | None:
    """Why ``path_is_allowed`` can never match ``entry``, or None.

    Returns a reason CODE - ``"root"``, ``"absolute"`` or
    ``"traversal"`` - and leaves the sentence to the caller, because the
    two callers address different people:
    ``decompose._validate_allowed_path_entry`` addresses the architect
    inside a retry loop, ``factory._preflight_component_scope``
    addresses the operator before a run starts. The predicate is shared
    so a hazard added for one is caught for the other; only the wording
    forks.

    An entry that cannot match is worse than a missing one: it reads as
    authorisation and grants none, so every file it was meant to allow
    is reported outside scope.
    """
    stripped = entry.strip()
    if stripped.startswith("/"):
        return "root" if stripped.rstrip("/") == "" else "absolute"
    if ".." in PurePosixPath(stripped).parts:
        return "traversal"
    normalized = stripped
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.rstrip("/") in ("", "."):
        return "root"
    return None


def check_violations(
    changed_files: set[str],
    allowed_paths: list[str],
    ignored_paths: list[str] | None = None,
) -> list[str]:
    """Check for files that violate ALLOWED_PATHS.

    ``ignored_paths`` contains exact harness-owned files or directory
    prefixes for this invocation, never a blanket state-directory bypass.
    Returns list of disallowed files.
    """
    if not allowed_paths:
        return []

    violations = []
    for file in sorted(changed_files):
        if ignored_paths and path_is_allowed(file, ignored_paths):
            continue
        if not path_is_allowed(file, allowed_paths):
            violations.append(file)
    return violations


def _revert_violation(
    file: str,
    ui: UI,
    cwd: Path | None,
    baseline: git.WorkspaceBaseline | None,
) -> None:
    """Undo one out-of-scope change.

    With a baseline the revert source is the BASELINE COMMIT, not the
    index: the violation may already be committed, and restoring from
    the index would be a no-op that reports success while leaving the
    file exactly as the agent left it - the guard would then clear the
    violation and the next iteration would re-detect it forever. A file
    that did not exist at the baseline is dropped from the index and
    deleted, so it disappears from the delta the way a revert should.
    """
    if baseline is not None and baseline.head is not None:
        if git.restore_file_from(file, baseline.head, cwd):
            ui.info(f"  Restored: {file}")
        else:
            git.remove_from_index(file, cwd)
            git.delete_untracked(file, cwd)
            ui.info(f"  Deleted: {file}")
        return
    if git.is_file_tracked(file, cwd):
        git.restore_file(file, cwd)
        ui.info(f"  Restored: {file}")
    else:
        git.delete_untracked(file, cwd)
        ui.info(f"  Deleted: {file}")


def enforce_allowed_paths(
    config: KstrlConfig,
    ui: UI,
    cwd: Path | None = None,
    interaction: InteractionChannel | None = None,
    ignored_paths: list[str] | None = None,
    baseline: git.WorkspaceBaseline | None = None,
) -> tuple[bool, list[str]]:
    """Enforce ALLOWED_PATHS after an iteration.

    Returns (ok, violations) where:
    - ok is True if enforcement passed (no violations or resolved)
    - violations is list of disallowed files

    ``ignored_paths`` is the caller's exact set of harness-owned outputs
    for the active run.

    ``baseline`` is the workspace as it stood before the agent started
    (``git.capture_workspace_baseline``). With one, the guard judges the
    agent's COMPLETE delta since that point, commits included, and
    subtracts files that were already dirty. Without one it keeps the
    historical index+worktree-only view, which is blind to anything the
    agent committed (R8 review finding 4) - a default preserved so
    direct callers that never captured a baseline behave as before.

    In non-interactive mode, returns (False, violations) if any violations.
    In interactive mode, prompts user for action.
    """
    # Skip if no allowed_paths configured
    if not config.allowed_paths:
        return True, []

    # Skip if not in git repo
    if not git.is_git_repo(cwd):
        return True, []

    # Get changed files
    changed = (
        git.get_changed_files_since(baseline, cwd)
        if baseline is not None
        else git.get_changed_files(cwd)
    )
    violations = check_violations(
        changed,
        config.allowed_paths,
        ignored_paths,
    )

    if not violations:
        return True, []

    # Display violations
    ui.channel_header("GUARD", "Disallowed changes")
    ui.kv("ALLOWED_PATHS", ", ".join(config.allowed_paths))
    ui.info("")
    ui.info("Disallowed files:")
    for f in violations:
        ui.info(f"    - {f}")

    if not config.interactive:
        ui.err(
            "Disallowed changes detected. "
            "Set INTERACTIVE=1 to review/revert, or clear ALLOWED_PATHS to disable enforcement."
        )
        return False, violations

    # Interactive mode - prompt for action (PR A: through the seam)
    channel = interaction if interaction is not None else UiInteractionChannel(ui)
    if not channel.can_prompt():
        # Non-TTY in interactive mode - take default action (quit)
        ui.warn("Non-TTY in interactive mode, defaulting to quit")
        return False, violations

    response = channel.request(
        PromptRequest(
            kind=PromptKind.GUARD,
            header="Disallowed changes detected. What would you like to do?",
            options=("Quit", "Revert and continue", "Continue anyway"),
            default=0,
        )
    )
    if not response.answered:
        ui.warn("Prompt unavailable, defaulting to quit")
        return False, violations
    choice = response.choice

    if choice == 0:
        # Quit
        return False, violations
    elif choice == 1:
        # Revert
        ui.info("Reverting disallowed changes...")
        for f in violations:
            _revert_violation(f, ui, cwd, baseline)
        return True, []
    else:
        # Continue anyway
        ui.warn("Continuing with disallowed changes")
        return True, violations
