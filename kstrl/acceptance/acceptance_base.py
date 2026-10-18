"""Which designed checks the base keeps (#700).

The base reading removes a check the verification designer wrote when the
check contradicts the base, and :func:`kept_on_base` says which checks each
head then runs. An operator's plan is kept whole.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from kstrl.acceptance.acceptance import FAIL, PASS, Check, ComponentPlan, PinnedPlan, verdict_of

if TYPE_CHECKING:
    from kstrl.ui import UI


#: What the base reading says of a component when the base removed every
#: check the verification designer wrote for it (:func:`kept_on_base`).
NO_DESIGNED_CHECK = "no designed acceptance check"


def kept_on_base(
    plan: PinnedPlan, exits: Mapping[str, Mapping[str, int | None]], ui: UI
) -> tuple[dict[str, ComponentPlan], dict[str, str]]:
    """The checks each head runs, by component, and :data:`NO_DESIGNED_CHECK`
    for each component left with none. An operator's plan is kept whole:
    the base refuses a contradicted check of an operator, who must know
    that it is weak. Of a plan the designer wrote, the checks
    :func:`_removed_on_base` names are removed."""
    if not plan.designed:
        return dict(plan.components), {}
    kept: dict[str, ComponentPlan] = {}
    said: dict[str, str] = {}
    for comp, part in plan.components.items():
        removed = _removed_on_base(comp, part, exits[comp], ui)
        checks = tuple(check for check in part.checks if check.id not in removed)
        if checks:
            kept[comp] = replace(part, checks=checks)
        else:
            said[comp] = NO_DESIGNED_CHECK
            ui.warn(f"  {comp}: {NO_DESIGNED_CHECK}")
    return kept, said


def _removed_on_base(
    comp: str, part: ComponentPlan, exits: Mapping[str, int | None], ui: UI
) -> set[str]:
    """The ids of the designed checks of ``comp`` that the base removes, each
    one said with :func:`_why_removed`."""
    verdicts = {check.id: verdict_of(exits[check.id]) for check in part.checks}
    holds = any(c.on_base == "fails" and verdicts[c.id] == FAIL for c in part.checks)
    removed: set[str] = set()
    for check in part.checks:
        why = _why_removed(check, verdicts[check.id], exits[check.id], holds)
        if why:
            removed.add(check.id)
            ui.warn(f"  {comp}: the designed check {check.id} {why}: it is removed")
    return removed


def _why_removed(check: Check, verdict: str, code: int | None, holds: bool) -> str:
    """Why the base removes a designed check, or "" to keep it. A check with
    ``onBase: fails`` that passes on the base cannot tell the change from no
    change (owner decision of 2026-10-06 on #700). A check with ``onBase:
    passes`` that fails on the base, or cannot run there, cannot measure
    kept behaviour on this base (owner decision of 2026-10-09). The second
    kind is removed only when its component ``holds`` a check that fails on
    the base as it says; otherwise it stays, and the base refuses the plan
    through it."""
    if check.on_base == "fails" and verdict == PASS:
        return f"passes on the base (exit {code}), so it cannot tell the change from no change"
    if check.on_base == "passes" and verdict != PASS and holds:
        did = "fails" if verdict == FAIL else "could not run"
        said = "no exit" if code is None else f"exit {code}"
        return (
            f"{did} on the base ({said}), where it says it passes, "
            "so it cannot measure kept behaviour on this base"
        )
    return ""
