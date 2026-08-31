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
from typing import Final

# R0.6 input hygiene: component ids and branch names are LLM-emitted
# (architect output) and flow into filesystem paths
# (.kstrl/worktrees/<id>, scripts/kstrl/feature/<id>) and git argv
# (git worktree add, git push -u origin <branch>). Both are validated
# against conservative allowlists at every parse boundary. Rejection is
# deliberate - silent sanitizing would hide architect drift.
COMPONENT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_COMPONENT_ID_RE = re.compile(COMPONENT_ID_PATTERN)

# #281: kstrl's own ROLE rows are written into surfaces that are
# otherwise keyed by an LLM-emitted component id - the usage meter, the
# run's event stream, the reducer's component table, the evolution
# journal, and serve's spend ledger. While a role key was a bare word
# ("architect") it shared that keyspace, so a spec that led the
# architect to emit a component genuinely called `architect` merged the
# two rows: the component's engineer/review/security/distill spend
# folded into the role's, and - worse - `RunSpend.architect_calls` went
# non-zero for a run whose architect may never have reported, clearing
# `unmetered_phases` and letting the daemon call a day's total exact on
# no evidence.
#
# The prefix removes the CLASS rather than one name: it namespaces every
# role row, including ones added later. It lives beside
# COMPONENT_ID_PATTERN because that is what makes it safe - the pattern
# anchors the first character to [a-z0-9], and '@' is outside the
# charset besides, so no valid component id can ever be spelled this
# way. That is a property of two constants in one file, and
# tests/test_input_hygiene.py pins them together, so loosening the
# pattern fails a test instead of silently re-merging the meter.
#
# '@' rather than a punctuation mark that reads more like prose,
# because a role key is not only a dict key: `ks decompose` opens the
# architect's transcript at
# .kstrl/runs/<run>/components/<key>/engineer.log, so the prefix becomes
# a PATH SEGMENT. '@' is unremarkable in a path on every filesystem
# kstrl targets and needs no quoting in a shell; ':' is neither.
# validate_branch_name rejects '@' too, so a role key that ever reached
# a git ref would be a loud rejection rather than a silent bad ref.
ROLE_KEY_PREFIX: Final = "@"


def role_component_key(role: str) -> str:
    """The component-table key for a kstrl ROLE rather than a component.

    Roles are kstrl's own vocabulary; component ids are the architect
    LLM's. Both are written to the same keyed surfaces, so the role side
    is namespaced and the component side is left exactly as the operator
    and the architect wrote it (#281). No compatibility break falls on
    manifests: nothing a manifest carries changes.

    ``role`` is the bare role name and stays bare wherever it is a PHASE
    key or an operator-facing label - phase keys are kstrl's vocabulary
    on both sides, so they never collided in the first place, and
    prefixing a label would only make the honesty warnings harder to
    read.
    """
    return f"{ROLE_KEY_PREFIX}{role}"


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
