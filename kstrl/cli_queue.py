"""The ks queue command group: the verbs that move work items between states."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import click

from kstrl.cli_preflight import _reject_blank_project_name
from kstrl.cli_seam import _actor, _autonomy_ui, cli
from kstrl.config_report import UI_MODES
from kstrl.ui.base import UI
from kstrl.workqueue_items import ItemState


@cli.group(name="queue")
def queue_group() -> None:
    """Manage the continuous-intake work queue (R8.6).

    Work waits here instead of requiring a human to fire each run.
    `ks serve` drains it. Nothing in this group starts a factory run or
    spends anything: these verbs only move items between states.
    """


_queue_root_option = click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
_queue_ui_option = click.option(
    "--ui",
    type=click.Choice(UI_MODES),
    default="auto",
    help="UI mode",
)
_queue_no_color_option = click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)


def _queue_for(root: Path | None) -> tuple[Path, Any]:
    from kstrl.workqueue import Queue
    from kstrl.workqueue_items import QueueConfig

    root_dir = (root or Path.cwd()).resolve()
    return root_dir, Queue(root_dir, QueueConfig.load(root_dir))


def _resolve_queue_item(queue: Any, item_id: str, ui_impl: UI) -> Any:
    """Look up one item or exit; an ambiguous prefix is an error."""
    from kstrl.workqueue_items import QueueError

    try:
        item = queue.get(item_id)
    except QueueError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    if item is None:
        ui_impl.err(f"No queue item matching {item_id!r}")
        sys.exit(2)
    return item


@queue_group.command(name="add")
@click.argument("spec", type=click.Path(exists=True, path_type=Path))
@click.option("--priority", type=int, default=0, help="Higher runs first")
@click.option("--title", default="", help="Human label (default: spec filename)")
@click.option(
    "--project-name",
    default="",
    callback=_reject_blank_project_name,
    help="Factory project name (omit it and serve derives queue-<id>)",
)
@click.option(
    "--auto-merge/--stop-at-pr",
    "auto_merge",
    default=False,
    help=(
        "Request auto-merge when green (still gated by the autonomy "
        "ladder), or wait for a human merge approval (default)"
    ),
)
@click.option(
    "--max-attempts",
    type=click.IntRange(min=1),
    default=None,
    help="Execution attempts before poisoning (default: [queue] max_attempts)",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_add(
    spec: Path,
    priority: int,
    title: str,
    project_name: str,
    auto_merge: bool,
    max_attempts: int | None,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Enqueue a spec file.

    The spec is COPIED into the item, so editing or deleting the
    original afterwards cannot change what eventually runs.
    """
    from kstrl.workqueue_items import MergeDisposition, QueueError
    from kstrl.workqueue_store import queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    disposition = MergeDisposition.AUTO_MERGE if auto_merge else MergeDisposition.STOP_AT_PR
    try:
        with queue_lock(root_dir):
            # Under the lock, any staging directory is abandoned by
            # definition (R8.6 #185 F3), so this is the natural recovery
            # point for an enqueue killed mid-publish.
            queue.sweep_staging()
            item = queue.add(
                spec,
                title=title,
                priority=priority,
                project_name=project_name,
                merge_disposition=disposition,
                max_attempts=max_attempts,
                actor=_actor(),
            )
    except QueueError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    ui_impl.ok(f"Queued {item.item_id} - {item.title}")
    ui_impl.kv("merge", str(item.merge_disposition))
    ui_impl.kv("attempts allowed", str(item.max_attempts))
    if queue.is_paused():
        ui_impl.warn("Queue is paused; this item waits until `ks queue resume`.")
    sys.exit(0)


@queue_group.command(name="ls")
@click.option(
    "--state",
    "states",
    multiple=True,
    type=click.Choice([state.value for state in ItemState]),
    help="Filter by state (repeatable)",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_ls(
    states: tuple[str, ...],
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """List queue items in run order."""
    from kstrl.workqueue import summarize
    from kstrl.workqueue_items import ItemState

    _root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    selected: tuple[ItemState, ...] | None = None
    if states:
        try:
            selected = tuple(ItemState(value) for value in states)
        except ValueError as exc:
            ui_impl.err(str(exc))
            sys.exit(2)
    items = queue.items(selected)
    pause = queue.pause_state()
    if pause.active():
        detail = f" ({pause.reason})" if pause.reason else ""
        ui_impl.warn(f"Queue is PAUSED{detail}")
    if not items:
        ui_impl.ok("Queue is empty.")
        sys.exit(0)
    ui_impl.section("Queue")
    for item in items:
        attempts = f"{item.attempts}/{item.max_attempts}"
        ui_impl.info(
            f"  {item.item_id}  {str(item.state):<8} p{item.priority:<3} {attempts:<6} {item.title}"
        )
    ui_impl.info("")
    ui_impl.kv("summary", summarize(queue.counts()))
    sys.exit(0)


@queue_group.command(name="show")
@click.argument("item_id")
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_show(
    item_id: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Show one item in full, with its transition history."""
    _root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = _resolve_queue_item(queue, item_id, ui_impl)

    ui_impl.section(item.title)
    ui_impl.kv("id", item.item_id)
    ui_impl.kv("state", str(item.state))
    ui_impl.kv("priority", str(item.priority))
    ui_impl.kv("attempts", f"{item.attempts} of {item.max_attempts}")
    ui_impl.kv("merge", str(item.merge_disposition))
    ui_impl.kv("source", str(item.source) + (f" ({item.source_ref})" if item.source_ref else ""))
    ui_impl.kv("created", item.created_at)
    ui_impl.kv("updated", item.updated_at)
    if item.project_name:
        ui_impl.kv("project", item.project_name)
    if item.last_run_id:
        ui_impl.kv("last run", item.last_run_id)
    if item.lease_pid:
        ui_impl.kv(
            "lease",
            f"pid {item.lease_pid} on {item.lease_host} "
            f"until {item.lease_expires_at}" + (" (EXPIRED)" if item.lease_expired() else ""),
        )
    for url in item.pr_urls:
        ui_impl.kv("pr", url)
    if item.last_error:
        ui_impl.kv("last error", item.last_error)
    if item.poison_reason:
        ui_impl.kv("poisoned", item.poison_reason)
    ui_impl.kv("spec", str(queue.spec_path(item)))

    history = queue.journal_entries(item.item_id)
    if history:
        ui_impl.subsection("History")
        for entry in history:
            origin = entry.get("from") or "-"
            reason = entry.get("reason") or ""
            actor = entry.get("actor") or ""
            ui_impl.info(
                f"  {entry.get('ts', '')}  {origin} -> {entry.get('to', '')}"
                + (f"  ({reason})" if reason else "")
                + (f"  by {actor}" if actor else "")
            )
    sys.exit(0)


def _retry_refusal(item: Any) -> str:
    """Why `ks queue retry` refuses ``item``, naming the command that does apply."""
    from kstrl.workqueue_items import ItemState

    refusal = f"{item.item_id} is {item.state}; only failed or poisoned items can be retried"
    if item.state is ItemState.AWAITING_ANSWER:
        refusal += (
            "; it waits for the owner's answer: run "
            f"`ks queue answer {item.item_id} <answered spec file>`"
        )
    return refusal


@queue_group.command(name="answer")
@click.argument("item_id")
@click.argument("spec", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--reset-attempts",
    is_flag=True,
    help="Zero the attempt counter (explicit human decision to spend again)",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_answer(
    item_id: str,
    spec: Path,
    reset_attempts: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Answer an escalated item: replace its spec and send it back to queued.

    Only an item awaiting an answer can be answered (#644), or a poisoned
    item that an undecided spec_escalation row in the inbox names: the
    architect escalated, but the spec-issues write failed, so serve could
    not tell. SPEC is the answered spec; it replaces the item's queued
    copy, so the next `ks serve` cycle runs it. An item that has spent its
    attempts needs `--reset-attempts`, as `ks queue retry` does. Identical
    bytes are accepted and recorded as unchanged. The poison streak is
    not changed, as `ks queue retry` does not change it.
    """
    from kstrl.decisions import escalation_naming
    from kstrl.workqueue_items import ItemState, QueueError
    from kstrl.workqueue_store import queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    try:
        raw = spec.read_bytes()
    except OSError as exc:
        ui_impl.err(f"Could not read {spec}: {exc}")
        sys.exit(2)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        ui_impl.err(f"Could not read {spec}: {exc}")
        sys.exit(2)
    try:
        with queue_lock(root_dir):
            # Read under the lock, so the state checked is the state answered.
            item = _resolve_queue_item(queue, item_id, ui_impl)
            row_id = row_run = ""
            if item.state is ItemState.POISON:
                row = escalation_naming(root_dir, item.item_id)
                row_id, row_run = row.id, row.run_id
            record = queue.answer(
                item,
                text,
                actor=_actor(),
                reset_attempts=reset_attempts,
                escalation_row=row_id,
                escalated_run=row_run,
            )
    except (QueueError, OSError) as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    ui_impl.ok(
        f"Answered {item.item_id}: queued again ({item.attempts}/{item.max_attempts} attempts used)"
    )
    ui_impl.kv("spec before", record["spec_sha256_before"])
    ui_impl.kv("spec after", record["spec_sha256_after"])
    if record["unchanged"]:
        ui_impl.warn(
            "The answered spec is unchanged, so the architect reads the same text "
            "and may escalate the same question again."
        )
    sys.exit(0)


@queue_group.command(name="retry")
@click.argument("item_id")
@click.option(
    "--reset-attempts",
    is_flag=True,
    help="Zero the attempt counter (explicit human decision to spend again)",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_retry(
    item_id: str,
    reset_attempts: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Send a failed or poisoned item back to queued.

    A poisoned item that has already spent its attempts needs
    `--reset-attempts` to run again: re-queuing it otherwise would have
    it poisoned straight back without spending anything, which looks
    like the command silently failed.
    """
    from kstrl.workqueue_items import ItemState, QueueError
    from kstrl.workqueue_store import queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = _resolve_queue_item(queue, item_id, ui_impl)
    if item.state not in (ItemState.FAILED, ItemState.POISON):
        ui_impl.err(_retry_refusal(item))
        sys.exit(2)
    if not reset_attempts and item.attempts_remaining == 0:
        ui_impl.err(
            f"{item.item_id} has used all {item.max_attempts} attempts; "
            "pass --reset-attempts to authorize spending again"
        )
        sys.exit(2)
    try:
        with queue_lock(root_dir):
            queue.requeue(
                item,
                reason="retry (manual)",
                actor=_actor(),
                reset_attempts=reset_attempts,
            )
    except (QueueError, OSError) as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    ui_impl.ok(f"Requeued {item.item_id} ({item.attempts}/{item.max_attempts} attempts used)")
    sys.exit(0)


@queue_group.command(name="priority")
@click.argument("item_id")
@click.option(
    "--to",
    "priority",
    type=int,
    required=True,
    help="The new priority. Higher runs first; negative values are allowed",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_priority(
    item_id: str,
    priority: int,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Change a queued item's priority, keeping its id and history.

    Only a queued item can change: every other state is refused by name
    and nothing is written. The change is a row in `ks queue show`.
    """
    from kstrl.workqueue_items import QueueError
    from kstrl.workqueue_store import queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = _resolve_queue_item(queue, item_id, ui_impl)
    try:
        with queue_lock(root_dir):
            changed, old = queue.set_priority(item.item_id, priority, actor=_actor())
    except (QueueError, OSError) as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    if old == priority:
        ui_impl.info(f"{changed.item_id} is already at priority {priority}; nothing changed")
    else:
        ui_impl.ok(f"{changed.item_id} ({changed.title}) priority {old} -> {priority}")
    sys.exit(0)


@queue_group.command(name="rm")
@click.argument("item_id")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_rm(
    item_id: str,
    yes: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Delete an item and its spec."""
    from kstrl.workqueue_items import QueueError
    from kstrl.workqueue_store import queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = _resolve_queue_item(queue, item_id, ui_impl)
    if not yes and not click.confirm(
        f"Delete {item.item_id} ({item.title})?",
        default=False,
    ):
        ui_impl.info("Left alone.")
        sys.exit(0)
    try:
        with queue_lock(root_dir):
            queue.remove(item, actor=_actor())
    except (QueueError, OSError) as exc:
        # A deletion that failed must not print success: the operator
        # would believe the item is gone when it is still queued (#185 F6).
        ui_impl.err(f"Could not remove {item.item_id}: {exc}")
        sys.exit(2)
    ui_impl.ok(f"Removed {item.item_id}")
    sys.exit(0)


@queue_group.command(name="pause")
@click.option("--reason", default="", help="Why intake is paused")
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_pause(
    reason: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Stop admitting queued work.

    Does not touch anything already running - a pause is an admission
    gate, not a kill switch.
    """
    from kstrl.workqueue_store import queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    with queue_lock(root_dir):
        queue.pause(reason=reason, actor=_actor())
    ui_impl.ok("Queue paused; nothing new will be claimed.")
    if reason:
        ui_impl.kv("reason", reason)
    sys.exit(0)


@queue_group.command(name="resume")
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_resume(root: Path | None, ui: str, no_color: bool) -> None:
    """Start admitting queued work again.

    Also restarts the poison streak at 0, so the next serve cycle does not
    pause the queue again on the poisons the operator has looked at.
    """
    from kstrl.serve import ServeStateError, SpendLedger
    from kstrl.statedir import ControlStateError
    from kstrl.workqueue_items import ItemState
    from kstrl.workqueue_store import queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    with queue_lock(root_dir):
        # The streak first: the poison breaker reads it on every cycle, so
        # a resume that leaves it standing is undone by the next one (#707).
        try:
            cleared = SpendLedger(root_dir).reset_poison_streak()
        except (ServeStateError, ControlStateError, OSError) as exc:
            ui_impl.err(f"Queue NOT resumed: could not clear the poison streak: {exc}")
            sys.exit(2)
        queue.resume(actor=_actor(), detail={"consecutive_poison_cleared": cleared})
    waiting = len(queue.items((ItemState.QUEUED,)))
    ui_impl.ok(f"Queue resumed; {waiting} item(s) waiting.")
    if cleared:
        ui_impl.kv("poison streak", f"{cleared} cleared")
    sys.exit(0)


def _allowed_actors_kv(actors: list[str]) -> str:
    """Beside `label`: a config typo then prints differently from a
    working gate (#188 simplify pass item 3)."""
    return ", ".join(actors) if actors else "(empty: anyone who can apply the label may spend)"


@queue_group.command(name="sync")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Poll and report what would be enqueued, writing nothing",
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def queue_sync(
    dry_run: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Pull labelled GitHub issues into the queue (R8.6).

    Polls open issues carrying the trigger label and enqueues the ones
    not already seen. Remote items ALWAYS wait for a human merge approval.

    The trigger label is the authorization, and applying it needs only
    GitHub's Triage role. Set [intake_github] allowed_actors to the logins
    you trust to spend; an issue labelled by anyone else is refused and
    the refusal names the actor.
    """
    from kstrl.intake_gh import GitHubIntakeConfig, IntakeError
    from kstrl.intake_github import sync as run_sync
    from kstrl.workqueue_items import QueueError, QueueLockedError
    from kstrl.workqueue_store import queue_lock

    root_dir, queue = _queue_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    try:
        config = GitHubIntakeConfig.load(root_dir)
    except (IntakeError, ValueError) as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    if not config.enabled:
        ui_impl.err(
            "GitHub intake is off. Set [intake_github] enabled = true in "
            "kstrl.toml (or KSTRL_INTAKE_GITHUB_ENABLED=1)."
        )
        sys.exit(2)

    # --dry-run runs the PRODUCTION planner with writes disabled, rather
    # than a second decision tree. Review #187 F4/F11: the old dry-run
    # had its own logic that ignored the admission cap, and a dry run
    # that disagrees with the real thing is worse than none.
    if dry_run:
        config = replace(config, dry_run=True)

    try:
        with queue_lock(root_dir):
            result = run_sync(queue, config, root_dir)
    except QueueLockedError as exc:
        ui_impl.err(f"{exc}. Another queue operation is in progress; retry shortly.")
        sys.exit(2)
    except (QueueError, OSError) as exc:
        ui_impl.err(f"Sync failed: {exc}")
        sys.exit(2)

    heading = "Would sync from" if dry_run else "Sync from"
    ui_impl.section(f"{heading} {result.repo or 'unknown repo'}")
    ui_impl.kv("label", config.queued_label)
    ui_impl.kv("allowed_actors", _allowed_actors_kv(config.allowed_actors))
    ui_impl.kv("polled", str(result.polled))
    if dry_run:
        ui_impl.kv("would enqueue", str(len(result.would_enqueue)))
    else:
        ui_impl.kv("enqueued", str(len(result.enqueued)))

    for entry in result.planned:
        ref = entry.issue.source_ref(result.repo)
        verdict = "ENQUEUE" if entry.decision.admits else str(entry.decision)
        line = f"  {ref:<28} {verdict:<22} {entry.issue.title}"
        if entry.decision.admits:
            ui_impl.ok(line)
        elif entry.decision is entry.decision.__class__.REFUSE_UNAUTHORIZED:
            ui_impl.err(f"{line}\n      {entry.reason}")
        else:
            ui_impl.info(f"{line}  ({entry.reason})")
    if not result.planned:
        ui_impl.info("  nothing labelled")

    for error in result.errors:
        ui_impl.err(f"  {error}")
    # Nonzero on error so a cron/launchd wrapper notices. Partial success
    # is real: items already enqueued stay enqueued.
    sys.exit(1 if result.errors else 0)
