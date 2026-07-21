"""CLI entry point for Ralph."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import envcompat

if TYPE_CHECKING:
    from kstrl.evolution import EvolutionConfig
    from kstrl.interaction import InteractionChannel

import click
from click.core import ParameterSource

from kstrl import __version__
from kstrl.agents import (
    ClaudeCodeAgent,
    ClaudeSdkAgent,
    CodexAgent,
    get_agent,
)
from kstrl.agents.base import Agent
from kstrl.agents.logging import LoggingAgent
from kstrl.breaker import BreakerConfig
from kstrl.commandrun import CommandRun, open_command_run
from kstrl.config import KstrlConfig, _parse_paths, resolve_config_file
from kstrl.config_report import build_config_report
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.decompose import SpecBlockerError, decompose_spec
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
from kstrl.factory import FactoryConfig, run_factory
from kstrl.feature_cmd import FeatureParams, run_feature
from kstrl.init_cmd import DEFAULT_FEATURE_UNDERSTAND, run_init
from kstrl.interaction import (
    PromptKind,
    PromptRequest,
    UiInteractionChannel,
)
from kstrl.launch import assemble_factory_configs
from kstrl.loop import run_loop
from kstrl.manifest import Manifest
from kstrl.observability import (
    event_age_seconds,
    format_age,
    latest_run_id,
    read_progress_events,
)
from kstrl.output import build_console
from kstrl.prd import PRD
from kstrl.proposals import append_to_agent_learnings as _append_to_agent_learnings
from kstrl.proposals import existing_proposal_titles as _existing_proposal_titles
from kstrl.proposals import mark_applied, parse_proposal_file
from kstrl.reducer import ComponentState, RunState, fold, load_run_state, upconvert_v1
from kstrl.retry_plan import RetryError, prepare_retry
from kstrl.sandbox import SandboxConfig
from kstrl.shutdown import StopController, install_signal_handlers
from kstrl.timeout import TimeoutConfig
from kstrl.ui.base import UI


def _console_ui(
    mode: str = "auto",
    no_color: bool = False,
    ascii_only: bool = False,
    force_rich: bool = False,
) -> UI:
    """Event-native drop-in for get_ui() (TUI rewrite chunk 7).

    Same signature and mode resolution; returns the console's
    EventBridgeUI so every line the command narrates becomes a typed
    Log event, rendered synchronously and byte-identically onto the
    same concrete UI get_ui() would have picked. run_factory discovers
    the bus via ``ui.bus`` to attach the run's file sinks.
    """
    return build_console(
        mode, no_color=no_color, ascii_only=ascii_only, force_rich=force_rich,
    ).ui


def _use_cli_value(ctx: click.Context, name: str) -> bool:
    return ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE


# Accepted spellings for the agent type across the config surface.
# ralph.toml documents "claude" | "codex" | "custom"; the --agent-type
# flags and RALPH_AGENT_TYPE historically use "claude-code" | "codex" |
# "auto". Both families resolve to get_agent's vocabulary here.
_AGENT_TYPE_ALIASES: dict[str, str] = {
    "": "auto",
    "auto": "auto",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claude-sdk": "claude-sdk",
    "codex": "codex",
    "custom": "custom",
}


def _agent_preflight(
    agent_cmd: str | None, agent_type: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Check that the agent the resolved config selects is reachable.

    Mirrors the factory/decompose preflight (R2.4, H-12): the check must
    look for whichever agent the config actually selects, not
    hardcode codex. Returns ``(canonical_agent_type, error, hint)``:
    ``canonical_agent_type`` is the get_agent-vocabulary spelling to
    construct the agent with (so a toml ``type = "claude"`` selects
    Claude Code rather than falling through to the codex default), and
    ``error``/``hint`` are user-facing lines when the preflight fails.
    """
    if agent_cmd:
        # Custom command takes precedence in get_agent regardless of
        # type; there is nothing to look up in PATH.
        return agent_type, None, None

    normalized = (agent_type or "auto").strip().lower()
    canonical = _AGENT_TYPE_ALIASES.get(normalized)

    if canonical is None:
        return (
            agent_type,
            f"Unknown agent type {agent_type!r} "
            "(expected: claude, claude-sdk, codex, custom, or auto)",
            "Fix [agent].type in ralph.toml, RALPH_AGENT_TYPE, or --agent-type",
        )
    if canonical == "custom":
        return (
            agent_type,
            'Agent type "custom" is configured but no agent command is set',
            "Set [agent].command in ralph.toml, AGENT_CMD, or --agent-cmd",
        )
    if canonical == "claude-code":
        if not ClaudeCodeAgent.is_available():
            return (
                agent_type,
                "claude not found in PATH (config selects agent type 'claude')",
                "Install Claude Code, or use --agent-cmd / change [agent].type",
            )
        return "claude-code", None, None
    if canonical == "claude-sdk":
        if not ClaudeSdkAgent.is_available():
            return (
                agent_type,
                "claude-agent-sdk is not installed "
                "(config selects agent type 'claude-sdk')",
                "Install the sdk extra (uv sync --extra sdk), "
                "or change [agent].type",
            )
        return "claude-sdk", None, None
    if canonical == "codex":
        if not CodexAgent.is_available():
            return (
                agent_type,
                "codex not found in PATH (config selects agent type 'codex')",
                "Install codex, or use --agent-cmd / change [agent].type",
            )
        return "codex", None, None

    # auto: accept whichever agent is installed, like the factory does.
    if not ClaudeCodeAgent.is_available() and not CodexAgent.is_available():
        return (
            agent_type,
            "No agent available (codex and claude not found in PATH)",
            "Install an agent or use --agent-cmd to specify a custom one",
        )
    return "auto", None, None


def _check_agent_preflight(config: KstrlConfig, ui_impl: UI) -> None:
    """Run the agent preflight against a resolved config; exit(1) on failure.

    On success, canonicalizes ``config.agent_type`` in place so every
    downstream ``get_agent`` call selects the same agent the preflight
    verified.
    """
    canonical, error, hint = _agent_preflight(config.agent_cmd, config.agent_type)
    if error is not None:
        ui_impl.err(error)
        if hint is not None:
            ui_impl.info(hint)
        sys.exit(1)
    config.agent_type = canonical


def _check_prd_preflight(prd_file: Path, ui_impl: UI) -> None:
    """Validate prd.json existence + schema BEFORE any agent spend (R2.4).

    Without this, the agent burns full iterations against a prompt
    referencing a nonexistent PRD before Phase 1 reports "Failed to load
    PRD". Error style mirrors ``ralph init``'s per-field messages.
    """
    if not prd_file.exists():
        ui_impl.err(f"PRD file not found: {prd_file}")
        ui_impl.info(
            "Run `ralph init` to scaffold scripts/kstrl/prd.json, "
            "or point --prd / PRD_FILE at an existing PRD."
        )
        sys.exit(1)

    try:
        with open(prd_file) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        ui_impl.err(f"Invalid JSON in {prd_file}: {exc}")
        sys.exit(1)
    except OSError as exc:
        ui_impl.err(f"Cannot read PRD file {prd_file}: {exc}")
        sys.exit(1)

    errors = PRD.validate_schema(data)
    if errors:
        ui_impl.err(f"PRD schema validation failed for {prd_file}:")
        for error in errors:
            ui_impl.info(f"  - {error}")
        sys.exit(1)


def _apply_cli_overrides(
    ctx: click.Context,
    config: KstrlConfig,
    root_dir: Path,
    prompt_default: Path,
    prd_default: Path,
) -> set[str]:
    """Overlay explicitly-passed CLI flags onto a loaded KstrlConfig.

    Shared by ``run`` and ``config show`` so what the observability
    command prints is exactly what the run command executes. Only flags
    the invoking command declares are considered (``ctx.params``), and
    only when the user actually passed them. Returns the KstrlConfig
    field names a flag overrode, for per-value source reporting.
    """
    def passed(name: str) -> bool:
        return name in ctx.params and _use_cli_value(ctx, name)

    overridden: set[str] = set()
    if passed("max_iterations"):
        config.max_iterations = ctx.params["max_iterations"]
        overridden.add("max_iterations")
    if passed("prompt"):
        config.prompt_file = _resolve_path(
            root_dir, ctx.params["prompt"], prompt_default
        )
        overridden.add("prompt_file")
    if passed("prd"):
        config.prd_file = _resolve_path(root_dir, ctx.params["prd"], prd_default)
        overridden.add("prd_file")
    if passed("sleep"):
        config.sleep_seconds = ctx.params["sleep"]
        overridden.add("sleep_seconds")
    if passed("interactive"):
        config.interactive = ctx.params["interactive"]
        overridden.add("interactive")
    if passed("allowed_paths"):
        config.allowed_paths = _parse_paths(ctx.params["allowed_paths"])
        overridden.add("allowed_paths")
    if passed("branch"):
        config.kstrl_branch = ctx.params["branch"]
        config.kstrl_branch_explicit = True
        overridden.add("kstrl_branch")
    if passed("agent_cmd"):
        config.agent_cmd = ctx.params["agent_cmd"]
        overridden.add("agent_cmd")
    if passed("model"):
        config.model = ctx.params["model"]
        overridden.add("model")
    if passed("reasoning"):
        config.model_reasoning_effort = ctx.params["reasoning"]
        overridden.add("model_reasoning_effort")
    if passed("agent_type"):
        config.agent_type = ctx.params["agent_type"]
        overridden.add("agent_type")
    if passed("ui"):
        config.ui_mode = _normalize_ui_mode(ctx.params["ui"])
        overridden.add("ui_mode")
    if passed("no_color"):
        config.no_color = ctx.params["no_color"]
        overridden.add("no_color")
    if passed("ascii"):
        config.ascii_only = ctx.params["ascii"]
        overridden.add("ascii_only")
    return overridden


def _collect_toml_notes(
    notes: list[str],
    section: str,
    loaded: Any,
    baseline: Any,
    flag_overridden: set[str],
) -> None:
    """Record which effective values ralph.toml moved off the CLI default.

    Before R2.1 six ralph.toml sections were silently ignored by the
    factory command, so a value that now takes effect is a behavior
    change for existing setups; the collected NOTE lines make that
    visible at startup. Comparing the loaded config against an env-only
    baseline isolates the toml contribution (env overlays are applied
    identically on both sides, so they cancel out). Fields whose CLI
    flag was explicitly passed are excluded: the flag wins, so the toml
    value is not effective.
    """
    for f in dataclass_fields(loaded):
        if f.name in flag_overridden:
            continue
        loaded_val = getattr(loaded, f.name)
        baseline_val = getattr(baseline, f.name)
        if loaded_val != baseline_val:
            notes.append(
                f"NOTE: [{section}] {f.name} = {loaded_val!r} from "
                f"ralph.toml (built-in default: {baseline_val!r}; "
                f"this section was ignored before R2.1)"
            )


def _resolve_root(root: Path | None, prompt: Path | None, prd: Path | None) -> Path:
    if root is not None:
        return root.resolve()

    for candidate in (prompt, prd):
        if candidate is None:
            continue
        resolved = candidate.resolve()
        parent = resolved.parent
        if parent.name == "kstrl" and parent.parent.name == "scripts":
            return parent.parent.parent

    return Path.cwd()


def _resolve_path(root: Path, value: str | None, default: Path) -> Path:
    if value is None or value == "":
        return default
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _derive_feature_name(prd_path: Path, root: Path) -> str:
    try:
        rel = prd_path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = None

    if rel is not None and len(rel.parts) >= 4:
        if rel.parts[0] == "scripts" and rel.parts[1] == "kstrl" and rel.parts[2] == "feature":
            return rel.parts[3]

    return prd_path.stem


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@click.group(invoke_without_command=True)
@click.version_option(version=__version__)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """kstrl - Agentic loop harness for AI-driven development."""
    if ctx.invoked_subcommand is not None:
        return
    # Bare `ks` on a TTY opens the home shell (D1 user decision);
    # everywhere else stays byte-identical to click's no-args behavior
    # (help on stdout, exit 2) - the pipe/CI contract.
    if (
        sys.stdout.isatty()
        and sys.stdin.isatty()
        and envcompat.get("KSTRL_NO_TUI") != "1"
    ):
        from kstrl.tui.home import run_home_shell

        ctx.exit(run_home_shell(Path.cwd()))
    click.echo(ctx.get_help())
    ctx.exit(2)


@cli.command()
@click.argument("max_iterations", type=int, default=10)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prompt", "-p",
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
    "--model", "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--sleep", "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive", "-i",
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
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
    help="Proceed even if another ralph invocation holds "
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

    prompt_for_root = (
        Path(prompt) if _use_cli_value(ctx, "prompt") and prompt is not None else None
    )
    if prompt_for_root is None and env_prompt is not None:
        prompt_for_root = Path(env_prompt)

    prd_for_root = Path(prd) if _use_cli_value(ctx, "prd") and prd is not None else None
    if prd_for_root is None and env_prd is not None:
        prd_for_root = Path(env_prd)

    root_value = root if _use_cli_value(ctx, "root") else None
    root_dir = _resolve_root(root_value, prompt_for_root, prd_for_root)

    # Build config from ralph.toml + environment defaults first.
    config = KstrlConfig.load(root_dir)

    # Apply CLI overrides when explicitly provided.
    _apply_cli_overrides(
        ctx, config, root_dir,
        prompt_default=root_dir / "scripts/kstrl/prompt.md",
        prd_default=root_dir / "scripts/kstrl/prd.json",
    )

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(
        config.ui_mode,
        config.no_color,
        config.ascii_only,
        force_rich=force_rich,
    )

    if config.max_iterations < 0:
        ui_impl.err(
            f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})"
        )
        sys.exit(2)

    # R2.4 preflight: verify the agent the config selects is reachable,
    # then validate the PRD - both BEFORE any agent invocation.
    _check_agent_preflight(config, ui_impl)
    _check_prd_preflight(config.prd_file, ui_impl)

    # Single-component factory invocation
    from kstrl.config import load_toml_section
    from kstrl.feedforward import FeedforwardConfig
    from kstrl.manifest import Manifest
    from kstrl.security import SecurityConfig
    from kstrl.verify import VerifyConfig

    # Determine branch from config or PRD. The preflight above already
    # validated existence + schema, so a load failure here is a real bug
    # worth surfacing, not something to swallow.
    prd_branch = PRD.load(config.prd_file).branch_name

    effective_branch = config.kstrl_branch or prd_branch or "kstrl/run"

    # Detect base branch from git
    detected_base = "main"
    try:
        import subprocess as _sp
        head_ref = _sp.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=root_dir, capture_output=True, text=True, timeout=5,
        )
        if head_ref.returncode == 0:
            # "refs/remotes/origin/main" -> "main"
            detected_base = head_ref.stdout.strip().rsplit("/", 1)[-1]
    except Exception:
        pass

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
    # resolve through the loaders (ralph.toml overlaid with env); the
    # structural fields below are forced because `ralph run` is by
    # definition a local, single-component, no-PR invocation.
    # R2.3: --no-verify sets the explicit skip sentinel; passing
    # verify_config=None meant "use defaults" in run_factory and Phase 1
    # ran anyway. Feedforward is independent of --no-verify (it builds
    # context, not checks).
    factory_cfg = FactoryConfig.load(root_dir)
    factory_cfg.max_parallel = 1
    factory_cfg.use_worktrees = False
    factory_cfg.single_pr = False
    factory_cfg.create_prs = False
    factory_cfg.verify_config = None if no_verify else VerifyConfig.load(root_dir)
    factory_cfg.skip_verification = no_verify
    factory_cfg.security_config = SecurityConfig.load(root_dir)
    factory_cfg.contract_config = None
    factory_cfg.feedforward_config = FeedforwardConfig.load(root_dir)
    factory_cfg.timeout_config = TimeoutConfig.load(root_dir)
    factory_cfg.force_lock = force_lock
    # `ralph run` reviews in advisory mode unless the project's
    # ralph.toml explicitly opts into a different review_mode (there is
    # no review_mode env var, so the toml section check is exhaustive).
    if "review_mode" not in load_toml_section(resolve_config_file(root_dir), "factory"):
        factory_cfg.review_mode = "advisory"

    # R0.5 (H-15): `ralph run` persists to its own run-manifest.json so
    # it can never clobber a factory run's resumable manifest.json.
    stop = StopController()
    uninstall = install_signal_handlers(stop)
    try:
        factory_result = run_factory(
            manifest, factory_cfg, config, ui_impl, root_dir,
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
    type=click.Choice(["auto", "rich", "plain", "gum"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
def init(directory: Path, ui: str, no_color: bool) -> None:
    """Initialize Ralph in a project directory.

    DIRECTORY is the target project directory (default: current directory).
    """
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)
    exit_code = run_init(directory, ui_impl)
    sys.exit(exit_code)


@cli.command()
@click.argument("max_iterations", type=int, default=10)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--prompt", "-p",
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
    "--model", "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--sleep", "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive", "-i",
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
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
    "--tui/--no-tui",
    "tui",
    default=None,
    help="Embedded dashboard (default: auto - on when stdin/stdout are "
         "TTYs and --ui is not plain; KSTRL_NO_TUI=1 forces off)",
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
    tui: bool | None,
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

    prompt_for_root = (
        Path(prompt) if _use_cli_value(ctx, "prompt") and prompt is not None else None
    )
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
        from kstrl.init_cmd import DEFAULT_CODEBASE_MAP
        codebase_map.parent.mkdir(parents=True, exist_ok=True)
        codebase_map.write_text(DEFAULT_CODEBASE_MAP)

    config = KstrlConfig.load(root_dir)

    # Apply CLI overrides when explicitly provided.
    if _use_cli_value(ctx, "max_iterations"):
        config.max_iterations = max_iterations
    if _use_cli_value(ctx, "prompt"):
        config.prompt_file = _resolve_path(
            root_dir, prompt, kstrl_dir / "understand_prompt.md"
        )
    if _use_cli_value(ctx, "prd"):
        config.prd_file = _resolve_path(
            root_dir, prd, kstrl_dir / "prd.json"
        )
    if _use_cli_value(ctx, "sleep"):
        config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        config.interactive = interactive
    if _use_cli_value(ctx, "allowed_paths"):
        config.allowed_paths = _parse_paths(allowed_paths)
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
        and not envcompat.contains("KSTRL_BRANCH")
        and not config.kstrl_branch_explicit
    ):
        config.kstrl_branch = "kstrl/understanding"
        config.kstrl_branch_explicit = False

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(
        config.ui_mode,
        config.no_color,
        config.ascii_only,
        force_rich=force_rich,
    )

    if config.max_iterations < 0:
        ui_impl.err(
            f"MAX_ITERATIONS must be non-negative (got: {config.max_iterations})"
        )
        sys.exit(2)

    # R2.4 preflight: accept whichever agent the resolved config selects.
    _check_agent_preflight(config, ui_impl)

    sandbox_cfg = SandboxConfig.load(root_dir)
    if sandbox_cfg.enabled and config.agent_cmd:
        ui_impl.warn(
            "[sandbox] enabled but the agent is a custom command; sandbox "
            "settings cannot be applied to it and are ignored"
        )
    agent = get_agent(
        config.agent_cmd, config.model, config.model_reasoning_effort,
        config.agent_type, sandbox=sandbox_cfg,
        max_budget_usd=config.agent_budget_usd,
    )

    use_tui = tui if tui is not None else (
        sys.stdout.isatty()
        and sys.stdin.isatty()
        and envcompat.get("KSTRL_NO_TUI") != "1"
        and config.ui_mode != "plain"
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
        from kstrl.tui.embed import EmbeddedContext, run_embedded
        from kstrl.tui.screens.component import ComponentScreen
        from kstrl.tui.screens.overview import OverviewScreen

        def _target(embed_ctx: EmbeddedContext) -> int:
            command_run = open_command_run(
                embed_ctx.ui, root_dir, "understand",
                component="understand", run_id=embed_ctx.run_id,
            )
            try:
                return _understand_core(
                    config, agent, root_dir, embed_ctx.ui,
                    run=command_run,
                    interaction=embed_ctx.channel,
                    stop_check=embed_ctx.stop.is_set,
                )
            finally:
                command_run.close()

        sys.exit(run_embedded(
            _target, root_dir=root_dir,
            run_id=mint_run_id("understand"),
            screen_factory=lambda: [
                OverviewScreen(observe_only=False),
                ComponentScreen("understand"),
            ],
        ))

    command_run = open_command_run(
        ui_impl, root_dir, "understand", component="understand",
    )
    try:
        code = _understand_core(
            config, agent, root_dir, ui_impl, run=command_run,
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
    interaction: InteractionChannel | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> int:
    """The understand loop as an event-stream run (TUI surface C1).

    The reducer projects the work onto the pseudo-component
    "understand": one plan row, one phase, the loop's iterations. When
    recording, the agent is wrapped so its transcript lands where the
    dashboard's transcript pane tails.
    """
    bus = run.bus
    component = "understand"
    loop_agent = agent
    transcript = run.transcript_path(component)
    if transcript is not None:
        loop_agent = LoggingAgent(agent, transcript)

    started = time.monotonic()
    bus.emit(RunStarted(project=root_dir.name, components=1))
    bus.emit(RunPlan(components=(
        {"id": component, "title": "Codebase understanding", "deps": []},
    )))
    bus.emit(ComponentStarted(component=component))
    bus.emit(PhaseStarted(component=component, phase="understand", attempt=1))

    try:
        result = run_loop(
            config, ui_impl, loop_agent, root_dir,
            timeouts=TimeoutConfig.load(root_dir),
            breaker_config=BreakerConfig.load(root_dir),
            bus=bus,
            interaction=interaction,
            stop_check=stop_check,
        )
    except Exception as exc:
        duration = round(time.monotonic() - started, 2)
        detail = f"{type(exc).__name__}: {exc}"
        bus.emit(PhaseCompleted(
            component=component, phase="understand", passed=False,
            detail=detail, duration_seconds=duration,
        ))
        bus.emit(ComponentFailed(component=component, error=detail))
        bus.emit(RunCompleted(
            completed=0, failed=1, duration_seconds=duration,
        ))
        raise

    duration = round(time.monotonic() - started, 2)
    passed = result.completed and result.exit_code == 0
    failure_detail = (
        f"exit {result.exit_code}"
        if result.exit_code != 0
        else "ended before completion"
    )
    bus.emit(PhaseCompleted(
        component=component, phase="understand", passed=passed,
        detail="" if passed else failure_detail,
        duration_seconds=duration,
    ))
    if passed:
        map_path = config.codebase_map_file
        try:
            map_display = str(map_path.relative_to(root_dir))
        except ValueError:
            map_display = str(map_path)
        bus.emit(ArtifactWritten(label="codebase_map", path=map_display))
        bus.emit(ComponentCompleted(
            component=component, duration_seconds=duration,
            iterations=result.iterations,
        ))
    else:
        bus.emit(ComponentFailed(
            component=component,
            error=f"understand loop {failure_detail}",
        ))
    bus.emit(RunCompleted(
        completed=1 if passed else 0,
        failed=0 if passed else 1,
        duration_seconds=duration,
    ))
    return result.exit_code


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
    type=int,
    help="Iterations for the feature understanding phase",
)
@click.option(
    "--understand-prompt", "-p",
    type=str,
    help="Prompt file path for feature understanding",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model", "-m",
    help="Model for codex agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--sleep", "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--interactive", "-i",
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
    type=int,
    default=5,
    help="Maximum auto repair runs after a failed implementation",
)
@click.option(
    "--repair-iterations",
    type=int,
    default=5,
    help="Iterations per repair run",
)
@click.option(
    "--repair-agent-cmd",
    help="Custom agent command for repair runs (prompt piped to stdin)",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
    "--tui/--no-tui",
    "tui",
    default=None,
    help="Embedded dashboard (default: auto - on when stdin/stdout are "
         "TTYs and --ui is not plain; KSTRL_NO_TUI=1 forces off)",
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
    tui: bool | None,
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
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(
        base_config.ui_mode,
        base_config.no_color,
        base_config.ascii_only,
        force_rich=force_rich,
    )

    codebase_map = kstrl_dir / "codebase_map.md"
    if not codebase_map.exists():
        ui_impl.err(f"codebase_map.md not found: {codebase_map}")
        ui_impl.info("Run `ralph init` or `ralph understand` first.")
        sys.exit(1)

    if _use_cli_value(ctx, "understand_iterations"):
        if understand_iterations is None or understand_iterations < 0:
            ui_impl.err(
                "UNDERSTAND_ITERATIONS must be non-negative "
                f"(got: {understand_iterations})"
            )
            sys.exit(2)
        understand_iterations_value = understand_iterations
    else:
        if base_config.max_iterations < 0:
            ui_impl.err(
                "UNDERSTAND_ITERATIONS must be non-negative "
                f"(got: {base_config.max_iterations})"
            )
            sys.exit(2)
        understand_iterations_value = base_config.max_iterations

    if repair_max_runs < 0:
        ui_impl.err(
            f"REPAIR_MAX_RUNS must be non-negative (got: {repair_max_runs})"
        )
        sys.exit(2)

    if repair_iterations < 0:
        ui_impl.err(
            f"REPAIR_ITERATIONS must be non-negative (got: {repair_iterations})"
        )
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
        sys.exit(1)

    try:
        prd_doc = PRD.load(prd_path)
    except Exception as exc:
        ui_impl.err(f"Invalid PRD: {exc}")
        sys.exit(1)

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

    sandbox_cfg = SandboxConfig.load(root_dir)
    if sandbox_cfg.enabled and base_config.agent_cmd:
        ui_impl.warn(
            "[sandbox] enabled but the agent is a custom command; sandbox "
            "settings cannot be applied to it and are ignored"
        )
    agent = get_agent(
        base_config.agent_cmd,
        base_config.model,
        base_config.model_reasoning_effort,
        base_config.agent_type,
        sandbox=sandbox_cfg,
        max_budget_usd=base_config.agent_budget_usd,
    )

    if _use_cli_value(ctx, "understand_prompt"):
        understand_prompt_file: Path | None = _resolve_path(
            root_dir, understand_prompt, kstrl_dir / "feature_understand_prompt.md"
        )
    elif "PROMPT_FILE" not in os.environ:
        understand_prompt_file = kstrl_dir / "feature_understand_prompt.md"
    else:
        understand_prompt_file = None

    params = FeatureParams(
        prd_path=prd_path,
        prd_doc=prd_doc,
        feature_name=feature_name,
        feature_dir=feature_dir,
        feature_understand=feature_understand,
        log_dir=log_dir,
        understand_iterations=understand_iterations_value,
        understand_prompt_file=understand_prompt_file,
        implementation_auto_run=implementation_auto_run,
        repair_max_runs=repair_max_runs,
        repair_iterations=repair_iterations,
        repair_agent_cmd=repair_agent_cmd,
        branch_override=branch if _use_cli_value(ctx, "branch") else None,
        allowed_paths_override=(
            _parse_paths(implementation_allowed_paths)
            if _use_cli_value(ctx, "implementation_allowed_paths")
            else None
        ),
        sandbox=sandbox_cfg,
    )

    use_tui = tui if tui is not None else (
        sys.stdout.isatty()
        and sys.stdin.isatty()
        and envcompat.get("KSTRL_NO_TUI") != "1"
        and base_config.ui_mode != "plain"
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
        from kstrl.tui.embed import EmbeddedContext, run_embedded
        from kstrl.tui.screens.component import ComponentScreen
        from kstrl.tui.screens.overview import OverviewScreen

        def _target(embed_ctx: EmbeddedContext) -> int:
            command_run = open_command_run(
                embed_ctx.ui, root_dir, "feature",
                component=feature_name, run_id=embed_ctx.run_id,
            )
            try:
                return run_feature(
                    params, base_config, agent, embed_ctx.ui, root_dir,
                    interaction=embed_ctx.channel,
                    run=command_run,
                    stop_check=embed_ctx.stop.is_set,
                )
            finally:
                command_run.close()

        sys.exit(run_embedded(
            _target, root_dir=root_dir,
            run_id=mint_run_id("feature"),
            screen_factory=lambda: [
                OverviewScreen(observe_only=False),
                ComponentScreen(feature_name),
            ],
        ))

    command_run = open_command_run(
        ui_impl, root_dir, "feature", component=feature_name,
    )
    try:
        code = run_feature(
            params, base_config, agent, ui_impl, root_dir,
            run=command_run,
        )
    finally:
        command_run.close()
    sys.exit(code)


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
    help="Name for this factory project",
)
@click.option(
    "--base-branch",
    default="main",
    help="Base git branch",
)
@click.option(
    "--single-pr",
    is_flag=True,
    help="Use a single branch for all components",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model", "-m",
    help="Model for the agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--agent-type",
    type=click.Choice(["auto", "claude-code", "claude-sdk", "codex"]),
    default="auto",
    help="Agent type",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
    base_branch: str,
    single_pr: bool,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str,
    ui: str,
    no_color: bool,
    tui: bool | None,
) -> None:
    """Decompose a spec into components and generate PRDs."""
    ctx = click.get_current_context()

    root_dir = root.resolve() if root else Path.cwd()

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    effective_cmd = agent_cmd or os.environ.get("AGENT_CMD")
    effective_model = model if _use_cli_value(ctx, "model") else os.environ.get("MODEL")
    effective_reasoning = (
        reasoning if _use_cli_value(ctx, "reasoning")
        else os.environ.get("MODEL_REASONING_EFFORT")
    )
    effective_type = (
        agent_type if _use_cli_value(ctx, "agent_type")
        else envcompat.get("KSTRL_AGENT_TYPE", "auto")
    )

    # R2.4 mirror (measured 2026-07-20): canonicalize aliases like
    # "claude" before get_agent, whose unrecognized-type fallthrough is
    # codex; the preflight also covers the no-agent-available check.
    canonical_type, type_error, type_hint = _agent_preflight(
        effective_cmd, effective_type,
    )
    if type_error:
        ui_impl.err(type_error)
        if type_hint:
            ui_impl.info(type_hint)
        sys.exit(1)
    effective_type = canonical_type or effective_type

    agent = get_agent(effective_cmd, effective_model, effective_reasoning, effective_type)

    def _decompose_core(core_ui: UI, command_run: CommandRun) -> int:
        try:
            manifest = decompose_spec(
                spec_path=spec,
                project_name=project_name,
                base_branch=base_branch,
                single_pr=single_pr,
                agent=agent,
                ui=core_ui,
                root_dir=root_dir,
                bus=command_run.bus,
                transcript=command_run.transcript_writer("architect"),
            )
            core_ui.ok(f"Decomposed into {len(manifest.components)} components")
            return 0
        except SpecBlockerError as exc:
            core_ui.err(str(exc))
            # R1.7: point at the durable artifact so the user iterates
            # against a file, not scrollback.
            if exc.artifact_path is not None:
                core_ui.info(f"Spec issues written to: {exc.artifact_path}")
            return 2
        except ValueError as exc:
            core_ui.err(str(exc))
            return 1

    use_tui = tui if tui is not None else (
        sys.stdout.isatty()
        and sys.stdin.isatty()
        and envcompat.get("KSTRL_NO_TUI") != "1"
        and _normalize_ui_mode(ui) != "plain"
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
                embed_ctx.ui, root_dir, "decompose",
                component="architect", run_id=embed_ctx.run_id,
            )
            try:
                return _decompose_core(embed_ctx.ui, command_run)
            finally:
                command_run.close()

        sys.exit(run_embedded(
            _target, root_dir=root_dir,
            run_id=mint_run_id("decompose"),
            screen_factory=initial_screens_for_kind(
                "decompose", observe_only=False,
            ),
        ))

    command_run = open_command_run(
        ui_impl, root_dir, "decompose", component="architect",
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
    help=(
        "Markdown spec file or SpecKit artifact directory "
        "(runs decompose first)"
    ),
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, path_type=Path),
    help="Existing manifest file (skip decompose)",
)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--project-name",
    help="Name for this factory project (required with --spec)",
)
@click.option(
    "--base-branch",
    default="main",
    help="Base git branch",
)
@click.option(
    "--single-pr",
    is_flag=True,
    help="Use a single branch/PR for all components",
)
@click.option(
    "--max-parallel",
    type=int,
    default=None,
    help="Maximum parallel components (default: 4)",
)
@click.option(
    "--max-retries",
    type=int,
    default=None,
    help="Maximum retries per component (default: 3)",
)
@click.option(
    "--create-prs/--no-prs",
    default=None,
    help="Create PRs for completed components (default: on)",
)
@click.option(
    "--verify-command",
    help="Legacy: single verify command (prefer --test-command etc.)",
)
@click.option(
    "--test-command",
    help="Test suite command (default: 'uv run pytest')",
)
@click.option(
    "--typecheck-command",
    help="Typecheck command (default: 'uv run mypy .')",
)
@click.option(
    "--lint-command",
    help="Lint command (default: 'uv run ruff check .')",
)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip Phase 1 mechanical verification",
)
@click.option(
    "--dead-code-cleanup",
    is_flag=True,
    default=None,
    help=(
        "Enable dead code cleanup: ruff auto-fixes unused "
        "imports/variables, vulture detects remaining dead code"
    ),
)
@click.option(
    "--dead-code-command",
    help="Custom dead code detection command (default: vulture on changed files)",
)
@click.option(
    "--mutation-testing",
    is_flag=True,
    default=None,
    help="Enable mutation testing (requires mutmut, off by default)",
)
@click.option(
    "--mutation-threshold",
    type=float,
    default=None,
    help="Mutation score threshold percent (default: 50)",
)
@click.option(
    "--review-mode",
    type=click.Choice(["hard", "advisory", "skip"]),
    default=None,
    help="Phase 2 review: hard (block), advisory (warn), skip "
         "(default: hard)",
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
    type=click.Choice(["hard", "advisory", "skip"]),
    default=None,
    help="Phase 2.5 security review: hard (block on critical+high), "
         "advisory (warn only), skip (default - opt in explicitly)",
)
@click.option(
    "--security-agent-cmd",
    help="Custom agent for security reviewer "
         "(default: same as implementation agent)",
)
@click.option(
    "--security-model",
    help="Model for security reviewer agent "
         "(default: same as implementation agent)",
)
@click.option(
    "--security-fail-threshold",
    type=click.Choice(["critical", "high", "medium", "low"]),
    default=None,
    help="In hard mode, findings at or above this severity block "
         "(default: high - critical+high fail)",
)
@click.option(
    "--contract-check",
    type=click.Choice(["tier", "final", "skip"]),
    default=None,
    help="Phase 3 contract testing: tier (per-tier), final (end-only), "
         "skip (default: tier)",
)
@click.option(
    "--contract-test-cmd",
    help="Test command for contract testing (default: same as --test-command)",
)
@click.option(
    "--agent-timeout",
    type=float,
    default=None,
    help="Timeout per agent iteration in seconds; 0 disables "
         "(default: 1800, or RALPH_TIMEOUT_AGENT_ITERATION / "
         "[timeout].agent_iteration in ralph.toml)",
)
@click.option(
    "--component-timeout",
    type=float,
    default=None,
    help="Timeout per component total in seconds; 0 disables "
         "(default: 7200, or RALPH_TIMEOUT_COMPONENT / "
         "[timeout].component_total in ralph.toml)",
)
@click.option(
    "--max-adversarial-calls",
    type=int,
    default=None,
    help="Hard cap on adversarial LLM calls (review + security + "
         "distill) per run; 0 = unbounded (default: 0, or "
         "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS / "
         "[factory].max_adversarial_calls in ralph.toml)",
)
@click.option(
    "--max-total-tokens",
    type=int,
    default=None,
    help="Run-level token budget across ALL phases (engineer + review "
         "+ security + distill); 0 = unbounded. On breach the current "
         "component fails with a synthetic budget finding and pending "
         "components halt (default: 0, or RALPH_FACTORY_MAX_TOTAL_TOKENS "
         "/ [factory].max_total_tokens in ralph.toml)",
)
@click.option(
    "--pause-before-pr-merge/--no-pause-before-pr-merge",
    default=None,
    help="Pause for human approval before each component's PR "
         "push+merge (default: off, or "
         "KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE / "
         "[factory].pause_before_pr_merge in ralph.toml)",
)
@click.option(
    "--progress-log",
    type=click.Path(path_type=Path),
    help="Path for the JSONL progress log (default: .kstrl/progress.jsonl; "
         "the log is on by default, disable via "
         "[factory].progress_log_enabled = false or "
         "KSTRL_FACTORY_PROGRESS_LOG_ENABLED=0)",
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
         "(default: off, or RALPH_FACTORY_KEEP_WORKTREES_ON_FAILURE / "
         "[factory].keep_worktrees_on_failure in ralph.toml)",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another ralph invocation holds "
         ".kstrl/factory.lock (may corrupt the other run's state)",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.option(
    "--agent-cmd",
    help="Custom agent command (prompt piped to stdin)",
)
@click.option(
    "--model", "-m",
    help="Model for the agent",
)
@click.option(
    "--reasoning",
    help="Reasoning effort (low, medium, high, max)",
)
@click.option(
    "--agent-type",
    type=click.Choice(["auto", "claude-code", "claude-sdk", "codex"]),
    default="auto",
    help="Agent type",
)
@click.option(
    "--sleep", "-s",
    type=float,
    default=2.0,
    help="Sleep seconds between iterations",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
    base_branch: str,
    single_pr: bool,
    max_parallel: int | None,
    max_retries: int | None,
    create_prs: bool | None,
    verify_command: str | None,
    test_command: str | None,
    typecheck_command: str | None,
    lint_command: str | None,
    no_verify: bool,
    dead_code_cleanup: bool | None,
    dead_code_command: str | None,
    mutation_testing: bool | None,
    mutation_threshold: float | None,
    review_mode: str | None,
    review_agent_cmd: str | None,
    review_model: str | None,
    security_mode: str | None,
    security_agent_cmd: str | None,
    security_model: str | None,
    security_fail_threshold: str | None,
    contract_check: str | None,
    contract_test_cmd: str | None,
    agent_timeout: float | None,
    component_timeout: float | None,
    max_adversarial_calls: int | None,
    max_total_tokens: int | None,
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

    effective_cmd = agent_cmd or os.environ.get("AGENT_CMD")
    effective_model = model if _use_cli_value(ctx, "model") else os.environ.get("MODEL")
    effective_reasoning = (
        reasoning if _use_cli_value(ctx, "reasoning")
        else os.environ.get("MODEL_REASONING_EFFORT")
    )
    effective_type = (
        agent_type if _use_cli_value(ctx, "agent_type")
        else envcompat.get("KSTRL_AGENT_TYPE", "auto")
    )

    # R2.4 mirror (measured 2026-07-20): canonicalize aliases like
    # "claude" before get_agent, whose unrecognized-type fallthrough is
    # codex; the preflight also covers the no-agent-available check.
    canonical_type, type_error, type_hint = _agent_preflight(
        effective_cmd, effective_type,
    )
    if type_error:
        ui_impl.err(type_error)
        if type_hint:
            ui_impl.info(type_hint)
        sys.exit(1)
    effective_type = canonical_type or effective_type

    agent = get_agent(effective_cmd, effective_model, effective_reasoning, effective_type)

    # Get or create manifest
    if manifest_path:
        try:
            manifest = Manifest.load(manifest_path)
        except Exception as exc:
            ui_impl.err(f"Failed to load manifest: {exc}")
            sys.exit(1)
    else:
        assert spec is not None
        if not project_name:
            ui_impl.err("--project-name is required with --spec")
            sys.exit(2)

        try:
            manifest = decompose_spec(
                spec_path=spec,
                project_name=project_name,
                base_branch=base_branch,
                single_pr=single_pr,
                agent=agent,
                ui=ui_impl,
                root_dir=root_dir,
            )
        except SpecBlockerError as exc:
            # Architect halted: spec has blocker-severity issues. Surface
            # them and exit cleanly. The user fixes the spec and re-runs,
            # iterating against the persisted artifact (R1.7).
            ui_impl.err(str(exc))
            if exc.artifact_path is not None:
                ui_impl.info(f"Spec issues written to: {exc.artifact_path}")
            sys.exit(2)
        except ValueError as exc:
            ui_impl.err(str(exc))
            sys.exit(1)

    # Build configs (R2.1). Resolution order for every phase config:
    # explicit CLI flag > env > ralph.toml > dataclass default. The
    # loaders handle env-over-toml-over-default; flags use None
    # sentinels so "not passed" is distinguishable from "passed the
    # default value", and an explicitly-passed flag is applied on top.
    from kstrl.contract import ContractConfig
    from kstrl.evolution import EvolutionConfig
    from kstrl.feedforward import FeedforwardConfig
    from kstrl.security import SecurityConfig
    from kstrl.verify import VerifyConfig

    toml_notes: list[str] = []

    factory_config = FactoryConfig.load(root_dir)
    _collect_toml_notes(
        toml_notes, "factory", factory_config, FactoryConfig.from_env(),
        flag_overridden={
            name
            for name, passed in (
                ("max_parallel", max_parallel is not None),
                ("max_retries", max_retries is not None),
                ("create_prs", create_prs is not None),
                ("review_mode", review_mode is not None),
                ("max_adversarial_calls", max_adversarial_calls is not None),
                ("max_total_tokens", max_total_tokens is not None),
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
    if pause_before_pr_merge is not None:
        factory_config.pause_before_pr_merge = pause_before_pr_merge
    if no_worktrees:
        factory_config.use_worktrees = False
    if keep_worktrees_on_failure:
        factory_config.keep_worktrees_on_failure = True
    factory_config.single_pr = manifest.single_pr
    factory_config.verify_command = verify_command
    factory_config.review_agent_cmd = review_agent_cmd
    factory_config.review_model = review_model
    factory_config.progress_log_path = progress_log
    if progress_log is not None:
        # An explicit --progress-log path is an explicit opt-in; it wins
        # over a toml/env progress_log_enabled = false.
        factory_config.progress_log_enabled = True
    factory_config.force_lock = force_lock
    # R2.3: --no-verify is an explicit skip sentinel that run_factory
    # honors; verify_config=None alone would substitute default checks.
    factory_config.skip_verification = no_verify

    v_config: VerifyConfig | None = None
    if not no_verify:
        v_config = VerifyConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes, "verify", v_config, VerifyConfig.from_env(),
            flag_overridden={
                name
                for name, passed in (
                    ("test_command", test_command is not None),
                    ("typecheck_command", typecheck_command is not None),
                    ("lint_command", lint_command is not None),
                    ("dead_code_cleanup", dead_code_cleanup is not None),
                    ("dead_code_command", dead_code_command is not None),
                    ("mutation_testing", mutation_testing is not None),
                    ("mutation_threshold", mutation_threshold is not None),
                )
                if passed
            },
        )
        if test_command is not None:
            v_config.test_command = test_command
        if typecheck_command is not None:
            v_config.typecheck_command = typecheck_command
        if lint_command is not None:
            v_config.lint_command = lint_command
        if dead_code_cleanup is not None:
            v_config.dead_code_cleanup = dead_code_cleanup
        if dead_code_command is not None:
            v_config.dead_code_command = dead_code_command
        if mutation_testing is not None:
            v_config.mutation_testing = mutation_testing
        if mutation_threshold is not None:
            v_config.mutation_threshold = mutation_threshold

    s_config = SecurityConfig.load(root_dir)
    _collect_toml_notes(
        toml_notes, "security", s_config, SecurityConfig.from_env(),
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

    # --test-command historically flowed through to contract testing
    # when --contract-test-cmd was absent; both are explicit CLI input,
    # so either beats env/toml.
    cli_contract_cmd = contract_test_cmd or test_command
    contract_resolved = ContractConfig.load(root_dir)
    _collect_toml_notes(
        toml_notes, "contract", contract_resolved, ContractConfig.from_env(),
        flag_overridden={
            name
            for name, passed in (
                ("mode", contract_check is not None),
                ("test_command", cli_contract_cmd is not None),
            )
            if passed
        },
    )
    if contract_check is not None:
        contract_resolved.mode = contract_check
    if cli_contract_cmd is not None:
        contract_resolved.test_command = cli_contract_cmd
    # mode == "skip" keeps the historical contract of passing no config.
    c_config: ContractConfig | None = (
        contract_resolved if contract_resolved.mode != "skip" else None
    )

    ff_config = FeedforwardConfig.load(root_dir)
    _collect_toml_notes(
        toml_notes, "feedforward", ff_config, FeedforwardConfig.from_env(),
        flag_overridden=set(),
    )

    # Evolution config is consumed inside run_factory via
    # EvolutionConfig.load(root_dir); loaded here only for the NOTE sweep.
    _collect_toml_notes(
        toml_notes, "evolution", EvolutionConfig.load(root_dir),
        EvolutionConfig.from_env(root_dir), flag_overridden=set(),
    )

    # R0.1: TimeoutConfig is the single source for timeout values.
    timeout_config = TimeoutConfig.load(root_dir)
    _collect_toml_notes(
        toml_notes, "timeout", timeout_config, TimeoutConfig.from_env(),
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
    factory_config.feedforward_config = ff_config
    factory_config.timeout_config = timeout_config

    # Display summary and confirm (resolved values, not raw flags)
    ui_impl.section("Factory Plan")
    ui_impl.kv("Project", manifest.project_name)
    ui_impl.kv("Components", str(len(manifest.components)))
    ui_impl.kv("Base branch", manifest.base_branch)
    ui_impl.kv("Single PR", "yes" if manifest.single_pr else "no")
    ui_impl.kv("Max parallel", str(factory_config.max_parallel))
    ui_impl.kv("Create PRs", "yes" if factory_config.create_prs else "no")

    # R2.1 behavior change: ralph.toml sections that used to be silently
    # ignored now take effect. Surface every value a toml section moved
    # away from the CLI default so existing setups see the change.
    for note in toml_notes:
        ui_impl.info(note)

    topo = manifest.topological_order()
    ui_impl.info("")
    ui_impl.info("Execution order:")
    for i, comp_id in enumerate(topo, 1):
        comp = manifest.get_component(comp_id)
        status = comp.status if comp else "?"
        dep_list = ", ".join(comp.dependencies) if comp and comp.dependencies else ""
        deps = f" (depends on: {dep_list})" if dep_list else ""
        ui_impl.info(f"  {i}. {comp_id} [{status}]{deps}")

    _factory_channel = UiInteractionChannel(ui_impl)
    if not yes and _factory_channel.can_prompt():
        response = _factory_channel.request(PromptRequest(
            kind=PromptKind.CONFIRM,
            header="Proceed with factory execution?",
            options=("Start", "Quit"),
            default=0,
        ))
        if response.answered and response.choice != 0:
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

    # Ensure prompt file exists
    if not base_config.prompt_file.exists():
        default_prompt = kstrl_dir / "prompt.md"
        if default_prompt.exists():
            base_config.prompt_file = default_prompt

    # R0.5 (H-15): state saves back to the file it was loaded from.
    # --manifest /custom.json persists to /custom.json; --spec runs keep
    # the default scripts/kstrl/manifest.json that decompose wrote.
    use_tui = tui if tui is not None else (
        sys.stdout.isatty()
        and sys.stdin.isatty()
        and envcompat.get("KSTRL_NO_TUI") != "1"
        and _normalize_ui_mode(ui) != "plain"
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

        sys.exit(run_factory_embedded(
            manifest, factory_config, base_config, root_dir,
            manifest_path,
        ))

    stop = StopController()
    uninstall = install_signal_handlers(stop)
    try:
        result = run_factory(
            manifest, factory_config, base_config, ui_impl, root_dir,
            manifest_path=manifest_path,
            stop=stop,
        )
    finally:
        uninstall()
    sys.exit(result.exit_code)


# Display structure for the KstrlConfig-backed ralph.toml sections:
# section -> [(toml_key, dataclass_field)]. Mirrors DEFAULT_KSTRL_TOML in
# init_cmd.py plus the env/flag-only UI knobs (ui_mode, no_color).
@cli.group(name="config")
def config_group() -> None:
    """Inspect Ralph configuration."""


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
@click.option("--agent-type", help="Override [agent] type")
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
    mirror `ralph run`'s config-affecting options, so the output is what
    a run invoked with the same flags would execute. Factory-phase
    sections (factory/verify/security/...) have no flags here; their
    values resolve from env > ralph.toml > defaults.

    Source detection for env is behavioral: a value is tagged (env) when
    removing the environment changes it. An env var that sets a value
    identical to the toml/default value is therefore reported as the
    lower-precedence source; the effective value is identical either way.
    """
    ctx = click.get_current_context()
    root_dir = root.resolve() if root else Path.cwd()

    def _overlay(config: KstrlConfig) -> set[str]:
        return _apply_cli_overrides(
            ctx, config, root_dir,
            prompt_default=root_dir / "scripts/kstrl/prompt.md",
            prd_default=root_dir / "scripts/kstrl/prd.json",
        )

    try:
        report = build_config_report(root_dir, overlay=_overlay)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    toml_path = report.toml_path
    click.echo(f"# Resolved kstrl config for {root_dir}")
    click.echo(f"# ralph.toml: {toml_path if report.toml_exists else '(absent)'}")
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

    sys.exit(0)


def _age_label(ts: str) -> str:
    """"5m ago" for an event timestamp, or "" when unparseable."""
    age = event_age_seconds(ts)
    if age is None:
        return ""
    return f"{format_age(age)} ago"


def _age_label_epoch(ts: float) -> str:
    """"5m ago" for a float epoch timestamp (reducer times), or ""."""
    if ts <= 0:
        return ""
    age = max(0.0, time.time() - ts)
    return f"{format_age(age)} ago"


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
    ui_impl.section("Ralph status")
    ui_impl.kv("Project", manifest.project_name)
    ui_impl.kv("Manifest", str(manifest_file))
    ui_impl.kv("Base branch", manifest.base_branch)

    if state is not None and source_path is not None:
        label = (
            "Events" if source_path.name == "events.jsonl" else "Progress log"
        )
        ui_impl.kv(label, str(source_path))
        if state.run_id:
            ui_impl.kv("Run id", state.run_id)
        if state.last_event_ts:
            age = _age_label_epoch(state.last_event_ts)
            run_state = "finished" if state.finished else "in flight"
            ui_impl.kv(
                "Run state",
                f"{run_state} (last event {age})" if age else run_state,
            )
        if state.usage_calls:
            note = "+" if state.unreported_calls else ""
            ui_impl.kv(
                "Run usage",
                f"{state.total_tokens}{note} tokens, "
                f"${state.cost_usd:.4f}{note}"
                + (
                    f" of {state.max_total_tokens} token cap"
                    if state.max_total_tokens else ""
                ),
            )

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
                if comp_state is not None and comp_state.pr_state else ""
            )
            ui_impl.kv("  pr", f"{comp.pr_url}{pr_note}")
        elif comp_state is not None and comp_state.pr_url:
            ui_impl.kv(
                "  pr", f"{comp_state.pr_url} ({comp_state.pr_state})",
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
                    f"{comp_state.last_event} ({age})"
                    if age else comp_state.last_event,
                )
            if comp_state.checkpoint_open:
                ui_impl.kv(
                    "  checkpoint",
                    f"{comp_state.checkpoint_open} awaiting decision",
                )
            if (
                comp_state.last_heartbeat_ts
                and comp.status in ("running", "verifying")
            ):
                ui_impl.kv(
                    "  worker",
                    f"last heartbeat {_age_label_epoch(comp_state.last_heartbeat_ts)}",
                )
            if comp_state.usage_calls:
                note = (
                    f" (lower bound: {comp_state.unreported_calls} "
                    f"call(s) unreported)"
                    if comp_state.unreported_calls else ""
                )
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
                path for path in (
                    root_dir / ".kstrl" / "worktrees" / state.run_id / comp.id,
                    root_dir / ".kstrl" / "debug" / state.run_id / comp.id,
                )
                if path.exists()
            ]
            for path in evidence:
                ui_impl.kv("  evidence", str(path))


@cli.command()
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path (defaults to current directory)",
)
@click.option(
    "--run-id",
    "run_id",
    help="Run to observe (unique prefix ok; default: newest run)",
)
@click.option(
    "--poll",
    type=click.FloatRange(min=0, min_open=True),
    default=0.2,
    help="Tail poll interval in seconds (default: 0.2, spike-measured)",
)
def dash(root: Path | None, run_id: str | None, poll: float) -> None:
    """Live dashboard over a factory run (observe-only).

    Tails .kstrl/runs/<run_id>/ - a run in flight in another terminal,
    or a finished one (post-mortem replay works by construction). This
    command never writes to the run; E6 checkpoints are answered where
    the factory runs.
    """
    import sys as _sys

    root_dir = root.resolve() if root else Path.cwd()
    if not (_sys.stdout.isatty() and _sys.stdin.isatty()):
        click.echo(
            "ralph dash needs a terminal; use `ralph status` for "
            "non-interactive output.",
            err=True,
        )
        _sys.exit(2)

    from kstrl.tui.runs import find_run, latest_run

    ref = find_run(root_dir, run_id) if run_id else latest_run(root_dir)
    if ref is None:
        click.echo(
            f"No run found under {root_dir / '.kstrl' / 'runs'}"
            + (f" matching '{run_id}'" if run_id else "")
            + ". Run `ralph factory` first, or check --root.",
            err=True,
        )
        _sys.exit(1)

    from kstrl.tui.app import KstrlTuiApp, Mode
    from kstrl.tui.dispatch import initial_screens_for_kind

    app = KstrlTuiApp(
        run_dir=ref.run_dir, root_dir=root_dir,
        mode=Mode.DASH, poll_interval=poll,
        screen_factory=initial_screens_for_kind(
            ref.kind, observe_only=True,
        ),
    )
    try:
        code = app.run()
    finally:
        # Spike finding 2: belt-and-braces terminal restore for any
        # exit path Textual could not clean up after.
        _sys.stdout.write("\x1b[?1049l\x1b[?25h\x1b[0m")
        _sys.stdout.flush()
    _sys.exit(code or 0)


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
    help="Manifest file (default: scripts/kstrl/manifest.json, falling "
         "back to scripts/kstrl/run-manifest.json)",
)
@click.option(
    "--progress-log",
    "progress_log_path",
    type=click.Path(path_type=Path),
    help="Progress log to join onto the manifest "
         "(default: <root>/.kstrl/progress.jsonl)",
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
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
    help="Open the dashboard for the newest run (default: auto - on "
         "when stdin/stdout are TTYs, --ui is not plain, and --watch "
         "is not set; KSTRL_NO_TUI=1 forces off)",
)
def status(
    root: Path | None,
    manifest_path: Path | None,
    progress_log_path: Path | None,
    watch: bool,
    interval: float,
    ui: str,
    no_color: bool,
    tui: bool | None,
) -> None:
    """Show per-component status from the manifest + progress log.

    On a TTY this opens the dashboard for the newest run of any kind
    (post-mortem or live); the plain text report remains the contract
    for pipes/CI, --no-tui, --watch, and KSTRL_NO_TUI=1.

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

    use_tui = tui if tui is not None else (
        sys.stdout.isatty()
        and sys.stdin.isatty()
        and envcompat.get("KSTRL_NO_TUI") != "1"
        and _normalize_ui_mode(ui) != "plain"
        and not watch
    )
    if use_tui:
        from kstrl.tui.runs import latest_run

        ref = latest_run(root_dir)
        if ref is not None:
            from kstrl.tui.app import KstrlTuiApp, Mode
            from kstrl.tui.dispatch import initial_screens_for_kind

            app = KstrlTuiApp(
                run_dir=ref.run_dir, root_dir=root_dir, mode=Mode.DASH,
                screen_factory=initial_screens_for_kind(
                    ref.kind, observe_only=True,
                ),
            )
            try:
                code = app.run()
            finally:
                sys.stdout.write("\x1b[?1049l\x1b[?25h\x1b[0m")
                sys.stdout.flush()
            sys.exit(code or 0)
        # No run dirs yet: fall through to the plain report, whose
        # missing-manifest guidance is the useful answer here.

    if manifest_path is not None:
        candidates = [manifest_path]
    else:
        # Factory runs persist to manifest.json; `ralph run` persists to
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
            ui_impl.info(
                "Run `ralph factory` or `ralph run` first, or pass --manifest."
            )
            return 1

        try:
            manifest = Manifest.load(manifest_file)
        except (OSError, ValueError) as exc:
            ui_impl.err(f"Failed to load manifest {manifest_file}: {exc}")
            return 1

        state, source_path = _load_state(manifest)
        _render_status(
            manifest, manifest_file, ui_impl,
            state=state, source_path=source_path,
            root_dir=root_dir,
        )
        if (
            source_path is not None
            and source_path.name == "events.jsonl"
            and not watch
        ):
            ui_impl.info("")
            ui_impl.info("Dashboard: ralph dash")
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
    help="Manifest file (default: scripts/kstrl/manifest.json)",
)
@click.option(
    "--progress-log",
    type=click.Path(path_type=Path),
    help="Path for JSONL progress log",
)
@click.option(
    "--keep-worktrees-on-failure",
    is_flag=True,
    help="Keep a failed component's worktree for post-mortem instead of "
         "removing it at cleanup (also via "
         "KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE / "
         "[factory].keep_worktrees_on_failure in ralph.toml)",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another ralph invocation holds "
         ".kstrl/factory.lock (may corrupt the other run's state)",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain", "gum"]),
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
    yes: bool,
    ui: str,
    no_color: bool,
) -> None:
    """Retry a FAILED component from the factory manifest (R3.3).

    Resets COMPONENT_ID and its cascade-skipped dependents to PENDING,
    removes the failed attempt's kept worktree and branch (a retry
    starts fresh from the base branch; the failed attempt's findings
    stay in the evolution journal), then re-enters the factory with the
    same manifest. Phase configs resolve env > ralph.toml > defaults,
    exactly like `ralph factory` invoked without flags. The run-level
    factory lock applies as usual.
    """
    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    manifest_file = (
        manifest_path if manifest_path is not None
        else root_dir / "scripts" / "kstrl" / "manifest.json"
    )
    if not manifest_file.exists():
        ui_impl.err(f"No manifest found at {manifest_file}")
        ui_impl.info("Run `ralph factory` first, or pass --manifest.")
        sys.exit(1)
    try:
        manifest = Manifest.load(manifest_file)
    except (OSError, ValueError) as exc:
        ui_impl.err(f"Failed to load manifest {manifest_file}: {exc}")
        sys.exit(1)

    try:
        prepare_retry(manifest, component_id, manifest_file, root_dir, ui_impl)
    except ValueError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)
    except RetryError:
        sys.exit(1)

    _retry_channel = UiInteractionChannel(ui_impl)
    if not yes and _retry_channel.can_prompt():
        response = _retry_channel.request(PromptRequest(
            kind=PromptKind.CONFIRM,
            header=f"Re-enter the factory to retry '{component_id}'?",
            options=("Start", "Quit"),
            default=0,
        ))
        if response.answered and response.choice != 0:
            sys.exit(0)

    # Config assembly mirrors `ralph factory` with no flags: every phase
    # config resolves env > ralph.toml > defaults (R2.1 control plane).
    factory_config, base_config = assemble_factory_configs(
        root_dir,
        single_pr=manifest.single_pr,
        progress_log_path=progress_log,
        force_lock=force_lock,
        keep_worktrees_on_failure=keep_worktrees_on_failure,
    )
    _check_agent_preflight(base_config, ui_impl)

    stop = StopController()
    uninstall = install_signal_handlers(stop)
    try:
        result = run_factory(
            manifest, factory_config, base_config, ui_impl, root_dir,
            manifest_path=manifest_file,
            stop=stop,
        )
    finally:
        uninstall()
    sys.exit(result.exit_code)


@cli.command()
@click.option(
    "--apply",
    "apply_id",
    type=str,
    default=None,
    help="Apply a specific proposal (e.g. PROP-001) or 'all' for all proposals",
)
@click.option(
    "--status",
    "show_status",
    is_flag=True,
    help="Show experiment trends",
)
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    help="Project root path",
)
@click.option(
    "--ui",
    type=click.Choice(["auto", "rich", "plain"]),
    default="auto",
    help="UI mode",
)
@click.option(
    "--no-color",
    is_flag=True,
    help="Disable colors",
)
def evolve(
    apply_id: str | None,
    show_status: bool,
    root: Path | None,
    ui: str,
    no_color: bool,
) -> None:
    """Analyze factory runs and propose harness improvements.

    Without arguments, analyzes recent runs and shows proposals.
    Use --status to see experiment trends.
    Use --apply PROP-NNN (or 'all') to apply proposals: convention-type
    proposals (target claude_md) are appended to the project CLAUDE.md
    Agent Learnings section after confirmation; every other target
    prints manual instructions.
    """
    from kstrl.evolution import EvolutionConfig, EvolutionJournal

    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    # R2.1: honor [evolution] in ralph.toml + env, anchored to --root.
    evo_config = EvolutionConfig.load(root_dir)

    if not evo_config.enabled:
        ui_impl.err("Evolution is disabled in config")
        sys.exit(1)

    journal = EvolutionJournal(evo_config)

    if show_status:
        ui_impl.section("Experiment Trends")
        trends = journal.get_experiment_trends(last_n=10)
        if not trends:
            ui_impl.info("No experiments recorded yet. Run `ralph factory` first.")
            sys.exit(0)

        for entry in trends:
            ui_impl.info(
                f"  {entry.get('run_id', '?')} | "
                f"completed={entry.get('completed', '?')} "
                f"failed={entry.get('failed', '?')} "
                f"retry_rate={entry.get('retry_rate', '?')}"
            )
        sys.exit(0)

    if apply_id:
        proposals_dir = root_dir / ".kstrl" / "proposals"
        if not proposals_dir.exists():
            ui_impl.err("No proposals found. Run `ralph evolve` first.")
            sys.exit(1)
        exit_code = _evolve_apply(
            apply_id, proposals_dir, root_dir, evo_config, ui_impl,
        )
        sys.exit(exit_code)

    # Default: analyze and propose
    ui_impl.section("Evolution: Analyzing Runs")
    patterns = journal.get_cross_run_patterns(lookback_runs=evo_config.lookback_runs)

    if not patterns:
        ui_impl.info("No recurring failure patterns found across recent runs.")
        ui_impl.info("Run more factory sessions to accumulate data.")
        sys.exit(0)

    ui_impl.ok(f"Found {len(patterns)} recurring patterns")
    for pattern in patterns:
        ui_impl.info(
            f"  [{pattern.check_name}] {pattern.description} "
            f"(seen in {pattern.frequency} components)"
        )

    # R6.3: honor [evolution] auto_propose - when disabled, evolve only
    # reports patterns and never writes proposal files.
    if not evo_config.auto_propose:
        ui_impl.info(
            "auto_propose is disabled ([evolution] auto_propose = false); "
            "patterns reported, no proposals generated."
        )
        sys.exit(0)

    proposals_dir = root_dir / ".kstrl" / "proposals"
    proposals = journal.propose_improvements(patterns)
    # Idempotence across repeated `ralph evolve` runs: a proposal whose
    # title already exists on disk is the same pattern re-detected, not
    # new signal - skip it rather than duplicating files.
    existing_titles = _existing_proposal_titles(proposals_dir)
    fresh = [p for p in proposals if p.title not in existing_titles]
    already = len(proposals) - len(fresh)
    # R6.2: monotonic IDs across invocations - number only the fresh
    # proposals, continuing after the highest PROP number on disk, so a
    # deduped batch never burns or reuses an existing number.
    start = journal.next_proposal_number(proposals_dir)
    for offset, proposal in enumerate(fresh):
        proposal.id = f"PROP-{start + offset:03d}"
    if fresh:
        paths = journal.save_proposals(fresh, proposals_dir)
        ui_impl.section("Proposals Generated")
        for path in paths:
            ui_impl.info(f"  {path}")
        if already:
            ui_impl.info(
                f"  ({already} proposal(s) already on disk; not duplicated)"
            )
        ui_impl.info("")
        ui_impl.info("Review proposals and apply with `ralph evolve --apply <ID>`")
    elif already:
        ui_impl.info(
            f"All {already} proposal(s) for these patterns already exist "
            f"in {proposals_dir}."
        )
    else:
        ui_impl.info("No actionable proposals generated from current patterns.")

    sys.exit(0)


def _evolve_apply(
    apply_id: str,
    proposals_dir: Path,
    root_dir: Path,
    evo_config: EvolutionConfig,
    ui_impl: UI,
) -> int:
    """R6.3: the minimal REAL apply path. Convention-type proposals
    (computational, target=claude_md) append to the project CLAUDE.md
    Agent Learnings section after explicit confirmation
    (auto_apply_computational=true skips the prompt); every other
    proposal type prints honest manual instructions - no false
    "applied" claims. Mechanics live in kstrl.proposals (shared with
    the evolve screen); narration and the click.confirm wrapper stay
    here."""
    if apply_id.lower() == "all":
        paths = sorted(proposals_dir.glob("prop-*.md"))
        if not paths:
            ui_impl.err(f"No proposal files in {proposals_dir}.")
            return 1
    else:
        candidate = proposals_dir / f"{apply_id.lower()}.md"
        if not candidate.exists():
            ui_impl.err(
                f"Proposal '{apply_id}' not found "
                f"(expected {candidate})."
            )
            return 1
        paths = [candidate]

    claude_md = root_dir / "CLAUDE.md"
    failures = 0
    for path in paths:
        proposal = parse_proposal_file(path)
        pid = proposal.display_id
        if proposal.applied:
            ui_impl.info(
                f"{pid} already applied at {proposal.applied}; skipping."
            )
            continue
        if not proposal.is_convention:
            ui_impl.info(f"{pid}: {proposal.title}")
            ui_impl.warn(
                f"  Automated apply only covers convention-type proposals "
                f"(target claude_md). This one targets "
                f"'{proposal.target or 'unknown'}': review {path} and "
                f"apply it manually."
            )
            continue
        ui_impl.info(f"{pid}: {proposal.title}")
        ui_impl.info(f"  Convention: {proposal.convention}")
        if not evo_config.auto_apply_computational:
            # PR A: the old bare click.confirm raised click.Abort on
            # non-TTY EOF and crashed the command. Piped input
            # ("echo y | ralph evolve --apply ...") must keep working,
            # so this stays click.confirm - with EOF now meaning
            # "declined", never a crash.
            try:
                confirmed = click.confirm(
                    f"Append this convention to {claude_md}?", default=False,
                )
            except click.Abort:
                ui_impl.info("")
                confirmed = False
            if not confirmed:
                ui_impl.info(f"  {pid} not applied (declined).")
                continue
        if not _append_to_agent_learnings(
            claude_md, pid, proposal.convention,
        ):
            ui_impl.err(
                f"  Could not apply {pid}: {claude_md} is missing or has "
                f"no '## Agent Learnings' section. Add the section or "
                f"apply manually from {path}."
            )
            failures += 1
            continue
        mark_applied(path)
        ui_impl.ok(f"  {pid} appended to {claude_md}.")
    return 1 if failures else 0


def main() -> None:
    """Main entry point."""
    cli()


def deprecated_ralph_main() -> None:
    """Entry point for the deprecated `ralph` command (rename shim)."""
    print(
        "warning: the `ralph` command is deprecated; use `ks` (or `kstrl`).",
        file=sys.stderr,
    )
    cli()


if __name__ == "__main__":
    main()
