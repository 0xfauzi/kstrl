"""Which tier a stored fact belongs in, found by the paths its evidence cites (#517).

A fact is written under ``<knowledge_root>/<component_id>/<run_id>/``,
and a component id does not survive a new manifest (#453 D5: 28 facts on
disk, 0 reachable from slice 2). The paths a fact's evidence cites do
survive, so retrieval reads every component directory and places each
fact by its owner's id or by the paths it cites.

Kept out of ``knowledge.py`` so that file only gains call sites. This
module imports nothing from ``kstrl`` at run time, which is what lets
``knowledge.py`` import it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kstrl.knowledge import Fact
    from kstrl.manifest import Manifest
    from kstrl.scope import ComponentScope, RunScope

# The three prefix tiers, named once. ``knowledge._tier_for_section``
# decodes a rendered heading back to one of these, and ``tier_for``
# below encodes a fact into one; both read the same constants.
TIER_CORE = "core"
TIER_DEPENDENCY = "dependency"
TIER_SIBLING = "sibling"
FACT_TIERS = (TIER_CORE, TIER_DEPENDENCY, TIER_SIBLING)


def cited_path(item: str) -> str:
    """The path an evidence item cites: everything before the first ``:``.

    Evidence items look like ``path/to/file.py:42-58``.
    """
    return item.split(":", 1)[0].strip()


def fact_paths(fact: Fact) -> tuple[str, ...]:
    """Every non-empty path the fact's evidence cites, in evidence order."""
    return tuple(path for path in map(cited_path, fact.evidence) if path)


def path_within(path: str, entry: str) -> bool:
    """Whether ``path`` is ``entry`` or lies inside it, compared part by part.

    ``src/app`` holds ``src/app/x.py`` and does not hold
    ``src/application/x.py``: a string prefix would say it does. A
    trailing ``/`` and a leading ``./`` on either side change nothing.
    An absolute path, or one with a ``..`` part, lies inside nothing,
    and an entry with no parts holds nothing.
    """
    cited = PurePosixPath(path)
    if cited.is_absolute() or ".." in cited.parts:
        return False
    parts = PurePosixPath(entry).parts
    return bool(parts) and cited.parts[: len(parts)] == parts


def cites_within(fact: Fact, entries: Sequence[str] | None) -> bool:
    """Whether any path the fact cites lies inside any of ``entries``.

    ``None`` or an empty list matches nothing. A component with no
    authored scope is matched by its id alone, never treated as owning
    every path.
    """
    if not entries:
        return False
    return any(path_within(path, entry) for path in fact_paths(fact) for entry in entries)


def tier_for(
    owner: str,
    fact: Fact,
    component_id: str,
    own_paths: Sequence[str] | None,
    dependency_ids: Collection[str],
    dependency_paths: Mapping[str, Sequence[str]],
) -> str:
    """The tier ``fact`` goes in when building ``component_id``'s prefix.

    ``owner`` is the directory the fact was read from, which is what
    retrieval has always keyed on. Core: written under this component,
    or citing a path inside its own scope. Dependency: written under one
    of ``dependency_ids``, or citing a path inside one of their scopes.
    Everything else is a sibling. ``dependency_paths`` may hold more
    components than ``dependency_ids``; only the ids in
    ``dependency_ids`` are consulted.
    """
    if owner == component_id or cites_within(fact, own_paths):
        return TIER_CORE
    if owner in dependency_ids:
        return TIER_DEPENDENCY
    if any(cites_within(fact, dependency_paths.get(dep)) for dep in dependency_ids):
        return TIER_DEPENDENCY
    return TIER_SIBLING


def evidence_cites_existing_path(evidence: list[str], worktree_path: Path) -> bool:
    """Return True when at least one evidence item cites a path that
    exists inside the worktree.

    Everything before the first ``:`` of an item is treated as a
    worktree-relative path. Absolute paths and paths that resolve
    outside the worktree never count: evidence must point at the
    artifact under review.
    """
    try:
        resolved_root = worktree_path.resolve()
    except OSError:
        return False
    for item in evidence:
        cited = cited_path(item)
        if not cited or Path(cited).is_absolute():
            continue
        try:
            candidate = (worktree_path / cited).resolve()
        except OSError:
            continue
        if candidate == resolved_root or not candidate.is_relative_to(resolved_root):
            continue
        if candidate.exists():
            return True
    return False


def is_stale(fact: Fact, worktree: Path) -> bool:
    """Whether none of the paths the fact cites exists in ``worktree``.

    Path existence only: a fact whose files still exist but whose
    content changed is not detected here.
    """
    return not evidence_cites_existing_path(fact.evidence, worktree)


def authored_paths(scope: ComponentScope) -> list[str] | None:
    """The scope a component's own PRD authored, or None.

    A run-wide ``--allowed-paths`` list is the same for every component,
    so it says nothing about which component a path belongs to, and an
    unconstrained or unresolved scope authored nothing.
    """
    return scope.allowed_paths if scope.source == "component_prd" else None


def paths_by_component(manifest: Manifest, run_scope: RunScope) -> dict[str, list[str]]:
    """Each manifest component's authored scope, for the components that have one."""
    out: dict[str, list[str]] = {}
    for comp in manifest.components:
        paths = authored_paths(run_scope.for_component(comp.id))
        if paths:
            out[comp.id] = paths
    return out
