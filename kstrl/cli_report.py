"""Commands that report state: ks status, ks dash, ks config show and the plan listing."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click

from kstrl.cli_preflight import _AgentTypeChoice, _apply_cli_overrides
from kstrl.cli_seam import _console_ui, _preflight_warn, cli
from kstrl.config import KstrlConfig
from kstrl.config_report import UI_MODES, build_config_report
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.manifest import COMPONENT_STATUS_VALUES, Manifest
from kstrl.observability import event_age_seconds, format_age, latest_run_id, read_progress_events
from kstrl.procgroup import pid_is_alive
from kstrl.reducer import ComponentState, RunState, fold, load_run_state, upconvert_v1
from kstrl.ui.base import UI
from kstrl.version import stamp_label


def _format_component_status(status: str | None) -> str:
    """Render a component status for the plan, flagging an off-enum one.

    A status the enum does not know can never be scheduled, so echoing it
    plain reads as confirmation that the manifest is correct - the plan
    line for a component that will silently never run looks exactly like
    the plan line for one that will (#263). ``None`` means the execution
    order named an id the manifest has no component for.
    """
    if status is None:
        return "?"
    if status in COMPONENT_STATUS_VALUES:
        return status
    return f"{status} (not a valid status)"


def _print_execution_order(manifest: Manifest, ui: UI) -> None:
    """The plan's execution order, or why there is none (#531).

    A graph that does not validate has no order to print:
    ``topological_order`` raises ValueError on it. ``run_factory`` refuses
    that graph with exit 2 after it has
    recorded the architect's spend (#257), so this listing must not end
    the command first.
    """
    ui.info("")
    if manifest.validate_dag():
        ui.warn("Execution order: none, the dependency graph does not validate")
        return
    ui.info("Execution order:")
    for i, comp_id in enumerate(manifest.topological_order(), 1):
        comp = manifest.get_component(comp_id)
        status = _format_component_status(comp.status if comp else None)
        dep_list = ", ".join(comp.dependencies) if comp and comp.dependencies else ""
        deps = f" (depends on: {dep_list})" if dep_list else ""
        ui.info(f"  {i}. {comp_id} [{status}]{deps}")


def _echo_config_problems(problems: list[str]) -> None:
    """`ks config show`'s rejected-section block, in ONE shape.

    Both of that command's failure paths print it: the one where no row
    could be built, and the one where the rows printed and some section
    was still rejected. Two renderings 35 lines apart is how an operator
    piping stderr gets a different answer for the same fault.
    """
    click.echo("# Rejected sections (every other command refuses while these stand)")
    for problem in problems:
        click.echo(f"  {problem}")
    click.echo("")
    click.echo(f"error: {len(problems)} unusable config section(s)", err=True)


# Display structure for the KstrlConfig-backed kstrl.toml sections:
# section -> [(toml_key, dataclass_field)]. Mirrors DEFAULT_KSTRL_TOML in
# init_cmd.py plus the env/flag-only UI knobs (ui_mode, no_color).
@cli.group(name="config")
def config_group() -> None:
    """Inspect kstrl configuration."""


@config_group.command(name="show")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option("--max-iterations", type=int, help="Override [run] max_iterations")
@click.option("--prompt", "-p", type=str, help="Override [paths] prompt")
@click.option("--prd", type=str, help="Override [paths] prd")
@click.option("--sleep", "-s", type=float, help="Override [run] sleep_seconds")
@click.option("--interactive", "-i", is_flag=True, help="Override [run] interactive")
@click.option("--allowed-paths", help="Override [paths] allowed")
@click.option("--branch", help="Override [git] branch")
@click.option("--agent-cmd", help="Override [agent] command")
@click.option("--model", "-m", help="Override [agent] model")
@click.option("--reasoning", help="Override [agent] reasoning_effort")
@click.option("--agent-type", type=_AgentTypeChoice(), help="Override [agent] type")
@click.option(
    "--ui",
    type=click.Choice(UI_MODES),
    default="auto",
    help="Override [ui] ui_mode",
)
@click.option("--no-color", is_flag=True, help="Override [ui] no_color")
@click.option("--ascii", is_flag=True, help="Override [ui] ascii")
def config_show(
    root: Path | None,
    max_iterations: int | None,
    prompt: str | None,
    prd: str | None,
    sleep: float | None,
    interactive: bool,
    allowed_paths: str | None,
    branch: str | None,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str | None,
    ui: str,
    no_color: bool,
    ascii: bool,
) -> None:
    """Print the fully resolved config with the source of each value.

    Every value is tagged (flag), (env), (toml), or (default). Flags
    mirror `ks run`'s config-affecting options, so the output is what
    a run invoked with the same flags would execute. Factory-phase
    sections (factory/verify/security/...) have no flags here; their
    values resolve from env > kstrl.toml > defaults.

    Source detection for env is behavioral: a value is tagged (env) when
    removing the environment changes it. An env var that sets a value
    identical to the toml/default value is therefore reported as the
    lower-precedence source; the effective value is identical either way.
    """
    ctx = click.get_current_context()
    root_dir = root.resolve() if root else Path.cwd()

    def _overlay(config: KstrlConfig) -> set[str]:
        return _apply_cli_overrides(
            ctx,
            config,
            root_dir,
            prompt_default=root_dir / "scripts/kstrl/prompt.md",
            prd_default=root_dir / "scripts/kstrl/prd.json",
        )

    from kstrl.config_preflight import (
        SURFACE_REJECTIONS,
        config_problem_lines,
        raise_if_defect,
    )

    try:
        report = build_config_report(root_dir, overlay=_overlay)
    except SURFACE_REJECTIONS as exc:
        # No rows are possible: the base config itself was rejected, or
        # the document will not parse. Report it in the same shape as the
        # success-with-problems path below, and in the seam's words -
        # section, key, offending value - rather than the bare coercion
        # message this used to print.
        #
        # The tuple is IMPORTED, and `config` being preflight-exempt is
        # exactly why it has to be: no entry seam catches this first.
        # `except ValueError` here let `[run] max_iterations = ["3"]`
        # out as a raw TypeError traceback, from the one command whose
        # whole job is explaining a broken config (#289).
        #
        # Except when the RuntimeError is one kstrl never defined: that
        # is our defect, and printing it under "configuration problems"
        # both blames the operator and loses the traceback.
        raise_if_defect(exc)
        _echo_config_problems(config_problem_lines(root_dir, warn=_preflight_warn) or [str(exc)])
        sys.exit(1)

    toml_path = report.toml_path
    click.echo(f"# Resolved kstrl config for {root_dir}")
    click.echo(f"# kstrl.toml: {toml_path if report.toml_exists else '(absent)'}")
    click.echo("")

    section = ""
    for row in report.rows:
        if row.section != section:
            if section:
                click.echo("")
            section = row.section
            click.echo(f"[{section}]")
        click.echo(f"  {row.key} = {row.value}  ({row.source})")
    click.echo("")

    # Every command refuses on an unusable section, so ONE command has to
    # always run and always explain: this one. The rows above cover what
    # resolved (a rejected section costs its rows, not the report); the
    # problems below say why, in the words the rest of the CLI uses.
    problems = config_problem_lines(root_dir, warn=_preflight_warn)
    if problems:
        _echo_config_problems(problems)
        sys.exit(1)

    sys.exit(0)


def _age_label(ts: str) -> str:
    """ "5m ago" for an event timestamp, or "" when unparseable."""
    age = event_age_seconds(ts)
    if age is None:
        return ""
    return f"{format_age(age)} ago"


def _age_label_epoch(ts: float) -> str:
    """ "5m ago" for a float epoch timestamp (reducer times), or ""."""
    if ts <= 0:
        return ""
    age = max(0.0, time.time() - ts)
    return f"{format_age(age)} ago"


def _render_safe_mode(ui_impl: UI, root_dir: Path) -> None:
    """Print the safe-mode block: nominal, or one line per reason.

    Shared by `ks status` and `ks serve --dry-run` so the two surfaces
    cannot word the same state differently. A REPORT, never a gate:
    safe mode refuses nothing by itself, because every signal it reads
    already refuses where refusing is right.

    ``info`` rather than ``warn`` for the reason lines: PlainUI prefixes
    warnings with "WARN: ", which would break the line format the
    runbook documents.
    """
    from kstrl.safemode import safe_mode_reasons

    reasons = safe_mode_reasons(root_dir)
    if not reasons:
        ui_impl.kv("safe mode", "nominal")
        return
    ui_impl.kv("safe mode", f"{len(reasons)} reason(s)")
    for reason in reasons:
        ui_impl.info(f"  - [{reason.source}] {reason.detail} (see {reason.recovery})")


def _render_run_version(
    ui_impl: UI,
    manifest: Manifest,
    state: RunState | None,
    source_path: Path | None,
) -> None:
    """The kstrl version that wrote the run this report shows (#451).

    From the event stream when the report reads one, because every line
    of it is stamped; from the manifest's run stamp when there is no
    stream. A v1 progress log carries no stamp, so that arm prints none.
    A record with no stamp was written before stamping, and the label
    says so rather than reporting it as a defect.
    """
    if state is not None and source_path is not None:
        if source_path.name == "events.jsonl":
            ui_impl.kv("kstrl version", stamp_label(state.kstrl_version))
        return
    if manifest.run_id:
        ui_impl.kv("Run id", manifest.run_id)
        ui_impl.kv("kstrl version", stamp_label(manifest.kstrl_version))


def _render_architect_run(ui_impl: UI, state: RunState) -> None:
    """The run that holds this run's architect records, when it names one (#587)."""
    if state.architect_run_id:
        ui_impl.kv("Architect run", state.architect_run_id)


def _run_state_label(state: RunState) -> str:
    """``finished``, ``in flight``, or stopped when the run's kstrl process
    is gone without a finish record (#642). A run that recorded no pid
    stays ``in flight``: there is nothing to check."""
    if state.finished:
        return "finished"
    if state.pid and not pid_is_alive(state.pid):
        return f"stopped without a finish record (kstrl process {state.pid} is not running)"
    return "in flight"


def _render_leftover_agents(ui_impl: UI, root_dir: Path) -> None:
    """Agent processes a kstrl process that is gone left running (#642).

    Report only: `ks status` takes no lock and refuses nothing. The
    lock-taking commands refuse on the same reading.
    """
    from kstrl.worktree_sweep import leftover_agents, leftover_lines

    sweep = leftover_agents(root_dir)
    if not (sweep.survivors or sweep.error):
        return
    count = "unknown" if sweep.error else f"{len(sweep.survivors)} still running"
    ui_impl.kv("leftover agents", count)
    for line in leftover_lines(sweep):
        ui_impl.info(f"  - {line}")


def _render_status(
    manifest: Manifest,
    manifest_file: Path,
    ui_impl: UI,
    state: RunState | None = None,
    source_path: Path | None = None,
    root_dir: Path | None = None,
) -> None:
    """Render the per-component status view from a manifest.

    ``state`` is the reducer's RunState joined onto the same
    per-component skeleton (chunk 8): phase (authoritative under the v2
    layout, inferred for v1 logs), attempt, last-event age, usage
    totals, PR/checkpoint/heartbeat detail, and evidence paths.
    """
    ui_impl.section("ks status")
    ui_impl.kv("Project", manifest.project_name)
    ui_impl.kv("Manifest", str(manifest_file))
    ui_impl.kv("Base branch", manifest.base_branch)

    if state is not None and source_path is not None:
        label = "Events" if source_path.name == "events.jsonl" else "Progress log"
        ui_impl.kv(label, str(source_path))
        if state.run_id:
            ui_impl.kv("Run id", state.run_id)
        _render_architect_run(ui_impl, state)
        if state.last_event_ts:
            age = _age_label_epoch(state.last_event_ts)
            run_state = _run_state_label(state)
            ui_impl.kv(
                "Run state",
                f"{run_state} (last event {age})" if age else run_state,
            )
        if state.usage_calls:
            # Per axis (R8 review finding 1): the two totals can be
            # short by different amounts, and on the measured run only
            # the dollar one was.
            token_note = "+" if state.tokens_are_lower_bound else ""
            cost_note = "+" if state.cost_is_lower_bound else ""
            caps = []
            if state.max_total_tokens:
                caps.append(f"{state.max_total_tokens} token cap")
            if state.max_cost_usd:
                caps.append(f"${state.max_cost_usd} cost cap")
            ui_impl.kv(
                "Run usage",
                f"{state.total_tokens}{token_note} tokens, "
                f"${state.cost_usd:.4f}{cost_note}" + (f" of {', '.join(caps)}" if caps else ""),
            )
            for gap in state.coverage_gaps.values():
                # The ceiling's own words, uncovered magnitude in TOKENS.
                if gap.detail:
                    ui_impl.kv("  coverage", gap.detail)
    _render_run_version(ui_impl, manifest, state, source_path)

    if root_dir is not None:
        _render_safe_mode(ui_impl, root_dir)
        _render_leftover_agents(ui_impl, root_dir)

    counts: dict[str, int] = {}
    for comp in manifest.components:
        counts[comp.status] = counts.get(comp.status, 0) + 1
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    ui_impl.kv("Components", f"{len(manifest.components)} ({summary})" if summary else "0")

    for comp in manifest.components:
        ui_impl.info("")
        ui_impl.info(f"{comp.id}: {comp.status}")
        ui_impl.kv("  branch", comp.branch_name)
        ui_impl.kv("  retries", str(comp.retries))
        if comp.started_at:
            ui_impl.kv("  started_at", comp.started_at)
        if comp.completed_at:
            ui_impl.kv("  completed_at", comp.completed_at)

        comp_state: ComponentState | None = (
            state.components.get(comp.id) if state is not None else None
        )
        if comp.pr_url:
            pr_note = (
                f" ({comp_state.pr_state})"
                if comp_state is not None and comp_state.pr_state
                else ""
            )
            ui_impl.kv("  pr", f"{comp.pr_url}{pr_note}")
        elif comp_state is not None and comp_state.pr_url:
            ui_impl.kv(
                "  pr",
                f"{comp_state.pr_url} ({comp_state.pr_state})",
            )
        if comp.error:
            ui_impl.kv("  error", comp.error)

        if comp_state is not None:
            if comp_state.phase:
                ui_impl.kv("  phase", comp_state.phase)
            attempt = comp_state.attempt or comp.retries + 1
            ui_impl.kv("  attempt", str(attempt))
            if comp_state.last_event:
                age = _age_label_epoch(comp_state.last_event_ts)
                ui_impl.kv(
                    "  last event",
                    f"{comp_state.last_event} ({age})" if age else comp_state.last_event,
                )
            if comp_state.checkpoint_open:
                ui_impl.kv(
                    "  checkpoint",
                    f"{comp_state.checkpoint_open} awaiting decision",
                )
            if comp_state.last_heartbeat_ts and comp.status in ("running", "verifying"):
                ui_impl.kv(
                    "  worker",
                    f"last heartbeat {_age_label_epoch(comp_state.last_heartbeat_ts)}",
                )
            if comp_state.usage_calls:
                # Which AXIS is short, not just how many calls said
                # nothing (R8 review finding 1): a call reporting a cost
                # and no token count is not "unreported" but still
                # leaves the token total short.
                short = [
                    name
                    for name, is_short in (
                        ("tokens", comp_state.tokens_are_lower_bound),
                        ("cost", comp_state.cost_is_lower_bound),
                    )
                    if is_short
                ]
                detail = f"lower bound: {', '.join(short)}" if short else ""
                if comp_state.unreported_calls:
                    detail += f"; {comp_state.unreported_calls} call(s) unreported"
                note = f" ({detail})" if detail else ""
                ui_impl.kv(
                    "  usage",
                    f"{comp_state.total_tokens} tokens, "
                    f"${comp_state.cost_usd:.4f}, "
                    f"{comp_state.usage_calls} calls{note}",
                )
        # Evidence paths: whatever this run left on disk for the
        # component (worktree kept after a failure, adversarial raw
        # outputs under .kstrl/debug/).
        if root_dir is not None and state is not None and state.run_id:
            evidence = [
                path
                for path in (
                    root_dir / ".kstrl" / "worktrees" / state.run_id / comp.id,
                    root_dir / ".kstrl" / "debug" / state.run_id / comp.id,
                )
                if path.exists()
            ]
            for path in evidence:
                ui_impl.kv("  evidence", str(path))
    _render_ci(ui_impl, root_dir, manifest)


def _render_ci(ui_impl: UI, root_dir: Path | None, manifest: Manifest) -> None:
    """The recorded CI state of every merge commit kstrl produced (#570).

    Reads the ledger ``ks serve`` and ``ks ci poll`` write; no network.
    A commit never read, and every commit when the record cannot be
    read, shows as unknown with the reason, never as passed.
    """
    if root_dir is None:
        return
    from kstrl.ci_state import read_ci_ledger, recorded_merges

    try:
        merges = recorded_merges(root_dir, manifest)
        ledger = read_ci_ledger(root_dir)
    except (OSError, ValueError) as exc:
        ui_impl.info("")
        ui_impl.err(f"CI: unknown, the record could not be read: {type(exc).__name__}: {exc}")
        return
    if not merges:
        return
    ui_impl.info("")
    ui_impl.info("CI (merge commits)")
    for component_id, sha in merges:
        reading = ledger.latest(sha)
        shown = (
            f"{sha[:12]}  unknown: never read"
            if reading is None
            else f"{sha[:12]}  {reading.state}: {reading.reason} (read {reading.observed_at})"
        )
        ui_impl.kv(f"  {component_id}", shown)
    if ledger.dropped:
        ui_impl.kv("  ledger", f"{ledger.dropped} line(s) could not be read")


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    help="Manifest file (default: <root>/scripts/kstrl/manifest.json, falling "
    "back to <root>/scripts/kstrl/run-manifest.json)",
)
@click.option(
    "--progress-log",
    "progress_log_path",
    type=click.Path(path_type=Path),
    help="Progress log to join onto the manifest (default: the newest run under "
    "<root>/.kstrl/runs/, falling back to <root>/.kstrl/progress.jsonl)",
)
@click.option(
    "--watch",
    is_flag=True,
    help="Re-render on an interval until interrupted",
)
@click.option(
    "--interval",
    type=float,
    default=5.0,
    help="Polling interval in seconds for --watch (default: 5)",
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
def status(
    root: Path | None,
    manifest_path: Path | None,
    progress_log_path: Path | None,
    watch: bool,
    interval: float,
    ui: str,
    no_color: bool,
) -> None:
    """Show per-component status from the manifest + progress log.

    R3.2 (plain report): joins the factory manifest with the
    ProgressLog (default .kstrl/progress.jsonl): per component status,
    retries, branch, timestamps, plus phase, attempt, last-event age,
    usage totals and evidence paths for the latest run found in the
    log. Works manifest-only when no log exists.
    """
    import time as _time

    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    if manifest_path is not None:
        candidates = [manifest_path]
    else:
        # Factory runs persist to manifest.json; `ks run` persists to
        # run-manifest.json (R0.5, H-15). Prefer the factory manifest.
        candidates = [
            root_dir / "scripts" / "kstrl" / "manifest.json",
            root_dir / "scripts" / "kstrl" / "run-manifest.json",
        ]

    def _load_state(manifest: Manifest) -> tuple[RunState | None, Path | None]:
        """Chunk 8: the versioned reader.

        An explicit --progress-log pins the v1 arm on that file.
        Otherwise the reducer resolves the newest v2 run dir (preferring
        the manifest's recorded run when it still exists on disk) and
        falls back to v1 progress.jsonl up-conversion.
        """
        if progress_log_path is not None:
            raw = read_progress_events(progress_log_path)
            if not raw:
                return None, None
            rid = latest_run_id(raw)
            return (
                fold((upconvert_v1(e) for e in raw), run_id=rid),
                progress_log_path,
            )
        state, source = load_run_state(root_dir, manifest.run_id or "")
        if manifest.run_id and (source is None or not state.started_ts):
            # The recorded run left no stream (dir pruned, or a v1 log
            # that predates it): fall back to the newest stream rather
            # than rendering nothing.
            state, source = load_run_state(root_dir)
        if source is None:
            return None, None
        return state, source

    def _load_and_render() -> int:
        manifest_file = next((p for p in candidates if p.exists()), None)
        if manifest_file is None:
            looked = ", ".join(str(p) for p in candidates)
            ui_impl.err(f"No manifest found (looked for: {looked})")
            ui_impl.info("Run `ks factory` or `ks run` first, or pass --manifest.")
            # Safe mode does not depend on a manifest, and "nothing has
            # run here" is exactly when an operator wants to know whether
            # the factory is holding back. Withholding the answer on this
            # path would make the question unaskable on a repo that has
            # never completed a run - including this one (R10.4).
            _render_safe_mode(ui_impl, root_dir)
            return 2

        try:
            manifest = Manifest.load(manifest_file)
        except (OSError, ValueError) as exc:
            ui_impl.err(f"Failed to load manifest {manifest_file}: {exc}")
            # Same reason as the missing-manifest path above: the
            # predicate does not read the manifest, so a broken one must
            # not hide a paused queue.
            _render_safe_mode(ui_impl, root_dir)
            return 2

        state, source_path = _load_state(manifest)
        _render_status(
            manifest,
            manifest_file,
            ui_impl,
            state=state,
            source_path=source_path,
            root_dir=root_dir,
        )
        return 0

    if not watch:
        sys.exit(_load_and_render())

    try:
        while True:
            click.clear()
            exit_code = _load_and_render()
            if exit_code != 0:
                sys.exit(exit_code)
            _time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        sys.exit(0)
