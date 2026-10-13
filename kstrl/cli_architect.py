"""ks decompose and ks retry: the architect run that writes a manifest, and the
re-entry into ks factory for one failed component.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from kstrl.agents import get_agent
from kstrl.cli_factory import factory
from kstrl.cli_preflight import (
    _HANDED_RUN_LOCK,
    _agent_preflight,
    _agent_settings,
    _AgentTypeChoice,
    _reject_blank_project_name,
)
from kstrl.cli_seam import _PROGRESS_LOG_HELP, _console_ui, _load_manifest_or_exit, cli
from kstrl.commandrun import CommandRun, open_command_run
from kstrl.config import LIMIT_FLOAT, LIMIT_INT
from kstrl.config_report import UI_MODES
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.decompose import decompose_spec
from kstrl.decompose_audit import SpecBlockerError
from kstrl.events import ARCHITECT_COMPONENT, ARCHITECT_ROLE
from kstrl.factory_config import FactoryConfig
from kstrl.factory_preflight import (
    BaseRefusedError,
    base_check_before_architect,
    report_preflight,
)
from kstrl.git import resolve_base_branch
from kstrl.interaction import PromptKind, PromptRequest, UiInteractionChannel
from kstrl.launch_record import option_argv
from kstrl.owner_answers import OwnerAnswerError
from kstrl.proc import acquire_run_lock, limit_seconds
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
from kstrl.ui import UI

if TYPE_CHECKING:
    pass


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
) -> None:
    """Decompose a spec into components and generate PRDs."""
    from kstrl.verify_model import VerifyConfig

    ctx = click.get_current_context()

    root_dir = root.resolve() if root else Path.cwd()

    force_rich = os.environ.get("KSTRL_UI_FORCE_RICH") == "1"
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
    force_rich = os.environ.get("KSTRL_UI_FORCE_RICH") == "1"
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
        report_preflight(ui_impl, RESUME_REFUSAL, problems)
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

    run_lock = acquire_run_lock(root_dir, ui_impl, force=force_lock)
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
