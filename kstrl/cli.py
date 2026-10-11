"""CLI entry point for kstrl."""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kstrl.inbox import InboxItem

    # Annotation only, and the cheapest available: with
    # `from __future__ import annotations` above, _check_document's
    # signature is a string at run time and this block never executes.
    # Not a deferral - line 104 already imports kstrl.verify at module
    # scope, so it is on `ks --help`'s import path either way (measured
    # here at 5.2ms of cli.py's 84.7ms cumulative).
    from kstrl.verify_model import VerifyConfig


import click

from kstrl import cli_check, cli_feature, cli_learning, cli_loop, cli_queue, cli_report
from kstrl.agents import (
    get_agent,
)
from kstrl.agents.base import (
    ARCHITECT_COMPONENT,
    ARCHITECT_ROLE,
    UsageTotals,
    collect_usage,
    usage_cursor,
)
from kstrl.cli_preflight import (
    _HANDED_RUN_LOCK,
    _agent_preflight,
    _agent_settings,
    _AgentTypeChoice,
    _check_agent_preflight,
    _check_prompt_preflight,
    _collect_evolution_notes,
    _collect_toml_notes,
    _measure_base_before_architect,
    _reject_blank_project_name,
    _resolve_factory_run_lock,
    _stack_checkpoint,
)
from kstrl.cli_queue import (
    _allowed_actors_kv,
    _queue_for,
    _queue_no_color_option,
    _queue_root_option,
    _queue_ui_option,
)
from kstrl.cli_report import _print_execution_order, _render_safe_mode
from kstrl.cli_seam import (
    _PROGRESS_LOG_HELP,
    _actor,
    _autonomy_ui,
    _console_ui,
    _load_manifest_or_exit,
    _use_cli_value,
    cli,
)
from kstrl.commandrun import CommandRun, open_command_run
from kstrl.config import (
    KstrlConfig,
    reconcile_progress_config,
)
from kstrl.config_numbers import LIMIT_FLOAT, LIMIT_INT
from kstrl.config_report import UI_MODES
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.contract import ContractMode
from kstrl.decompose import SpecBlockerError, decompose_spec
from kstrl.factory import (
    BaseRefusedError,
    _report_preflight,
    base_check_before_architect,
    run_factory,
)
from kstrl.factory_config import VALID_REVIEW_MODES, FactoryConfig
from kstrl.factory_lock import (
    FactoryLockHeldError,
    LeftoverAgentsError,
    _acquire_run_lock,
    _RunLock,
)
from kstrl.git_base import (
    resolve_base_branch,
)
from kstrl.interaction import (
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from kstrl.launch_record import option_argv, replayable_flags
from kstrl.manifest import (
    MERGE_GATE_PARK_KEY,
    ComponentStatus,
    Manifest,
)
from kstrl.owner_answers import OwnerAnswerError
from kstrl.plan_gate import PLAN_GATE_KEY, spec_pin_errors
from kstrl.retry_plan import (
    RESUME_REFUSAL,
    RetryError,
    plan_resume,
    prepare_retry,
    preview_retry,
    print_resume_plan,
    print_retry_plan,
    retry_confirm_header,
)
from kstrl.security import _SEVERITY_ORDER, SecurityMode
from kstrl.serve_launchd import LAUNCHD_MODES
from kstrl.serve_spend import ARCHITECT_RUN_KIND
from kstrl.shutdown import StopController, install_signal_handlers
from kstrl.stack import (
    STACK_KEY,
    load_stack,
)
from kstrl.timeout import TimeoutConfig, limit_seconds
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


@cli.command()
@click.option(
    "--spec",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help=(
        "Markdown spec file, or a SpecKit artifact directory "
        "(spec.md [+ plan.md] [+ tasks.md]) to decompose"
    ),
)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--project-name",
    required=True,
    callback=_reject_blank_project_name,
    help="Name for this factory project",
)
@click.option(
    "--base-branch",
    default=None,
    help="Base git branch (default: auto-detected from the repository)",
)
@click.option(
    "--single-pr",
    is_flag=True,
    help="Use a single branch for all components",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another kstrl invocation holds "
    ".kstrl/factory.lock (may corrupt the other run's state)",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model",
    "-m",
    help="Model for the agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--agent-type",
    type=_AgentTypeChoice(),
    default="auto",
    help="Agent type",
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
@click.option(
    "--tui/--no-tui",
    "tui",
    default=None,
    help="Embedded dashboard (default: auto - on when stdin/stdout are "
    "TTYs and --ui is not plain; KSTRL_NO_TUI=1 forces off)",
)
def decompose(
    spec: Path,
    root: Path | None,
    project_name: str,
    base_branch: str | None,
    single_pr: bool,
    force_lock: bool,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str,
    ui: str,
    no_color: bool,
    tui: bool | None,
) -> None:
    """Decompose a spec into components and generate PRDs."""
    from kstrl.verify_model import VerifyConfig

    ctx = click.get_current_context()

    root_dir = root.resolve() if root else Path.cwd()

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    effective_cmd, effective_model, effective_reasoning, effective_type = _agent_settings(
        ctx, root_dir, agent_cmd, model, reasoning, agent_type
    )

    # R2.4 mirror (measured 2026-07-20): canonicalize aliases like
    # "claude" before get_agent, whose unrecognized-type fallthrough is
    # codex; the preflight also covers the no-agent-available check.
    canonical_type, type_error, type_hint = _agent_preflight(
        effective_cmd,
        effective_type,
    )
    if type_error:
        ui_impl.err(type_error)
        if type_hint:
            ui_impl.info(type_hint)
        sys.exit(2)
    effective_type = canonical_type or effective_type

    agent = get_agent(
        effective_cmd, effective_model, effective_reasoning, effective_type, root_dir=root_dir
    )

    # decompose_spec takes a `str` base and runs it through
    # validate_branch_name, so the flag's None is resolved here rather
    # than deeper, the way `ks check` already resolves --base (#259).
    effective_base = resolve_base_branch(base_branch, root_dir)
    # #603: the architect runs in this process, so an unset limit lets a
    # hung call hold the command; kstrl.toml was checked at entry.
    factory_config = FactoryConfig.load(root_dir)
    architect_timeout = limit_seconds(factory_config.architect_timeout_seconds)
    verify_config = VerifyConfig.load(root_dir)

    def _decompose_core(core_ui: UI, command_run: CommandRun) -> int:
        try:
            manifest = decompose_spec(
                spec_path=spec,
                project_name=project_name,
                base_branch=effective_base,
                single_pr=single_pr,
                agent=agent,
                ui=core_ui,
                root_dir=root_dir,
                bus=command_run.bus,
                transcript=command_run.transcript_writer(ARCHITECT_COMPONENT),
                prompt_call=command_run.agent_call(ARCHITECT_COMPONENT, ARCHITECT_ROLE),
                force_lock=force_lock,
                timeout=architect_timeout,
                before_spend=base_check_before_architect(
                    root_dir,
                    effective_base,
                    factory_config,
                    verify_config,
                    command_run.run_id,
                    core_ui,
                ),
            )
            core_ui.ok(f"Decomposed into {len(manifest.components)} components")
            return 0
        except (SpecBlockerError, OwnerAnswerError, BaseRefusedError) as exc:
            core_ui.err(str(exc))
            # R1.7: point at the durable artifacts so the user iterates
            # against files, not scrollback. Plural, because after #260
            # the audit holds the finding and the register holds the
            # question the owner has to answer.
            for line in exc.artifact_lines():
                core_ui.info(line)
            return 2
        except ValueError as exc:
            core_ui.err(str(exc))
            return 1

    use_tui = (
        tui
        if tui is not None
        else (
            sys.stdout.isatty()
            and sys.stdin.isatty()
            and os.environ.get("KSTRL_NO_TUI") != "1"
            and _normalize_ui_mode(ui) != "plain"
        )
    )
    if use_tui:
        if not (sys.stdout.isatty() and sys.stdin.isatty()):
            click.echo(
                "--tui requires an interactive terminal; use --no-tui "
                "for non-interactive execution.",
                err=True,
            )
            sys.exit(2)
        from kstrl.runid import mint_run_id
        from kstrl.tui.dispatch import initial_screens_for_kind
        from kstrl.tui.embed import EmbeddedContext, run_embedded

        def _target(embed_ctx: EmbeddedContext) -> int:
            command_run = open_command_run(
                embed_ctx.ui,
                root_dir,
                "decompose",
                component=ARCHITECT_COMPONENT,
                run_id=embed_ctx.run_id,
            )
            try:
                return _decompose_core(embed_ctx.ui, command_run)
            finally:
                command_run.close()

        sys.exit(
            run_embedded(
                _target,
                root_dir=root_dir,
                run_id=mint_run_id("decompose"),
                screen_factory=initial_screens_for_kind(
                    "decompose",
                    observe_only=False,
                ),
            )
        )

    command_run = open_command_run(
        ui_impl,
        root_dir,
        "decompose",
        component=ARCHITECT_COMPONENT,
    )
    try:
        code = _decompose_core(ui_impl, command_run)
    finally:
        command_run.close()
    sys.exit(code)


@cli.command()
@click.option(
    "--spec",
    type=click.Path(exists=True, path_type=Path),
    help=("Markdown spec file or SpecKit artifact directory (runs decompose first)"),
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, path_type=Path),
    help="Existing manifest file to build from instead of decomposing a spec "
    "(no default: pass this or --spec)",
)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--project-name",
    callback=_reject_blank_project_name,
    help="Name for this factory project (required with --spec)",
)
@click.option(
    "--base-branch",
    default=None,
    help="Base git branch (default: auto-detected from the repository)",
)
@click.option(
    "--single-pr",
    is_flag=True,
    help="Use a single branch/PR for all components",
)
@click.option(
    "--max-parallel",
    type=LIMIT_INT,
    default=None,
    help="Maximum parallel components (default: 4)",
)
@click.option(
    "--max-retries",
    type=LIMIT_INT,
    default=None,
    help="Maximum retries per component (default: 3)",
)
@click.option(
    "--create-prs/--no-prs",
    default=None,
    help="Create PRs for completed components (default: on)",
)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip Phase 1 mechanical verification",
)
@click.option(
    "--accept-red-base",
    metavar="SHA",
    default="",
    help="Run on a base branch whose gates fail, for this run only: at least 12 "
    "characters of the base commit's sha. A base that moved since refuses again; "
    "`ks retry` replays it",
)
@click.option(
    "--acceptance",
    metavar="DIR",
    default="",
    help="A directory outside the repository holding plan.json: acceptance checks run "
    "on the base and on each component's head inside the isolation rung. A failed "
    "held-out check halts the component; any other failed check is retried. Needs a "
    "[stack]; `ks retry` replays it",
)
@click.option(
    "--design-acceptance",
    is_flag=True,
    default=False,
    help="Ask the verification designer (the [review] agent) for each component's "
    "acceptance checks before the plan gate, then run them as --acceptance does: they "
    "gate the run as an operator's checks do. Needs a [stack]; `ks retry` replays it",
)
@click.option(
    "--bug-report",
    is_flag=True,
    default=False,
    help="The run fixes a reported bug (`ks serve` passes it for an issue with the bug "
    "label). When no acceptance check that the base keeps fails on the base, the run is "
    "refused before any engineer and an inbox item names the checks; approving it runs "
    "the bug without a reproduction. Needs --acceptance or --design-acceptance; "
    "`ks retry` replays it",
)
@click.option(
    "--review-mode",
    type=click.Choice(VALID_REVIEW_MODES),
    default=None,
    help="Phase 2 review: hard (block), advisory (warn), skip (default: hard)",
)
@click.option(
    "--review-agent-cmd",
    help="Custom agent for reviewer (default: same as implementation agent)",
)
@click.option(
    "--review-model",
    help="Model for reviewer agent",
)
@click.option(
    "--security-mode",
    type=click.Choice([mode.value for mode in SecurityMode]),
    default=None,
    help="Phase 2.5 security review: hard (block on findings at or above "
    "--security-fail-threshold), "
    "advisory (warn only, default), skip (turn the review off)",
)
@click.option(
    "--security-agent-cmd",
    help="Custom agent for security reviewer (default: same as implementation agent)",
)
@click.option(
    "--security-model",
    help="Model for security reviewer agent (default: same as implementation agent)",
)
@click.option(
    "--security-fail-threshold",
    type=click.Choice(list(_SEVERITY_ORDER)),
    default=None,
    help="In hard mode, findings at or above this severity block "
    "(default: high - critical+high fail)",
)
@click.option(
    "--contract-check",
    type=click.Choice([mode.value for mode in ContractMode]),
    default=None,
    help="Phase 3 contract testing: tier (per-tier), final (end-only), skip (default: tier)",
)
@click.option(
    "--agent-timeout",
    type=LIMIT_FLOAT,
    default=None,
    help="Timeout per agent iteration in seconds; 0 disables "
    "(default: no limit, or KSTRL_TIMEOUT_AGENT_ITERATION / "
    "[timeout].agent_iteration in kstrl.toml)",
)
@click.option(
    "--component-timeout",
    type=LIMIT_FLOAT,
    default=None,
    help="Timeout per component total in seconds; 0 disables "
    "(default: no limit, or KSTRL_TIMEOUT_COMPONENT / "
    "[timeout].component_total in kstrl.toml)",
)
@click.option(
    "--max-adversarial-calls",
    type=LIMIT_INT,
    default=None,
    help="Hard cap on adversarial LLM calls (review + security + "
    "distill) per run; 0 = unbounded (default: 0, or "
    "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS / "
    "[factory].max_adversarial_calls in kstrl.toml)",
)
@click.option(
    "--max-total-tokens",
    type=LIMIT_INT,
    default=None,
    help="Run-level token budget across ALL phases (engineer + review "
    "+ security + distill); 0 = unbounded. Counts CACHE READS at "
    "par, so it is a poor proxy for spend - prefer --max-cost-usd. "
    "On breach the current component fails with a synthetic budget "
    "finding and pending components halt (default: 0, or "
    "KSTRL_FACTORY_MAX_TOTAL_TOKENS "
    "/ [factory].max_total_tokens in kstrl.toml)",
)
@click.option(
    "--max-cost-usd",
    type=LIMIT_FLOAT,
    default=None,
    help="Run-level USD budget across ALL phases; 0 = unbounded. "
    "Checked between engineer iterations and at phase boundaries, "
    "so it is NOT a hard cap: the iteration already in flight is "
    "unbounded. Distinct from [agent] budget_usd, which is "
    "adapter-internal (claude-sdk only). Default: 0, or "
    "KSTRL_FACTORY_MAX_COST_USD / [factory].max_cost_usd in "
    "kstrl.toml",
)
@click.option(
    "--pause-before-pr-merge/--no-pause-before-pr-merge",
    default=None,
    help="Pause for human approval before each component's PR "
    "push+merge (default: off, or "
    "KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE / "
    "[factory].pause_before_pr_merge in kstrl.toml)",
)
@click.option(
    "--progress-log",
    type=click.Path(path_type=Path),
    help=_PROGRESS_LOG_HELP,
)
@click.option(
    "--no-worktrees",
    is_flag=True,
    help="Disable git worktrees (forces sequential execution)",
)
@click.option(
    "--keep-worktrees-on-failure",
    is_flag=True,
    help="Keep a failed component's worktree for post-mortem instead of "
    "removing it at cleanup; the failure summary points at it "
    "(default: off, or KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE / "
    "[factory].keep_worktrees_on_failure in kstrl.toml)",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another kstrl invocation holds "
    ".kstrl/factory.lock (may corrupt the other run's state)",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model",
    "-m",
    help="Model for the agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--agent-type",
    type=_AgentTypeChoice(),
    default="auto",
    help="Agent type",
)
@click.option(
    "--sleep",
    "-s",
    type=LIMIT_FLOAT,
    default=2.0,
    help="Sleep seconds between iterations",
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
@click.option(
    "--tui/--no-tui",
    "tui",
    default=None,
    help="Embedded dashboard (default: auto - on when stdin/stdout are "
    "TTYs and --ui is not plain; KSTRL_NO_TUI=1 forces off)",
)
def factory(
    tui: bool | None,
    spec: Path | None,
    manifest_path: Path | None,
    root: Path | None,
    project_name: str | None,
    base_branch: str | None,
    single_pr: bool,
    max_parallel: int | None,
    max_retries: int | None,
    create_prs: bool | None,
    no_verify: bool,
    accept_red_base: str,
    acceptance: str,
    design_acceptance: bool,
    bug_report: bool,
    review_mode: str | None,
    review_agent_cmd: str | None,
    review_model: str | None,
    security_mode: str | None,
    security_agent_cmd: str | None,
    security_model: str | None,
    security_fail_threshold: str | None,
    contract_check: str | None,
    agent_timeout: float | None,
    component_timeout: float | None,
    max_adversarial_calls: int | None,
    max_total_tokens: int | None,
    max_cost_usd: float | None,
    pause_before_pr_merge: bool | None,
    progress_log: Path | None,
    no_worktrees: bool,
    keep_worktrees_on_failure: bool,
    force_lock: bool,
    yes: bool,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str,
    sleep: float,
    ui: str,
    no_color: bool,
) -> None:
    """Run the software factory - decompose and execute a spec.

    Provide either --spec (to decompose first) or --manifest (to resume).
    """
    ctx = click.get_current_context()

    if not spec and not manifest_path:
        ui_impl = _console_ui("auto", no_color)
        ui_impl.err("Either --spec or --manifest is required")
        sys.exit(2)

    root_dir = root.resolve() if root else Path.cwd()

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    effective_cmd, effective_model, effective_reasoning, effective_type = _agent_settings(
        ctx, root_dir, agent_cmd, model, reasoning, agent_type
    )

    # R2.4 mirror (measured 2026-07-20): canonicalize aliases like
    # "claude" before get_agent, whose unrecognized-type fallthrough is
    # codex; the preflight also covers the no-agent-available check.
    canonical_type, type_error, type_hint = _agent_preflight(
        effective_cmd,
        effective_type,
    )
    if type_error:
        ui_impl.err(type_error)
        if type_hint:
            ui_impl.info(type_hint)
        sys.exit(2)
    effective_type = canonical_type or effective_type

    agent = get_agent(
        effective_cmd, effective_model, effective_reasoning, effective_type, root_dir=root_dir
    )

    # R8 review (#180): the budget ceilings are checked before the
    # decompose call below, so the architect never spends under a ceiling
    # that bounds nothing. The flags are checked by their click type
    # (config_numbers.LimitNumber, #571) before this body runs, and the
    # file and env values inside load. The flags are NOT applied here:
    # applying them early would change what _collect_toml_notes reports
    # as overridden further down.
    # #696 slice 3: before the configs that hold the stack are loaded, so an
    # answer given here is the one they read, and before any spend.
    _stack_checkpoint(root_dir, ui_impl, no_verify=no_verify)
    factory_config = FactoryConfig.load(root_dir)

    # Get or create manifest.
    #
    # #257: a --spec run also pays for the architect here, before the
    # factory run's id or directory exists, so what it cost has to be
    # carried into the run by hand for `--max-cost-usd` to bound five
    # roles instead of four. A --manifest resume ran no architect and
    # leaves this empty.
    #
    # Only the paths that reach run_factory carry it: a blocker halt exits
    # below, and its spend is recorded only in the decompose run the
    # architect ran as (#567). `ks serve` charges that run when no factory
    # run of the launch names it (#587). The factory run names it with
    # ``architect_run_id``, so the spend is counted once either way.
    # #597: `ks factory` takes the run lock before its first change, above
    # the manifest load and before the architect spends anything on
    # --spec, and holds it across the confirmation below (cli.py:3132,
    # lane #594's own lines). See ``_resolve_factory_run_lock`` for the
    # handed-vs-acquired resolution.
    run_lock: _RunLock = _resolve_factory_run_lock(ctx, root_dir, ui_impl, force_lock)

    try:
        architect_usage = UsageTotals()
        architect_run_id = ""
        if manifest_path:
            try:
                manifest = Manifest.load(manifest_path)
            except Exception as exc:
                ui_impl.err(f"Failed to load manifest: {exc}")
                sys.exit(2)
        else:
            assert spec is not None
            if not project_name:
                ui_impl.err("--project-name is required with --spec")
                sys.exit(2)

            # Read BEFORE the work, and sliced from there afterwards, the
            # same way `decompose_spec` reports it - so the two derivations
            # of "what the architect spent" cannot disagree, and neither
            # rests on an invariant about who else touched this agent.
            usage_before = usage_cursor(agent)
            # #567: the architect runs as a decompose run of its own, the way
            # `ks decompose` and the dashboard's decompose launch run it, so
            # its prompt records, events and transcript have a run to live
            # in. Not the factory's run: that directory would exist for the
            # whole architect call with no event stream, and a factory-kind
            # run with no stream is what safe mode reports as "could not
            # read run" (measured on a failed decompose).
            architect_run = open_command_run(
                ui_impl,
                root_dir,
                ARCHITECT_RUN_KIND,
                component=ARCHITECT_COMPONENT,
            )
            architect_run_id = architect_run.run_id
            spec_base = resolve_base_branch(base_branch, root_dir)
            try:
                manifest = decompose_spec(
                    spec_path=spec,
                    project_name=project_name,
                    # The --manifest path above is untouched: a manifest
                    # already carries its own base_branch. Only the --spec
                    # path has a flag to resolve, and detection is the
                    # default rather than the literal "main" (#259).
                    base_branch=spec_base,
                    single_pr=single_pr,
                    agent=agent,
                    ui=ui_impl,
                    root_dir=root_dir,
                    bus=architect_run.bus,
                    transcript=architect_run.transcript_writer(ARCHITECT_COMPONENT),
                    prompt_call=architect_run.agent_call(ARCHITECT_COMPONENT, ARCHITECT_ROLE),
                    force_lock=force_lock,
                    run_lock=run_lock,
                    timeout=limit_seconds(factory_config.architect_timeout_seconds),
                    before_spend=functools.partial(
                        _measure_base_before_architect,
                        root_dir,
                        spec_base,
                        factory_config,
                        accept_red_base,
                        no_verify,
                        architect_run_id,
                        ui_impl,
                    ),
                )
            except (SpecBlockerError, OwnerAnswerError) as exc:
                # Architect halted: it escalated a question only the owner
                # can answer (#260), or the owner's inbox answers cannot be
                # read (#639). Surface it and exit cleanly. The user
                # answers, edits the spec and re-runs, iterating against the
                # persisted artifacts (R1.7).
                ui_impl.err(str(exc))
                for line in exc.artifact_lines():
                    ui_impl.info(line)
                sys.exit(2)
            except ValueError as exc:
                ui_impl.err(str(exc))
                sys.exit(1)
            finally:
                architect_run.close()
            architect_usage = collect_usage(agent, since=usage_before)

        # Build configs (R2.1). Resolution order for every phase config:
        # explicit CLI flag > env > kstrl.toml > dataclass default. The
        # loaders handle env-over-toml-over-default; flags use None
        # sentinels so "not passed" is distinguishable from "passed the
        # default value", and an explicitly-passed flag is applied on top.
        from kstrl.contract import ContractConfig
        from kstrl.feedforward import CodebaseScanConfig
        from kstrl.security import SecurityConfig
        from kstrl.verify_model import VerifyConfig

        toml_notes: list[str] = []

        # factory_config was loaded in the budget preflight above.
        _collect_toml_notes(
            toml_notes,
            "factory",
            factory_config,
            FactoryConfig.from_env(),
            flag_overridden={
                name
                for name, passed in (
                    ("max_parallel", max_parallel is not None),
                    ("max_retries", max_retries is not None),
                    ("create_prs", create_prs is not None),
                    ("review_mode", review_mode is not None),
                    ("max_adversarial_calls", max_adversarial_calls is not None),
                    ("max_total_tokens", max_total_tokens is not None),
                    ("max_cost_usd", max_cost_usd is not None),
                    ("pause_before_pr_merge", pause_before_pr_merge is not None),
                    ("use_worktrees", no_worktrees),
                    ("keep_worktrees_on_failure", keep_worktrees_on_failure),
                    # The manifest is authoritative for single_pr, so a toml
                    # value never becomes effective in this command.
                    ("single_pr", True),
                )
                if passed
            },
        )
        if max_parallel is not None:
            factory_config.max_parallel = max_parallel
        if max_retries is not None:
            factory_config.max_retries = max_retries
        if create_prs is not None:
            factory_config.create_prs = create_prs
        if review_mode is not None:
            factory_config.review_mode = review_mode
        if max_adversarial_calls is not None:
            factory_config.max_adversarial_calls = max_adversarial_calls
        if max_total_tokens is not None:
            factory_config.max_total_tokens = max_total_tokens
        if max_cost_usd is not None:
            factory_config.max_cost_usd = max_cost_usd
        if pause_before_pr_merge is not None:
            factory_config.pause_before_pr_merge = pause_before_pr_merge
            # #195: passing the flag either way is an explicit operator
            # request, so the ladder may not lower it. This is also what
            # makes `ks serve` honour a stop_at_pr item at L3: its child
            # always gets one of --pause-before-pr-merge /
            # --no-pause-before-pr-merge (serve.subprocess_factory_runner).
            factory_config.explicit_fields |= {"pause_before_pr_merge"}
        if no_worktrees:
            factory_config.use_worktrees = False
        if keep_worktrees_on_failure:
            factory_config.keep_worktrees_on_failure = True
        factory_config.single_pr = manifest.single_pr
        factory_config.review_agent_cmd = review_agent_cmd
        factory_config.review_model = review_model
        factory_config.progress_log_path = progress_log
        if progress_log is not None:
            # An explicit --progress-log path is an explicit opt-in; it wins
            # over a toml/env progress_log_enabled = false.
            factory_config.progress_log_enabled = True
        factory_config.force_lock = force_lock
        factory_config.accept_red_base = accept_red_base
        factory_config.acceptance_dir = acceptance
        factory_config.design_acceptance = design_acceptance
        factory_config.bug_report = bug_report
        # #436: what `ks retry` replays; see kstrl/launch_record.py.
        factory_config.launch_flags = replayable_flags(ctx)
        # R2.3: --no-verify is an explicit skip sentinel that run_factory
        # honors; verify_config=None alone would substitute default checks.
        factory_config.skip_verification = no_verify

        v_config: VerifyConfig | None = None
        if not no_verify:
            v_config = VerifyConfig.load(root_dir)
            _collect_toml_notes(
                toml_notes,
                "verify",
                v_config,
                VerifyConfig.from_env(),
                flag_overridden=set(),
            )

        s_config = SecurityConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes,
            "security",
            s_config,
            SecurityConfig.from_env(),
            flag_overridden={
                name
                for name, passed in (
                    ("mode", security_mode is not None),
                    ("agent_cmd", security_agent_cmd is not None),
                    ("model", security_model is not None),
                    ("fail_threshold", security_fail_threshold is not None),
                )
                if passed
            },
        )
        if security_mode is not None:
            s_config.mode = security_mode
        if security_agent_cmd is not None:
            s_config.agent_cmd = security_agent_cmd
        if security_model is not None:
            s_config.model = security_model
        if security_fail_threshold is not None:
            s_config.fail_threshold = security_fail_threshold

        contract_resolved = ContractConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes,
            "contract",
            contract_resolved,
            ContractConfig.from_env(),
            flag_overridden={
                name for name, passed in (("mode", contract_check is not None),) if passed
            },
        )
        if contract_check is not None:
            contract_resolved.mode = contract_check
        # mode == "skip" keeps the historical contract of passing no config.
        c_config: ContractConfig | None = (
            contract_resolved if contract_resolved.mode != "skip" else None
        )

        ff_config = CodebaseScanConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes,
            "codebase_scan",
            ff_config,
            CodebaseScanConfig.from_env(),
            flag_overridden=set(),
        )

        # Evolution config is consumed inside run_factory via
        # EvolutionJournal.open; swept here only for the NOTE lines, and by
        # a helper because a failed load must not cost the run. The helper
        # says why this section and not its five raising siblings.
        _collect_evolution_notes(toml_notes, root_dir, ui_impl)

        # R0.1: TimeoutConfig is the single source for timeout values.
        timeout_config = TimeoutConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes,
            "timeout",
            timeout_config,
            TimeoutConfig.from_env(),
            flag_overridden={
                name
                for name, passed in (
                    ("agent_iteration", agent_timeout is not None),
                    ("component_total", component_timeout is not None),
                )
                if passed
            },
        )
        if agent_timeout is not None:
            timeout_config.agent_iteration = agent_timeout
        if component_timeout is not None:
            timeout_config.component_total = component_timeout

        factory_config.verify_config = v_config
        factory_config.security_config = s_config
        factory_config.contract_config = c_config
        factory_config.codebase_scan_config = ff_config
        factory_config.timeout_config = timeout_config

        # Display summary and confirm (resolved values, not raw flags)
        ui_impl.section("Factory Plan")
        ui_impl.kv("Project", manifest.project_name)
        ui_impl.kv("Components", str(len(manifest.components)))
        ui_impl.kv("Base branch", manifest.base_branch)
        ui_impl.kv("Single PR", "yes" if manifest.single_pr else "no")
        ui_impl.kv("Max parallel", str(factory_config.max_parallel))
        ui_impl.kv("Create PRs", "yes" if factory_config.create_prs else "no")

        # R2.1 behavior change: kstrl.toml sections that used to be silently
        # ignored now take effect. Surface every value a toml section moved
        # away from the CLI default so existing setups see the change.
        for note in toml_notes:
            ui_impl.info(note)

        _print_execution_order(manifest, ui_impl)

        _factory_channel = UiInteractionChannel(ui_impl)
        if not yes and _factory_channel.can_prompt():
            response = _factory_channel.request(
                PromptRequest(
                    kind=PromptKind.CONFIRM,
                    header="Proceed with factory execution?",
                    options=("Start", "Quit"),
                    default=0,
                )
            )
            # #594: only an answered Start starts a paid run. An unanswered
            # confirm carries no choice (#647), so it quits exactly as Quit does.
            if response.choice != 0:
                ui_impl.info("Factory not started.")
                sys.exit(0)

        kstrl_dir = root_dir / "scripts" / "kstrl"
        base_config = KstrlConfig.load(root_dir)
        if _use_cli_value(ctx, "agent_cmd"):
            base_config.agent_cmd = agent_cmd
        if _use_cli_value(ctx, "model"):
            base_config.model = model
        if _use_cli_value(ctx, "reasoning"):
            base_config.model_reasoning_effort = reasoning
        if _use_cli_value(ctx, "agent_type"):
            base_config.agent_type = agent_type
        if _use_cli_value(ctx, "sleep"):
            base_config.sleep_seconds = sleep
        base_config.ui_mode = "plain"
        base_config.no_color = True

        # R2.4 mirror for the factory path (measured 2026-07-20 on the first
        # real factory run): without this, a toml alias like type = "claude"
        # reaches get_agent RAW in every engineer worker and silently falls
        # through to the codex default - and _cli_family misreads the
        # engineer family, inverting the R7.1 reviewer rotation.
        _check_agent_preflight(base_config, ui_impl)

        # --no-verify leaves no reader, so there is nothing to reconcile.
        if v_config is not None:
            # R8 review: the progress log's writer ([paths] progress) and its
            # reader ([verify] progress_file_path) default to the same
            # derivation, so they agree until exactly ONE is set - and then the
            # self-critique check inspects a file the engineer never wrote and
            # fails for a reason the operator cannot see from either setting.
            # The reconciliation lives in config.py (and is applied in ONE path
            # domain, review finding 3) so a test can exercise the same wiring
            # this command runs.
            mismatch = reconcile_progress_config(base_config, v_config, root_dir)
            if mismatch is not None:
                ui_impl.warn(mismatch)

        # Ensure prompt file exists
        if not base_config.prompt_file.exists():
            default_prompt = kstrl_dir / "prompt.md"
            if default_prompt.exists():
                base_config.prompt_file = default_prompt

        # #286: after the fallback above, so it speaks about the file every
        # worker will actually read (#569: from the root checkout, never a
        # copy in its worktree).
        _check_prompt_preflight(base_config.prompt_file, ui_impl)

        # R0.5 (H-15): state saves back to the file it was loaded from.
        # --manifest /custom.json persists to /custom.json; --spec runs keep
        # the default scripts/kstrl/manifest.json that decompose wrote.
        use_tui = (
            tui
            if tui is not None
            else (
                sys.stdout.isatty()
                and sys.stdin.isatty()
                and os.environ.get("KSTRL_NO_TUI") != "1"
                and _normalize_ui_mode(ui) != "plain"
            )
        )
        if use_tui:
            if not (sys.stdout.isatty() and sys.stdin.isatty()):
                click.echo(
                    "--tui requires an interactive terminal; use --no-tui "
                    "for non-interactive execution.",
                    err=True,
                )
                sys.exit(2)
            # PR F: embedded dashboard. The pre-execution confirm already
            # happened on the plain terminal (plan decision: no
            # modal-before-app); everything from here renders in Textual.
            from kstrl.tui.embed import run_factory_embedded

            sys.exit(
                run_factory_embedded(
                    manifest,
                    factory_config,
                    base_config,
                    root_dir,
                    manifest_path,
                    architect_usage=architect_usage,
                    architect_run_id=architect_run_id,
                    run_lock=run_lock,
                )
            )

        stop = StopController()
        uninstall = install_signal_handlers(stop)
        try:
            result = run_factory(
                manifest,
                factory_config,
                base_config,
                ui_impl,
                root_dir,
                manifest_path=manifest_path,
                stop=stop,
                architect_usage=architect_usage,
                architect_run_id=architect_run_id,
                run_lock=run_lock,
            )
        finally:
            uninstall()
        sys.exit(result.exit_code)
    finally:
        run_lock.release()


@cli.command()
@click.argument("component_id")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    help="Manifest file (default: <root>/scripts/kstrl/manifest.json)",
)
@click.option(
    "--progress-log",
    type=click.Path(path_type=Path),
    help=_PROGRESS_LOG_HELP,
)
@click.option(
    "--keep-worktrees-on-failure",
    is_flag=True,
    help="Keep a failed component's worktree for post-mortem instead of "
    "removing it at cleanup (also via "
    "KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE / "
    "[factory].keep_worktrees_on_failure in kstrl.toml)",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another kstrl invocation holds "
    ".kstrl/factory.lock (may corrupt the other run's state)",
)
@click.option(
    "--max-cost-usd",
    type=LIMIT_FLOAT,
    default=None,
    help="Run-level USD budget for the retry; overrides the value the run "
    "being resumed was launched with. 0 = unbounded.",
)
@click.option(
    "--max-total-tokens",
    type=LIMIT_INT,
    default=None,
    help="Run-level token budget for the retry; overrides the value the run "
    "being resumed was launched with. 0 = unbounded.",
)
@click.option(
    "--max-adversarial-calls",
    type=LIMIT_INT,
    default=None,
    help="Cap on adversarial LLM calls for the retry; overrides the value the "
    "run being resumed was launched with. 0 = unbounded.",
)
@click.option(
    "--agent-timeout",
    type=LIMIT_FLOAT,
    default=None,
    help="Timeout per agent iteration in seconds for the retry; overrides the "
    "value the run being resumed was launched with. 0 disables.",
)
@click.option(
    "--component-timeout",
    type=LIMIT_FLOAT,
    default=None,
    help="Timeout per component total in seconds for the retry; overrides the "
    "value the run being resumed was launched with. 0 disables.",
)
@click.option(
    "--max-parallel",
    type=LIMIT_INT,
    default=None,
    help="Maximum parallel components for the retry; overrides the value "
    "the run being resumed was launched with.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
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
def retry(
    component_id: str,
    root: Path | None,
    manifest_path: Path | None,
    progress_log: Path | None,
    keep_worktrees_on_failure: bool,
    force_lock: bool,
    max_cost_usd: float | None,
    max_total_tokens: int | None,
    max_adversarial_calls: int | None,
    agent_timeout: float | None,
    component_timeout: float | None,
    max_parallel: int | None,
    yes: bool,
    ui: str,
    no_color: bool,
) -> None:
    """Retry a FAILED component from the factory manifest (R3.3).

    Resets COMPONENT_ID and its cascade-skipped dependents to PENDING,
    removes the failed attempt's kept worktree and branch (a retry
    starts fresh from the base branch; the failed attempt's findings
    stay in the evolution journal), then re-enters `ks factory` with the
    same manifest and the options the run being resumed was launched
    with, read from its launch record (#436). --max-parallel,
    --keep-worktrees-on-failure and every run limit option given here win
    over the recorded ones. A retry that would drop a run limit the run
    it resumes ran under (cost, tokens, adversarial calls, agent and
    component timeouts), or cannot tell whether it did, is refused before
    anything is changed (#526). Nothing is changed until the confirmation
    is answered Start and the run lock is taken; a live run's lock is a
    refusal (exit 2), and the lock is held into the run (#597).

    One branch is kept instead (#646): when Phase 1 failed only on
    policy_envelope, and an approved inbox item was
    taken on the branch's tip, the retry keeps that commit and judges it
    again with no engineer. The plan says which, and why.
    """
    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    manifest_file = (
        manifest_path
        if manifest_path is not None
        else root_dir / "scripts" / "kstrl" / "manifest.json"
    )
    if not manifest_file.exists():
        ui_impl.err(f"No manifest found at {manifest_file}")
        ui_impl.info("Run `ks factory` first, or pass --manifest.")
        sys.exit(2)
    manifest = _load_manifest_or_exit(manifest_file, ui_impl)

    try:
        preview = preview_retry(manifest, component_id, root_dir=root_dir)
    except ValueError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    plan, problems, _unkept = plan_resume(
        root_dir,
        manifest,
        manifest_file,
        factory,
        max_cost_usd=max_cost_usd,
        max_parallel=max_parallel,
        keep_worktrees_on_failure=keep_worktrees_on_failure,
        limits={
            "max_total_tokens": max_total_tokens,
            "max_adversarial_calls": max_adversarial_calls,
            "agent_timeout": agent_timeout,
            "component_timeout": component_timeout,
        },
    )
    if plan is None:
        _report_preflight(ui_impl, RESUME_REFUSAL, problems)
        sys.exit(2)

    print_retry_plan(ui_impl, preview, manifest_file)
    print_resume_plan(ui_impl, plan)

    _retry_channel = UiInteractionChannel(ui_impl)
    if not yes and _retry_channel.can_prompt():
        response = _retry_channel.request(
            PromptRequest(
                kind=PromptKind.CONFIRM,
                header=retry_confirm_header(preview),
                options=("Start", "Quit"),
                default=0,
            )
        )
        if response.choice != 0:  # None when nobody answered (#647)
            sys.exit(0)

    run_lock = _acquire_run_lock(root_dir, ui_impl, force=force_lock)
    try:
        # Re-read under the lock: a run may have saved while the question was open.
        manifest = _load_manifest_or_exit(manifest_file, ui_impl)
        try:
            if preview_retry(manifest, component_id, root_dir=root_dir) != preview:
                ui_impl.err(
                    f"{manifest_file} changed while the confirmation was open, so "
                    f"nothing was changed; run `ks retry {component_id}` again to see "
                    "the new plan"
                )
                sys.exit(2)
            prepare_retry(manifest, component_id, manifest_file, root_dir, ui_impl)
        except ValueError as exc:
            ui_impl.err(str(exc))
            sys.exit(2)
        except RetryError:
            sys.exit(2)

        # Re-enter through `ks factory` itself, so config assembly, preflights
        # and the Execution header are the factory's own and cannot drift.
        argv = option_argv(
            factory,
            {
                "manifest_path": str(manifest_file),
                "root": str(root_dir),
                "yes": True,
                "tui": False,
                "ui": ui,
                "no_color": no_color,
                "progress_log": str(progress_log) if progress_log is not None else None,
                "force_lock": force_lock,
            },
        )
        factory_ctx = factory.make_context(
            "factory", [*argv, *plan.argv], parent=click.get_current_context()
        )
        factory_ctx.meta[_HANDED_RUN_LOCK] = run_lock
        with factory_ctx:
            factory.invoke(factory_ctx)
    finally:
        run_lock.release()


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
            from kstrl.serve import settle_approval_run
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
COMMAND_MODULES = (cli_check, cli_feature, cli_learning, cli_loop, cli_queue, cli_report)


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
