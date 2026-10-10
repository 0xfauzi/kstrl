"""R8.6 continuous intake: the local work queue substrate.

Intake before this module is one-shot - a human fires ``ks factory
--spec`` and the run ends. The queue is what lets work arrive without a
human firing each run, so it is the survival capability R8.6 exists to
add, not plumbing around one.

**Why a directory queue and not a library.** Queue libraries were
evaluated and rejected in the R8.6 verdict (litequeue is near-dormant and
conflicts with our Python floor; persist-queue buys nothing at a
concurrency of one; huey inverts control - it would own the process the
factory needs to own). A maildir-style directory tree gives durability,
crash recovery, and human inspectability with no dependency and no
schema migration story.

**Ordering is a money-safety property, not a style choice.** Every
transition writes ``meta.json`` FIRST and renames SECOND, because the
rename is the commit point. ``attempts`` is therefore incremented before
the rename that starts execution: a crash in the window over-counts an
attempt (the item gets one fewer retry - safe) instead of under-counting
one (the item retries without the attempt being recorded - an unbounded
retry loop). A measured factory engineer iteration costs ~$1.70-2.60 on
a first attempt and $3.99-7.42 on a retry, so an uncounted attempt is
not a bookkeeping slip.

The retry POLICY - which failures may be retried at all - deliberately
does not live here. This module records attempts and moves items; R8.6
PR 2 (``ks serve``) decides, on positive evidence only, whether a
failure was an infrastructure error. See ``docs/dark-factory-roadmap.md``
R8.6.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kstrl.atomicio import atomic_write_text
from kstrl.workqueue_items import (
    _LEGAL_TRANSITIONS,
    ALL_STATES,
    ItemSource,
    ItemState,
    JournalEntry,
    MergeDisposition,
    QueueBudgetExhausted,
    QueueError,
    QueueItem,
    _iso,
    _utc_now,
    is_safe_component,
    mint_item_id,
)
from kstrl.workqueue_store import (
    META_FILENAME,
    QueueStore,
    atomic_write,
)


class Queue(QueueStore):
    """Maildir-style work queue over ``.kstrl/queue/``.

    Reads scan the state directories; writes rename an item directory
    between them. No method rewrites history - the journal is
    append-only and the item directories carry current state.
    """

    # --------------------------------------------------------------- write

    def _write_meta(self, item: QueueItem, directory: Path) -> None:
        atomic_write(
            directory / META_FILENAME,
            json.dumps(item.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )

    def add(
        self,
        spec_source: Path | str,
        *,
        title: str = "",
        priority: int = 0,
        merge_disposition: MergeDisposition = MergeDisposition.STOP_AT_PR,
        source: ItemSource = ItemSource.LOCAL,
        source_ref: str = "",
        target_repo: str = "",
        project_name: str = "",
        max_attempts: int | None = None,
        spec_filename: str = "spec.md",
        design_acceptance: bool = False,
        actor: str = "",
    ) -> QueueItem:
        """Enqueue a spec.

        ``spec_source`` is either a path to copy or literal spec text.
        The spec is COPIED into the item directory rather than
        referenced: an item whose spec file was edited or deleted after
        enqueue would run something other than what was reviewed.
        """
        if isinstance(spec_source, Path):
            content = spec_source.read_text(encoding="utf-8")
            spec_filename = spec_source.name
            resolved_title = title or spec_source.stem
        else:
            content = spec_source
            resolved_title = title or "untitled"
        if not content.strip():
            raise QueueError("refusing to enqueue an empty spec")
        # The text form of this API is what PR 3's remote adapters call,
        # so the filename is untrusted: a caller-supplied
        # "../../../escaped.md" wrote outside the queue while still
        # publishing a normal-looking item (#185 F2).
        if not is_safe_component(spec_filename):
            raise QueueError(f"spec filename must be a plain basename, got {spec_filename!r}")
        resolved_attempts = self.config.max_attempts if max_attempts is None else max_attempts
        # QueueConfig rejects this, but a per-item override bypassed it and
        # admitted an item with no execution budget at all (#185 F4).
        if resolved_attempts < 1:
            raise QueueError(f"max_attempts must be >= 1, got {resolved_attempts}")

        now = _iso(_utc_now())
        item = QueueItem(
            item_id=mint_item_id(),
            title=resolved_title,
            spec_filename=spec_filename,
            state=ItemState.QUEUED,
            priority=priority,
            created_at=now,
            updated_at=now,
            max_attempts=resolved_attempts,
            merge_disposition=merge_disposition,
            source=source,
            source_ref=source_ref,
            target_repo=target_repo,
            project_name=project_name,
            design_acceptance=design_acceptance,
        )

        self.ensure_dirs()
        directory = self.state_path(ItemState.QUEUED) / item.item_id
        if directory.exists():
            raise QueueError(f"queue item {item.item_id} already exists")
        # Build under the staging directory and rename into place, so a
        # scan never sees an item whose spec has not landed yet. Staging
        # sits outside the state directories entirely (#185 F3).
        staging = self.staging_path / item.item_id
        staging.mkdir(parents=True, exist_ok=False)
        try:
            atomic_write(staging / spec_filename, content)
            self._write_meta(item, staging)
            os.replace(str(staging), str(directory))
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        self._journal(
            JournalEntry(
                ts=now,
                item_id=item.item_id,
                from_state="",
                to_state=str(ItemState.QUEUED),
                reason="added",
                actor=actor,
                attempts=item.attempts,
                detail={"source": str(source), "source_ref": source_ref},
            )
        )
        return item

    def transition(
        self,
        item: QueueItem,
        to_state: ItemState,
        *,
        reason: str = "",
        actor: str = "",
        charge_attempt: bool = False,
        detail: dict[str, Any] | None = None,
        **updates: Any,
    ) -> QueueItem:
        """Move ``item`` to ``to_state``, recording why.

        Writes ``meta.json`` first and renames second - the rename is the
        commit point (see the module docstring). ``charge_attempt``
        increments ``attempts`` in the pre-rename write, so an interrupted
        transition over-counts rather than under-counts. ``detail`` is
        journalled beside the transition; omitted or ``None`` journals
        the field's own default (an empty dict).
        """
        from_state = item.state
        legal = _LEGAL_TRANSITIONS.get(from_state, frozenset())
        if to_state not in legal:
            allowed = ", ".join(sorted(str(s) for s in legal)) or "nothing"
            raise QueueError(
                f"illegal queue transition {from_state} -> {to_state} "
                f"for {item.item_id} (legal: {allowed})"
            )

        source_dir = self.item_dir(item)
        if not source_dir.is_dir():
            raise QueueError(
                f"queue item {item.item_id} is not in {from_state} (expected {source_dir})"
            )
        target_dir = self.state_path(to_state) / item.item_id
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            raise QueueError(f"queue item {item.item_id} already exists in {to_state}")

        for key, value in updates.items():
            if not hasattr(item, key):
                raise QueueError(f"unknown QueueItem field {key!r}")
            setattr(item, key, value)
        if charge_attempt:
            item.attempts += 1
        item.state = to_state
        item.updated_at = _iso(_utc_now())

        # Order is load-bearing: meta first (so a charged attempt is
        # durable before any spend), rename second (the commit).
        self._write_meta(item, source_dir)
        try:
            os.replace(str(source_dir), str(target_dir))
        except OSError:
            # The move did not commit, so the in-memory object must stop
            # claiming it did - a caller that kept using this item would
            # otherwise address the wrong directory. ``attempts`` is
            # deliberately NOT rolled back: it is already durable on disk
            # and an over-count is the safe direction.
            item.state = from_state
            raise

        self._journal(
            JournalEntry(
                ts=item.updated_at,
                item_id=item.item_id,
                from_state=str(from_state),
                to_state=str(to_state),
                reason=reason,
                actor=actor,
                attempts=item.attempts,
                detail=detail or {},
            )
        )
        return item

    def lease(
        self,
        item: QueueItem,
        *,
        pid: int = 0,
        host: str = "",
        actor: str = "",
    ) -> QueueItem:
        """Claim a queued item for this worker. Spends nothing."""
        import socket

        expires = _utc_now() + timedelta(seconds=self.config.lease_ttl_seconds)
        return self.transition(
            item,
            ItemState.LEASED,
            reason="leased",
            actor=actor,
            lease_pid=pid or os.getpid(),
            lease_host=host or socket.gethostname(),
            lease_expires_at=_iso(expires),
        )

    def start(self, item: QueueItem, *, run_id: str = "", actor: str = "") -> QueueItem:
        """Begin execution. THIS is where the attempt is charged.

        Charged here rather than on completion because completion is the
        step a crash, a sleep, or a killed process can skip. An attempt
        that ran but was never counted is an unbounded retry loop.

        ``max_attempts`` is enforced HERE, at the spending boundary, not
        only in the CLI's retry policy. Review #185 F5 showed the bound
        was advertised but not enforced: start -> fail -> requeue ->
        lease -> start produced a running item with ``attempts == 2``
        against ``max_attempts == 1``. A bound that only holds when every
        caller remembers to check it is not a bound, and the callers here
        will be an unattended daemon and a reaper.
        """
        if item.attempts_remaining <= 0:
            raise QueueBudgetExhausted(
                f"{item.item_id} has used all {item.max_attempts} attempts; "
                "refusing to start (poison it or reset attempts explicitly)"
            )
        return self.transition(
            item,
            ItemState.RUNNING,
            reason="started",
            actor=actor,
            charge_attempt=True,
            last_run_id=run_id,
        )

    def adopt_lease(
        self,
        item: QueueItem,
        *,
        pid: int,
        host: str = "",
        actor: str = "",
    ) -> QueueItem:
        """Re-point a held lease at the process actually doing the work.

        Not a transition: the item stays where it is and only its lease
        fields move, so this is the one write that rewrites ``meta.json``
        in place. Needed because ``lease``/``start`` record the DAEMON's
        pid, while the run executes in a child process. Review #186 F1:
        if the daemon dies and the child survives, a successor's reaper
        sees the daemon gone, judges the lease dead, and requeues a run
        that is still executing - two factories on one repo.

        Refuses unless the item is leased or running: adopting a lease on
        a queued item would invent one.
        """
        if item.state not in (ItemState.LEASED, ItemState.RUNNING):
            raise QueueError(f"cannot adopt a lease on {item.item_id} in state {item.state}")
        import socket

        directory = self.item_dir(item)
        if not directory.is_dir():
            raise QueueError(f"queue item {item.item_id} is not at {directory}")
        item.lease_pid = pid
        item.lease_host = host or socket.gethostname()
        item.lease_expires_at = _iso(_utc_now() + timedelta(seconds=self.config.lease_ttl_seconds))
        item.updated_at = _iso(_utc_now())
        self._write_meta(item, directory)
        self._journal(
            JournalEntry(
                ts=item.updated_at,
                item_id=item.item_id,
                from_state=str(item.state),
                to_state=str(item.state),
                reason="lease adopted by the run process",
                actor=actor,
                attempts=item.attempts,
                detail={"lease_pid": pid},
            )
        )
        return item

    def finish_ok(
        self,
        item: QueueItem,
        *,
        actor: str = "",
        pr_urls: tuple[str, ...] = (),
    ) -> QueueItem:
        """A green finish, unioned with any PR the run left behind."""
        union = tuple(dict.fromkeys(item.pr_urls + pr_urls))
        return self.transition(
            item,
            ItemState.DONE,
            reason="completed",
            actor=actor,
            last_error="",
            pr_urls=union,
            detail={"pr_urls": list(union)} if union else None,
        )

    def finish_failed(
        self,
        item: QueueItem,
        *,
        error: str = "",
        actor: str = "",
        pr_urls: tuple[str, ...] = (),
    ) -> QueueItem:
        """A red finish that MAY be retried, unioned with any PR the run left behind."""
        union = tuple(dict.fromkeys(item.pr_urls + pr_urls))
        return self.transition(
            item,
            ItemState.FAILED,
            reason="failed",
            actor=actor,
            last_error=error,
            pr_urls=union,
            detail={"pr_urls": list(union)} if union else None,
        )

    def await_approval(
        self,
        item: QueueItem,
        *,
        reason: str,
        run_id: str,
        actor: str = "",
        pr_urls: tuple[str, ...] = (),
    ) -> QueueItem:
        """A run that parked work at the merge gate (#465).

        Not a failure and not a finish: the work passed every gate and
        waits for a human. ``reason`` is journalled so the item's history
        says what it waits for. ``run_id`` is the run that parked it, and
        the only thing that joins this item to the approval (#464): the
        approval run looks the item up by the manifest's run id.
        """
        union = tuple(dict.fromkeys(item.pr_urls + pr_urls))
        return self.transition(
            item,
            ItemState.AWAITING_APPROVAL,
            reason="awaiting approval",
            actor=actor,
            pr_urls=union,
            last_run_id=run_id,
            detail={"reason": reason, "run_id": run_id},
        )

    def await_answer(
        self,
        item: QueueItem,
        *,
        reason: str,
        run_id: str,
        actor: str = "",
    ) -> QueueItem:
        """A run whose architect escalated a question to the owner (#644).

        Not a failure: the halt is the architect's judgement, and the item
        waits for `ks queue answer`. ``run_id`` is the decompose run that
        escalated, where the question and the prompt the architect read are
        recorded; "" when the launch window did not hold exactly one.
        """
        return self.transition(
            item,
            ItemState.AWAITING_ANSWER,
            reason="awaiting an answer",
            actor=actor,
            last_run_id=run_id,
            detail={"reason": reason, "run_id": run_id},
        )

    def answer(
        self,
        item: QueueItem,
        text: str,
        *,
        actor: str = "",
        reset_attempts: bool = False,
        escalation_row: str = "",
        escalated_run: str = "",
    ) -> dict[str, Any]:
        """Replace an awaiting item's spec with ``text`` and requeue it (#644).

        Call under ``queue_lock`` with an item read under it. Every check
        runs before anything is written, so a refusal leaves ``meta.json``
        and the spec as they were. The spec is written BEFORE the rename:
        a crash between the two leaves the item waiting with the new text,
        and answering again with the same file finishes the move, which is
        why identical bytes are journalled as ``unchanged`` rather than
        refused. Returns the journalled detail.

        A POISON item is answered only with ``escalation_row``, the id of the
        undecided spec_escalation inbox row that names it, which the caller
        read with ``decisions.escalation_naming`` (#644, owner decision 2(c)).
        ``escalated_run`` is that row's run; an awaiting item's own
        ``last_run_id`` names it otherwise.
        """
        escalated = item.state is ItemState.POISON and bool(escalation_row)
        if item.state is not ItemState.AWAITING_ANSWER and not escalated:
            raise QueueError(
                f"{item.item_id} is {item.state}; only an item awaiting an answer can be "
                "answered, or a poisoned item an undecided spec_escalation row names"
            )
        if not text.strip():
            raise QueueError(f"the answered spec is empty; {item.item_id} keeps its spec")
        if not reset_attempts and item.attempts_remaining <= 0:
            raise QueueError(
                f"{item.item_id} has used all {item.max_attempts} attempts; "
                "pass --reset-attempts to authorize spending again"
            )
        spec = self.spec_path(item)
        before = hashlib.sha256(spec.read_bytes()).hexdigest()
        after = hashlib.sha256(text.encode("utf-8")).hexdigest()
        atomic_write_text(spec, text)
        detail: dict[str, Any] = {
            "spec_sha256_before": before,
            "spec_sha256_after": after,
            "unchanged": before == after,
            "escalated_run": escalated_run or item.last_run_id,
            "escalation_row": escalation_row,
        }
        updates: dict[str, Any] = {
            "lease_pid": 0,
            "lease_host": "",
            "lease_expires_at": "",
            "not_before": "",
            "poison_reason": "",
        }
        if reset_attempts:
            updates["attempts"] = 0
        self.transition(
            item, ItemState.QUEUED, reason="answered", actor=actor, detail=detail, **updates
        )
        return detail

    def relink_run(
        self, item: QueueItem, *, run_id: str, reason: str, actor: str = ""
    ) -> QueueItem:
        """Point an awaiting item at the run that parked its next component (#464).

        An approval run merges the approved component and continues; when
        a dependent meets the merge gate it parks under the approval run's
        own id, and the next `ks inbox approve` finds the item by that id.
        Not a transition, so ``meta.json`` is rewritten in place, as
        ``adopt_lease`` does. Refuses unless the item awaits approval.
        """
        if item.state is not ItemState.AWAITING_APPROVAL:
            raise QueueError(f"cannot relink {item.item_id} in state {item.state}")
        directory = self.item_dir(item)
        if not directory.is_dir():
            raise QueueError(f"queue item {item.item_id} is not at {directory}")
        item.last_run_id = run_id
        item.updated_at = _iso(_utc_now())
        self._write_meta(item, directory)
        self._journal(
            JournalEntry(
                ts=item.updated_at,
                item_id=item.item_id,
                from_state=str(item.state),
                to_state=str(item.state),
                reason="parked again",
                actor=actor,
                attempts=item.attempts,
                detail={"run_id": run_id, "reason": reason},
            )
        )
        return item

    def set_priority(self, item_id: str, priority: int, *, actor: str) -> tuple[QueueItem, int]:
        """Change a queued item's priority in place (#650). The caller holds ``queue_lock``.

        Takes an id, never an item: the item is re-read here, under the
        caller's lock, so a copy read before the lock cannot be written
        back over a newer ``meta.json``. Only a queued item's priority is
        ever read (``next_ready`` scans ``queued/`` alone), and a leased
        item's ``meta.json`` is rewritten by ``start`` from serve's own
        copy, which would undo the change, so every other state is
        refused by name.

        Not a transition: the item stays in ``queued/``, ``meta.json`` is
        rewritten in place and the journal gets a same-state row, as
        ``relink_run`` does. Returns the item and its old priority. An
        unchanged value writes nothing.
        """
        item = self.get(item_id)
        if item is None or item.item_id != item_id:
            raise QueueError(f"{item_id} is no longer in the queue; nothing changed")
        if item.state is not ItemState.QUEUED:
            raise QueueError(
                f"{item_id} is {item.state}; only a queued item's priority can change, "
                "so nothing changed"
            )
        old = item.priority
        if old == priority:
            return item, old
        item.priority = priority
        item.updated_at = _iso(_utc_now())
        self._write_meta(item, self.item_dir(item))
        self._journal(
            JournalEntry(
                ts=item.updated_at,
                item_id=item.item_id,
                from_state=str(item.state),
                to_state=str(item.state),
                reason=f"priority {old} -> {priority}",
                actor=actor,
                attempts=item.attempts,
                detail={"priority_from": old, "priority_to": priority},
            )
        )
        return item, old

    def poison(
        self,
        item: QueueItem,
        *,
        reason: str,
        actor: str = "",
    ) -> QueueItem:
        """Park an item that must never be retried automatically.

        The terminal state for spec-level failures and for anything whose
        failure could not be positively classified. Requires a reason:
        an item a human has to look at should say what it is waiting for.
        """
        if not reason.strip():
            raise QueueError("poison requires a reason")
        return self.transition(
            item,
            ItemState.POISON,
            reason="poisoned",
            actor=actor,
            poison_reason=reason,
        )

    def requeue(
        self,
        item: QueueItem,
        *,
        reason: str = "requeued",
        actor: str = "",
        reset_attempts: bool = False,
        not_before: str | None = None,
    ) -> QueueItem:
        """Send an item back to ``queued/``.

        ``reset_attempts`` is for an explicit human retry only. The
        automatic paths never reset the counter - a retry policy that
        can zero its own bound is not a bound.
        """
        updates: dict[str, Any] = {
            "lease_pid": 0,
            "lease_host": "",
            "lease_expires_at": "",
        }
        if not_before is not None:
            updates["not_before"] = not_before
        if reset_attempts:
            updates["attempts"] = 0
            updates["poison_reason"] = ""
            # A human authorizing a fresh run should not then wait out a
            # backoff computed for the automatic path.
            updates["not_before"] = ""
        return self.transition(
            item,
            ItemState.QUEUED,
            reason=reason,
            actor=actor,
            **updates,
        )

    def remove(self, item: QueueItem, *, actor: str = "") -> None:
        """Delete an item outright.

        Refuses while the item is running: removing the directory out
        from under a live worker loses the audit trail for money already
        spent.
        """
        if item.state is ItemState.RUNNING:
            raise QueueError(f"{item.item_id} is running; stop the run before removing it")
        directory = self.item_dir(item)
        # NOT ignore_errors: that swallowed permission and filesystem
        # failures, after which this journaled a "removed" record and the
        # CLI printed success for an item still on disk - a false operator
        # result AND a false audit trail (#185 F6).
        shutil.rmtree(directory)
        if directory.exists():
            raise QueueError(f"failed to remove {item.item_id}: {directory} still exists")
        self._journal(
            JournalEntry(
                ts=_iso(_utc_now()),
                item_id=item.item_id,
                from_state=str(item.state),
                to_state="removed",
                reason="removed",
                actor=actor,
                attempts=item.attempts,
            )
        )

    # --------------------------------------------------------------- report

    def counts(self) -> dict[ItemState, int]:
        tally = dict.fromkeys(ALL_STATES, 0)
        for item in self.items():
            tally[item.state] += 1
        return tally

    def next_ready(self, now: datetime | None = None) -> QueueItem | None:
        """The item a worker should claim next, or None.

        Returns None while paused: the pause is an admission gate, and
        checking it anywhere other than the point of claiming would let
        a racing worker slip one more run past a budget stop.

        Items still inside their retry backoff are skipped rather than
        blocking the queue behind them - a flaking item must not starve
        the ones that would succeed.
        """
        moment = now or _utc_now()
        if self.is_paused(moment):
            return None
        for item in self.items((ItemState.QUEUED,)):
            if item.ready_at(moment):
                return item
        return None


def summarize(counts: dict[ItemState, int]) -> str:
    """One-line queue summary, omitting empty states."""
    parts = [f"{count} {state}" for state, count in counts.items() if count]
    return ", ".join(parts) if parts else "empty"
