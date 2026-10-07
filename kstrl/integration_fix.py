"""What is left of the integration fix component (#483; design #480 section 3.4).

#696 decision 6 gives kstrl no way to tell which allowedPaths entries hold
tests: that read one language's test-path conventions. A fix is never
scoped to a whole component (design 3.4), so no fix can be scoped, and
every open code finding is handed off (``integration_loop.decide_round``).
The blocking loop then stops red instead of building a fix.

What stays reads fix components that an earlier kstrl built and wrote to
disk: a run resumed on such a manifest must still refuse an incomplete one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kstrl.integration_state import FIX_COMPONENT_PREFIX
from kstrl.manifest import Manifest
from kstrl.statedir import pre_run_prd_path

_FEATURE_DIR = "scripts/kstrl/feature"
#: kstrl's own files. A finding that cites one is handed off for that reason.
_HARNESS_PREFIXES = ("scripts/kstrl/", ".kstrl/")

#: Why every other open code finding is handed off (#696 decision 6).
NO_FIX_SCOPE = (
    "kstrl reads no test-path convention, so no fix can be scoped to the cited "
    "files and their tests (#696)"
)


def fix_prd_rel(component_id: str) -> str:
    return f"{_FEATURE_DIR}/{component_id}/prd.json"


def handoff_reason(locations: Sequence[str]) -> str:
    """Why a finding citing ``locations`` is handed off. Never "": no fix
    is built (#696 decision 6)."""
    harness = [loc for loc in locations if loc.startswith(_HARNESS_PREFIXES)]
    if harness:
        return f"it cites kstrl's own files: {', '.join(harness)}"
    return NO_FIX_SCOPE


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
