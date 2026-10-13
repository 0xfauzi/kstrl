"""ks feature: understand a codebase for one feature, then build it."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from kstrl.agents import get_agent
from kstrl.cli_preflight import _check_agent_preflight, _check_prompt_preflight
from kstrl.cli_seam import _console_ui, _resolve_path, _resolve_root, _use_cli_value, cli
from kstrl.commandrun import open_command_run
from kstrl.config import KstrlConfig, parse_paths
from kstrl.config_numbers import LIMIT_FLOAT, LIMIT_INT
from kstrl.config_report import UI_MODES
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.feature_cmd import FeatureParams, run_feature
from kstrl.prd import PRD
from kstrl.sandbox import SandboxConfig, with_stack_writable
from kstrl.scaffold_templates import DEFAULT_FEATURE_UNDERSTAND
from kstrl.stack import stack_in_force


def _derive_feature_name(prd_path: Path, root: Path) -> str:
    try:
        rel = prd_path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = None

    if rel is not None and len(rel.parts) >= 4:
        if rel.parts[0] == "scripts" and rel.parts[1] == "kstrl" and rel.parts[2] == "feature":
            return rel.parts[3]

    return prd_path.stem


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prd",
    type=str,
    help="Feature PRD file path",
)
@click.option(
    "--understand-iterations",
    type=LIMIT_INT,
    help="Iterations for the feature understanding phase",
)
@click.option(
    "--understand-prompt",
    "-p",
    type=str,
    help="Prompt file path for feature understanding",
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
    "--implementation-allowed-paths",
    help="Comma-separated allowed paths for implementation/repairs",
)
@click.option(
    "--implementation-auto-run",
    is_flag=True,
    help="Skip review gate and start implementation automatically",
)
@click.option(
    "--repair-max-runs",
    type=LIMIT_INT,
    default=5,
    help="Maximum auto repair runs after a failed implementation",
)
@click.option(
    "--repair-iterations",
    type=LIMIT_INT,
    default=5,
    help="Iterations per repair run",
)
@click.option(
    "--repair-agent-cmd",
    help="Custom agent command for repair runs (prompt piped to stdin)",
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
    help="Skip the advisory verification reports (raw loops, no post-checks)",
)
def feature(
    root: Path | None,
    prd: str | None,
    understand_iterations: int | None,
    understand_prompt: str | None,
    agent_cmd: str | None,
    repair_agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    sleep: float,
    interactive: bool,
    branch: str | None,
    implementation_allowed_paths: str | None,
    implementation_auto_run: bool,
    repair_max_runs: int,
    repair_iterations: int,
    ui: str,
    no_color: bool,
    ascii: bool,
    no_verify: bool,
) -> None:
    """Run feature understanding, then implementation.

    This mode:
    - Uses feature_understand_prompt.md for understanding by default
    - Only allows edits to the feature understand file during understanding
    - Uses the PRD branch by default
    - Starts implementation after review
    """
    ctx = click.get_current_context()
    env_prompt = os.environ.get("PROMPT_FILE")
    env_prd = os.environ.get("PRD_FILE")

    prompt_for_root = (
        Path(understand_prompt)
        if _use_cli_value(ctx, "understand_prompt") and understand_prompt is not None
        else None
    )
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)
    kstrl_dir = root_dir / "scripts" / "kstrl"

    base_config = KstrlConfig.load(root_dir)

    # Apply CLI overrides that should affect both phases.
    if _use_cli_value(ctx, "sleep"):
        base_config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        base_config.interactive = interactive
    if _use_cli_value(ctx, "agent_cmd"):
        base_config.agent_cmd = agent_cmd
    if _use_cli_value(ctx, "model"):
        base_config.model = model
    if _use_cli_value(ctx, "reasoning"):
        base_config.model_reasoning_effort = reasoning
    if _use_cli_value(ctx, "ui"):
        base_config.ui_mode = _normalize_ui_mode(ui)
    if _use_cli_value(ctx, "no_color"):
        base_config.no_color = no_color
    if _use_cli_value(ctx, "ascii"):
        base_config.ascii_only = ascii

    base_config.ui_mode = _normalize_ui_mode(base_config.ui_mode)

    # Check codex availability
    force_rich = os.environ.get("KSTRL_UI_FORCE_RICH") == "1"
    ui_impl = _console_ui(
        base_config.ui_mode,
        base_config.no_color,
        base_config.ascii_only,
        force_rich=force_rich,
    )

    codebase_map = kstrl_dir / "codebase_map.md"
    if not codebase_map.exists():
        ui_impl.err(f"codebase_map.md not found: {codebase_map}")
        ui_impl.info("Run `ks init` or `ks understand` first.")
        sys.exit(2)

    if _use_cli_value(ctx, "understand_iterations"):
        if understand_iterations is None or understand_iterations < 0:
            ui_impl.err(
                f"UNDERSTAND_ITERATIONS must be non-negative (got: {understand_iterations})"
            )
            sys.exit(2)
        understand_iterations_value = understand_iterations
    else:
        if base_config.max_iterations < 0:
            ui_impl.err(
                f"UNDERSTAND_ITERATIONS must be non-negative (got: {base_config.max_iterations})"
            )
            sys.exit(2)
        understand_iterations_value = base_config.max_iterations

    if repair_max_runs < 0:
        ui_impl.err(f"REPAIR_MAX_RUNS must be non-negative (got: {repair_max_runs})")
        sys.exit(2)

    if repair_iterations < 0:
        ui_impl.err(f"REPAIR_ITERATIONS must be non-negative (got: {repair_iterations})")
        sys.exit(2)

    if _use_cli_value(ctx, "prd"):
        prd_path = _resolve_path(root_dir, prd, kstrl_dir / "prd.json")
    elif env_prd is not None:
        prd_path = _resolve_path(root_dir, env_prd, kstrl_dir / "prd.json")
    else:
        prd_path = None
    if prd_path is None:
        ui_impl.err("Feature PRD is required. Use --prd or PRD_FILE.")
        sys.exit(2)

    if not prd_path.exists():
        ui_impl.err(f"Feature PRD not found: {prd_path}")
        sys.exit(2)

    try:
        prd_doc = PRD.load(prd_path)
    except Exception as exc:
        ui_impl.err(f"Invalid PRD: {exc}")
        sys.exit(2)

    feature_name = _derive_feature_name(prd_path, root_dir)
    if not feature_name:
        ui_impl.err("Unable to determine feature name from PRD path.")
        sys.exit(2)

    feature_dir = kstrl_dir / "feature" / feature_name
    feature_dir.mkdir(parents=True, exist_ok=True)
    feature_understand = feature_dir / "understand.md"
    if not feature_understand.exists():
        feature_understand.write_text(DEFAULT_FEATURE_UNDERSTAND)

    log_dir = root_dir / ".kstrl" / "logs" / f"feature_{feature_name}"

    # R2.4 preflight: accept whichever agent the resolved config selects.
    _check_agent_preflight(base_config, ui_impl)

    # #700: the engineer runs the confirmed [stack]'s checks in its sandbox.
    sandbox_cfg = with_stack_writable(
        SandboxConfig.load(root_dir), root_dir, stack_in_force(root_dir)
    )
    agent = get_agent(
        base_config.agent_cmd,
        base_config.model,
        base_config.model_reasoning_effort,
        base_config.agent_type,
        sandbox=sandbox_cfg,
        max_budget_usd=base_config.agent_budget_usd,
        root_dir=root_dir,
    )

    # ``understand_prompt_file`` is the OVERRIDE feature_cmd applies, so
    # None means "leave the understand loop on the resolved config".
    # ``understand_reads`` is the file that loop will actually open,
    # which is what the #286 preflight has to be pointed at; they differ
    # in exactly the PROMPT_FILE case.
    if _use_cli_value(ctx, "understand_prompt"):
        understand_prompt_file: Path | None = _resolve_path(
            root_dir, understand_prompt, kstrl_dir / "feature_understand_prompt.md"
        )
        understand_reads = understand_prompt_file
    elif "PROMPT_FILE" not in os.environ:
        understand_prompt_file = kstrl_dir / "feature_understand_prompt.md"
        understand_reads = understand_prompt_file
    else:
        understand_prompt_file = None
        understand_reads = base_config.prompt_file

    # #286: the two prompts this command actually opens, and only those.
    #
    # The engineer prompt is the scaffolded file rather than
    # ``base_config.prompt_file``: `ks feature` has always run its
    # implement and repair loops on `scripts/kstrl/prompt.md`, ignoring
    # [paths] prompt and PROMPT_FILE. Warning about the resolved path
    # would name a file the command never opens and stay silent about
    # the stale one it does. That was a literal in feature_cmd matching
    # a literal here; it is now passed on FeatureParams below, so the
    # path warned about and the path read cannot drift apart.
    #
    # ``understand_reads`` is resolved above: checking the engineer path
    # alone lost the PROMPT_FILE case, where the understand loop stays on
    # the resolved config because feature_cmd only overrides it for a
    # non-None ``understand_prompt_file``.
    engineer_prompt_file = kstrl_dir / "prompt.md"
    _check_prompt_preflight(engineer_prompt_file, ui_impl)
    _check_prompt_preflight(understand_reads, ui_impl)

    params = FeatureParams(
        prd_path=prd_path,
        prd_doc=prd_doc,
        feature_name=feature_name,
        feature_dir=feature_dir,
        feature_understand=feature_understand,
        log_dir=log_dir,
        understand_iterations=understand_iterations_value,
        understand_prompt_file=understand_prompt_file,
        prompt_file=engineer_prompt_file,
        implementation_auto_run=implementation_auto_run,
        no_verify=no_verify,
        repair_max_runs=repair_max_runs,
        repair_iterations=repair_iterations,
        repair_agent_cmd=repair_agent_cmd,
        branch_override=branch if _use_cli_value(ctx, "branch") else None,
        allowed_paths_override=(
            parse_paths(implementation_allowed_paths)
            if _use_cli_value(ctx, "implementation_allowed_paths")
            else None
        ),
        sandbox=sandbox_cfg,
    )

    command_run = open_command_run(
        ui_impl,
        root_dir,
        "feature",
        component=feature_name,
    )
    try:
        code = run_feature(
            params,
            base_config,
            agent,
            ui_impl,
            root_dir,
            run=command_run,
        )
    finally:
        command_run.close()
    sys.exit(code)
