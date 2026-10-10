"""`ks evolve`, `ks autonomy`, `ks health` and `ks recheck`: the journal of
outcomes, the autonomy ladder and the evidence each one reads.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from kstrl.autonomy import DEMOTION_TRIGGER_LABELS
from kstrl.cli_seam import _autonomy_ui, _console_ui, cli
from kstrl.config_report import UI_MODES
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.factory import _report_preflight
from kstrl.ui.base import UI

if TYPE_CHECKING:
    from kstrl.autonomy import AutonomyState
    from kstrl.autonomy_replay import RunRecord
    from kstrl.evolution import EvolutionConfig, EvolutionJournal, FailurePattern, PatternRouting


def _echo_journal_repairs(journal: EvolutionJournal, ui_impl: UI) -> None:
    """`ks evolve --status`'s report of interrupted journal writes (#312).

    Why the count exists at all is argued once, on
    ``EvolutionJournal.get_repair_count``. What is decided HERE:

    ``warn``, not ``err``: this command exits 0, and every other
    ``ui_impl.err`` in this module precedes a non-zero exit, so printing
    ERROR in red would make a repaired journal indistinguishable from
    "Evolution is disabled in config".

    POSSIBLE loss, not confirmed loss (#327 round 2, F8): the line
    names both outcomes and points at the one line that decides which.

    Silent at zero, which is the whole reason this is worth a line at
    all: a healthy journal that prints "0 repairs" every time teaches an
    operator to skip the line that matters.

    Only ``ks evolve --status`` calls it, and that is still true after
    #333: the evolve TUI screen reports repairs now, through
    ``EvolveScreen._show_repairs``, which asks the journal for itself
    because ``get_repair_count`` is click-free and on the journal. This
    helper writes through ``UI`` in the click module and cannot be
    reused as is, so the two are one measurement rendered twice rather
    than two measurements. The wording is one string now, on the journal
    (``repair_summary``): saying it was deliberately the same on both
    was a claim with nothing keeping it (#352 round 2, N4). What is
    still per-surface is the prefix and the decision to show anything.

    Takes the journal and asks IT for the path, rather than being handed
    both: a count from one journal printed beside another one's path is
    a report that cannot be wrong today and could be after any edit. A
    helper rather than four lines inline because ``evolve`` is already
    over the cognitive gate at 23, so a branch added there would fail
    the staged complexity ratchet on a function this change is not
    otherwise touching.
    """
    summary = journal.repair_summary()
    if summary is not None:
        ui_impl.warn(f"  {summary}")


def _echo_iteration_criterion(
    journal: EvolutionJournal,
    trends: list[dict[str, Any]],
    ui_impl: UI,
) -> None:
    """`ks evolve --status`'s verdict on #233's entry criterion.

    A module-level helper rather than nine more lines inside ``evolve``,
    and the reason is measured rather than stylistic: ``evolve`` sits at
    cyclomatic 15 against ``cyclomatic_ratchet.py``'s limit of 10 and at
    cognitive 23 against ``complexipy``'s 15. Both hooks fail a function
    this commit makes worse, so a single added branch there fails the
    commit. This helper adds no branch to ``evolve``.

    The lines themselves are built by ``EvolutionJournal`` and not here,
    so the journal path never reaches this module; see
    ``iteration_criterion_lines``.
    """
    ui_impl.section("Iteration criterion (#233)")
    for line in journal.iteration_criterion_lines(trends):
        ui_impl.info(line)


@cli.command()
@click.option(
    "--status",
    "show_status",
    is_flag=True,
    help="Show experiment trends",
)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--ui",
    type=click.Choice(UI_MODES),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
def evolve(
    show_status: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Analyze factory runs and report recurring failure patterns.

    Without arguments, analyzes recent runs, routes each recurring
    pattern to the inbox, the candidate lessons or neither, and prints
    the learning-readiness numbers. Nothing is written.
    Use --status to see experiment trends.
    """
    from kstrl.evolution import EvolutionConfig, EvolutionJournal, route_patterns

    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    # R2.1: honor [evolution] in kstrl.toml + env, anchored to --root.
    # Unguarded on purpose: `_PREFLIGHT_REQUIRED` lists this command as
    # one that [evolution] is FATAL for, so the entry seam has already
    # rejected a config this would raise on.
    evo_config = EvolutionConfig.load(root_dir)
    _echo_retired_evolution_keys(evo_config, ui_impl)

    if not evo_config.enabled:
        ui_impl.err("Evolution is disabled in config")
        sys.exit(2)

    journal = EvolutionJournal(evo_config)

    if show_status:
        ui_impl.section("Experiment Trends")
        trends = journal.get_experiment_trends(last_n=10)
        # Before the empty-trends exit, not after (#327 round 2, F7).
        # decompose and autonomy both write to the journal before any
        # factory run has written experiments.tsv, so a repair with no
        # trends is not an edge case: it is the state the operator most
        # likely to HAVE a repair is in, and the exit below skipped it.
        _echo_journal_repairs(journal, ui_impl)
        if not trends:
            ui_impl.info("No experiments recorded yet. Run `ks factory` first.")
            sys.exit(0)

        for entry in trends:
            failure = str(entry.get("common_failure", ""))[:40]
            ui_impl.info(
                f"  {entry.get('run_id', '?')} | "
                f"project={entry.get('project', '?')} "
                f"completed={entry.get('completed', '?')} "
                f"failed={entry.get('failed', '?')} "
                f"retry_rate={entry.get('retry_rate', '?')} "
                f"avg_iterations={entry.get('avg_iterations', '?')} "
                f"common_failure={failure}"
            )

        _echo_iteration_criterion(journal, trends, ui_impl)
        sys.exit(0)

    ui_impl.section("Evolution: Analyzing Runs")
    patterns = journal.get_cross_run_patterns(lookback_runs=evo_config.lookback_runs)
    routing = route_patterns(patterns)
    _report_patterns_and_readiness(journal, evo_config, patterns, routing, ui_impl, root_dir)
    _echo_retired_proposals_dir(root_dir, ui_impl)
    sys.exit(0)


def _echo_retired_evolution_keys(evo_config: EvolutionConfig, ui_impl: UI) -> None:
    """Name the [evolution] keys #217 retired, so they do not vanish silently.

    They are neither read nor refused: existing kstrl.toml files set
    them, and a refusal would stop every run over a key that does
    nothing.
    """
    if evo_config.retired_keys:
        ui_impl.warn(
            f"[evolution] {', '.join(evo_config.retired_keys)} in kstrl.toml: "
            f"no effect since #217 (the proposal generator is deleted)"
        )


def _echo_retired_proposals_dir(root_dir: Path, ui_impl: UI) -> None:
    """One line about files the deleted proposal generator left behind.

    The directory is the operator's: it is counted and named, never
    read, changed or deleted.
    """
    proposals_dir = root_dir / ".kstrl" / "proposals"
    if not proposals_dir.is_dir():
        return
    try:
        count = f"{sum(1 for entry in proposals_dir.iterdir() if entry.is_file())} file(s)"
    except OSError as exc:
        count = f"files not counted ({exc})"
    ui_impl.info(
        f"{proposals_dir}: {count} from the deleted proposal generator; "
        f"nothing reads this directory since #217"
    )


def _report_patterns_and_readiness(
    journal: EvolutionJournal,
    evo_config: EvolutionConfig,
    patterns: list[FailurePattern],
    routing: PatternRouting,
    ui_impl: UI,
    root_dir: Path,
) -> None:
    """Print the patterns found, the routing disclosure and the
    readiness numbers, in that order, whether or not any pattern
    recurred.

    Extracted out of ``evolve`` on its own (#217 added no new branch
    here that the plan did not already ask for; this split keeps
    ``evolve``'s own complexity from growing past the repo's ratchet).
    """
    if patterns:
        ui_impl.ok(f"Found {len(patterns)} recurring patterns")
        for pattern in patterns:
            ui_impl.info(
                f"  [{pattern.check_name}] {pattern.description} "
                f"(seen in {pattern.frequency} components)"
            )
    else:
        ui_impl.info("No recurring failure patterns found across recent runs.")
        ui_impl.info("Run more factory sessions to accumulate data.")

    _echo_pattern_routing(routing, ui_impl)
    _echo_learning_readiness(journal, evo_config, patterns, ui_impl, root_dir)


def _echo_pattern_routing(routing: PatternRouting, ui_impl: UI) -> None:
    """Print every routed pattern under a heading naming its bucket.

    This is a guard in the clearing direction: it decides which
    patterns are lessons. The repo's rule is that a guard which clears
    must be able to prove a site compliant, and this one cannot prove an
    operator agrees with a row of _CATEGORY_BY_CHECK. So it never drops
    anything silently: each pattern is printed under the heading of its
    bucket, and the first time a row of that table is wrong the operator
    sees it in this output.
    """
    from kstrl.evolution import UNENROLLED_CATEGORY, category_for_check

    if routing.lessons:
        ui_impl.section(
            "Candidate lessons (kstrl acts on none; put a standing rule in the [paths] memory file)"
        )
        for pattern in routing.lessons:
            ui_impl.info(
                f"  [{pattern.check_name}] {pattern.error_signature} "
                f"(category {category_for_check(pattern.check_name)})"
            )
    if routing.mechanical:
        ui_impl.section("Routed to the inbox (mechanical, not a lesson)")
        for pattern in routing.mechanical:
            ui_impl.info(
                f"  [{pattern.check_name}] {pattern.error_signature} "
                f"(infrastructure; the run already opened an inbox item, "
                f"see `ks inbox`)"
            )
    if routing.unrouted:
        ui_impl.section("Not routed (not a learnable category)")
        for pattern in routing.unrouted:
            ui_impl.info(
                f"  [{pattern.check_name}] {pattern.error_signature} "
                f"(category {category_for_check(pattern.check_name)}; not a lesson)"
            )
        ui_impl.info(
            "  A check name not in evolution._CATEGORY_BY_CHECK lands here "
            f"too, with category '{UNENROLLED_CATEGORY}'."
        )


def _echo_learning_readiness(
    journal: EvolutionJournal,
    evo_config: EvolutionConfig,
    patterns: list[FailurePattern],
    ui_impl: UI,
    root_dir: Path,
) -> None:
    """Print the numbers that gate the unbuilt phases of #217.

    Sections 5.2 and 7 of docs/continuous-learning-design.md both say
    the attribution thresholds must be measured before they are chosen,
    and nothing reported the input to that measurement. These are reads
    of aggregates that already existed. A command that says zero is
    doing its job; that is what the instrument is for.

    ``patterns`` is passed in rather than re-read. The caller has
    already computed it from the same journal, and a second read is a
    second answer to one question. The lines themselves are built by
    ``kstrl.evolve_report`` so the TUI's evolve screen shows the same
    text (#433 F12).
    """
    from kstrl.evolve_report import readiness_lines

    ui_impl.section("Learning readiness")
    for line in readiness_lines(journal, evo_config, patterns, root_dir):
        ui_impl.info(line)


@cli.group(name="autonomy")
def autonomy_group() -> None:
    """Inspect and change the autonomy ladder (R8.2).

    Autonomy is earned, bounded, and revocable: promotion needs evidence
    AND your explicit acknowledgement, demotion is automatic, and every
    transition is recorded.
    """


_autonomy_root_option = click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
_autonomy_ui_option = click.option(
    "--ui",
    type=click.Choice(UI_MODES),
    default="auto",
    help="UI mode",
)
_autonomy_no_color_option = click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)


#: Why the evidence counters stay at zero: the factory records ladder
#: evidence only while ``[autonomy] enabled`` is true.
_AUTONOMY_DISABLED_NOTE = "Evidence is not recorded while [autonomy] enabled = false."


def _report_autonomy_since(
    ui_impl: UI, state: AutonomyState, *, state_file: Path, enabled: bool
) -> None:
    """Print ``since`` only for a ladder record read from disk (#484).

    ``AutonomyState.load`` returns a fresh default when ``state_file`` is
    missing and when it discards a damaged one, and a fresh default's
    ``since`` is the time of the call. Printing it would report evidence
    that was never recorded, with a new start time on every invocation.
    """
    recorded = state_file.exists() and state.degraded_reason is None
    ui_impl.kv("since", state.since if recorded else "-")
    if not enabled:
        ui_impl.info(_AUTONOMY_DISABLED_NOTE)


@autonomy_group.command(name="status")
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_status(root: Path | None, ui: str, no_color: bool) -> None:
    """Show the current level, its flag bundle, and what promotion needs."""
    from kstrl.autonomy import (
        AutonomyConfig,
        AutonomyState,
        entry_signal_blockers,
        flag_bundle_for,
        resolve_runtime_level,
    )
    from kstrl.policy import PolicyConfig

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    config = AutonomyConfig.load(root_dir)
    state = AutonomyState.load(root_dir)
    policy_enabled = PolicyConfig.load(root_dir).enabled
    level, clamps = resolve_runtime_level(
        state,
        config,
        policy_enabled=policy_enabled,
        root_dir=root_dir,
    )

    ui_impl.section("Autonomy")
    ui_impl.kv("level", f"L{state.level} - {state.autonomy_level.label}")
    if int(level) != state.level:
        ui_impl.kv("in force", f"L{int(level)}")
        for note in clamps:
            ui_impl.warn(f"  {note}")
    ui_impl.kv(
        "enabled",
        "yes" if config.enabled else "no ([autonomy] enabled=false)",
    )
    _report_autonomy_since(
        ui_impl,
        state,
        state_file=AutonomyState.path_for(root_dir),
        enabled=config.enabled,
    )
    if state.last_promoted_by:
        ui_impl.kv("promoted by", state.last_promoted_by)

    ui_impl.subsection("Flag bundle")
    # The bundle for the level that would actually be ENFORCED: showing
    # the stored level's permissions when max_level (or a disabled policy
    # envelope) clamps the run would advertise deploy/auto-merge the run
    # will never grant.
    for line in flag_bundle_for(level).describe():
        ui_impl.info(f"  {line}")

    ui_impl.subsection("Evidence at this level")
    ui_impl.kv("decisive runs", str(state.decisive_runs_at_level))
    ui_impl.kv("components merged", str(state.components_merged_at_level))
    ui_impl.kv("clean merge streak", str(state.clean_merges_at_level))
    ui_impl.kv("policy violations", str(state.policy_violations_at_level))
    if state.cooldown_runs_remaining:
        ui_impl.kv(
            "cool-down",
            f"{state.cooldown_runs_remaining} run(s) remaining",
        )

    blockers = [
        *state.promotion_blockers(),
        *entry_signal_blockers(root_dir, state.autonomy_level),
    ]
    ui_impl.subsection("Promotion")
    if blockers:
        ui_impl.warn("  Not eligible:")
        for blocker in blockers:
            ui_impl.info(f"    - {blocker}")
    else:
        ui_impl.ok("  Criteria met; `ks autonomy promote --actor <you> --ack <why>`")
    ui_impl.info("  Thresholds are UNMEASURED placeholders; run `ks autonomy replay`.")
    sys.exit(0)


@autonomy_group.command(name="promote")
@click.option("--actor", required=True, help="Who is acknowledging (a human)")
@click.option("--ack", required=True, help="Why the evidence justifies promotion")
@click.option(
    "--force",
    is_flag=True,
    help="Override unmet criteria (recorded as such in the audit trail)",
)
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_promote(
    actor: str,
    ack: str,
    force: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Raise the autonomy level by one. Requires a human ack."""
    from kstrl.autonomy import (
        AutonomyError,
        AutonomyLevel,
        AutonomyState,
        commit_transition,
        control_relocation_error,
        entry_signal_blockers,
        promotion_authority_error,
    )

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    # --actor/--ack are strings any caller can supply, so they cannot by
    # themselves distinguish a human from an unattended agent. Require an
    # out-of-band signal the agent's subprocess does not have: a
    # controlling terminal. Checked BEFORE the state is even loaded.
    authority_error = promotion_authority_error(force=force)
    if authority_error is not None:
        ui_impl.err(f"Promotion refused: {authority_error}")
        sys.exit(2)
    state = AutonomyState.load(root_dir)
    target = AutonomyLevel(min(int(state.autonomy_level) + 1, int(AutonomyLevel.L4_DEPLOY)))
    relocation_error = control_relocation_error(root_dir, target_level=target)
    if relocation_error is not None:
        ui_impl.err(f"Promotion refused: {relocation_error}")
        sys.exit(2)
    try:
        record = state.promote(
            actor=actor,
            ack=ack,
            force=force,
            signal_blockers=entry_signal_blockers(root_dir, state.autonomy_level),
        )
    except AutonomyError as exc:
        ui_impl.err(f"Promotion refused: {exc}")
        sys.exit(2)
    commit_transition(state, record, root_dir)
    ui_impl.ok(
        f"Promoted L{record.from_level} -> L{record.to_level} "
        f"({state.autonomy_level.label}) by {actor}"
    )
    if force:
        ui_impl.warn("  Recorded as a forced promotion over unmet criteria.")
    sys.exit(0)


@autonomy_group.command(name="demote")
@click.option("--reason", required=True, help="Why the level is being revoked")
@click.option(
    "--trigger",
    type=click.Choice(DEMOTION_TRIGGER_LABELS),
    default="manual",
    help="Which trigger fired",
)
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_demote(
    reason: str,
    trigger: str,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Drop the autonomy level by one and start the cool-down."""
    from kstrl.autonomy import AutonomyState, DemotionTrigger, commit_transition

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    state = AutonomyState.load(root_dir)
    chosen = next(
        (t for t in DemotionTrigger if t.label == trigger),
        DemotionTrigger.MANUAL,
    )
    record = state.demote(chosen, reason, actor="operator")
    if record is None:
        ui_impl.info("Already at L1 Supervised; nothing to revoke.")
        sys.exit(0)
    commit_transition(state, record, root_dir)
    ui_impl.warn(
        f"Demoted L{record.from_level} -> L{record.to_level} "
        f"({state.autonomy_level.label}); cool-down "
        f"{state.cooldown_runs_remaining} decisive run(s)"
    )
    sys.exit(0)


@autonomy_group.command(name="history")
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_history(root: Path | None, ui: str, no_color: bool) -> None:
    """Show every recorded level transition."""
    from kstrl.autonomy import AutonomyState

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    state = AutonomyState.load(root_dir)
    if not state.history:
        ui_impl.info("No transitions recorded; still at the starting level.")
        sys.exit(0)
    ui_impl.section("Autonomy history")
    for record in state.history:
        detail = f" [{record.trigger}]" if record.trigger else ""
        ui_impl.info(
            f"  {record.at}  L{record.from_level} -> L{record.to_level}  "
            f"{record.direction}{detail}  by {record.actor}"
        )
        if record.reason:
            ui_impl.info(f"      {record.reason}")
    sys.exit(0)


def _load_history_or_exit(
    ui: UI, root_dir: Path, experiments: Path | None = None
) -> tuple[list[RunRecord], EvolutionConfig]:
    """Load the recorded run history once, or print why not and exit 2.

    ``ks health`` and ``ks autonomy replay`` both route through this: one
    path-resolution rule (``EvolutionConfig.load``), one refusal class,
    where before #151's simplify pass each command wrote its own
    ``except (OSError, ValueError)`` clause, and ``ks autonomy replay``
    read the file a second time through a DIFFERENT default. Returning
    the config too, not just the runs, is what lets a caller reach
    ``journal_path`` without a second ``kstrl.toml`` read.
    """
    from kstrl.autonomy_replay import load_runs
    from kstrl.evolution import EvolutionConfig

    try:
        config = EvolutionConfig.load(root_dir)
        return load_runs(experiments or config.experiments_path), config
    except (OSError, TypeError, ValueError) as exc:
        # ValueError beside OSError because UnicodeDecodeError is one and
        # escapes a fail-closed `except OSError` (CLAUDE.md, encoding is
        # two-sided). TypeError because `EvolutionConfig.load` raises it
        # for a toml array where a number belongs; the list of what that
        # loader raises lives on `EvolutionConfig.load_or_none`'s
        # docstring. Without it a config typo is a traceback at exit 1,
        # which `ks health` documents as a breach.
        ui.err(f"could not read the recorded run history: {exc}")
        sys.exit(2)


@autonomy_group.command(name="replay")
@_autonomy_root_option
@click.option(
    "--experiments",
    type=click.Path(path_type=Path),
    help="Path to experiments.tsv (default: <root>/.kstrl/experiments.tsv)",
)
@_autonomy_ui_option
@_autonomy_no_color_option
def autonomy_replay_cmd(
    root: Path | None,
    experiments: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Replay the ladder's thresholds over recorded run history.

    Reports what WOULD have fired and whether the sample is large enough
    to calibrate anything. Never mutates ladder state. Exit code 2 means
    "insufficient data", so a script cannot mistake it for a green run.

    An UNREADABLE experiments.tsv exits 2 as well, and this is where it
    gets a cause. ``load_runs`` used to swallow the OSError and the
    decode error and return no runs, which reached the operator as
    "INSUFFICIENT DATA": a sentence about the project's history for
    what is a permission or an encoding problem. The exit code is the
    same because nothing was replayed either way; the difference is
    that the line above it now names the file and the error.

    This is also the advisory mode for the R8.4 health rules: it reports
    would-have-fired breaches and never demotes, so a candidate rule set is
    scored against real history before it is allowed to revoke a level.
    """
    from kstrl.autonomy_replay import replay
    from kstrl.health import breach_lines, readings_from

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    runs, config = _load_history_or_exit(ui_impl, root_dir, experiments)
    report = replay(runs)
    readings = readings_from(runs, config.journal_path)
    breaches = [r.breach for r in readings if r.breach is not None]
    for line in report.render().splitlines():
        ui_impl.info(line)
    ui_impl.info("R8.4 health rules (advisory), would have fired on this history:")
    for line in breach_lines(breaches) or ["  (none)"]:
        ui_impl.info(line)
    sys.exit(0 if report.sufficient_data else 2)


@cli.command(name="health")
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def health_cmd(root: Path | None, ui: str, no_color: bool) -> None:
    """Trend the factory's own run metrics against its own history (R8.4).

    Advisory. Exit 1 means at least one metric is outside the control
    limits computed from this repository's baseline period; exit 0 means
    no breach, which includes a history too short to say anything; exit 2
    means the recorded history could not be read, and the line above it
    names the cause.
    """
    from kstrl.health import health_report, readings_from

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    runs, config = _load_history_or_exit(ui_impl, root_dir)
    readings = readings_from(runs, config.journal_path)
    summary, breaches = health_report(readings)
    for line in summary.splitlines():
        ui_impl.info(line)
    sys.exit(1 if breaches else 0)


@cli.command(name="recheck")
@click.argument("record", type=click.Path(path_type=Path))
@_autonomy_root_option
@_autonomy_ui_option
@_autonomy_no_color_option
def recheck_cmd(record: Path, root: Path | None, ui: str, no_color: bool) -> None:
    """Run an acceptance record's saved checks again and compare (#700).

    RECORD is a record.json that `ks factory --acceptance` wrote, outside
    the repository, under <control dir>/runs/<run>/acceptance/<component>/
    attempt-<n>/. A relative RECORD is read from the current directory.
    `ks factory` prints each record's absolute path on a `- record:` line
    under its Acceptance lines. Every file beside it must match its
    index.json and the saved checks must be the record's plan; they then
    run again at the recorded head commit, in the replay of the [stack]
    the record ran under. Exit 0 means every verdict
    agrees with the record, 1 that one does not, and 2 that the record
    could not be rechecked, with the reason above it.
    """
    from kstrl.factory import FactoryConfig
    from kstrl.recheck import recheck
    from kstrl.verify import VerifyConfig

    root_dir = (root or Path.cwd()).resolve()
    ui_impl = _autonomy_ui(ui, no_color)
    config = FactoryConfig.load(root_dir)
    config.verify_config = VerifyConfig.load(root_dir)
    refused, lines, agrees = recheck(root_dir, record.resolve(), config, ui_impl)
    if _report_preflight(ui_impl, "the acceptance record cannot be rechecked", refused):
        sys.exit(2)
    for line in lines:
        ui_impl.info(line)
    sys.exit(0 if agrees else 1)
