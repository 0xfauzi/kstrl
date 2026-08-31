"""A loadable component PRD where a test manifest says one is (#293).

``factory._preflight_component_scope`` refuses a component whose
pre-run PRD will not read: the plan-time scope snapshot (#269) then has
no scope, ``check_scope_source`` fails closed on that error, and because
the snapshot is fixed for the life of the run every retry reproduces
the identical failure. Refusing before the first engineer call is the
whole point of that preflight.

A manifest naming a PRD that is not on disk could never have run in
production either - ``factory._run_component`` copies that exact file
into the worktree and ``verify.check_prd_stories`` re-reads it - so
fixtures that stub the engineer still have to put one there.

The one writer, so the shape a fixture PRD needs stays in one place.
``tests/test_harness_path_scope._write_prd`` delegates here rather
than serialising its own: that module's PRDs carry content its
assertions read (a named story, approved fixtures), so it keeps its
own defaults and hands them over.
"""

from __future__ import annotations

import json
from pathlib import Path

#: One story that already passes, for a fixture that needs a PRD with
#: something in it. Lives here because a fixture writing BOTH a pre-run
#: and a worktree copy has to give them the same stories - Phase 1
#: compares the two (#264/#268) and reads a difference as the engineer
#: rewriting its own story set.
PASSING_STORY: dict[str, object] = {
    "id": "US-001",
    "title": "T",
    "acceptanceCriteria": ["AC1"],
    "priority": 1,
    "passes": True,
    "notes": "",
}


def write_component_prd(
    root: Path,
    rel: str,
    *,
    branch: str = "test",
    allowed_paths: list[str] | None = None,
    stories: list[dict[str, object]] | None = None,
    fixtures: list[dict[str, object]] | None = None,
    body: str | None = None,
) -> Path:
    """Write a minimal loadable PRD at ``root / rel`` and return it.

    No stories by default, so ``check_prd_stories`` passes ("All 0
    stories passing") and a fixture that stubs the engineer keeps
    whatever outcome it was written to assert. No ``allowedPaths``
    unless asked: an unconstrained component is the historical
    no-constraint case, and a fixture that is not about scope should
    not acquire one.

    ``stories`` matters when a fixture ALSO writes a worktree copy.
    ``verify.check_prd_stories`` compares the two and refuses a story
    set the engineer changed (#264/#268), so a pre-run copy with no
    stories beside a worktree copy with one reads as tampering. Give
    both the same stories, or use ``PASSING_STORY`` for both.

    ``body`` writes raw text instead, valid or not. The escape hatch
    for the tests that need an UNLOADABLE PRD at exactly this path:
    they still want one writer deciding where a component's PRD lives,
    and only the content differs.
    """
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if body is not None:
        path.write_text(body)
        return path
    document: dict[str, object] = {
        "branchName": branch,
        "userStories": [] if stories is None else stories,
    }
    if allowed_paths is not None:
        document["allowedPaths"] = allowed_paths
    if fixtures is not None:
        document["fixtures"] = fixtures
    path.write_text(json.dumps(document))
    return path
