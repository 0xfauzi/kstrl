"""CLI entry point for Ralph."""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from click.core import ParameterSource

from ralph_py import __version__
from ralph_py.agents import ClaudeCodeAgent, CodexAgent, get_agent
from ralph_py.config import RalphConfig, _parse_paths
from ralph_py.decompose import decompose_spec
from ralph_py.factory import FactoryConfig, run_factory
from ralph_py.init_cmd import DEFAULT_FEATURE_UNDERSTAND, run_init
from ralph_py.loop import run_loop
from ralph_py.manifest import Manifest
from ralph_py.prd import PRD
from ralph_py.ui import get_ui


def _use_cli_value(ctx: click.Context, name: str) -> bool:
    return ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE


def _resolve_root(root: Path | None, prompt: Path | None, prd: Path | None) -> Path:
    if root is not None:
        return root.resolve()

    for candidate in (prompt, prd):
        if candidate is None:
            continue
        resolved = candidate.resolve()
        parent = resolved.parent
        if parent.name == "ralph" and parent.parent.name == "scripts":
            return parent.parent.parent

    return Path.cwd()


def _resolve_path(root: Path, value: str | None, default: Path) -> Path:
    if value is None or value == "":
        return default
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _normalize_ui_mode(value: str) -> str:
    normalized = (value or "auto").strip().lower()
    if normalized == "gum":
        return "rich"
    if normalized in {"plain", "off", "no", "0"}:
        return "plain"
    if normalized not in {"auto", "rich", "plain"}:
        return "auto"
    return normalized


def _derive_feature_name(prd_path: Path, root: Path) -> str:
    try:
        rel = prd_path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = None

    if rel is not None and len(rel.parts) >= 4:
        if rel.parts[0] == "scripts" and rel.parts[1] == "ralph" and rel.parts[2] == "feature":
            return rel.parts[3]

    return prd_path.stem


class LoggingAgent:
    """Agent wrapper that appends streamed output to a log file."""

    def __init__(self, agent: object, log_path: Path) -> None:
        self._agent = agent
        self._log_path = log_path

    @property
    def name(self) -> str:
        return self._agent.name

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ):  # type: ignore[override]
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a") as handle:
            for line in self._agent.run(prompt, cwd):
                handle.write(f"{line}\n")
                handle.flush()
                yield line

    @property
    def final_message(self) -> str | None:
        return self._agent.final_message


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Ralph - Agentic loop harness for AI-driven development."""
    pass


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
    "--legacy",
    is_flag=True,
    help="Use legacy direct loop (bypass factory pipeline)",
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
    legacy: bool,
) -> None:
    """Run the agentic loop.

    MAX_ITERATIONS is the maximum number of iterations (default: 10).

    By default, delegates to the factory pipeline with mechanical verification.
    Use --no-verify to skip verification or --legacy for the old direct loop.
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

    # Build config from environment defaults first.
    config = RalphConfig.from_env(root_dir)

    # Apply CLI overrides when explicitly provided.
    if _use_cli_value(ctx, "max_iterations"):
        config.max_iterations = max_iterations
    if _use_cli_value(ctx, "prompt"):
        config.prompt_file = _resolve_path(
            root_dir, prompt, root_dir / "scripts/ralph/prompt.md"
        )
    if _use_cli_value(ctx, "prd"):
        config.prd_file = _resolve_path(
            root_dir, prd, root_dir / "scripts/ralph/prd.json"
        )
    if _use_cli_value(ctx, "sleep"):
        config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        config.interactive = interactive
    if _use_cli_value(ctx, "allowed_paths"):
        config.allowed_paths = _parse_paths(allowed_paths)
    if _use_cli_value(ctx, "branch"):
        config.ralph_branch = branch
        config.ralph_branch_explicit = True
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

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    # Check codex availability if not using custom agent
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(
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

    if not config.agent_cmd and not CodexAgent.is_available():
        ui_impl.err("codex not found in PATH")
        ui_impl.info("Install codex or use --agent-cmd to specify a custom agent")
        sys.exit(1)

    # Legacy mode: use direct loop (old behavior)
    if legacy:
        agent = get_agent(config.agent_cmd, config.model, config.model_reasoning_effort)
        result = run_loop(config, ui_impl, agent, root_dir)
        sys.exit(result.exit_code)

    # Default: delegate to factory pipeline as single-component run
    from ralph_py.feedforward import FeedforwardConfig
    from ralph_py.manifest import Manifest
    from ralph_py.verify import VerifyConfig

    # Determine branch from config or PRD
    prd_branch = ""
    if config.prd_file.exists():
        try:
            from ralph_py.prd import PRD as PRDLoader
            prd_doc = PRDLoader.load(config.prd_file)
            prd_branch = prd_doc.branch_name
        except Exception:
            pass

    effective_branch = config.ralph_branch or prd_branch or "ralph/run"

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

    # Build factory config for single-component mode
    v_config = None if no_verify else VerifyConfig()
    ff_config = FeedforwardConfig() if not no_verify else None

    factory_cfg = FactoryConfig(
        max_parallel=1,
        max_retries=3,
        use_worktrees=False,
        single_pr=False,
        create_prs=False,
        verify_config=v_config,
        review_mode="advisory",
        contract_config=None,
        feedforward_config=ff_config,
    )

    factory_result = run_factory(manifest, factory_cfg, config, ui_impl, root_dir)
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
    ui_impl = get_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)
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
    help="Git branch (default: ralph/understanding)",
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
    - Works on ralph/understanding branch by default
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
    ralph_dir = root_dir / "scripts" / "ralph"

    # Create codebase_map.md if missing
    codebase_map = ralph_dir / "codebase_map.md"
    if not codebase_map.exists():
        from ralph_py.init_cmd import DEFAULT_CODEBASE_MAP
        codebase_map.parent.mkdir(parents=True, exist_ok=True)
        codebase_map.write_text(DEFAULT_CODEBASE_MAP)

    config = RalphConfig.from_env(root_dir)

    # Apply CLI overrides when explicitly provided.
    if _use_cli_value(ctx, "max_iterations"):
        config.max_iterations = max_iterations
    if _use_cli_value(ctx, "prompt"):
        config.prompt_file = _resolve_path(
            root_dir, prompt, ralph_dir / "understand_prompt.md"
        )
    if _use_cli_value(ctx, "prd"):
        config.prd_file = _resolve_path(
            root_dir, prd, ralph_dir / "prd.json"
        )
    if _use_cli_value(ctx, "sleep"):
        config.sleep_seconds = sleep
    if _use_cli_value(ctx, "interactive"):
        config.interactive = interactive
    if _use_cli_value(ctx, "allowed_paths"):
        config.allowed_paths = _parse_paths(allowed_paths)
    if _use_cli_value(ctx, "branch"):
        config.ralph_branch = branch
        config.ralph_branch_explicit = True
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
        config.prompt_file = ralph_dir / "understand_prompt.md"
    if not _use_cli_value(ctx, "allowed_paths") and "ALLOWED_PATHS" not in os.environ:
        config.allowed_paths = ["scripts/ralph/codebase_map.md"]
    if not _use_cli_value(ctx, "branch") and "RALPH_BRANCH" not in os.environ:
        config.ralph_branch = "ralph/understanding"
        config.ralph_branch_explicit = False

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

    # Check codex availability
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(
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

    if not config.agent_cmd and not CodexAgent.is_available():
        ui_impl.err("codex not found in PATH")
        ui_impl.info("Install codex or use --agent-cmd to specify a custom agent")
        sys.exit(1)

    agent = get_agent(config.agent_cmd, config.model, config.model_reasoning_effort)

    result = run_loop(config, ui_impl, agent, root_dir)
    sys.exit(result.exit_code)


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
    ralph_dir = root_dir / "scripts" / "ralph"

    base_config = RalphConfig.from_env(root_dir)

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
    ui_impl = get_ui(
        base_config.ui_mode,
        base_config.no_color,
        base_config.ascii_only,
        force_rich=force_rich,
    )

    codebase_map = ralph_dir / "codebase_map.md"
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
        prd_path = _resolve_path(root_dir, prd, ralph_dir / "prd.json")
    elif env_prd is not None:
        prd_path = _resolve_path(root_dir, env_prd, ralph_dir / "prd.json")
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

    feature_dir = ralph_dir / "feature" / feature_name
    feature_dir.mkdir(parents=True, exist_ok=True)
    feature_understand = feature_dir / "understand.md"
    if not feature_understand.exists():
        feature_understand.write_text(DEFAULT_FEATURE_UNDERSTAND)

    log_dir = root_dir / ".ralph" / "logs" / f"feature_{feature_name}"

    def log_path(label: str, attempt: int | None = None) -> Path:
        stamp = _timestamp()
        if attempt is None:
            name = f"{label}_{stamp}.log"
        else:
            name = f"{label}_{attempt:02d}_{stamp}.log"
        return log_dir / name

    def build_repair_prd(log_file: Path, attempt: int) -> Path:
        repair_dir = feature_dir / "repairs"
        repair_dir.mkdir(parents=True, exist_ok=True)
        repair_path = repair_dir / f"repair_{_timestamp()}.json"
        latest_path = repair_dir / "latest.json"

        verification: list[str] = []
        seen: set[str] = set()
        for story in prd_doc.user_stories:
            for item in story.acceptance_criteria:
                lower = item.lower()
                has_check = "typecheck" in lower or "tests" in lower or "lint" in lower
                if has_check and "pass" in lower:
                    if item not in seen:
                        seen.add(item)
                        verification.append(item)

        try:
            rel_log = log_file.relative_to(root_dir)
            log_ref = rel_log.as_posix()
        except ValueError:
            log_ref = str(log_file)

        criteria = [f"Repair failures reported in {log_ref}"]
        criteria.extend(verification)

        repair_story = {
            "id": f"REPAIR-{attempt:02d}",
            "title": "Repair failures from last run",
            "acceptanceCriteria": criteria,
            "priority": 1,
            "passes": False,
            "notes": f"Original PRD: {prd_path}",
        }
        repair_doc = {
            "branchName": prd_doc.branch_name,
            "userStories": [repair_story],
        }
        with open(repair_path, "w") as handle:
            json.dump(repair_doc, handle, indent=2)
            handle.write("\n")
        with open(latest_path, "w") as handle:
            json.dump(repair_doc, handle, indent=2)
            handle.write("\n")

        return repair_path

    if not base_config.agent_cmd and not CodexAgent.is_available():
        ui_impl.err("codex not found in PATH")
        ui_impl.info("Install codex or use --agent-cmd to specify a custom agent")
        sys.exit(1)

    agent = get_agent(
        base_config.agent_cmd,
        base_config.model,
        base_config.model_reasoning_effort,
    )

    # Feature understanding phase
    understand_config = copy.deepcopy(base_config)
    understand_config.max_iterations = understand_iterations_value
    if _use_cli_value(ctx, "understand_prompt"):
        understand_config.prompt_file = _resolve_path(
            root_dir, understand_prompt, ralph_dir / "feature_understand_prompt.md"
        )
    elif "PROMPT_FILE" not in os.environ:
        understand_config.prompt_file = ralph_dir / "feature_understand_prompt.md"
    understand_config.prd_file = prd_path
    rel_feature_understand = feature_understand.relative_to(root_dir).as_posix()
    understand_config.allowed_paths = [rel_feature_understand]
    if _use_cli_value(ctx, "branch"):
        understand_config.ralph_branch = branch
        understand_config.ralph_branch_explicit = True

    understand_log = log_path("understand")
    understand_agent = LoggingAgent(agent, understand_log)
    understand_result = run_loop(understand_config, ui_impl, understand_agent, root_dir)
    if understand_result.exit_code != 0:
        sys.exit(understand_result.exit_code)

    # Review gate
    ui_impl.section("Feature understand review")
    ui_impl.kv("Understand file", str(feature_understand))
    if implementation_auto_run:
        ui_impl.info("IMPLEMENTATION_AUTO_RUN enabled: skipping review gate")
    else:
        if not ui_impl.can_prompt():
            ui_impl.err(
                "Interactive review required. Re-run with --implementation-auto-run."
            )
            sys.exit(2)

        choice = ui_impl.choose(
            "Review the understand file and confirm implementation start:",
            ["Start implementation", "Quit to amend"],
            default=0,
        )
        if choice != 0:
            ui_impl.info("Amend the understand file and re-run `ralph feature`.")
            sys.exit(0)

    # Implementation phase
    run_config = copy.deepcopy(base_config)
    run_config.prd_file = prd_path
    run_config.max_iterations = len(prd_doc.user_stories)
    if run_config.max_iterations == 0:
        ui_impl.warn("PRD has no user stories. Skipping implementation.")
        sys.exit(0)
    run_config.prompt_file = root_dir / "scripts/ralph/prompt.md"
    if _use_cli_value(ctx, "implementation_allowed_paths"):
        run_config.allowed_paths = _parse_paths(implementation_allowed_paths)
    if _use_cli_value(ctx, "branch"):
        run_config.ralph_branch = branch
        run_config.ralph_branch_explicit = True

    run_log = log_path("run")
    run_agent = LoggingAgent(agent, run_log)
    result = run_loop(run_config, ui_impl, run_agent, root_dir)
    if result.exit_code == 0 or repair_max_runs == 0 or result.iterations == 0:
        sys.exit(result.exit_code)

    last_log = run_log
    repair_result = result
    for attempt in range(1, repair_max_runs + 1):
        repair_prd = build_repair_prd(last_log, attempt)
        repair_config = copy.deepcopy(base_config)
        repair_config.prd_file = repair_prd
        repair_config.prompt_file = root_dir / "scripts/ralph/prompt.md"
        repair_config.max_iterations = repair_iterations
        if _use_cli_value(ctx, "implementation_allowed_paths"):
            repair_config.allowed_paths = _parse_paths(implementation_allowed_paths)
        repair_config.ralph_branch = ""
        repair_config.ralph_branch_explicit = True

        repair_log = log_path("repair", attempt)
        repair_agent_base = get_agent(
            repair_agent_cmd or base_config.agent_cmd,
            base_config.model,
            base_config.model_reasoning_effort,
        )
        repair_agent = LoggingAgent(repair_agent_base, repair_log)
        repair_result = run_loop(repair_config, ui_impl, repair_agent, root_dir)
        if repair_result.exit_code == 0:
            sys.exit(0)
        last_log = repair_log

    sys.exit(repair_result.exit_code)


@cli.command()
@click.option(
    "--spec",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="Markdown spec file to decompose",
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
    type=click.Choice(["auto", "claude-code", "codex"]),
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
) -> None:
    """Decompose a spec into components and generate PRDs."""
    ctx = click.get_current_context()

    root_dir = root.resolve() if root else Path.cwd()

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    effective_cmd = agent_cmd or os.environ.get("AGENT_CMD")
    effective_model = model if _use_cli_value(ctx, "model") else os.environ.get("MODEL")
    effective_reasoning = (
        reasoning if _use_cli_value(ctx, "reasoning")
        else os.environ.get("MODEL_REASONING_EFFORT")
    )
    effective_type = (
        agent_type if _use_cli_value(ctx, "agent_type")
        else os.environ.get("RALPH_AGENT_TYPE", "auto")
    )

    if not effective_cmd and not CodexAgent.is_available() and not ClaudeCodeAgent.is_available():
        ui_impl.err("No agent available (codex and claude not found in PATH)")
        ui_impl.info("Install an agent or use --agent-cmd to specify a custom one")
        sys.exit(1)

    agent = get_agent(effective_cmd, effective_model, effective_reasoning, effective_type)

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
        ui_impl.ok(f"Decomposed into {len(manifest.components)} components")
    except ValueError as exc:
        ui_impl.err(str(exc))
        sys.exit(1)


@cli.command()
@click.option(
    "--spec",
    type=click.Path(exists=True, path_type=Path),
    help="Markdown spec file (runs decompose first)",
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
    default=4,
    help="Maximum parallel components",
)
@click.option(
    "--max-retries",
    type=int,
    default=3,
    help="Maximum retries per component",
)
@click.option(
    "--create-prs/--no-prs",
    default=True,
    help="Create PRs for completed components",
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
    help="Enable dead code cleanup: ruff auto-fixes unused imports/variables, vulture detects remaining dead code",
)
@click.option(
    "--dead-code-command",
    help="Custom dead code detection command (default: vulture on changed files)",
)
@click.option(
    "--mutation-testing",
    is_flag=True,
    help="Enable mutation testing (requires mutmut, off by default)",
)
@click.option(
    "--mutation-threshold",
    type=float,
    default=50.0,
    help="Mutation score threshold percent (default: 50)",
)
@click.option(
    "--review-mode",
    type=click.Choice(["hard", "advisory", "skip"]),
    default="hard",
    help="Phase 2 review: hard (block), advisory (warn), skip",
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
    "--contract-check",
    type=click.Choice(["tier", "final", "skip"]),
    default="tier",
    help="Phase 3 contract testing: tier (per-tier), final (end-only), skip",
)
@click.option(
    "--contract-test-cmd",
    help="Test command for contract testing (default: same as --test-command)",
)
@click.option(
    "--agent-timeout",
    type=float,
    default=1800.0,
    help="Timeout per agent iteration in seconds (default: 1800)",
)
@click.option(
    "--component-timeout",
    type=float,
    default=7200.0,
    help="Timeout per component total in seconds (default: 7200)",
)
@click.option(
    "--progress-log",
    type=click.Path(path_type=Path),
    help="Path for JSONL progress log",
)
@click.option(
    "--no-worktrees",
    is_flag=True,
    help="Disable git worktrees (forces sequential execution)",
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
    type=click.Choice(["auto", "claude-code", "codex"]),
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
def factory(
    spec: Path | None,
    manifest_path: Path | None,
    root: Path | None,
    project_name: str | None,
    base_branch: str,
    single_pr: bool,
    max_parallel: int,
    max_retries: int,
    create_prs: bool,
    verify_command: str | None,
    test_command: str | None,
    typecheck_command: str | None,
    lint_command: str | None,
    no_verify: bool,
    dead_code_cleanup: bool,
    dead_code_command: str | None,
    mutation_testing: bool,
    mutation_threshold: float,
    review_mode: str,
    review_agent_cmd: str | None,
    review_model: str | None,
    contract_check: str,
    contract_test_cmd: str | None,
    agent_timeout: float,
    component_timeout: float,
    progress_log: Path | None,
    no_worktrees: bool,
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
        ui_impl = get_ui("auto", no_color)
        ui_impl.err("Either --spec or --manifest is required")
        sys.exit(2)

    root_dir = root.resolve() if root else Path.cwd()

    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    effective_cmd = agent_cmd or os.environ.get("AGENT_CMD")
    effective_model = model if _use_cli_value(ctx, "model") else os.environ.get("MODEL")
    effective_reasoning = (
        reasoning if _use_cli_value(ctx, "reasoning")
        else os.environ.get("MODEL_REASONING_EFFORT")
    )
    effective_type = (
        agent_type if _use_cli_value(ctx, "agent_type")
        else os.environ.get("RALPH_AGENT_TYPE", "auto")
    )

    if not effective_cmd and not CodexAgent.is_available() and not ClaudeCodeAgent.is_available():
        ui_impl.err("No agent available (codex and claude not found in PATH)")
        ui_impl.info("Install an agent or use --agent-cmd to specify a custom one")
        sys.exit(1)

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
        except ValueError as exc:
            ui_impl.err(str(exc))
            sys.exit(1)

    # Display summary and confirm
    ui_impl.section("Factory Plan")
    ui_impl.kv("Project", manifest.project_name)
    ui_impl.kv("Components", str(len(manifest.components)))
    ui_impl.kv("Base branch", manifest.base_branch)
    ui_impl.kv("Single PR", "yes" if manifest.single_pr else "no")
    ui_impl.kv("Max parallel", str(max_parallel))
    ui_impl.kv("Create PRs", "yes" if create_prs else "no")

    topo = manifest.topological_order()
    ui_impl.info("")
    ui_impl.info("Execution order:")
    for i, comp_id in enumerate(topo, 1):
        comp = manifest.get_component(comp_id)
        status = comp.status if comp else "?"
        dep_list = ", ".join(comp.dependencies) if comp and comp.dependencies else ""
        deps = f" (depends on: {dep_list})" if dep_list else ""
        ui_impl.info(f"  {i}. {comp_id} [{status}]{deps}")

    if not yes and ui_impl.can_prompt():
        choice = ui_impl.choose(
            "Proceed with factory execution?",
            ["Start", "Quit"],
            default=0,
        )
        if choice != 0:
            sys.exit(0)

    # Build configs
    from ralph_py.contract import ContractConfig
    from ralph_py.verify import VerifyConfig

    v_config: VerifyConfig | None = None
    if not no_verify:
        v_config = VerifyConfig(
            test_command=test_command,
            typecheck_command=typecheck_command,
            lint_command=lint_command,
            dead_code_cleanup=dead_code_cleanup,
            dead_code_command=dead_code_command,
            mutation_testing=mutation_testing,
            mutation_threshold=mutation_threshold,
        )

    c_config: ContractConfig | None = None
    if contract_check != "skip":
        c_config = ContractConfig(
            mode=contract_check,
            test_command=contract_test_cmd or test_command or "uv run pytest",
        )

    factory_config = FactoryConfig(
        max_parallel=max_parallel,
        max_retries=max_retries,
        use_worktrees=not no_worktrees,
        single_pr=manifest.single_pr,
        create_prs=create_prs,
        verify_command=verify_command,
        verify_config=v_config,
        review_mode=review_mode,
        review_agent_cmd=review_agent_cmd,
        review_model=review_model,
        contract_config=c_config,
        progress_log_path=progress_log,
    )

    ralph_dir = root_dir / "scripts" / "ralph"
    base_config = RalphConfig.from_env(root_dir)
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

    # Ensure prompt file exists
    if not base_config.prompt_file.exists():
        default_prompt = ralph_dir / "prompt.md"
        if default_prompt.exists():
            base_config.prompt_file = default_prompt

    result = run_factory(manifest, factory_config, base_config, ui_impl, root_dir)
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
    Use --apply to apply proposals.
    """
    from ralph_py.evolution import EvolutionConfig, EvolutionJournal

    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    evo_config = EvolutionConfig()

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
        proposals_dir = root_dir / ".ralph" / "proposals"
        if not proposals_dir.exists():
            ui_impl.err("No proposals found. Run `ralph evolve` first.")
            sys.exit(1)

        ui_impl.info(f"Applying proposal: {apply_id}")
        ui_impl.warn("Proposal application is not yet automated. Review proposals in .ralph/proposals/ and apply manually.")
        sys.exit(0)

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

    proposals = journal.propose_improvements(patterns)
    if proposals:
        proposals_dir = root_dir / ".ralph" / "proposals"
        paths = journal.save_proposals(proposals, proposals_dir)
        ui_impl.section("Proposals Generated")
        for path in paths:
            ui_impl.info(f"  {path}")
        ui_impl.info("")
        ui_impl.info("Review proposals and apply with `ralph evolve --apply <ID>`")
    else:
        ui_impl.info("No actionable proposals generated from current patterns.")

    sys.exit(0)


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
