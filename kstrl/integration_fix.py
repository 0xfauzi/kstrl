"""The integration fix component (#483; design #480 section 3.4).

Pure data plus two writers, the fix PRD and the manifest append. Nothing
here runs git or an agent; ``kstrl/integration_loop.py`` orders the writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.decisions import SpecDecision, _render_full
from kstrl.guards import path_is_allowed
from kstrl.integration_state import FIX_COMPONENT_PREFIX
from kstrl.manifest import Component, Manifest
from kstrl.prd import PRD, UserStory
from kstrl.scope import RunScope
from kstrl.statedir import plan_prd_path, pre_run_prd_path

INTEGRATION_FIX_PROMPT_VERSION = "1.0.0"

# One acceptance criterion per line, for each story of a fix PRD (#483).
# Instruction to the engineer LLM, so it is enrolled (H3). Engineer-facing:
# the calibration suite scores no fixture for it (the #303 position).
INTEGRATION_FIX_PROMPT = """\
The integration review of the merged feature reported: {text}
That defect no longer holds at {locations}.
A new regression test fails before this change and passes after it, if the defect is observable.
"""

_FEATURE_DIR = "scripts/kstrl/feature"
#: kstrl's own files. No fix writes here except its own feature subtree.
_HARNESS_PREFIXES = ("scripts/kstrl/", ".kstrl/")
_TEST_SEGMENTS = frozenset({"test", "tests"})


@dataclass(frozen=True)
class FindingScope:
    """What one finding lets a fix write, or why it cannot be scoped."""

    paths: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    handoff: str = ""


def fix_id(number: int) -> str:
    return f"{FIX_COMPONENT_PREFIX}{number}"


def fix_prd_rel(component_id: str) -> str:
    return f"{_FEATURE_DIR}/{component_id}/prd.json"


def fix_subtree(component_id: str) -> str:
    return f"{_FEATURE_DIR}/{component_id}/"


def fix_branch(component_id: str) -> str:
    return f"kstrl/factory/{component_id}"


def is_test_path(entry: str) -> bool:
    """An allowedPaths entry that names tests: a ``test`` or ``tests``
    segment, or a last segment starting ``test_``."""
    parts = entry.rstrip("/").split("/")
    return any(part in _TEST_SEGMENTS for part in parts) or parts[-1].startswith("test_")


def finding_scope(
    locations: Sequence[str], manifest: Manifest, run_scope: RunScope
) -> FindingScope:
    """The narrowest scope that lets a fix work on one finding (design 3.4):
    the cited files, then the test entries of every component whose
    plan-time allowedPaths hold a cited file. Never a whole component's
    scope. A finding citing kstrl's own files, or one whose test paths
    cannot be determined, is handed off."""
    harness = [loc for loc in locations if loc.startswith(_HARNESS_PREFIXES)]
    if harness:
        return FindingScope(handoff=f"it cites kstrl's own files: {', '.join(harness)}")
    owners: list[str] = []
    tests: list[str] = []
    for comp in manifest.components:
        allowed = run_scope.for_component(comp.id).allowed_paths or []
        if not any(path_is_allowed(loc, allowed) for loc in locations):
            continue
        owners.append(comp.id)
        for entry in allowed:
            if is_test_path(entry) and entry not in tests:
                tests.append(entry)
    if not tests:
        return FindingScope(
            owners=tuple(owners),
            handoff="no test path could be determined from the allowedPaths of "
            "the components that own the cited files",
        )
    return FindingScope(tuple(dict.fromkeys([*locations, *tests])), tuple(owners))


def tooling_criteria(manifest: Manifest, root_dir: Path) -> list[str]:
    """Every tests, typecheck or lint "pass" criterion of the feature PRDs,
    once each, in manifest order (the ``feature_cmd._build_repair_prd`` rule).

    Raises what ``PRD.load`` raises: the caller stops the loop on it.
    """
    found: list[str] = []
    for comp in manifest.components:
        if comp.id.startswith(FIX_COMPONENT_PREFIX):
            continue
        source = pre_run_prd_path(root_dir, comp.id, comp.prd_path, plan_id=comp.plan_id)
        for story in PRD.load(source).user_stories:
            for item in story.acceptance_criteria:
                lower = item.lower()
                tool = "typecheck" in lower or "tests" in lower or "lint" in lower
                if tool and "pass" in lower and item not in found:
                    found.append(item)
    return found


def render_fix_criteria(text: str, locations: Sequence[str]) -> str:
    """The enrolled template for one finding. One line of ``text``."""
    return INTEGRATION_FIX_PROMPT.format(
        text=" ".join(text.split()), locations=", ".join(locations)
    )


def _notes(suggestion: str, owners: Sequence[str], decisions: Sequence[SpecDecision]) -> str:
    """The reviewer's suggestion, then every register decision binding a
    cited component, verbatim. The engineer's decisions block would show
    them only as one-line summaries (decisions.build_decisions_context)."""
    bound = [_render_full(d) for d in decisions if d.component and d.component in owners]
    return "\n".join(part for part in (suggestion, *bound) if part)


def build_fix_prd(
    component_id: str,
    findings: Sequence[Mapping[str, Any]],
    scopes: Mapping[str, FindingScope],
    scope: Sequence[str],
    tooling: Sequence[str],
    decisions: Sequence[SpecDecision],
) -> PRD:
    """One story per open code finding. The story id is the finding id."""
    stories: list[UserStory] = []
    for number, finding in enumerate(findings, start=1):
        criteria = render_fix_criteria(str(finding["text"]), finding["locations"]).splitlines()
        stories.append(
            UserStory(
                id=str(finding["id"]),
                title=str(finding["id"]),
                acceptance_criteria=[*criteria, *tooling],
                priority=number,
                passes=False,
                notes=_notes(
                    str(finding.get("suggestion", "")),
                    scopes[finding["id"]].owners,
                    decisions,
                ),
            )
        )
    return PRD(
        branch_name=fix_branch(component_id), user_stories=stories, allowed_paths=list(scope)
    )


def write_fix_prd(root_dir: Path, component_id: str, plan_id: str, prd: PRD) -> None:
    """Stage 2 (design 3.4): at ``root_dir``, outside every worktree, and
    at ``plan_prd_path`` rather than ``fix_prd_rel``, which the fix's
    branch commits (#545). ``plan_id`` is the run building the fix, and
    ``append_fix_component`` stamps the same id on the component (#568)."""
    path = plan_prd_path(root_dir, component_id, plan_id=plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    prd.save(path)


def append_fix_component(
    manifest: Manifest,
    manifest_path: Path,
    component_id: str,
    finding_ids: Sequence[str],
    plan_id: str,
) -> Component:
    """Stage 3 (design 3.4). Depends on every existing component, so it is
    ready only once the feature is complete."""
    comp = Component(
        id=component_id,
        title=f"Integration fix for {', '.join(finding_ids)}",
        description="Fixes findings of the integration review of the merged feature",
        dependencies=[c.id for c in manifest.components],
        prd_path=fix_prd_rel(component_id),
        branch_name=fix_branch(component_id),
        plan_id=plan_id,
    )
    manifest.components.append(comp)
    manifest.save(manifest_path)
    return comp


def reconcile_fixes(state: Mapping[str, Any], manifest: Manifest, root_dir: Path) -> list[str]:
    """Every fix whose three creation stages disagree (design 3.4). A fix is
    whole only with a state entry, a PRD and a manifest component.

    The PRD is looked for under the fix's plan id: the component's when
    stage 3 happened, else the run that recorded the state entry, which
    is the run that wrote the PRD (#568)."""
    planned = {str(entry["id"]): str(entry.get("runId", "")) for entry in state.get("fixes", [])}
    built = {c.id: c.plan_id for c in manifest.components if c.id.startswith(FIX_COMPONENT_PREFIX)}
    errors: list[str] = []
    for component_id in sorted(planned.keys() | built.keys()):
        plan_id = built.get(component_id) or planned.get(component_id, "")
        prd = pre_run_prd_path(root_dir, component_id, fix_prd_rel(component_id), plan_id=plan_id)
        present = {
            "state entry": component_id in planned,
            "PRD": prd.is_file(),
            "manifest component": component_id in built,
        }
        if not all(present.values()):
            stages = ", ".join(f"{name} {'yes' if ok else 'no'}" for name, ok in present.items())
            errors.append(f"integration fix {component_id} is incomplete: {stages}")
    return errors
