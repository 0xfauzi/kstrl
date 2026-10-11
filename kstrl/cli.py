"""CLI entry point for kstrl."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kstrl.inbox import InboxItem

import click

from kstrl import (
    cli_architect,
    cli_check,
    cli_factory,
    cli_feature,
    cli_learning,
    cli_loop,
    cli_queue,
    cli_report,
)
from kstrl.cli_factory import factory
from kstrl.cli_preflight import (
    _HANDED_RUN_LOCK,
)
from kstrl.cli_queue import (
    _allowed_actors_kv,
    _queue_for,
    _queue_no_color_option,
    _queue_root_option,
    _queue_ui_option,
)
from kstrl.cli_report import _render_safe_mode
from kstrl.cli_seam import (
    _actor,
    _autonomy_ui,
    _load_manifest_or_exit,
    cli,
)
from kstrl.config_report import UI_MODES
from kstrl.factory import (
    _report_preflight,
)
from kstrl.factory_lock import (
    FactoryLockHeldError,
    LeftoverAgentsError,
    _acquire_run_lock,
    _RunLock,
)
from kstrl.launch_record import option_argv
from kstrl.manifest import (
    MERGE_GATE_PARK_KEY,
    ComponentStatus,
    Manifest,
)
from kstrl.plan_gate import PLAN_GATE_KEY, spec_pin_errors
from kstrl.retry_plan import (
    RESUME_REFUSAL,
    plan_resume,
    print_resume_plan,
)
from kstrl.serve_launchd import LAUNCHD_MODES
from kstrl.stack import (
    STACK_KEY,
    load_stack,
)
from kstrl.ui.base import UI


def _inbox_run_lock(root_dir: Path, ui_impl: UI, *, not_done: str, then: str) -> _RunLock:
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
        return _acquire_run_lock(root_dir, ui_impl, force=False)
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
    ui_impl.err(
        f"{item.id[:8]} confirms the [stack] {str(item.evidence.get('stack_digest'))[:12]}, "
        f"and {reads}: nothing was approved. Run ks factory to file the stack kstrl.toml holds."
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
    from kstrl.inbox import Inbox, InboxConfig

    root_dir = (root or Path.cwd()).resolve()
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
    from kstrl.inbox import summarize

    _root_dir, box = _inbox_for(root)
    ui_impl = _autonomy_ui(ui, no_color)
    items = box.items() if show_all else box.open_items()
    if not items:
        ui_impl.ok("Inbox clear: nothing is waiting on you.")
        sys.exit(0)
    ui_impl.section("Inbox")
    for item in items:
        marker = {"high": "!", "normal": " ", "low": "."}.get(str(item.priority), " ")
        repeat = f" x{item.occurrences}" if item.occurrences > 1 else ""
        state = "" if item.is_open else f" [{item.status}]"
        ui_impl.info(f"  {marker} {item.id[:8]}  {str(item.kind):<18}{item.title}{repeat}{state}")
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
    ui_impl = _autonomy_ui(ui, no_color)
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
    from kstrl.inbox import InboxError
    from kstrl.waivers import approval_effect

    _root_dir, box = _inbox_for(root)
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
    from kstrl.workqueue_items import ItemState, QueueError

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
    from kstrl.serve_spend import ServeStateError, SpendLedger, owned_run_spend

    # The approval run is `ks factory` in this process.
    owned, spend = owned_run_spend(root_dir, runs_before, launch_pid=os.getpid())
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
    from kstrl.inbox import InboxError

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
        if _report_preflight(
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
            _report_preflight(ui_impl, RESUME_REFUSAL, problems)
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
                "tui": False,
                "ui": ui,
                "no_color": no_color,
            },
        )
        factory_ctx = factory.make_context(
            "factory", [*argv, *plan.argv], parent=click.get_current_context()
        )
        factory_ctx.meta[_HANDED_RUN_LOCK] = run_lock
        from kstrl.serve_spend import run_dir_names

        runs_before = run_dir_names(root_dir)
        try:
            with factory_ctx:
                factory.invoke(factory_ctx)
        except SystemExit as exc:
            # `ks factory` always leaves through sys.exit with the run's own
            # code. Settle the queue item serve parked on this run (#464),
            # then exit with that code.
            from kstrl.serve_settle import settle_approval_run
            from kstrl.workqueue import Queue
            from kstrl.workqueue_items import QueueConfig

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
    PENDING in the manifest (with its dependents un-skipped), so the next
    `ks factory` run picks it up. Refused with nothing changed while a
    factory run holds the run lock, whose next save would undo it (#597).
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
            reset = manifest.reset_for_retry(item.component)
        except (ValueError, KeyError) as exc:
            ui_impl.err(f"Could not requeue {item.component}: {exc}")
            if item.dedupe_key.startswith(MERGE_GATE_PARK_KEY):
                ui_impl.info(
                    f"It is parked at the merge gate: `ks inbox approve {item.id[:8]}` "
                    f"merges its reviewed branch, `ks inbox reject {item.id[:8]} "
                    "--comment ...` fails it."
                )
            sys.exit(2)
        manifest.save(manifest_path)
        box.resolve(item.id, actor=_actor(), comment="requeued via ks inbox retry")
    finally:
        run_lock.release()
    ui_impl.ok(
        f"Requeued {item.component} (reset: {', '.join(reset) or item.component}); "
        "run `ks factory` to pick it up."
    )
    sys.exit(0)


@cli.group(name="signals")
def signals_group() -> None:
    """Observe a tracker's runtime issues and record a verdict (R8.8 slice 1).

    Fetches one page of issues, classifies each against the ledger this
    group writes, and prints what it saw. Nothing here enqueues, notifies
    or spends: every disposition is a "would", never a "did" - the queue
    routing question (roadmap user decision 8) is still open.
    """


_signals_root_option = click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
_signals_ui_option = click.option(
    "--ui",
    type=click.Choice(UI_MODES),
    default="auto",
    help="UI mode",
)
_signals_no_color_option = click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)


@signals_group.command(name="poll")
@click.option(
    "--from-file",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Replay a captured tracker page instead of fetching one",
)
@click.option(
    "--capture",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the fetched or replayed page's raw text here",
)
@_signals_root_option
@_signals_ui_option
@_signals_no_color_option
def signals_poll(
    from_file: Path | None,
    capture: Path | None,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Fetch one tracker page, classify it against the ledger, print the tally."""
    from kstrl.signals import SignalsConfig, SignalsError, poll

    root_dir = (root or Path.cwd()).resolve()
    config = SignalsConfig.load(root_dir)
    ui_impl = _autonomy_ui(ui, no_color)
    if not config.enabled:
        ui_impl.err(
            "signals polling is off. Set [signals] enabled = true in "
            "kstrl.toml (or KSTRL_SIGNALS_ENABLED=1)."
        )
        sys.exit(2)
    try:
        report = poll(root_dir, config, from_file=from_file, capture=capture)
    except SignalsError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)

    ui_impl.section("Signals")
    ui_impl.info(f"{len(report.signals)} signals (dropped {report.dropped_rows})")
    tally: dict[str, int] = {}
    for row in report.signals:
        tally[str(row.disposition)] = tally.get(str(row.disposition), 0) + 1
    for disposition in sorted(tally):
        ui_impl.kv(disposition, str(tally[disposition]))
    ui_impl.kv("poll_new_issues", str(report.poll_new_issues))
    ui_impl.kv("poll_max_events_on_a_new_issue", str(report.poll_max_events_on_a_new_issue))
    sys.exit(0)


@signals_group.command(name="ls")
@_signals_root_option
@_signals_ui_option
@_signals_no_color_option
def signals_ls(root: Path | None, ui: str, no_color: bool) -> None:
    """Print the signal ledger, oldest first."""
    from kstrl.signals import read_ledger
    from kstrl.statedir import CONTROL_SIGNALS, control_file

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    ledger = read_ledger(control_file(root_dir, CONTROL_SIGNALS))
    if not ledger.signals:
        if ledger.dropped:
            ui_impl.err(f"{ledger.dropped} ledger row(s) could not be read")
        ui_impl.ok("No signals recorded yet.")
        sys.exit(0)
    ui_impl.section("Signals")
    for row in ledger.signals:
        ui_impl.info(
            f"  {row.observed_at}  {row.friendly_id:<20}{str(row.kind):<12}"
            f"{str(row.disposition):<16}events={row.event_count}"
        )
    if ledger.dropped:
        ui_impl.kv("dropped", str(ledger.dropped))
    sys.exit(0)


@cli.group(name="ci")
def ci_group() -> None:
    """Read and record the CI state of the commits kstrl merges produced (#553).

    Asks GitHub, through gh, for the checks on every merge commit kstrl
    recorded (the manifest and every run's pr_merged events), and
    appends what it read, with the time, to a ledger in the control
    directory. `ks serve` reads the commits that are due after every
    cycle, and `ks status` shows the newest reading of each. A state
    kstrl could not read is recorded as unknown, never as passed.
    """


@ci_group.command(name="poll")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Manifest file (default: <root>/scripts/kstrl/manifest.json)",
)
@_signals_root_option
@_signals_ui_option
@_signals_no_color_option
def ci_poll(manifest_path: Path | None, root: Path | None, ui: str, no_color: bool) -> None:
    """Read the CI state of every recorded merge commit and record it.

    Exit 1 when any commit's CI failed or could not be read: both need
    the operator. Exit 0 when every commit passed or is still running.
    """
    from kstrl.ci_state import CiState, poll_ci, recorded_merges

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    path = manifest_path or root_dir / "scripts" / "kstrl" / "manifest.json"
    if not path.exists():
        ui_impl.err(f"No manifest found at {path}")
        ui_impl.info("Run `ks factory` first, or pass --manifest.")
        sys.exit(2)
    manifest = _load_manifest_or_exit(path, ui_impl)
    merged = recorded_merges(root_dir, manifest)
    if not merged:
        ui_impl.ok(f"No merge commits recorded in {path} or in any run under {root_dir}.")
        sys.exit(0)
    readings = poll_ci(root_dir, [sha for _, sha in merged])
    ui_impl.section("CI")
    for (component_id, _), reading in zip(merged, readings, strict=True):
        ui_impl.info(
            f"  {component_id}  {reading.sha[:12]}  {reading.state}  "
            f"read {reading.observed_at}  {reading.reason}"
        )
    needs_operator = any(r.state in (CiState.FAILED, CiState.UNKNOWN) for r in readings)
    sys.exit(1 if needs_operator else 0)


@cli.command()
@click.option(
    "--once",
    is_flag=True,
    help="Run a single poll cycle and exit (launchd/cron fallback mode)",
)
@click.option(
    "--max-cycles",
    type=click.IntRange(min=0),
    default=0,
    help="Stop after this many cycles; 0 runs until interrupted",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what the next cycle would do without spending anything",
)
@click.option(
    "--print-plist",
    "print_plist",
    is_flag=True,
    help="Print a launchd LaunchAgent plist for this checkout and exit",
)
@click.option(
    "--plist-mode",
    type=click.Choice(LAUNCHD_MODES),
    default="keepalive",
    help="keepalive: one long-lived daemon; interval: `--once` on a timer",
)
@click.option(
    "--plist-interval",
    type=click.IntRange(min=1),
    default=5,
    help=(
        "MINUTES between runs in interval mode; must divide an hour "
        "(1,2,3,4,5,6,10,12,15,20,30,60) or be whole hours"
    ),
)
@_queue_root_option
@_queue_ui_option
@_queue_no_color_option
def serve(
    once: bool,
    max_cycles: int,
    dry_run: bool,
    print_plist: bool,
    plist_mode: str,
    plist_interval: int,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Drain the continuous-intake queue (R8.6).

    Runs one factory invocation at a time, holding a daemon singleton
    lock, and polls any enabled remote inbox at the start of each cycle.

    Under launchd, `--print-plist --plist-mode interval` schedules this
    with --once on a StartCalendarInterval, which is the only launchd
    timer that catches up after sleep: launchd.plist(5) says a
    StartInterval firing during sleep "will be missed", while
    StartCalendarInterval "will start the job the next time the computer
    wakes up".

    Only infrastructure failures are retried, and only with positive
    evidence. Spec-level failures go to poison/ and wait for a human.
    """
    from kstrl.serve import ServeLockedError
    from kstrl.serve import serve as run_serve
    from kstrl.serve_config import ServeConfig, ServeError
    from kstrl.serve_gates import (
        check_budget,
        check_cost_coverage,
        check_inbox_cap,
        check_parked_merges,
        check_poison_breaker,
        consecutive_poison_count,
        factory_lock_held,
    )
    from kstrl.serve_github import check_open_pr_bound
    from kstrl.serve_launchd import REQUIRE_TIMEOUT_ENV
    from kstrl.serve_spend import SpendLedger
    from kstrl.workqueue import Queue, summarize
    from kstrl.workqueue_items import QueueConfig

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)

    if print_plist:
        # Printed rather than installed: writing into ~/Library/LaunchAgents
        # and running launchctl are outward-facing acts an operator should
        # perform deliberately, and the docs walk through them.
        from kstrl.serve_launchd import launchd_log_dir, render_launchd_plist

        # launchd creates the log FILE but not its directory, and a
        # missing directory makes the job fail to spawn with nothing in
        # the log to say why. Create it here, where the operator is
        # actively setting the job up.
        try:
            launchd_log_dir(root_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            ui_impl.err(f"could not create the launchd log directory: {exc}")
            sys.exit(2)

        try:
            click.echo(
                render_launchd_plist(
                    root_dir,
                    mode=plist_mode,
                    interval_minutes=plist_interval,
                    factory_timeout_seconds=(ServeConfig.load(root_dir).factory_timeout_seconds),
                ),
                nl=False,
            )
        except ServeError as exc:
            ui_impl.err(str(exc))
            sys.exit(2)
        sys.exit(0)

    try:
        config = ServeConfig.load(root_dir)
    except (ServeError, ValueError) as exc:
        ui_impl.err(str(exc))
        sys.exit(2)

    # A scheduled LaunchAgent carries this marker, so the promise it was
    # installed under is re-checked on every invocation rather than only
    # the day the plist was written (#189 N3).
    if os.environ.get(REQUIRE_TIMEOUT_ENV) == "1" and (config.factory_timeout_seconds <= 0):
        ui_impl.err(
            "this scheduled job was installed on the promise of a bounded "
            "cycle, but [serve] factory_timeout_seconds is now 0. launchd "
            "neither kills nor replaces a job still running at the next "
            "firing, so an unbounded cycle would silently stop every later "
            "one. Restore the timeout, or reinstall the LaunchAgent in "
            "keepalive mode."
        )
        sys.exit(2)
    queue = Queue(root_dir, QueueConfig.load(root_dir))
    ledger = SpendLedger(root_dir)

    if dry_run:
        # Deliberately reports the same gates the loop evaluates, in the
        # same order, so "what would it do" cannot drift from "what it
        # does" without a test noticing.
        ui_impl.section("Serve dry run")
        _render_safe_mode(ui_impl, root_dir)
        ui_impl.kv("queue", summarize(queue.counts()))
        pause = queue.pause_state()
        ui_impl.kv(
            "paused",
            f"yes - {pause.reason}" if pause.active() else "no",
        )
        from kstrl.serve_spend import ServeStateError

        try:
            spend = ledger.read()
        except ServeStateError as exc:
            ui_impl.err(str(exc))
            sys.exit(2)
        floor = (
            (
                f" (a FLOOR: {spend.uncovered_calls} call(s) reported no cost"
                + (
                    "; unmetered: " + ", ".join(spend.unmetered_phases)
                    if spend.unmetered_phases
                    else ""
                )
                + ")"
            )
            if spend.lower_bound
            else ""
        )
        ui_impl.kv(
            "today",
            f"${spend.spent_usd:.2f} over {spend.runs} run(s){floor}",
        )
        ui_impl.kv(
            "daily budget",
            f"${config.daily_budget_usd:.2f}" if config.daily_budget_usd > 0 else "unset (no cap)",
        )
        ui_impl.kv("consecutive poison", str(consecutive_poison_count(ledger)))
        ui_impl.kv(
            "factory lock",
            "held by another run" if factory_lock_held(root_dir) else "free",
        )
        for label, admission in (
            ("poison breaker", check_poison_breaker(ledger, config)),
            ("cost coverage", check_cost_coverage(ledger, config)),
            ("budget", check_budget(ledger, config)),
            ("inbox cap", check_inbox_cap(root_dir)),
            ("parked merges", check_parked_merges(root_dir)),
            # Last, matching serve_cycle: the only gate that reaches the
            # network, and the only one a dry run can make slow.
            ("open-PR bound", check_open_pr_bound(config, root_dir)),
        ):
            if admission.allowed:
                ui_impl.info(f"  gate {label}: ok")
            else:
                ui_impl.warn(f"  gate {label}: BLOCKS - {admission.reason}")
        # The real cycle SYNCS before selecting, so a dry run that reads
        # only the existing queue can report "nothing ready" while the
        # live loop would admit an issue and spend on it immediately
        # (#189 N2). Runs the adapter's side-effect-free planner.
        from dataclasses import replace as _replace

        from kstrl.intake_gh import GitHubIntakeConfig, IntakeError
        from kstrl.intake_github import sync as intake_sync

        try:
            intake_config = GitHubIntakeConfig.load(root_dir)
        except (IntakeError, ValueError) as exc:
            intake_config = None
            ui_impl.warn(f"  intake config unreadable: {exc}")
        if intake_config is not None and intake_config.enabled:
            ui_impl.kv("allowed_actors", _allowed_actors_kv(intake_config.allowed_actors))
            plan = intake_sync(
                queue,
                _replace(intake_config, dry_run=True),
                root_dir,
            )
            ui_impl.kv("intake", f"{plan.polled} polled from {plan.repo or '?'}")
            for ref in plan.would_enqueue:
                ui_impl.ok(f"  would admit {ref}")
            for ref, reason in sorted(plan.skipped.items()):
                ui_impl.info(f"  skip {ref}: {reason}")
            for error in plan.errors:
                ui_impl.warn(f"  intake: {error}")
        else:
            ui_impl.kv("intake", "disabled")

        candidate = queue.next_ready()
        pending = f"{candidate.item_id} - {candidate.title}" if candidate else "nothing ready"
        if candidate is None and intake_config is not None and (intake_config.enabled):
            # Say so explicitly: "nothing ready" alone would be misleading
            # when intake is about to admit work.
            pending = "nothing queued yet (intake may admit the items above)"
        ui_impl.kv("next item", pending)
        sys.exit(0)

    ui_impl.info(
        f"ks serve on {root_dir} "
        f"(poll {config.poll_interval_seconds:.0f}s, "
        f"budget "
        + (f"${config.daily_budget_usd:.2f}/day" if config.daily_budget_usd > 0 else "unset")
        + f", caffeinate {'on' if config.caffeinate else 'off'})"
    )
    observer = _ServeUiObserver(ui_impl)
    try:
        results = run_serve(
            root_dir,
            once=once,
            config=config,
            observer=observer,
            max_cycles=max_cycles,
        )
    except ServeLockedError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    except KeyboardInterrupt:
        ui_impl.info("Interrupted; the current item keeps its lease for the reaper.")
        sys.exit(0)

    ran = [r for r in results if r.ran_item]
    ui_impl.info("")
    ui_impl.kv("cycles", str(len(results)))
    ui_impl.kv("items run", str(len(ran)))
    # Derived from the TERMINAL queue outcome, not from the classifier's
    # retry permission. Review #186 F10: an infra verdict whose last
    # attempt was spent is poisoned by serve_cycle, yet
    # Verdict.RETRY_INFRA.may_retry stays true - and the reaper and
    # merge-gate poison paths set no ran_item at all - so the old filter
    # exited 0 on work that was waiting for a human.
    needs_human = [r for r in results if r.needs_human]
    if needs_human:
        ui_impl.warn(
            f"{len(needs_human)} cycle(s) produced work awaiting a human; "
            "see `ks inbox ls` and `ks queue ls`"
        )
    sys.exit(1 if needs_human else 0)


class _ServeUiObserver:
    """Adapts the daemon's narration onto the console UI."""

    def __init__(self, ui_impl: UI) -> None:
        self._ui = ui_impl

    def info(self, message: str) -> None:
        self._ui.info(message)

    def warn(self, message: str) -> None:
        self._ui.warn(message)

    def err(self, message: str) -> None:
        self._ui.err(message)


#: The modules whose import registers their commands on ``cli``. Named
#: so that each import is read, and so that a module left off this
#: tuple is a visible omission rather than a silently missing command.
COMMAND_MODULES = (
    cli_architect,
    cli_check,
    cli_factory,
    cli_feature,
    cli_learning,
    cli_loop,
    cli_queue,
    cli_report,
)


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
