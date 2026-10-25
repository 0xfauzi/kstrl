"""ks inbox: list, show, approve, reject, snooze and retry the items that wait for the owner."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from kstrl.cli.cli_factory import factory
from kstrl.cli.cli_preflight import _HANDED_RUN_LOCK
from kstrl.cli.cli_queue import _queue_for
from kstrl.cli.cli_seam import _actor, _autonomy_ui, _load_manifest_or_exit, cli
from kstrl.cli.cli_serve import _ServeUiObserver
from kstrl.cli.config_report import UI_MODES
from kstrl.factory import (
    PLAN_GATE_KEY,
    RESUME_REFUSAL,
    plan_resume,
    prepare_retry,
    print_resume_plan,
    report_preflight,
    spec_pin_errors,
)
from kstrl.pipeline import option_argv
from kstrl.plan import MERGE_GATE_PARK_KEY, ComponentStatus, Manifest
from kstrl.proc import (
    FactoryLockHeldError,
    LeftoverAgentsError,
    RunLock,
    acquire_run_lock,
)
from kstrl.project import STACK_KEY, file_stack_item, load_stack, stack_in_force
from kstrl.ui import UI

if TYPE_CHECKING:
    from kstrl.owner import InboxItem


def _inbox_run_lock(root_dir: Path, ui_impl: UI, *, not_done: str, then: str) -> RunLock:
    """The run lock, taken before an inbox command changes anything, or exit 2 (#597).

    A live factory run saves its whole in-memory manifest, so a requeue
    written under it is undone at its next save. A merge decision, once
    recorded, is applied by whichever run picks it up next, not only one
    this command starts (measured, #596 item 2's D2): a decision recorded
    under a live run's lock would sit un-applied until some later run
    happened to pick it up, which is why this takes the lock before
    recording anything rather than after. `ks inbox` has no --force-lock,
    so the refusal names only waiting.
    """
    try:
        return acquire_run_lock(root_dir, ui_impl, force=False)
    except LeftoverAgentsError as exc:
        ui_impl.err(f"{not_done}: {exc}")
        sys.exit(2)
    except FactoryLockHeldError:
        ui_impl.err(
            f"{not_done}: a factory run holds the run lock on this root. Nothing was changed."
        )
        ui_impl.info(then)
        sys.exit(2)


def _refuse_stale_stack_approval(action: str, item_id: str, root: Path | None, ui_impl: UI) -> None:
    """Exit 2 when ``ks inbox approve`` names a stack item for text kstrl.toml no longer holds.

    Dispatched on the ``stack:`` key prefix, as a plan park is on its own
    (#696). Approving an old digest would make the newest approval one of
    a stack nobody runs, which un-confirms the one that is there.
    """
    if action != "approve":
        return
    root_dir, box = _inbox_for(root)
    item = box.get(item_id)
    if item is None or not item.dedupe_key.startswith(STACK_KEY):
        return
    stack = load_stack(root_dir)
    now = stack.digest if stack is not None else ""
    if now == item.evidence.get("stack_digest"):
        return
    reads = f"it now reads {now[:12]}" if now else "it has no [stack] now"
    then = (
        f"Write the table ks inbox show {item.id[:8]} prints into kstrl.toml, "
        "then approve it again."
    )
    if stack is not None:
        # The proposal says to edit the table if it is wrong: the edited
        # text is what a person confirms, on an item of its own (a user
        # run was sent to `ks factory` to file it).
        try:
            edited = file_stack_item(root_dir, stack)
        except Exception as exc:  # noqa: BLE001 - a filing that failed is said, never raised
            then = f"No item for it could be filed ({exc}); run ks factory to file one."
        else:
            then = (
                f"ks inbox approve {edited.id[:8]} confirms the text kstrl.toml holds; "
                f"ks inbox show {edited.id[:8]} prints it."
            )
    ui_impl.err(
        f"{item.id[:8]} confirms the [stack] {str(item.evidence.get('stack_digest'))[:12]}, "
        f"and {reads}: nothing was approved. {then}"
    )
    sys.exit(2)


@cli.group(name="inbox")
def inbox_group() -> None:
    """Triage what is waiting on a human (R8.3).

    One surface for policy exceptions, halted runs, unconfirmed merges,
    budget overruns, and autonomy demotions - the things that used to be
    discoverable only if you already knew where to look.
    """


_inbox_root_option = click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
_inbox_ui_option = click.option(
    "--ui",
    type=click.Choice(UI_MODES),
    default="auto",
    help="UI mode",
)
_inbox_no_color_option = click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)


def _inbox_for(root: Path | None) -> tuple[Path, Any]:
    """The inbox of ``root``, or exit 2 while an in-tree inbox log remains (#863).

    kstrl no longer moves ``.kstrl/inbox.jsonl`` to the control directory.
    Reading only the control directory would list an empty inbox while the
    items wait in the tree, so every ``ks inbox`` command refuses by name.
    """
    from kstrl.io import CONTROL_INBOX, legacy_control_refusal, legacy_control_remaining
    from kstrl.owner import Inbox, InboxConfig

    root_dir = (root or Path.cwd()).resolve()
    if CONTROL_INBOX in legacy_control_remaining(root_dir):
        click.echo(f"ERROR: {legacy_control_refusal(root_dir, (CONTROL_INBOX,))}", err=True)
        sys.exit(2)
    return root_dir, Inbox(root_dir, InboxConfig.load(root_dir))


@inbox_group.command(name="ls")
@click.option("--all", "show_all", is_flag=True, help="Include decided items")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_ls(
    show_all: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """List items awaiting a decision."""
    from kstrl.owner import summarize

    _root_dir, box = _inbox_for(root)
    ui_impl = _autonomy_ui(ui, no_color, sys.stdout)
    items = box.items() if show_all else box.open_items()
    if not items:
        ui_impl.ok("Inbox clear: nothing is waiting on you.")
        sys.exit(0)
    ui_impl.section("Inbox")
    for item in items:
        marker = {"high": "!", "normal": " ", "low": "."}.get(str(item.priority), " ")
        repeat = f" x{item.occurrences}" if item.occurrences > 1 else ""
        state = "" if item.is_open else f" [{item.status}]"
        ui_impl.info(f"  {marker} {item.id[:8]}  {str(item.kind):<19}{item.title}{repeat}{state}")
    ui_impl.info("")
    ui_impl.kv("summary", summarize(box.items()))
    if box.over_cap():
        ui_impl.warn(
            f"  Open items have reached the cap ({box.config.open_item_cap}); "
            "queue intake pauses here (R8.6)."
        )
    sys.exit(0)


@inbox_group.command(name="show")
@click.argument("item_id")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_show(
    item_id: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Show one item in full, including its evidence."""
    import json as _json

    _root_dir, box = _inbox_for(root)
    ui_impl = _autonomy_ui(ui, no_color, sys.stdout)
    item = box.get(item_id)
    if item is None:
        ui_impl.err(f"No inbox item matching {item_id!r}")
        sys.exit(2)
    ui_impl.section(item.title)
    ui_impl.kv("id", item.id)
    ui_impl.kv("kind", str(item.kind))
    ui_impl.kv("priority", str(item.priority))
    ui_impl.kv("status", str(item.status))
    ui_impl.kv("created", item.created_at)
    if item.component:
        ui_impl.kv("component", item.component)
    if item.run_id:
        ui_impl.kv("run", item.run_id)
    if item.occurrences > 1:
        ui_impl.kv("occurrences", str(item.occurrences))
    if item.decided_by:
        ui_impl.kv("decided by", f"{item.decided_by} at {item.decided_at}")
    if item.decision_comment:
        ui_impl.kv("comment", item.decision_comment)
    if item.detail:
        ui_impl.subsection("Detail")
        for line in item.detail.splitlines():
            ui_impl.info(f"  {line}")
    if item.evidence:
        ui_impl.subsection("Evidence")
        for line in _json.dumps(item.evidence, indent=2).splitlines():
            ui_impl.info(f"  {line}")
    sys.exit(0)


def _decide_and_report(
    action: str,
    item_id: str,
    root: Path | None,
    ui: str,
    no_color: bool,
    comment: str = "",
    hours: float | None = None,
) -> None:
    from kstrl.owner import InboxError, ItemKind, approval_effect

    root_dir, box = _inbox_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    try:
        if action == "approve":
            item = box.approve(item_id, actor=_actor(), comment=comment)
        elif action == "reject":
            item = box.reject(item_id, actor=_actor(), comment=comment)
        elif action == "snooze":
            item = box.snooze(item_id, actor=_actor(), hours=hours)
        else:
            item = box.resolve(item_id, actor=_actor(), comment=comment)
    except InboxError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    # item.status is the past-tense ItemStatus itself (a StrEnum), so this
    # prints "approved"/"rejected"/"snoozed"/"resolved" without a second
    # table that has to be kept in step with the first.
    ui_impl.ok(f"{item.status} {item.id[:8]}: {item.title}")
    # #595: say what the approval does, quoting the finding it covers.
    effect = approval_effect(item) if action == "approve" else None
    if effect:
        ui_impl.info(f"  {effect}")
    # What the confirmation now says, read back rather than assumed: an
    # approval of a stack whose replay failed confirms nothing.
    if action == "approve" and item.kind is ItemKind.STACK_CONFIRMATION:
        stack = stack_in_force(root_dir)
        if stack is not None and stack.unconfirmed:
            ui_impl.info(f"  the [stack] in kstrl.toml {stack.unconfirmed}")
        elif stack is not None:
            ui_impl.info("  the [stack] in kstrl.toml is confirmed: run ks factory next")
    sys.exit(0)


@inbox_group.command(name="approve")
@click.argument("item_id")
@click.option("--comment", default="", help="Why (recorded in the audit trail)")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_approve(
    item_id: str,
    comment: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Accept the exception and close the item.

    For a component parked at the merge gate, approval also re-enters
    `ks factory` on the same manifest, with the flags of the run that
    parked it: that run pushes the reviewed branch, opens the PR, merges
    it and continues with the dependents (#465).
    """
    _refuse_stale_stack_approval("approve", item_id, root, _autonomy_ui(ui, no_color))
    _decide_parked_merge_if_parked("approve", item_id, root, ui, no_color, comment)
    _decide_and_report("approve", item_id, root, ui, no_color, comment=comment)


@inbox_group.command(name="reject")
@click.argument("item_id")
@click.option("--comment", required=True, help="Why it was rejected")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_reject(
    item_id: str,
    comment: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Refuse the exception, recording why.

    For a component parked at the merge gate, rejection also re-enters
    `ks factory` on the same manifest, which records the component failed
    and skips its dependents (#465).
    """
    _decide_parked_merge_if_parked("reject", item_id, root, ui, no_color, comment)
    _decide_and_report("reject", item_id, root, ui, no_color, comment=comment)


def _serve_parked(root_dir: Path, run_id: str, ui_impl: UI) -> bool:
    """Whether `ks serve` launched the run that parked (#463).

    serve records the parked run's id on the queue item it moves to
    awaiting approval; that item is the only link. A queue that cannot be
    read is a refusal before the decision is recorded, because answering
    "no" would leave the approval run's spend out of the daemon's total.
    """
    from kstrl.queue import ItemState, QueueError

    try:
        _root, queue = _queue_for(root_dir)
        parked = queue.items((ItemState.AWAITING_APPROVAL,))
    except (QueueError, OSError) as exc:
        ui_impl.err(f"cannot read the queue to tell whether ks serve parked this run: {exc}")
        sys.exit(2)
    return bool(run_id) and any(item.last_run_id == run_id for item in parked)


def _charge_serve_for_approval_run(
    root_dir: Path, runs_before: frozenset[str], ui_impl: UI
) -> None:
    """Charge an approval run to the daemon's daily total, as serve charged the park (#463)."""
    from kstrl.serve import ServeStateError, SpendLedger, owned_run_spend

    # The approval run is `ks factory` in this process.
    owned, spend = owned_run_spend(root_dir, runs_before, launch_pid=os.getpid())
    if spend.unreadable:
        # #863: the daily budget cannot count what it cannot read, so intake
        # stops until an operator looks, as `ks serve` does for its own runs.
        reason = "the spend of the approval run cannot be read: " + " Also: ".join(spend.unreadable)
        ui_impl.err(reason)
        from kstrl.queue import queue_lock

        with queue_lock(root_dir, blocking=True):
            _queue_for(root_dir)[1].pause(reason=reason, actor="ks inbox")
    try:
        day = SpendLedger(root_dir).charge(
            spend.cost_usd,
            covered_calls=spend.cost_calls,
            total_calls=spend.usage_calls,
            unmetered_phases=spend.unmetered_phases,
            metered_run=bool(owned),
        )
    except (ServeStateError, OSError) as exc:
        ui_impl.err(
            f"the approval run spent ${spend.cost_usd:.2f} over {spend.usage_calls} call(s) "
            f"and could not be charged to the ks serve spend ledger: {exc}"
        )
        return
    ui_impl.info(
        f"charged ${spend.cost_usd:.2f} over {len(owned)} run dir(s) to the ks serve "
        f"spend ledger; today ${day.spent_usd:.2f}"
    )


def _not_parked(item: InboxItem, manifest: Manifest, manifest_file: Path, action: str) -> str:
    """Why ``item``'s park is not the one ``manifest_file`` holds, or "".

    A plan park (#602) is current while the manifest waits on the digest
    the item was filed for; a merge-gate park while its component is
    AWAITING_APPROVAL (#465). Anything else would re-enter the factory on
    work the decision was never about.
    """
    if item.dedupe_key.startswith(PLAN_GATE_KEY):
        if manifest.plan_awaiting_approval == item.evidence.get("plan_digest"):
            return ""
        return (
            f"{item.id[:8]}: {manifest_file} is not waiting on the plan this item was "
            f"filed for; there is no parked plan to {action}"
        )
    comp = manifest.get_component(item.component)
    if comp is not None and comp.status == ComponentStatus.AWAITING_APPROVAL.value:
        return ""
    where = comp.status if comp is not None else "not in the manifest"
    return (
        f"{item.id[:8]}: component '{item.component}' is {where}, not awaiting "
        f"approval in {manifest_file}; there is no parked merge to {action}"
    )


def _stale_plan_approval(
    item: InboxItem, manifest: Manifest, root_dir: Path, action: str
) -> list[str]:
    """Why approving this plan park would run a plan on a spec it was not
    made from, or [] (#639). Only an approval of a plan park: a rejection
    runs nothing, so it stays allowed."""
    if action != "approve" or not item.dedupe_key.startswith(PLAN_GATE_KEY):
        return []
    return spec_pin_errors(manifest, root_dir)


def _decide_parked_merge_if_parked(
    action: str,
    item_id: str,
    root: Path | None,
    ui: str,
    no_color: bool,
    comment: str,
) -> None:
    """Decide a merge-gate or plan park and re-enter the factory, or return (#465, #602).

    Returns only when ``item_id`` is not a park, and the caller then
    records the decision as it always has. For a park it never returns:
    it refuses (exit 2) or hands over to `ks factory`, which exits
    with the run's own code. Every refusal happens before the decision is
    recorded, so a refused command changes nothing: the run lock is taken
    before the manifest is read and handed to the run, so a live run's
    lock is a refusal here rather than after the decision (#597, #596).
    """
    from kstrl.owner import InboxError

    root_dir, box = _inbox_for(root)
    item = box.get(item_id)
    if item is None or not item.dedupe_key.startswith((MERGE_GATE_PARK_KEY, PLAN_GATE_KEY)):
        return
    ui_impl = _autonomy_ui(ui, no_color)
    said = {"approve": "approved", "reject": "rejected"}[action]
    manifest_file = root_dir / "scripts" / "kstrl" / "manifest.json"
    if not manifest_file.exists():
        ui_impl.err(f"No manifest at {manifest_file}")
        sys.exit(2)
    run_lock = _inbox_run_lock(
        root_dir,
        ui_impl,
        not_done=f"{item.id[:8]} was not {said}",
        then="When that run has finished, re-run this command: the decision is applied "
        "by the run it starts.",
    )
    try:
        manifest = _load_manifest_or_exit(manifest_file, ui_impl)
        not_parked = _not_parked(item, manifest, manifest_file, action)
        if not_parked:
            ui_impl.err(not_parked)
            sys.exit(2)
        if report_preflight(
            ui_impl,
            f"{item.id[:8]}: nothing was approved: the plan does not match the spec it "
            "was made from",
            _stale_plan_approval(item, manifest, root_dir, action),
        ):
            sys.exit(2)
        plan, problems, _unkept = plan_resume(
            root_dir,
            manifest,
            manifest_file,
            factory,
            max_cost_usd=None,
            max_parallel=None,
            keep_worktrees_on_failure=False,
        )
        if plan is None:
            report_preflight(ui_impl, RESUME_REFUSAL, problems)
            sys.exit(2)
        serve_parked = _serve_parked(root_dir, manifest.run_id, ui_impl)
        try:
            if action == "approve":
                decided = box.approve(item.id, actor=_actor(), comment=comment)
            else:
                decided = box.reject(item.id, actor=_actor(), comment=comment)
        except InboxError as exc:
            ui_impl.err(str(exc))
            sys.exit(2)
        ui_impl.ok(f"{decided.status} {item.id[:8]}: {item.title}")
        print_resume_plan(ui_impl, plan)
        argv = option_argv(
            factory,
            {
                "manifest_path": str(manifest_file),
                "root": str(root_dir),
                "yes": True,
                "ui": ui,
                "no_color": no_color,
            },
        )
        factory_ctx = factory.make_context(
            "factory", [*argv, *plan.argv], parent=click.get_current_context()
        )
        factory_ctx.meta[_HANDED_RUN_LOCK] = run_lock
        from kstrl.serve import run_dir_names

        runs_before = run_dir_names(root_dir)
        try:
            with factory_ctx:
                factory.invoke(factory_ctx)
        except SystemExit as exc:
            # `ks factory` always leaves through sys.exit with the run's own
            # code. Settle the queue item serve parked on this run (#464),
            # then exit with that code.
            from kstrl.queue import Queue, QueueConfig
            from kstrl.serve import settle_approval_run

            settle_approval_run(
                root_dir,
                Queue(root_dir, QueueConfig.load(root_dir)),
                parked_run_id=manifest.run_id,
                returncode=exc.code if isinstance(exc.code, int) else 1,
                actor=_actor(),
                observer=_ServeUiObserver(ui_impl),
            )
            raise
        finally:
            if serve_parked:
                _charge_serve_for_approval_run(root_dir, runs_before, ui_impl)
    finally:
        run_lock.release()


@inbox_group.command(name="snooze")
@click.argument("item_id")
@click.option("--hours", type=float, help="TTL (default: [inbox] snooze_hours)")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_snooze(
    item_id: str,
    hours: float | None,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Defer an item; it returns when the TTL lapses."""
    _decide_and_report("snooze", item_id, root, ui, no_color, hours=hours)


@inbox_group.command(name="retry")
@click.argument("item_id")
@_inbox_root_option
@_inbox_ui_option
@_inbox_no_color_option
def inbox_retry(
    item_id: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Requeue the item's component and close the item.

    A real round-trip, not a status change: the component is reset to
    PENDING in the manifest (with its dependents un-skipped), exactly as
    `ks retry` resets it, so the next `ks factory` run picks it up.
    Refused with nothing changed while a factory run holds the run lock,
    whose next save would undo it (#597).
    """
    root_dir, box = _inbox_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    item = box.get(item_id)
    if item is None:
        ui_impl.err(f"No inbox item matching {item_id!r}")
        sys.exit(2)
    if not item.component:
        ui_impl.err(f"{item.id[:8]} has no component to requeue")
        sys.exit(2)
    manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"
    if not manifest_path.exists():
        ui_impl.err(f"No manifest at {manifest_path}")
        sys.exit(2)
    # #597: the read, the reset, the save and the resolve all happen under
    # the run lock, so a live run cannot undo the requeue at its next save.
    run_lock = _inbox_run_lock(
        root_dir,
        ui_impl,
        not_done=f"{item.component} was not requeued",
        then=(
            "When that run has finished, re-run this command, or run "
            f"`ks retry {item.component}` to requeue it and run it at once."
        ),
    )
    try:
        manifest = _load_manifest_or_exit(manifest_path, ui_impl)
        try:
            reset = prepare_retry(
                manifest, item.component, manifest_path, root_dir, ui_impl
            ).reset_dependents
        except (ValueError, KeyError) as exc:
            ui_impl.err(f"Could not requeue {item.component}: {exc}")
            if item.dedupe_key.startswith(MERGE_GATE_PARK_KEY):
                ui_impl.info(
                    f"It is parked at the merge gate: `ks inbox approve {item.id[:8]}` "
                    f"merges its reviewed branch, `ks inbox reject {item.id[:8]} "
                    "--comment ...` fails it."
                )
            sys.exit(2)
        box.resolve(item.id, actor=_actor(), comment="requeued via ks inbox retry")
    finally:
        run_lock.release()
    ui_impl.ok(
        f"Requeued {item.component} (reset: {', '.join(reset) or item.component}); "
        "run `ks factory` to pick it up."
    )
    sys.exit(0)
