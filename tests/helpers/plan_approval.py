"""Approve a manifest's plan the way `ks inbox approve` records it (#602).

At L1 ``run_plan_gate`` runs a plan only when a person approved it. A test
that runs the factory at L1 for a reason unrelated to plans approves the
plan first, rather than moving to L2 and changing what it measures.
"""

from __future__ import annotations

from pathlib import Path

from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.manifest import Manifest
from kstrl.plan_gate import plan_dedupe_key, plan_digest


def approve_plan(root: Path, manifest: Manifest) -> None:
    """File and approve the plan_gate item for ``manifest``'s plan under ``root``.

    Call it after every PRD the manifest names is on disk: the digest reads them.
    """
    digest = plan_digest(manifest, root)
    box = Inbox(root, InboxConfig.load(root))
    item = box.add(
        ItemKind.PLAN_GATE,
        f"{manifest.project_name}: plan approved by the test",
        dedupe_key=plan_dedupe_key(digest),
        evidence={"plan_digest": digest},
    )
    box.approve(item.id, actor="test", comment="approved by tests/helpers/plan_approval.py")
