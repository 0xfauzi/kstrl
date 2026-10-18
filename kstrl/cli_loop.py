"""ks init, ks run and ks understand: the commands that drive the single-agent
loop in the checkout, and the notices for the factory knobs ks run overrides.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click

from kstrl.agents import (
    Agent,
    LoggingAgent,
    SandboxConfig,
    get_agent,
    recording_prompts,
    warn_unconfined,
)
from kstrl.cli_preflight import (
    _apply_cli_overrides,
    _check_agent_preflight,
    _check_prd_preflight,
    _check_prompt_preflight,
)
from kstrl.cli_seam import _console_ui, _resolve_path, _resolve_root, _use_cli_value, cli
from kstrl.config import LIMIT_FLOAT, LIMIT_INT, KstrlConfig, parse_paths, resolve_config_file
from kstrl.config_report import UI_MODES
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.events import (
    ArtifactWritten,
    ComponentCompleted,
    ComponentFailed,
    ComponentStarted,
    PhaseCompleted,
    PhaseStarted,
    RunCompleted,
    RunPlan,
    RunStarted,
)
from kstrl.factory import CommandRun, open_command_run, run_factory
from kstrl.git import detect_base_branch
from kstrl.loop import BreakerConfig, run_loop
from kstrl.pipeline import FactoryConfig
from kstrl.plan import PRD
from kstrl.proc import StopController, TimeoutConfig, install_signal_handlers
from kstrl.project import run_init
from kstrl.ui import UI

# Issue #207: `ks run` forces these FactoryConfig fields because it is by
# definition a local, single-component, no-PR invocation. When the resolved
# config (kstrl.toml + env) set one of them to a non-default value, the
# operator must be told the knob does not apply - a silent override of a
# configured knob is exactly the fail-open-and-quiet behaviour R8.3
# corrected on the factory path. Field name -> (forced value, why the
# configured value does not apply).
_RUN_FORCED_STRUCTURAL_FIELDS: tuple[tuple[str, object, str], ...] = (
    ("max_parallel", 1, "ks run executes a single component"),
    ("use_worktrees", False, "ks run works directly in the repo checkout"),
    ("single_pr", False, "ks run creates no PRs"),
    ("create_prs", False, "ks run creates no PRs"),
)


def _toml_literal(value: object) -> str:
    """Render a Python value the way the operator wrote it in kstrl.toml."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _run_structural_override_notices(loaded: FactoryConfig) -> list[str]:
    """Notices for structural config that `ks run` is about to force.

    Compares the resolved config against the dataclass defaults so a notice
    fires only when the operator (kstrl.toml or env) actually set a knob
    that ``ks run`` forces to a different value - an unset knob stays
    silent, so the notices never become background noise.

    ``pause_before_pr_merge`` is deliberately NOT handled here. The
    autonomy ladder resolves inside ``run_factory`` and its L1/L2 bundle
    can flip the gate on when no config flag ever set it, so any check at
    this point sees a stale value. The authoritative warning is
    ``factory.merge_gate_unreachable_warning``, emitted after autonomy
    resolution - the one point that sees the final flag on every path.
    """
    defaults = FactoryConfig()
    notices: list[str] = []
    for field_name, forced_value, reason in _RUN_FORCED_STRUCTURAL_FIELDS:
        configured = getattr(loaded, field_name)
        if configured == forced_value:
            continue
        if configured == getattr(defaults, field_name):
            continue
        notices.append(
            f"[factory] {field_name} = {_toml_literal(configured)} does not "
            f"apply to `ks run` ({reason}); using "
            f"{_toml_literal(forced_value)}"
        )
    return notices


@cli.command()
@click.argument("max_iterations", type=LIMIT_INT, default=10)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prompt",
    "-p",
    type=str,
    help="Prompt file path",
)
@click.option(
    "--prd",
    type=str,
    help="PRD file path",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model",
    "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--sleep",
    "-s",
    type=LIMIT_FLOAT,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    help="Enable human-in-the-loop mode",
)
@click.option(
    "--branch",
    help="Git branch to use (empty string to skip checkout)",
)
@click.option(
    "--allowed-paths",
    help="Comma-separated allowed paths for guardrails",
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
    "--ascii",
    is_flag=True,
    help="Use ASCII characters only",
)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip mechanical verification (raw loop, no post-checks)",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another kstrl invocation holds "
    ".kstrl/factory.lock (may corrupt the other run's state)",
)
def run(
    max_iterations: int,
    root: Path | None,
    prompt: str | None,
    prd: str | None,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    sleep: float,
    interactive: bool,
    branch: str | None,
    allowed_paths: str | None,
    ui: str,
    no_color: bool,
    ascii: bool,
    no_verify: bool,
    force_lock: bool,
) -> None:
    """Run the agentic loop as a single-component factory invocation.

    MAX_ITERATIONS is the maximum number of iterations (default: 10).

    Delegates to the factory pipeline with mechanical verification.
    Use --no-verify to skip the verification phase.
    """
    ctx = click.get_current_context()
    env_prompt = os.environ.get("PROMPT_FILE")
    env_prd = os.environ.get("PRD_FILE")

    prompt_for_root = Path(prompt) if _use_cli_value(ctx, "prompt") and prompt is not None else None
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)

    # Build config from kstrl.toml + environment defaults first.
    config = KstrlConfig.load(root_dir)

    # Apply CLI overrides when explicitly provided.
    _apply_cli_overrides(
        ctx,
        config,
        root_dir,
        prompt_default=root_dir / "scripts/kstrl/prompt.md",
        prd_default=root_dir / "scripts/kstrl/prd.json",
    )

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    force_rich = os.environ.get("KSTRL_UI_FORCE_RICH") == "1"
    ui_impl = _console_ui(
        config.ui_mode,
        config.no_color,
        config.ascii_only,
        force_rich=force_rich,
    )

    if config.max_iterations < 0:
        ui_impl.err(f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})")
        sys.exit(2)

    # R2.4 preflight: verify the agent the config selects is reachable,
    # then validate the PRD - both BEFORE any agent invocation.
    _check_agent_preflight(config, ui_impl)
    _check_prd_preflight(config.prd_file, ui_impl)
    _check_prompt_preflight(config.prompt_file, ui_impl)

    # Single-component factory invocation
    from kstrl.config import load_toml_section
    from kstrl.plan import Manifest
    from kstrl.project import CodebaseScanConfig
    from kstrl.review import SecurityConfig
    from kstrl.verify import VerifyConfig

    # Determine branch from config or PRD. The preflight above already
    # validated existence + schema, so a load failure here is a real bug
    # worth surfacing, not something to swallow.
    prd_branch = PRD.load(config.prd_file).branch_name

    effective_branch = config.kstrl_branch or prd_branch or "kstrl/run"

    # Detect base branch from git
    detected_base = detect_base_branch(root_dir)

    # Build single-component manifest from PRD
    rel_prd = str(config.prd_file)
    try:
        rel_prd = str(config.prd_file.relative_to(root_dir))
    except ValueError:
        pass

    manifest = Manifest.from_prd(
        prd_path=Path(rel_prd),
        branch=effective_branch,
        base_branch=detected_base,
    )

    # Build factory config for single-component mode (R2.1): tunables
    # resolve through the loaders (kstrl.toml overlaid with env); the
    # structural fields below are forced because `ks run` is by
    # definition a local, single-component, no-PR invocation.
    # R2.3: --no-verify sets the explicit skip sentinel; passing
    # verify_config=None meant "use defaults" in run_factory and Phase 1
    # ran anyway. Codebase scan is independent of --no-verify (it builds
    # context, not checks).
    factory_cfg = FactoryConfig.load(root_dir)
    # Issue #207: say which configured knobs the forcing below overrides,
    # BEFORE mutating the config. The merge-gate warning itself is emitted
    # later, by run_factory after autonomy resolution (see
    # factory.merge_gate_unreachable_warning).
    for notice in _run_structural_override_notices(factory_cfg):
        ui_impl.warn(notice)
    factory_cfg.max_parallel = 1
    factory_cfg.use_worktrees = False
    factory_cfg.single_pr = False
    factory_cfg.create_prs = False
    factory_cfg.verify_config = None if no_verify else VerifyConfig.load(root_dir)
    factory_cfg.skip_verification = no_verify
    factory_cfg.security_config = SecurityConfig.load(root_dir)
    factory_cfg.contract_config = None
    factory_cfg.codebase_scan_config = CodebaseScanConfig.load(root_dir)
    factory_cfg.timeout_config = TimeoutConfig.load(root_dir)
    factory_cfg.force_lock = force_lock
    # `ks run` reviews in advisory mode unless the project's
    # kstrl.toml explicitly opts into a different review_mode (there is
    # no review_mode env var, so the toml section check is exhaustive).
    if "review_mode" not in load_toml_section(resolve_config_file(root_dir), "factory"):
        factory_cfg.review_mode = "advisory"

    # R0.5 (H-15): `ks run` persists to its own run-manifest.json so
    # it can never clobber a factory run's resumable manifest.json.
    stop = StopController()
    uninstall = install_signal_handlers(stop)
    try:
        factory_result = run_factory(
            manifest,
            factory_cfg,
            config,
            ui_impl,
            root_dir,
            manifest_path=root_dir / "scripts" / "kstrl" / "run-manifest.json",
            stop=stop,
        )
    finally:
        uninstall()
    sys.exit(factory_result.exit_code)


@cli.command()
@click.argument("directory", type=click.Path(path_type=Path), default=".")
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
    "--upgrade-prompts",
    is_flag=True,
    help="Rewrite any scaffolded prompt template that is still a "
    "pristine older kstrl template. A template you have edited is "
    "reported and left alone.",
)
def init(directory: Path, ui: str, no_color: bool, upgrade_prompts: bool) -> None:
    """Initialize kstrl in a project directory.

    DIRECTORY is the target project directory (default: current directory).
    """
    force_rich = os.environ.get("KSTRL_UI_FORCE_RICH") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)
    exit_code = run_init(directory, ui_impl, upgrade_prompts=upgrade_prompts)
    sys.exit(exit_code)


@cli.command()
@click.argument("max_iterations", type=LIMIT_INT, default=10)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prompt",
    "-p",
    type=str,
    help="Prompt file path",
)
@click.option(
    "--prd",
    type=str,
    help="PRD file path",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command",
)
@click.option(
    "--model",
    "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--sleep",
    "-s",
    type=LIMIT_FLOAT,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    help="Enable human-in-the-loop mode",
)
@click.option(
    "--branch",
    help="Git branch (default: kstrl/understanding)",
)
@click.option(
    "--allowed-paths",
    help="Comma-separated allowed paths for guardrails",
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
    "--ascii",
    is_flag=True,
    help="Use ASCII characters only",
)
def understand(
    max_iterations: int,
    root: Path | None,
    prompt: str | None,
    prd: str | None,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    sleep: float,
    interactive: bool,
    branch: str | None,
    allowed_paths: str | None,
    ui: str,
    no_color: bool,
    ascii: bool,
) -> None:
    """Run codebase understanding loop (read-only mode).

    MAX_ITERATIONS is the maximum number of iterations (default: 10).

    This mode:
    - Uses understand_prompt.md instead of prompt.md
    - Only allows edits to codebase_map.md
    - Works on kstrl/understanding branch by default
    """
    ctx = click.get_current_context()
    env_prompt = os.environ.get("PROMPT_FILE")
    env_prd = os.environ.get("PRD_FILE")

    prompt_for_root = Path(prompt) if _use_cli_value(ctx, "prompt") and prompt is not None else None
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)
    kstrl_dir = root_dir / "scripts" / "kstrl"

    # Create codebase_map.md if missing
    codebase_map = kstrl_dir / "codebase_map.md"
    if not codebase_map.exists():
        from kstrl.project import DEFAULT_CODEBASE_MAP

        codebase_map.parent.mkdir(parents=True, exist_ok=True)
        codebase_map.write_text(DEFAULT_CODEBASE_MAP)

    config = KstrlConfig.load(root_dir)

    # Apply CLI overrides when explicitly provided.
    if _use_cli_value(ctx, "max_iterations"):
        config.max_iterations = max_iterations
    if _use_cli_value(ctx, "prompt"):
        config.prompt_file = _resolve_path(root_dir, prompt, kstrl_dir / "understand_prompt.md")
    if _use_cli_value(ctx, "prd"):
        config.prd_file = _resolve_path(root_dir, prd, kstrl_dir / "prd.json")
    if _use_cli_value(ctx, "sleep"):
        config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        config.interactive = interactive
    if _use_cli_value(ctx, "allowed_paths"):
        config.allowed_paths = parse_paths(allowed_paths)
    if _use_cli_value(ctx, "branch"):
        config.kstrl_branch = branch
        config.kstrl_branch_explicit = True
    if _use_cli_value(ctx, "agent_cmd"):
        config.agent_cmd = agent_cmd
    if _use_cli_value(ctx, "model"):
        config.model = model
    if _use_cli_value(ctx, "reasoning"):
        config.model_reasoning_effort = reasoning
    if _use_cli_value(ctx, "ui"):
        config.ui_mode = _normalize_ui_mode(ui)
    if _use_cli_value(ctx, "no_color"):
        config.no_color = no_color
    if _use_cli_value(ctx, "ascii"):
        config.ascii_only = ascii

    # Apply understanding defaults when not overridden by env or CLI.
    if not _use_cli_value(ctx, "prompt") and "PROMPT_FILE" not in os.environ:
        config.prompt_file = kstrl_dir / "understand_prompt.md"
    if not _use_cli_value(ctx, "allowed_paths") and "ALLOWED_PATHS" not in os.environ:
        config.allowed_paths = ["scripts/kstrl/codebase_map.md"]
    # Only fall back to the understand-mode branch default when no other
    # source (CLI / env / TOML) supplied a branch. KstrlConfig.load sets
    # kstrl_branch_explicit=True when TOML provides a non-empty [git].branch.
    if (
        not _use_cli_value(ctx, "branch")
        and "KSTRL_BRANCH" not in os.environ
        and not config.kstrl_branch_explicit
    ):
        config.kstrl_branch = "kstrl/understanding"
        config.kstrl_branch_explicit = False

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    force_rich = os.environ.get("KSTRL_UI_FORCE_RICH") == "1"
    ui_impl = _console_ui(
        config.ui_mode,
        config.no_color,
        config.ascii_only,
        force_rich=force_rich,
    )

    if config.max_iterations < 0:
        ui_impl.err(f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})")
        sys.exit(2)

    # R2.4 preflight: accept whichever agent the resolved config selects.
    _check_agent_preflight(config, ui_impl)
    _check_prompt_preflight(config.prompt_file, ui_impl)

    sandbox_cfg = SandboxConfig.load(root_dir)
    agent = get_agent(
        config.agent_cmd,
        config.model,
        config.model_reasoning_effort,
        config.agent_type,
        sandbox=sandbox_cfg,
        max_budget_usd=config.agent_budget_usd,
        root_dir=root_dir,
    )

    command_run = open_command_run(
        ui_impl,
        root_dir,
        "understand",
        component="understand",
    )
    try:
        code = _understand_core(
            config,
            agent,
            root_dir,
            ui_impl,
            run=command_run,
            sandbox=sandbox_cfg,
        )
    finally:
        command_run.close()
    sys.exit(code)


def _understand_core(
    config: KstrlConfig,
    agent: Agent,
    root_dir: Path,
    ui_impl: UI,
    *,
    run: CommandRun,
    sandbox: SandboxConfig,
) -> int:
    """The understand loop as an event-stream run (surface C1).

    The reducer projects the work onto the pseudo-component
    "understand": one plan row, one phase, the loop's iterations. When
    recording, the agent is wrapped so its transcript lands where
    ``ks status`` and replay read it. An understand agent with no
    sandbox is a warning in the run's events.jsonl (#700).
    """
    warn_unconfined(ui_impl.warn, sandbox, {"understand agent": config.agent_cmd})
    bus = run.bus
    component = "understand"
    loop_agent = agent
    transcript = run.transcript_path(component)
    if transcript is not None:
        loop_agent = LoggingAgent(agent, transcript)

    started = time.monotonic()
    bus.emit(RunStarted(project=root_dir.name, components=1, pid=os.getpid()))
    bus.emit(
        RunPlan(components=({"id": component, "title": "Codebase understanding", "deps": []},))
    )
    bus.emit(ComponentStarted(component=component))
    bus.emit(PhaseStarted(component=component, phase="understand", attempt=1))

    # #264/#268: the standalone loop gets the same carve-out the factory
    # gets. `ks understand` writes the codebase map - the understand
    # prompt says it is the ONLY file it may edit - and its progress log.
    # The `understand` command's own default allowed_paths already names
    # the map, but an operator who passes --allowed-paths REPLACES that
    # default, and the in-loop guard then reverts the one file the prompt
    # told the agent to write. `ks run` needs nothing here: it is a single-component
    # factory invocation (Manifest.from_prd -> run_factory), so it
    # carries the carve-out through factory._run_component.
    understand_harness_paths = config.standalone_harness_files(root_dir)

    try:
        with recording_prompts(run.agent_call(component, "understand")):
            result = run_loop(
                config,
                ui_impl,
                loop_agent,
                root_dir,
                timeouts=TimeoutConfig.load(root_dir),
                breaker_config=BreakerConfig.load(root_dir),
                bus=bus,
                guard_ignored_paths=understand_harness_paths,
                guard_state_root=root_dir,
            )
    except Exception as exc:
        duration = round(time.monotonic() - started, 2)
        detail = f"{type(exc).__name__}: {exc}"
        bus.emit(
            PhaseCompleted(
                component=component,
                phase="understand",
                passed=False,
                detail=detail,
                duration_seconds=duration,
            )
        )
        bus.emit(ComponentFailed(component=component, error=detail))
        bus.emit(
            RunCompleted(
                completed=0,
                failed=1,
                duration_seconds=duration,
            )
        )
        raise

    duration = round(time.monotonic() - started, 2)
    passed = result.completed and result.exit_code == 0
    failure_detail = (
        f"exit {result.exit_code}" if result.exit_code != 0 else "ended before completion"
    )
    bus.emit(
        PhaseCompleted(
            component=component,
            phase="understand",
            passed=passed,
            detail="" if passed else failure_detail,
            duration_seconds=duration,
        )
    )
    if passed:
        map_path = config.codebase_map_file
        try:
            map_display = str(map_path.relative_to(root_dir))
        except ValueError:
            map_display = str(map_path)
        bus.emit(ArtifactWritten(label="codebase_map", path=map_display))
        bus.emit(
            ComponentCompleted(
                component=component,
                duration_seconds=duration,
                iterations=result.iterations,
            )
        )
    else:
        bus.emit(
            ComponentFailed(
                component=component,
                error=f"understand loop {failure_detail}",
            )
        )
    bus.emit(
        RunCompleted(
            completed=1 if passed else 0,
            failed=0 if passed else 1,
            duration_seconds=duration,
        )
    )
    return result.exit_code
