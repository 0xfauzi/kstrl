"""How each item's end is recorded: the queue transition, the inbox item, the GitHub label, the
CycleResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl.queue import ItemSource, ItemState, Queue, QueueItem, queue_lock, short_item_id
from kstrl.serve_config import ServeObserver
from kstrl.serve_gates import check_parked_merges
from kstrl.serve_github import OpenPrCountStreak
from kstrl.serve_merge_gate import MergeGate
from kstrl.serve_outcome import (
    Outcome,
    RunOutcome,
    Verdict,
    _pr_urls_from_manifest,
    _run_id_from_manifest,
    classify_run,
)
from kstrl.serve_process import ReapResult
from kstrl.serve_spend import SpendLedger


@dataclass
class CycleResult:
    """What one poll cycle did. Every field is something a test asserts."""

    ran_item: str = ""
    verdict: Verdict | None = None
    reason: str = ""
    #: An item ended in a state only a human can move. Set on EVERY
    #: poison and refusal path, including the reaper's and the merge-gate
    #: refusal, neither of which sets ``ran_item``. Review #186 F10: the
    #: CLI derived its exit status from ``Verdict.may_retry``, which
    #: stays true for an infra verdict whose last attempt was spent, so
    #: `ks serve --once` exited 0 on a poisoned item.
    needs_human: bool = False
    reaped: ReapResult = field(default_factory=ReapResult)
    swept_staging: int = 0
    paused: str = ""
    skipped: str = ""
    charged_usd: float = 0.0
    inbox_items: tuple[str, ...] = ()
    #: Remote refs admitted by the intake stage this cycle.
    synced: tuple[str, ...] = ()
    #: Intake errors. Non-empty does NOT stop the cycle: a front-end
    #: outage must never block the local queue (R8.6).
    sync_errors: tuple[str, ...] = ()


def _file_inbox_item(
    root_dir: Path,
    *,
    kind_name: str,
    title: str,
    detail: str,
    dedupe_key: str,
    evidence: dict[str, Any],
    run_id: str = "",
) -> str:
    """Record a decision for a human, never failing the caller.

    An inbox write must not be able to undo a queue transition that
    already happened, so a failure here returns an empty id and the
    caller carries on. The queue journal remains the authoritative record
    either way.

    The caught set is the callee's WHOLE SURFACE, not the types somebody
    expected: ``Inbox.add`` takes the control lock, and
    ``InboxConfig.load`` casts per key. Both guards that read
    this site clear on ``tests.helpers.astwalk.catches_everything``, the
    one rule #364 reconciled them onto.
    """
    try:
        from kstrl.owner import Inbox, InboxConfig, ItemKind

        config = InboxConfig.load(root_dir)
        if not config.enabled:
            return ""
        box = Inbox(root_dir, config)
        item = box.add(
            ItemKind(kind_name),
            title,
            detail=detail,
            dedupe_key=dedupe_key,
            evidence=evidence,
            run_id=run_id,
        )
        return item.id
    except Exception:  # noqa: BLE001 - an inbox write must never fail its caller
        return ""


def _pause_on_unreadable_spend(
    root_dir: Path,
    queue: Queue,
    unreadable: tuple[str, ...],
    obs: ServeObserver,
    result: CycleResult,
) -> None:
    """Pause intake when the spend of a launch cannot be read (#863).

    The ledger is charged zero for such a run, so the daily budget can no
    longer fire on what it spent. The pause holds until an operator
    resumes, and the inbox item names each run directory and the cause.
    """
    if not unreadable:
        return
    from kstrl.serve_config import Admission
    from kstrl.serve_gates import _pause_queue

    detail = (
        "the spend of this launch cannot be read, so the daily budget cannot count it: "
        + " Also: ".join(unreadable)
    )
    result.paused = _pause_queue(queue, Admission(allowed=False, reason=detail), obs, root_dir)
    result.needs_human = True
    result.inbox_items += (
        _file_inbox_item(
            root_dir,
            kind_name="budget_overrun",
            title="Continuous intake paused: unreadable run spend",
            detail=detail,
            dedupe_key=f"serve-spend:{detail}",
            evidence={"unreadable": list(unreadable)},
        ),
    )


def _poison_for_merge_gate(
    queue: Queue,
    ledger: SpendLedger,
    candidate: QueueItem,
    gate: MergeGate,
) -> QueueItem | None:
    """Make a refused item terminal, unless the refusal clears itself.

    Called with the queue mutex held. Returns the poisoned item, or None
    when nothing was poisoned, which is the caller's signal that this
    refusal is a WAIT: nothing claimed, no attempt charged, re-checked on
    the next poll.

    The split is between a refusal about the repo's RESOLVED
    configuration and one about being unable to READ it. A ``create_prs
    = false`` repo does not stop conflicting with a promised human merge
    gate on its own, so that item needs a person and is terminal. A
    quoted boolean is a typo that clears the moment the file is fixed,
    and poison is documented as the state that is never retried
    automatically, so poisoning there would cost one terminal item per
    poll for as long as the typo stood and leave every one of them behind
    after the fix.
    """
    if gate.unreadable_section:
        return None
    queue.poison(
        candidate,
        reason=f"merge-gate conflict: {gate.refusal}",
        actor="serve",
    )
    ledger.record_terminal(poisoned=True)
    return queue.get(candidate.item_id)


def _report_merge_gate_refusal(
    root_dir: Path,
    result: CycleResult,
    candidate: QueueItem,
    gate: MergeGate,
    refused_item: QueueItem | None,
    observer: ServeObserver,
) -> None:
    """Tell the operator and the front-end about a poisoned item.

    Nothing to report when ``refused_item`` is None: the refusal was a
    wait, the item is untouched, and the caller has already put the
    message on ``CycleResult.skipped``, which is where the daemon prints
    a self-clearing refusal.

    Runs OUTSIDE the queue mutex on purpose: the writeback makes two
    ``gh`` calls at the configured timeout, and holding the mutex across
    them blocks every local queue transition (#187 F10).
    """
    if refused_item is None:
        return
    observer.err(f"{candidate.item_id}: {gate.refusal}")
    result.needs_human = True
    result.inbox_items += (
        _file_inbox_item(
            root_dir,
            kind_name="merge_gate",
            title=f"Queue item {short_item_id(candidate.item_id)} needs a merge decision",
            detail=gate.refusal,
            dedupe_key=f"queue-merge-gate:{candidate.item_id}",
            evidence={"item_id": candidate.item_id},
        ),
    )
    _report_remote_outcome(
        root_dir,
        refused_item,
        state="poison",
        detail=gate.refusal,
        observer=observer,
    )


def _record_count_failure(
    root_dir: Path,
    streak: OpenPrCountStreak | None,
    result: CycleResult,
) -> None:
    """File one inbox item once the open-PR count has been unusable N polls.

    A no-op for every other wait, and for the second and later polls of
    one streak. This is the only wait that files anything, because it is
    the only one that does not clear itself: `gh` off launchd's PATH and
    an expired `gh` token both hold forever, and until this the daemon
    waited on them with the queue unpaused, the inbox empty,
    ``needs_human`` False, the exit code 0 and one WARN line per poll in
    ``serve.err.log`` as the only evidence anywhere (#354 S5).

    It writes ``result`` rather than returning, so the caller adds no
    branch: ``serve_cycle`` is grandfathered over both complexity
    ratchets and any new branch in it is a regression the hook refuses.

    The reason comes off the STREAK, not off the caller's refusal
    string. Any wait gate's reason reaches this function, and only the
    open-PR bound's belongs in an item about counting pull requests.

    ``filed`` is set only when the inbox returns a real id, and only that
    id is appended to ``result.inbox_items``. ``_file_inbox_item``
    swallows every failure and returns ``""``, and returns ``""``
    without trying when ``[inbox] enabled = false``, so marking the
    streak before the write disarmed the alarm at the exact moment it
    was needed and put an id that names nothing into the result.
    ``needs_human`` is set either way: a human is needed whether or not
    the record of it reached the disk.

    An existing kind (``halted_run``) and a fixed dedupe key on purpose:
    no new outcome vocabulary (doctrine 6), and a repeat of a still-open
    item bumps its occurrence count rather than adding a row, so two
    daemons - or one restarted into the same fault - do not stack items.
    """
    if streak is None or not streak.should_file():
        return
    result.needs_human = True
    item_id = _file_inbox_item(
        root_dir,
        kind_name="halted_run",
        title="Continuous intake cannot count open pull requests",
        detail=(
            f"{streak.consecutive} polls in a row: {streak.reason}. The daemon "
            "is waiting and will admit nothing until the count works. If `gh` "
            "cannot run, check that it is on the daemon's PATH and "
            "authenticated; if the reason above is a full page, the repository "
            "has more open pull requests than the scan window. Either way, "
            "[serve] max_open_prs = 0 switches the bound off."
        ),
        dedupe_key="serve-open-pr-count-failure",
        evidence={"reason": streak.reason, "consecutive": streak.consecutive},
    )
    if not item_id:
        return
    streak.filed = True
    result.inbox_items += (item_id,)


def _report_remote_outcome(
    root_dir: Path,
    item: QueueItem | None,
    *,
    state: str,
    detail: str,
    observer: ServeObserver,
) -> None:
    """Tell the source front-end what happened, best effort.

    R8.6 requires the adapters be strictly additive, so this swallows
    every failure into a warning: the queue transition it describes has
    already committed locally, and a GitHub outage must not be able to
    roll it back or halt the daemon.
    """
    if item is None or item.source is not ItemSource.GITHUB:
        return
    try:
        from kstrl.intake_gh import GitHubIntakeConfig, report_outcome

        config = GitHubIntakeConfig.load(root_dir)
        if not config.enabled:
            return
        error = report_outcome(
            item,
            state=state,
            detail=detail,
            config=config,
            root_dir=root_dir,
        )
        if error:
            observer.warn(f"  writeback to {item.source_ref} failed: {error}")
    except Exception as exc:  # noqa: BLE001 - additive by contract
        observer.warn(f"  writeback to {item.source_ref} raised: {exc}")


def _report_reaped(
    root_dir: Path,
    queue: Queue,
    result: CycleResult,
    obs: ServeObserver,
) -> None:
    """Narrate what recovery did, and file the poisons a human must see.

    Its own function because both complexity ratchets in
    `.pre-commit-config.yaml` fail on any REGRESSION, and `serve_cycle`
    measured 31 cyclomatic / 50 cognitive before #364 added the config
    refusal above. Moving this block takes it to 29 / 48. Nothing here
    changed: the caller is where it was, between the recovery lock and
    the intake stage.
    """
    if result.swept_staging:
        obs.info(f"Swept {result.swept_staging} abandoned staging item(s)")
    for item_id in result.reaped.requeued + result.reaped.failed_for_retry:
        obs.warn(f"Reaped {item_id}: owner gone, requeued")
    for item_id in result.reaped.poisoned:
        obs.err(f"Reaped {item_id}: no attempts left, poisoned")
        result.needs_human = True
        _report_remote_outcome(
            root_dir,
            queue.get(item_id),
            state="poison",
            detail=("The run was interrupted and the item had no attempts left."),
            observer=obs,
        )
        result.inbox_items += (
            _file_inbox_item(
                root_dir,
                kind_name="halted_run",
                title=f"Queue item {short_item_id(item_id)} poisoned after an interrupted run",
                detail=(
                    "The run was interrupted and the item had no attempts left. "
                    "Inspect with `ks queue show` and requeue with "
                    "`ks queue retry --reset-attempts` if it should run again."
                ),
                dedupe_key=f"queue-poison:{item_id}",
                evidence={"item_id": item_id, "cause": "interrupted run"},
            ),
        )


def _settle_unfailed(
    root_dir: Path,
    queue: Queue,
    ledger: SpendLedger,
    running: QueueItem,
    verdict: Outcome,
    pr_urls: tuple[str, ...],
    run_id: str,
    architect_run_id: str,
    obs: ServeObserver,
    result: CycleResult,
) -> bool:
    """Finish an item whose run did not fail: done, awaiting approval, or
    awaiting an answer.

    True when the cycle is over for this item (including the item having
    vanished from the queue mid-run); False sends the caller down the
    failure branch. #465 added the second state: a park is neither a
    finish nor a failure, so it is not poisoned, it does not touch the
    poison streak, and it is not retried. #644 added the third on the same
    terms, for an architect escalation: the child already filed the one
    inbox row that names `ks queue answer`, so nothing is filed here. The
    remote writeback runs after the mutex is released (#187 F10); a
    GitHub-sourced item awaiting an answer gets the ``awaiting_answer``
    label and a comment naming the local command, and the claim that
    re-runs it moves the label back to ``running``. #654 added a red base,
    which is not the item's failure either: see ``_requeue_on_red_base``.
    """
    if verdict.verdict is Verdict.RED_BASE:
        _requeue_on_red_base(root_dir, queue, running, verdict, obs, result)
        return True
    with queue_lock(root_dir, blocking=True):
        current = queue.get(running.item_id)
        if current is None:
            obs.err(f"{running.item_id} vanished mid-run")
            return True
        if verdict.verdict is Verdict.SUCCESS:
            queue.finish_ok(current, actor="serve", pr_urls=pr_urls)
            ledger.record_terminal(poisoned=False)
            state, said, detail = "done", "done", "The factory run completed."
        elif verdict.verdict is Verdict.AWAITING_APPROVAL:
            queue.await_approval(
                current, reason=verdict.reason, run_id=run_id, actor="serve", pr_urls=pr_urls
            )
            result.needs_human = True
            state, said, detail = "awaiting_approval", "awaiting approval", verdict.reason
        elif verdict.verdict is Verdict.AWAITING_ANSWER:
            queue.await_answer(
                current, reason=verdict.reason, run_id=architect_run_id, actor="serve"
            )
            result.needs_human = True
            state, detail = "awaiting_answer", verdict.reason
            said = (
                "awaiting an answer: see `ks inbox ls`, then "
                f"`ks queue answer {running.item_id} <answered spec file>`"
            )
        else:
            return False
        finished = queue.get(running.item_id)
    obs.info(f"  {running.item_id} {said}")
    _report_remote_outcome(root_dir, finished, state=state, detail=detail, observer=obs)
    return True


def _requeue_on_red_base(
    root_dir: Path,
    queue: Queue,
    running: QueueItem,
    verdict: Outcome,
    obs: ServeObserver,
    result: CycleResult,
) -> None:
    """Pause claims and requeue an item whose run refused on a red base (#654).

    Owner decision 6: a red base is repo-wide, so poisoning would burn one
    item per poll and trip the breaker. The item goes back to queued through
    failed, the only legal way out of running, and keeps the attempt it was
    charged, because its architect was paid. Claims pause on the queue's own
    marker with no ``resume_after``, so only `ks queue resume` lifts it and
    the journal records who; the pause is written first, so it holds even if
    the item moved. The poison streak is not touched. One inbox item per
    measured base commit is filed, and a later refusal on the same commit
    bumps it.
    """
    evidence = verdict.evidence
    sha = str(evidence.get("base_sha", ""))
    paused = (
        f"the base branch {evidence.get('base_branch', '')} at {sha[:12]} fails its own "
        "gates; nothing is claimed until `ks queue resume`"
    )
    with queue_lock(root_dir, blocking=True):
        queue.pause(reason=paused, actor="serve")
        current = queue.get(running.item_id)
        if current is not None:
            failed = queue.finish_failed(current, error=verdict.reason, actor="serve")
            queue.requeue(failed, reason=f"red base: {verdict.reason}", actor="serve")
        requeued = queue.get(running.item_id)
    result.paused = paused
    result.needs_human = True
    obs.warn(f"Queue paused: {paused}")
    obs.warn(f"  {running.item_id} requeued, not judged: {verdict.reason}")
    detail = (
        f"{verdict.reason}\n\nQueue item {running.item_id} is queued again. Make the "
        "base branch pass its gates, then run `ks queue resume`."
    )
    result.inbox_items += (
        _file_inbox_item(
            root_dir,
            kind_name="halted_run",
            title="Continuous intake paused: the base branch fails its own gates",
            detail=detail,
            dedupe_key=f"serve-red-base:{sha}",
            evidence={**evidence, "item_id": running.item_id},
            run_id=str(evidence.get("run_id", "")),
        ),
    )
    _report_remote_outcome(root_dir, requeued, state="failed", detail=detail, observer=obs)


def settle_approval_run(
    root_dir: Path,
    queue: Queue,
    *,
    parked_run_id: str,
    returncode: int,
    actor: str,
    observer: ServeObserver,
) -> QueueItem | None:
    """Move the queue item a merge-gate decision ran for out of awaiting_approval (#464).

    Called by `ks inbox approve` and `ks inbox reject` once the factory
    run they started has exited. The item is the one ``serve`` parked on
    ``parked_run_id``, joined by the run id ``Queue.await_approval``
    recorded rather than by counting awaiting items. A park no daemon
    made (a manual `ks factory`) has no item, and this returns None.

    Read from the manifest the run left, in this order:

    - a component still awaits approval: a pre-spend check refused the
      run, or it merged the approved component and parked a dependent.
      The item stays, linked to the manifest's current run id.
    - exit 0: done, with every PR URL the manifest records.
    - anything else (a rejection, a branch that moved, an unconfirmed
      merge): poison, with the classifier's reason. The run was not a
      queue attempt, so nothing retries it.

    The park is checked first because a refused run exits 2, and the
    classifier reads exit 2 before it reads the manifest.
    """
    linked = [
        item
        for item in queue.items((ItemState.AWAITING_APPROVAL,))
        if parked_run_id and item.last_run_id == parked_run_id
    ]
    if len(linked) != 1:
        return None
    manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
    run_id = _run_id_from_manifest(manifest_path)
    park = check_parked_merges(root_dir)
    verdict = classify_run(
        root_dir,
        run=RunOutcome(returncode=returncode),
        manifest_path=manifest_path,
        owned_run_ids=(run_id,) if run_id else (),
    )
    pr_urls = _pr_urls_from_manifest(manifest_path, True, observer)
    with queue_lock(root_dir, blocking=True):
        current = queue.get(linked[0].item_id)
        if current is None or current.state is not ItemState.AWAITING_APPROVAL:
            return None
        if not park.allowed:
            if run_id == current.last_run_id:
                return current
            queue.relink_run(current, run_id=run_id, reason=park.reason, actor=actor)
            state, detail = "awaiting_approval", park.reason
        elif verdict.verdict is Verdict.SUCCESS:
            queue.finish_ok(current, actor=actor, pr_urls=pr_urls)
            merged = ", ".join(pr_urls) or "no PR recorded"
            state, detail = "done", f"Approved at the merge gate and completed: {merged}."
        else:
            queue.poison(current, reason=verdict.reason, actor=actor)
            state, detail = "poison", verdict.reason
        settled = queue.get(current.item_id)
    observer.info(f"  queue item {current.item_id} {state}")
    _report_remote_outcome(root_dir, settled, state=state, detail=detail, observer=observer)
    return settled
