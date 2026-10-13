"""ks serve, ks signals and ks ci: the long-running intake loop and the polls that feed it."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from kstrl.cli_queue import (
    _allowed_actors_kv,
    _queue_no_color_option,
    _queue_root_option,
    _queue_ui_option,
)
from kstrl.cli_report import _render_safe_mode
from kstrl.cli_seam import _autonomy_ui, _load_manifest_or_exit, cli
from kstrl.config_report import UI_MODES
from kstrl.serve_launchd import LAUNCHD_MODES
from kstrl.ui import UI


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
    from kstrl.io import CONTROL_SIGNALS, control_file
    from kstrl.signals import read_ledger

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
    from kstrl.queue import Queue, QueueConfig, summarize
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
