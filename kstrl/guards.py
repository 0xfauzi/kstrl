"""ALLOWED_PATHS enforcement for kstrl."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal

from kstrl import git
from kstrl.interaction import (
    InteractionChannel,
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from kstrl.statedir import STATE_DIR_NAME

if TYPE_CHECKING:
    from pathlib import Path

    from kstrl.config import KstrlConfig
    from kstrl.ui.base import UI


#: A repo-relative path inside kstrl's own state directory. Matched
#: rather than string-compared so ``.kstrl-backup/x`` and ``sub/.kstrl/x``
#: do not qualify: only the state directory AT THE WALK ROOT is kstrl's.
_WITHIN_STATE_DIR = re.compile(rf"^{re.escape(STATE_DIR_NAME)}/")


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


# The ways an allowedPaths entry can be unmatchable. A closed alias
# rather than bare strings: this set grew by one ("whitespace") in the
# #268 review and that edit had to touch three files by hand, which is
# exactly what an explicit type prevents. Every consumer maps it through
# a dict keyed by this alias, and a test asserts the keys agree.
ScopeHazard = Literal["absolute", "traversal", "root", "whitespace"]


def scope_entry_hazard(entry: str) -> ScopeHazard | None:
    """Why ``path_is_allowed`` can never match ``entry``, or None.

    Returns a ``ScopeHazard`` code and leaves the sentence to the
    caller, because the two callers address different people:
    ``decompose._validate_allowed_path_entry`` addresses the architect
    inside a retry loop, ``factory._preflight_component_scope``
    addresses the operator before a run starts. The predicate is shared
    so a hazard added for one is caught for the other; only the wording
    forks.

    An entry that cannot match is worse than a missing one: it reads as
    authorisation and grants none, so every file it was meant to allow
    is reported outside scope.

    Judged on the RAW entry, which is what ``path_is_allowed`` matches
    on (#268 review). Classifying the stripped form let `` src/`` pass as
    safe while authorising nothing, in a predicate whose entire job is
    catching exactly that. Surrounding whitespace is therefore its own
    hazard, and it is reported LAST so a `` /abs/`` entry names the more
    substantive problem first.
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
    if entry != stripped:
        return "whitespace"
    return None


def check_violations(
    changed_files: set[str],
    allowed_paths: list[str],
    ignored_paths: list[str] | None = None,
) -> list[str]:
    """Check for files that violate ALLOWED_PATHS.

    ``ignored_paths`` is harness-owned only: the exact files kstrl
    requires this invocation's agent to write
    (``config.component_harness_paths``, #264) plus the entries kstrl
    itself creates under its state directory
    (``statedir.state_dir_carve_out``, #274).

    Still never a blanket state-directory bypass. ``.kstrl/`` as a bare
    prefix is refused everywhere - ``decompose._ALLOWED_PATHS_EXCLUDE``
    will not let an architect authorise it - and the state carve-out
    names only the subtrees and files kstrl writes, so a path the
    harness does not create (``.kstrl/notes.md``) is still a violation.

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
) -> bool:
    """Undo one out-of-scope change. False when it was REFUSED.

    With a baseline the revert source is the BASELINE COMMIT, not the
    index: the violation may already be committed, and restoring from
    the index would be a no-op that reports success while leaving the
    file exactly as the agent left it - the guard would then clear the
    violation and the next iteration would re-detect it forever. A file
    that did not exist at the baseline is dropped from the index and
    deleted, so it disappears from the delta the way a revert should.

    Nothing under kstrl's own state directory is ever reverted (#274
    review). The paths that reach here are by definition the ones the
    carve-out does NOT cover, and ``statedir.STATE_NOT_CARVED`` keeps
    exactly the authority-carrying entries countable: the work queue,
    evolution proposals, the autonomy level, the pause marker. Deleting
    an untracked file is how this function disposes of a violation, and
    ``git.delete_untracked`` recurses into directories - so without this
    refusal, making those paths VISIBLE to the guard would also hand
    them to a deleter, and an operator choosing "Revert and continue"
    could destroy the pause marker they had just written. Reporting a
    file and destroying it are different powers; this function only has
    the second, so it declines the cases where only the first is wanted.
    """
    if _WITHIN_STATE_DIR.match(file):
        ui.warn(f"  Refused (kstrl state, reported not reverted): {file}")
        return False
    if baseline is not None and baseline.head is not None:
        if git.restore_file_from(file, baseline.head, cwd):
            ui.info(f"  Restored: {file}")
        else:
            git.remove_from_index(file, cwd)
            git.delete_untracked(file, cwd)
            ui.info(f"  Deleted: {file}")
        return True
    if git.is_file_tracked(file, cwd):
        git.restore_file(file, cwd)
        ui.info(f"  Restored: {file}")
    else:
        git.delete_untracked(file, cwd)
        ui.info(f"  Deleted: {file}")
    return True


def _revert_violations(
    violations: list[str],
    ui: UI,
    cwd: Path | None,
    baseline: git.WorkspaceBaseline | None,
) -> list[str]:
    """Revert what may be reverted; return what was refused.

    Its own function so ``enforce_allowed_paths`` keeps one statement
    here rather than a branch: the refusals have to travel back to the
    caller, because reporting "reverted" for a file still on disk is the
    silent-success failure this whole guard exists to avoid.
    """
    return [f for f in violations if not _revert_violation(f, ui, cwd, baseline)]


def _report_violations(
    ui: UI,
    allowed_paths: list[str],
    ignored_paths: list[str] | None,
    violations: list[str],
) -> None:
    """Print the scope failure, authored list and carve-out apart.

    The two lists never merge (#264, #274). An operator reading a scope
    failure has to be able to tell what THEY authorised from what the
    harness added on their behalf, and a retry agent must not read
    kstrl's own PRD, progress log or state directory as the thing it has
    to stop writing - that is the one instruction it cannot obey and
    still pass ``prd_stories``. Mirrors ``verify._diff_scope_details``,
    which does the same for the Phase 1 half of the same question.

    The parenthetical repeats ``verify._diff_scope_details`` and
    ``factory._run_component`` word for word. Deliberate: the three
    scope failures must read identically wherever they are caught, and
    the claim itself has to be the same claim, because a retry agent
    told "already in scope" at one guard and "not part of
    ALLOWED_PATHS" at another has been given two different instructions
    about the same files.
    """
    ui.channel_header("GUARD", "Disallowed changes")
    ui.kv("ALLOWED_PATHS", ", ".join(allowed_paths))
    if ignored_paths:
        ui.kv("HARNESS_PATHS", ", ".join(ignored_paths))
        ui.info(
            "  (kstrl's own files, already in scope, no need to widen allowedPaths)",
        )
    ui.info("")
    ui.info("Disallowed files:")
    for f in violations:
        ui.info(f"    - {f}")


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

    ``ignored_paths`` is the harness-owned set for the active run: the
    caller's per-invocation files plus kstrl's own state directory. It
    is reported separately from ``config.allowed_paths`` in the failure
    block, never folded into it.

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

    _report_violations(ui, config.allowed_paths, ignored_paths, violations)

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
        # Revert. Anything refused (kstrl's own state) is reported back
        # unreverted rather than silently counted as handled.
        ui.info("Reverting disallowed changes...")
        refused = _revert_violations(violations, ui, cwd, baseline)
        return not refused, refused
    else:
        # Continue anyway
        ui.warn("Continuing with disallowed changes")
        return True, violations
