"""ALLOWED_PATHS enforcement for Ralph."""

from __future__ import annotations

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


def enforce_allowed_paths(
    config: KstrlConfig,
    ui: UI,
    cwd: Path | None = None,
    interaction: InteractionChannel | None = None,
    ignored_paths: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Enforce ALLOWED_PATHS after an iteration.

    Returns (ok, violations) where:
    - ok is True if enforcement passed (no violations or resolved)
    - violations is list of disallowed files

    ``ignored_paths`` is the caller's exact set of harness-owned outputs
    for the active run.

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
    changed = git.get_changed_files(cwd)
    violations = check_violations(
        changed, config.allowed_paths, ignored_paths,
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

    response = channel.request(PromptRequest(
        kind=PromptKind.GUARD,
        header="Disallowed changes detected. What would you like to do?",
        options=("Quit", "Revert and continue", "Continue anyway"),
        default=0,
    ))
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
            if git.is_file_tracked(f, cwd):
                git.restore_file(f, cwd)
                ui.info(f"  Restored: {f}")
            else:
                git.delete_untracked(f, cwd)
                ui.info(f"  Deleted: {f}")
        return True, []
    else:
        # Continue anyway
        ui.warn("Continuing with disallowed changes")
        return True, violations
