"""The L1 plan gate: a person approves the exact plan before anything runs (#602).

At L1 the ladder records "plans: human-approved", and before #602 nothing
asked anyone: ``FlagBundle.auto_accept_plan`` was the only field on which
L1 and L2 differ and it had no reader, so an L1 run and an L2 run of the
same manifest behaved identically. :func:`run_plan_gate` is that reader.

The approval binds to a digest of the plan, not to the manifest file, and
the decision lives in the inbox (the XDG control directory, outside the
tree the agents can write). The manifest carries only the digest awaiting
approval, so ``ks serve`` and ``ks inbox approve`` can tell a plan park
from any other exit, and never the approval itself.

Only an answered "Approve the plan" approves. An unanswered request, an
out-of-range choice and the default (decide later) park the plan in the
inbox, the rule #594 set for the merge gate. ``--yes`` does not approve a
plan: it is given before ``--spec`` has produced one, and ``ks serve``
passes it on every launch.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import events as ev
from kstrl.decompose import load_spec_input, spec_digest
from kstrl.inbox import UNDECIDED, Inbox, InboxError, InboxItem, ItemKind, ItemStatus
from kstrl.interaction import PromptKind, PromptRequest
from kstrl.observability import NotifyConfig, NotifyHooks
from kstrl.pipeline import _iso_now
from kstrl.prd import PRD
from kstrl.statedir import ControlStateError, pre_run_prd_path
from kstrl.workqueue import relocated_spec

if TYPE_CHECKING:
    from kstrl.autonomy import FlagBundle
    from kstrl.manifest import Manifest
    from kstrl.pipeline import ComponentPipeline
    from kstrl.stack import Stack

#: The inbox dedupe-key prefix of a plan_gate item. ``kstrl/cli.py``
#: recognises a plan park by it when `ks inbox approve` or `ks inbox
#: reject` is given one.
PLAN_GATE_KEY = "plan-gate:"

#: The options the plan checkpoint offers. The default is the last one:
#: an accidental Enter or a detached TUI costs a wait, never an
#: unapproved run.
PLAN_OPTIONS = ("Approve the plan", "Reject the plan", "Decide later in the inbox")

#: The ``kind`` of this gate's checkpoint events: ``checkpoint_requested``
#: and ``checkpoint_resolved`` with ``kind="plan"``. A name, not a path.
PLAN_KIND = "plan"


class PlanUnreadableError(Exception):
    """A PRD the plan names cannot be read, so the plan has no digest."""


def plan_dedupe_key(digest: str) -> str:
    """The dedupe key of the plan_gate item for the plan ``digest``."""
    return f"{PLAN_GATE_KEY}{digest}"


def plan_digest(manifest: Manifest, root_dir: Path) -> str:
    """SHA-256 of the plan a person approves.

    Canonical JSON of the components in manifest order (id, title,
    description, sorted dependencies, PRD path, branch, plan id) and, from
    the PRD each component starts from (``pre_run_prd_path``, the one
    reader of a planned copy), its allowed paths, its approved fixtures and
    its stories' id, title and acceptance criteria: what
    ``PRD.tamper_changes`` pins as the plan the engineer may not rewrite.
    Statuses, ``passes``, ``notes`` and run ids are left out: they change
    while an approved plan runs, and a resumed run of the same plan must
    not be asked again. A PRD that cannot be read raises
    :class:`PlanUnreadableError`; it is never a digest of fewer files.
    """
    plan: list[dict[str, Any]] = []
    for comp in manifest.components:
        path = pre_run_prd_path(root_dir, comp.id, comp.prd_path, plan_id=comp.plan_id)
        try:
            prd = PRD.load(path)
        except (OSError, ValueError) as exc:
            raise PlanUnreadableError(f"{path}: {exc}") from exc
        plan.append(
            {
                "id": comp.id,
                "title": comp.title,
                "description": comp.description,
                "dependencies": sorted(comp.dependencies),
                "prdPath": comp.prd_path,
                "branchName": comp.branch_name,
                "planId": comp.plan_id,
                "allowedPaths": prd.allowed_paths,
                "fixtures": prd.fixtures,
                "stories": [
                    [story.id, story.title, story.acceptance_criteria] for story in prd.user_stories
                ],
            }
        )
    # #696: a plan made under a [stack] is approved together with it. Only
    # then, so the digest of every plan made without one is unchanged.
    approved: Any = (
        {"components": plan, "stackDigest": manifest.stack_digest}
        if manifest.stack_digest
        else plan
    )
    body = json.dumps(approved, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def spec_pin_errors(manifest: Manifest, root_dir: Path) -> list[str]:
    """Why this plan must not run on the spec as it reads now, or [] (#639).

    A plan with no pinned digest has nothing to compare: a ``ks run`` or
    ``ks feature`` manifest (no spec), or one written before #639, which
    the caller warns about. Otherwise the spec is read again through
    ``load_spec_input``, the reader decompose pinned, and a changed,
    missing or unreadable spec is a refusal with the ways out named.
    """
    if not manifest.spec_digest:
        return []
    replan = f"ks factory --spec {manifest.spec_path} --project-name {manifest.project_name}"
    path = Path(manifest.spec_path)
    if not path.is_absolute():
        path = root_dir / path
    if not path.exists():
        path = relocated_spec(root_dir, path) or path
    try:
        text = load_spec_input(path)
    except (OSError, ValueError) as exc:
        return [
            f"the spec this plan was made from cannot be read: {manifest.spec_path}: {exc}. "
            f"Nothing was run.",
            f"Restore it, or re-plan from the spec you have now: {replan}",
        ]
    now = spec_digest(text)
    if now == manifest.spec_digest:
        return []
    return [
        f"{manifest.spec_path} has changed since this plan was made (planned from "
        f"{manifest.spec_digest[:12]}, it now reads {now[:12]}). Nothing was run.",
        f"Build the spec as it is now: {replan} (one architect run).",
        f"Build the plan as it was: put {manifest.spec_path} back as it was (for a tracked "
        f"file, git diff -- {manifest.spec_path} shows what changed) and run this again.",
    ]


def stack_pin_errors(manifest: Manifest, stack: Stack | None) -> list[str]:
    """Why this plan must not run under the ``[stack]`` in force, or [] (#696).

    The sibling of :func:`spec_pin_errors`. A plan that pins no stack digest
    has nothing to compare (no ``[stack]`` when it was made, or a manifest
    from before #696). Otherwise the stack in kstrl.toml now, or its
    absence, must be the one the plan was made under.
    """
    if not manifest.stack_digest:
        return []
    now = stack.digest if stack is not None else ""
    if now == manifest.stack_digest:
        return []
    reads = f"it now reads {now[:12]}" if now else "kstrl.toml now has no [stack]"
    return [
        f"this plan was made under the [stack] {manifest.stack_digest[:12]}, and {reads}. "
        "Nothing was run.",
        f"Plan under the stack you have now: ks factory --spec {manifest.spec_path or '<spec>'} "
        f"--project-name {manifest.project_name} (one architect run), or put [stack] back as "
        "it was.",
    ]


def run_plan_gate(pipeline: ComponentPipeline, bundle: FlagBundle | None) -> int | None:
    """Ask a person to approve this run's plan, or park it. None runs the plan.

    ``bundle`` is the run's CLAMPED bundle, None when ``[autonomy]`` is
    off (then the config's own flags stand and nothing is asked). A plan
    with no components runs nothing, so nothing is asked about it. Returns
    the exit code that ends the run otherwise: 1 for a park (incomplete,
    not failed, the code a merge park gets) and 2 for a rejection or a
    refusal. Called after every pre-spend refusal and before anything is
    pushed, merged or scheduled.
    """
    if bundle is None or bundle.auto_accept_plan or not pipeline.manifest.components:
        return None
    try:
        digest = plan_digest(pipeline.manifest, pipeline.root_dir)
    except PlanUnreadableError as exc:
        pipeline.ui.err(f"  The plan cannot be approved because it cannot be read: {exc}")
        return 2
    key = plan_dedupe_key(digest)
    item = _find(pipeline, key)
    if item is not None and item.status is ItemStatus.APPROVED:
        _settle(pipeline, "", "approved", "inbox")
        return None
    if item is not None and item.status is ItemStatus.REJECTED:
        _settle(pipeline, "", "rejected", "inbox")
        return 2
    question = _question(pipeline.manifest)
    pipeline.bus.emit(ev.CheckpointRequested(kind=PLAN_KIND, question=question))
    if pipeline.interaction.can_prompt():
        response = pipeline.interaction.request(
            PromptRequest(
                kind=PromptKind.CHECKPOINT,
                header=question,
                options=PLAN_OPTIONS,
                default=len(PLAN_OPTIONS) - 1,
            )
        )
        # #594: only an ANSWERED choice decides. Unanswered, out of range
        # and "decide later" all park below.
        decision = (
            {0: "approved", 1: "rejected"}.get(response.choice)
            if response.choice is not None
            else None
        )
        if decision is not None:
            _record(pipeline, key, digest, approve=decision == "approved")
            _settle(pipeline, "", decision, "operator")
            return None if decision == "approved" else 2
        pipeline.ui.warn(
            f"  The plan checkpoint got no approval (answered={response.answered}, "
            f"choice={response.choice}); parking the plan for approval"
        )
    return _park_plan(pipeline, key, digest, item)


def _question(manifest: Manifest) -> str:
    order = manifest.topological_order()
    made_from = (
        f", made from {manifest.spec_path} ({manifest.spec_digest[:12]})"
        if manifest.spec_digest
        else ""
    )
    return (
        f"Approve the plan for {manifest.project_name}{made_from}: {len(order)} component(s) "
        f"({', '.join(order)})?"
    )


def _find(pipeline: ComponentPipeline, key: str) -> InboxItem | None:
    """The plan_gate item for ``key``; None when there is none, the inbox
    is off, or it cannot be read (which warns: unreadable is not a decision)."""
    if not pipeline.inbox_config.enabled:
        return None
    try:
        return Inbox(pipeline.root_dir, pipeline.inbox_config).find_by_dedupe_key(key)
    except (OSError, TypeError, ValueError, InboxError, ControlStateError) as exc:
        # The tuple ComponentPipeline._park_decision catches, for its reasons.
        pipeline.ui.warn(f"  Inbox read failed; the plan is not approved: {exc}")
        return None


def _record(pipeline: ComponentPipeline, key: str, digest: str, *, approve: bool) -> None:
    """File the item for an answer given at the prompt and decide it, so a
    resumed run of the same plan finds the decision instead of asking again.

    ``Inbox.add`` directly, handed hooks with no commands: ``add`` pages
    for every item it opens (#600), and the person who decided is at the
    prompt. A page about an item they just closed would be false.
    """
    if not pipeline.inbox_config.enabled:
        pipeline.ui.warn("  [inbox] is disabled: this decision holds for this run only")
        return
    comment = "decided at the run's plan checkpoint"
    try:
        box = Inbox(pipeline.root_dir, pipeline.inbox_config)
        item = box.add(
            ItemKind.PLAN_GATE,
            _title(pipeline.manifest),
            detail=_detail(pipeline.manifest),
            run_id=pipeline.run_id,
            dedupe_key=key,
            evidence=_evidence(pipeline, digest),
            # A config with no hooks: nothing to page about a closed item.
            notify=NotifyHooks(NotifyConfig(), run_id=pipeline.run_id),
        )
        if approve:
            box.approve(item.id, actor="operator", comment=comment)
        else:
            box.reject(item.id, actor="operator", comment=comment)
    except (OSError, TypeError, ValueError, InboxError, ControlStateError) as exc:
        pipeline.ui.warn(f"  Inbox write failed; this decision holds for this run only: {exc}")


def _settle(pipeline: ComponentPipeline, awaiting: str, decision: str, decided_by: str) -> None:
    """Record the gate's outcome on the manifest and in the run's events.

    The one manifest write in this module. ``awaiting`` is the digest a
    park waits on, "" otherwise. A park or a rejection ENDS the run, so it
    stamps ``completed_at`` the way the run summary does: a run that
    stopped here was not interrupted, and the next run must not carry its
    spend (the architect's, on ``ks factory --spec``) as its own (#463).
    """
    manifest = pipeline.manifest
    manifest.plan_awaiting_approval = awaiting
    if decision != "approved":
        manifest.completed_at = _iso_now()
    manifest.save(pipeline.manifest_path)
    pipeline.bus.emit(
        ev.CheckpointResolved(kind=PLAN_KIND, decision=decision, decided_by=decided_by)
    )
    if decision == "approved":
        pipeline.ui.ok(f"  Plan approved ({decided_by})")
    elif decision == "rejected":
        pipeline.ui.err(f"  The plan was rejected ({decided_by}): nothing was run")


def _park_plan(pipeline: ComponentPipeline, key: str, digest: str, item: InboxItem | None) -> int:
    """File (or keep) the plan_gate item and end the run with nothing run."""
    ui = pipeline.ui
    if not pipeline.inbox_config.enabled:
        ui.err(
            "  L1 needs a person to approve this plan. Nothing here can ask one and "
            "[inbox] is disabled, so nothing could approve it later: nothing was run. "
            "Enable [inbox], or run ks factory in a terminal and answer the plan checkpoint."
        )
        return 2
    if item is None or item.status not in UNDECIDED:
        # Through the pipeline so the notify hooks fire for a new item.
        pipeline._inbox_add(
            ItemKind.PLAN_GATE,
            _title(pipeline.manifest),
            detail=_detail(pipeline.manifest),
            dedupe_key=key,
            evidence=_evidence(pipeline, digest),
        )
    filed = _find(pipeline, key)
    if filed is None or filed.status not in UNDECIDED:
        ui.err("  The plan_gate inbox item could not be filed or read back: nothing was run")
        return 2
    ui.warn(
        f"  PLAN AWAITING APPROVAL: nothing was run. ks inbox approve {filed.id[:8]} runs "
        f"this plan; ks inbox reject {filed.id[:8]} --comment ... refuses it"
    )
    _settle(pipeline, digest, "parked", "inbox")
    return 1


def _title(manifest: Manifest) -> str:
    return f"{manifest.project_name}: plan awaiting approval"


def _detail(manifest: Manifest) -> str:
    return (
        "The autonomy level requires a person to approve each plan before it runs, and "
        "nothing has approved this one, so nothing was run. Components, in the order "
        f"they run: {', '.join(manifest.topological_order())}. From the shell, ks inbox "
        "approve <id> records approval and starts the ks factory run of this plan; "
        "ks inbox reject <id> --comment ... records rejection."
    )


def _evidence(pipeline: ComponentPipeline, digest: str) -> dict[str, Any]:
    return {
        "plan_digest": digest,
        "components": pipeline.manifest.topological_order(),
        "manifest": str(pipeline.manifest_path),
        "spec_path": pipeline.manifest.spec_path,
        "spec_digest": pipeline.manifest.spec_digest,
    }
