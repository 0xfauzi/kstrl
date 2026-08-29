"""Input hygiene for the names a manifest carries: component ids and branches.

Split out of ``manifest.py`` rather than left in it: these are pure string
rules that know nothing about a Component, a Manifest or a DAG, and keeping
them alongside the schema and the scheduler is what pushed that module past
the file-length gate.

Import them from HERE, not from ``kstrl.manifest``. ``manifest`` imports
both validators for its own use, but that is not a re-export: the project's
typecheck is ``mypy --strict``, which implies ``--no-implicit-reexport``, so
``from kstrl.manifest import validate_branch_name`` fails the gate with
"Module 'kstrl.manifest' does not explicitly export attribute".
"""

from __future__ import annotations

import re

# R0.6 input hygiene: component ids and branch names are LLM-emitted
# (architect output) and flow into filesystem paths
# (.kstrl/worktrees/<id>, scripts/kstrl/feature/<id>) and git argv
# (git worktree add, git push -u origin <branch>). Both are validated
# against conservative allowlists at every parse boundary. Rejection is
# deliberate - silent sanitizing would hide architect drift.
COMPONENT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_COMPONENT_ID_RE = re.compile(COMPONENT_ID_PATTERN)

# ASCII allowlist for branch names. Anything outside it (whitespace,
# ':', control characters, unicode dash confusables like U+2011) is
# rejected wholesale rather than enumerated.
_BRANCH_CHARSET_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_BRANCH_NAME_LENGTH = 200


def validate_component_id(comp_id: str) -> str | None:
    """Validate a component id, returning an error message or None.

    Component ids become path segments and branch segments, so the rules
    are strict: lowercase alphanumeric start, then letters/digits/./_/-
    only (max 64 chars total), no '..' sequence, no '.'/'.lock' suffix
    ('<id>.lock' would collide with the worktree lock file for id
    '<id>', and git refuses refs ending in '.' or '.lock').

    Error messages state the rule so the decompose retry loop can feed
    them back to the architect verbatim.
    """
    if not comp_id:
        return "component id must be a non-empty string"
    if not _COMPONENT_ID_RE.match(comp_id):
        return (
            f"component id {comp_id!r} is invalid: ids must match "
            f"{COMPONENT_ID_PATTERN} - start with a lowercase letter or "
            "digit, contain only lowercase letters, digits, '.', '_', "
            "'-', and be at most 64 characters (no '/', no spaces, no "
            "uppercase, ASCII only); e.g. 'auth-service'"
        )
    if ".." in comp_id:
        return f"component id {comp_id!r} is invalid: '..' is not allowed"
    if comp_id.endswith("."):
        return f"component id {comp_id!r} is invalid: must not end with '.'"
    if comp_id.endswith(".lock"):
        return f"component id {comp_id!r} is invalid: must not end with '.lock'"
    return None


def validate_branch_name(branch: str) -> str | None:
    """Validate a git branch name, returning an error message or None.

    Branch names reach git argv in ref position (git push, git worktree
    add, git merge). The rules reject option injection (leading '-'),
    traversal ('..'), whitespace, ':', and unicode lookalikes via an
    ASCII allowlist, while accepting the kstrl/factory/<id> pattern and
    ordinary user branches.
    """
    if not branch:
        return "branch name must be a non-empty string"
    if len(branch) > MAX_BRANCH_NAME_LENGTH:
        return f"branch name is too long ({len(branch)} chars, max {MAX_BRANCH_NAME_LENGTH})"
    if not _BRANCH_CHARSET_RE.match(branch):
        return (
            f"branch name {branch!r} contains disallowed characters: only "
            "ASCII letters, digits, '.', '_', '/', '-' are allowed "
            "(no whitespace, no ':', no non-ASCII characters)"
        )
    if branch.startswith("-"):
        return (
            f"branch name {branch!r} must not start with '-' "
            "(git would parse it as a command-line option)"
        )
    if ".." in branch:
        return f"branch name {branch!r} must not contain '..'"
    if branch.startswith("/") or branch.endswith("/") or "//" in branch:
        return (
            f"branch name {branch!r} must not have empty path segments "
            "(leading '/', trailing '/', or '//')"
        )
    if any(seg.startswith(".") for seg in branch.split("/")):
        return f"branch name {branch!r} must not have a path segment starting with '.'"
    if branch.endswith("."):
        return f"branch name {branch!r} must not end with '.'"
    if branch.endswith(".lock"):
        return f"branch name {branch!r} must not end with '.lock'"
    return None
