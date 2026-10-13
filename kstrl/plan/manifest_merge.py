"""Whether a component's change has reached the base branch."""

from __future__ import annotations

from kstrl.plan.manifest import Component, ComponentStatus


def has_merged(comp: Component) -> bool:
    """Whether ``comp``'s change has reached the base branch, or may have.

    MERGE_PENDING counts: its merge was initiated and may have landed. A
    recorded merge commit counts. So does a COMPLETED component with a
    PR, because once a PR exists COMPLETED requires a confirmed merge
    (R0.2) and ``merge_sha`` may be empty for a merged PR. A COMPLETED
    component with no PR (``create_prs=False``, or no gh) merged nothing.
    """
    if comp.status == ComponentStatus.MERGE_PENDING.value or comp.merge_sha:
        return True
    return comp.status == ComponentStatus.COMPLETED.value and bool(comp.pr_url)
