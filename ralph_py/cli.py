"""CLI entry point for Ralph."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ralph_py.evolution import EvolutionConfig

import click
from click.core import ParameterSource

from ralph_py import __version__
from ralph_py.agents import ClaudeCodeAgent, CodexAgent, get_agent
from ralph_py.agents.base import Agent, UsageRecord
from ralph_py.breaker import BreakerConfig
from ralph_py.config import RalphConfig, _parse_paths, load_toml_section
from ralph_py.decompose import SpecBlockerError, decompose_spec
from ralph_py.factory import FactoryConfig, run_factory
from ralph_py.init_cmd import DEFAULT_FEATURE_UNDERSTAND, run_init
from ralph_py.loop import run_loop
from ralph_py.manifest import Manifest
from ralph_py.observability import (
    RunActivity,
    event_age_seconds,
    format_age,
    latest_run_id,
    read_progress_events,
    summarize_events,
)
from ralph_py.prd import PRD
from ralph_py.sandbox import SandboxConfig
from ralph_py.timeout import TimeoutConfig
from ralph_py.ui import get_ui
from ralph_py.ui.base import UI


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
            "(expected: claude, codex, custom, or auto)",
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


def _check_agent_preflight(config: RalphConfig, ui_impl: UI) -> None:
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
            "Run `ralph init` to scaffold scripts/ralph/prd.json, "
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
    config: RalphConfig,
    root_dir: Path,
    prompt_default: Path,
    prd_default: Path,
) -> set[str]:
    """Overlay explicitly-passed CLI flags onto a loaded RalphConfig.

    Shared by ``run`` and ``config show`` so what the observability
    command prints is exactly what the run command executes. Only flags
    the invoking command declares are considered (``ctx.params``), and
    only when the user actually passed them. Returns the RalphConfig
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
        config.ralph_branch = ctx.params["branch"]
        config.ralph_branch_explicit = True
        overridden.add("ralph_branch")
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


@contextmanager
def _scrubbed_environ() -> Iterator[None]:
    """Temporarily clear os.environ so a loader sees toml + defaults only.

    Used by ``config show`` to isolate the env contribution: a field
    whose value changes when the environment disappears was env-set.
    """
    saved = dict(os.environ)
    os.environ.clear()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _format_config_value(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return repr(value)


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

    def __init__(self, agent: Agent, log_path: Path) -> None:
        self._agent = agent
        self._log_path = log_path

    @property
    def name(self) -> str:
        return self._agent.name

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a") as handle:
            for line in self._agent.run(prompt, cwd, timeout):
                handle.write(f"{line}\n")
                handle.flush()
                yield line

    @property
    def final_message(self) -> str | None:
        return self._agent.final_message

    @property
    def usage_records(self) -> list[UsageRecord]:
        """R3.1: forward the wrapped agent's usage records."""
        records = getattr(self._agent, "usage_records", None)
        return list(records) if records is not None else []


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
    "--force-lock",
    is_flag=True,
    help="Proceed even if another ralph invocation holds "
         ".ralph/factory.lock (may corrupt the other run's state)",
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
    config = RalphConfig.load(root_dir)

    # Apply CLI overrides when explicitly provided.
    _apply_cli_overrides(
        ctx, config, root_dir,
        prompt_default=root_dir / "scripts/ralph/prompt.md",
        prd_default=root_dir / "scripts/ralph/prd.json",
    )

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

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

    # R2.4 preflight: verify the agent the config selects is reachable,
    # then validate the PRD - both BEFORE any agent invocation.
    _check_agent_preflight(config, ui_impl)
    _check_prd_preflight(config.prd_file, ui_impl)

    # Single-component factory invocation
    from ralph_py.config import load_toml_section
    from ralph_py.feedforward import FeedforwardConfig
    from ralph_py.manifest import Manifest
    from ralph_py.security import SecurityConfig
    from ralph_py.verify import VerifyConfig

    # Determine branch from config or PRD. The preflight above already
    # validated existence + schema, so a load failure here is a real bug
    # worth surfacing, not something to swallow.
    prd_branch = PRD.load(config.prd_file).branch_name

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
    if "review_mode" not in load_toml_section(root_dir / "ralph.toml", "factory"):
        factory_cfg.review_mode = "advisory"

    # R0.5 (H-15): `ralph run` persists to its own run-manifest.json so
    # it can never clobber a factory run's resumable manifest.json.
    factory_result = run_factory(
        manifest, factory_cfg, config, ui_impl, root_dir,
        manifest_path=root_dir / "scripts" / "ralph" / "run-manifest.json",
    )
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

    config = RalphConfig.load(root_dir)

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
    # Only fall back to the understand-mode branch default when no other
    # source (CLI / env / TOML) supplied a branch. RalphConfig.load sets
    # ralph_branch_explicit=True when TOML provides a non-empty [git].branch.
    if (
        not _use_cli_value(ctx, "branch")
        and "RALPH_BRANCH" not in os.environ
        and not config.ralph_branch_explicit
    ):
        config.ralph_branch = "ralph/understanding"
        config.ralph_branch_explicit = False

    config.ui_mode = _normalize_ui_mode(config.ui_mode)

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
    )

    result = run_loop(
        config, ui_impl, agent, root_dir,
        timeouts=TimeoutConfig.load(root_dir),
        breaker_config=BreakerConfig.load(root_dir),
    )
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

    base_config = RalphConfig.load(root_dir)

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

    timeouts = TimeoutConfig.load(root_dir)
    breaker_config = BreakerConfig.load(root_dir)

    understand_log = log_path("understand")
    understand_agent = LoggingAgent(agent, understand_log)
    understand_result = run_loop(
        understand_config, ui_impl, understand_agent, root_dir,
        timeouts=timeouts, breaker_config=breaker_config,
    )
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
    result = run_loop(
        run_config, ui_impl, run_agent, root_dir,
        timeouts=timeouts, breaker_config=breaker_config,
    )
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
            base_config.agent_type,
            sandbox=sandbox_cfg,
        )
        repair_agent = LoggingAgent(repair_agent_base, repair_log)
        repair_result = run_loop(
            repair_config, ui_impl, repair_agent, root_dir,
            timeouts=timeouts, breaker_config=breaker_config,
        )
        if repair_result.exit_code == 0:
            sys.exit(0)
        last_log = repair_log

    sys.exit(repair_result.exit_code)


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
    except SpecBlockerError as exc:
        ui_impl.err(str(exc))
        # R1.7: point at the durable artifact so the user iterates
        # against a file, not scrollback.
        if exc.artifact_path is not None:
            ui_impl.info(f"Spec issues written to: {exc.artifact_path}")
        sys.exit(2)
    except ValueError as exc:
        ui_impl.err(str(exc))
        sys.exit(1)


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
         "RALPH_FACTORY_MAX_ADVERSARIAL_CALLS / "
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
         "RALPH_FACTORY_PAUSE_BEFORE_PR_MERGE / "
         "[factory].pause_before_pr_merge in ralph.toml)",
)
@click.option(
    "--progress-log",
    type=click.Path(path_type=Path),
    help="Path for the JSONL progress log (default: .ralph/progress.jsonl; "
         "the log is on by default, disable via "
         "[factory].progress_log_enabled = false or "
         "RALPH_FACTORY_PROGRESS_LOG_ENABLED=0)",
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
         ".ralph/factory.lock (may corrupt the other run's state)",
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
    from ralph_py.contract import ContractConfig
    from ralph_py.evolution import EvolutionConfig
    from ralph_py.feedforward import FeedforwardConfig
    from ralph_py.security import SecurityConfig
    from ralph_py.verify import VerifyConfig

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

    if not yes and ui_impl.can_prompt():
        choice = ui_impl.choose(
            "Proceed with factory execution?",
            ["Start", "Quit"],
            default=0,
        )
        if choice != 0:
            sys.exit(0)

    ralph_dir = root_dir / "scripts" / "ralph"
    base_config = RalphConfig.load(root_dir)
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

    # R0.5 (H-15): state saves back to the file it was loaded from.
    # --manifest /custom.json persists to /custom.json; --spec runs keep
    # the default scripts/ralph/manifest.json that decompose wrote.
    result = run_factory(
        manifest, factory_config, base_config, ui_impl, root_dir,
        manifest_path=manifest_path,
    )
    sys.exit(result.exit_code)


# Display structure for the RalphConfig-backed ralph.toml sections:
# section -> [(toml_key, dataclass_field)]. Mirrors DEFAULT_RALPH_TOML in
# init_cmd.py plus the env/flag-only UI knobs (ui_mode, no_color).
_RALPH_SHOW_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("agent", [
        ("type", "agent_type"),
        ("command", "agent_cmd"),
        ("model", "model"),
        ("reasoning_effort", "model_reasoning_effort"),
    ]),
    ("run", [
        ("max_iterations", "max_iterations"),
        ("sleep_seconds", "sleep_seconds"),
        ("interactive", "interactive"),
    ]),
    ("paths", [
        ("prompt", "prompt_file"),
        ("prd", "prd_file"),
        ("progress", "progress_file"),
        ("codebase_map", "codebase_map_file"),
        ("allowed", "allowed_paths"),
    ]),
    ("git", [
        ("branch", "ralph_branch"),
        ("auto_checkout", "auto_checkout"),
    ]),
    ("ui", [
        ("ascii", "ascii_only"),
        ("ui_mode", "ui_mode"),
        ("no_color", "no_color"),
    ]),
]


def _ralph_config_defaults(root_dir: Path) -> RalphConfig:
    """Built-in RalphConfig defaults with paths anchored like load()."""
    config = RalphConfig()
    config.prompt_file = root_dir / "scripts/ralph/prompt.md"
    config.prd_file = root_dir / "scripts/ralph/prd.json"
    config.progress_file = root_dir / "scripts/ralph/progress.txt"
    config.codebase_map_file = root_dir / "scripts/ralph/codebase_map.md"
    return config


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
    toml_path = root_dir / "ralph.toml"

    from ralph_py.contract import ContractConfig
    from ralph_py.evolution import EvolutionConfig
    from ralph_py.feedforward import FeedforwardConfig
    from ralph_py.knowledge import KnowledgeConfig
    from ralph_py.linear import LinearConfig
    from ralph_py.observability import NotifyConfig
    from ralph_py.security import SecurityConfig
    from ralph_py.verify import VerifyConfig

    # (section, loader, knob fields) - the documented ralph.toml surface.
    phase_sections: list[tuple[str, Any, list[str]]] = [
        ("factory", FactoryConfig.load, [
            "max_parallel", "max_retries", "retry_delay", "use_worktrees",
            "single_pr", "create_prs", "review_mode", "merge_timeout",
            "max_adversarial_calls", "max_total_tokens",
            "pause_before_pr_merge", "progress_log_enabled",
            "keep_worktrees_on_failure",
        ]),
        ("verify", VerifyConfig.load, [
            "test_command", "typecheck_command", "lint_command",
            "check_diff_scope", "check_bad_patterns", "dead_code_cleanup",
            "dead_code_command", "mutation_testing", "mutation_threshold",
            "mutation_timeout", "subprocess_timeout", "require_self_critique",
            "self_critique_min_bullets", "progress_file_path",
        ]),
        ("security", SecurityConfig.load, [
            "mode", "fail_threshold", "timeout_seconds", "agent_cmd",
            "agent_type", "model",
        ]),
        ("contract", ContractConfig.load, ["mode", "test_command", "timeout"]),
        ("feedforward", FeedforwardConfig.load, [
            "enabled", "module_map", "public_interfaces", "dependency_graph",
            "conventions", "max_context_tokens",
        ]),
        ("knowledge", KnowledgeConfig.load, [
            "enabled", "max_core_tokens", "max_dependency_tokens",
            "max_sibling_tokens", "distill_timeout_seconds", "distill_model",
            "max_facts_per_distill", "dependency_scope",
        ]),
        ("evolution", EvolutionConfig.load, [
            "enabled", "journal_path", "experiments_path",
            "min_pattern_frequency", "lookback_runs", "auto_propose",
            "auto_apply_computational",
        ]),
        ("timeout", TimeoutConfig.load, [
            f.name for f in dataclass_fields(TimeoutConfig)
        ]),
        ("notify", NotifyConfig.load, [
            "on_complete", "on_first_failure", "hook_timeout",
        ]),
        ("linear", LinearConfig.load, [
            "enabled", "team_id", "token_env", "auth_mode", "api_url",
            "dry_run", "timeout_seconds", "min_request_interval",
        ]),
    ]

    try:
        resolved_ralph = RalphConfig.load(root_dir)
        phase_resolved = {name: loader(root_dir) for name, loader, _ in phase_sections}
        with _scrubbed_environ():
            noenv_ralph = RalphConfig.load(root_dir)
            phase_noenv = {
                name: loader(root_dir) for name, loader, _ in phase_sections
            }
        phase_toml_keys = {
            name: set(load_toml_section(toml_path, name).keys())
            for name, _, _ in phase_sections
        }
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    defaults_ralph = _ralph_config_defaults(root_dir)

    # Per-field sources for RalphConfig, computed BEFORE flag overlay.
    ralph_sources: dict[str, str] = {}
    for f in dataclass_fields(RalphConfig):
        if getattr(resolved_ralph, f.name) != getattr(noenv_ralph, f.name):
            ralph_sources[f.name] = "env"
        elif getattr(noenv_ralph, f.name) != getattr(defaults_ralph, f.name):
            ralph_sources[f.name] = "toml"
        else:
            ralph_sources[f.name] = "default"

    flag_fields = _apply_cli_overrides(
        ctx, resolved_ralph, root_dir,
        prompt_default=root_dir / "scripts/ralph/prompt.md",
        prd_default=root_dir / "scripts/ralph/prd.json",
    )
    for name in flag_fields:
        ralph_sources[name] = "flag"
    resolved_ralph.ui_mode = _normalize_ui_mode(resolved_ralph.ui_mode)

    click.echo(f"# Resolved Ralph config for {root_dir}")
    click.echo(f"# ralph.toml: {toml_path if toml_path.exists() else '(absent)'}")
    click.echo("")

    for section, keys in _RALPH_SHOW_SECTIONS:
        click.echo(f"[{section}]")
        for toml_key, field_name in keys:
            value = getattr(resolved_ralph, field_name)
            source = ralph_sources[field_name]
            click.echo(
                f"  {toml_key} = {_format_config_value(value)}  ({source})"
            )
        click.echo("")

    for section, _, knob_fields in phase_sections:
        resolved = phase_resolved[section]
        noenv = phase_noenv[section]
        toml_keys = phase_toml_keys[section]
        click.echo(f"[{section}]")
        for field_name in knob_fields:
            value = getattr(resolved, field_name)
            if value != getattr(noenv, field_name):
                source = "env"
            elif field_name in toml_keys:
                source = "toml"
            else:
                source = "default"
            click.echo(
                f"  {field_name} = {_format_config_value(value)}  ({source})"
            )
        click.echo("")

    sys.exit(0)


def _age_label(ts: str) -> str:
    """"5m ago" for an event timestamp, or "" when unparseable."""
    age = event_age_seconds(ts)
    if age is None:
        return ""
    return f"{format_age(age)} ago"


def _render_status(
    manifest: Manifest,
    manifest_file: Path,
    ui_impl: UI,
    activity: RunActivity | None = None,
    log_path: Path | None = None,
    root_dir: Path | None = None,
) -> None:
    """Render the per-component status view from a manifest.

    ``activity`` is an observability.RunActivity joined onto the same
    per-component skeleton (R3.2): phase, attempt, last-event age, cost
    totals and evidence paths ride along when a progress log exists.
    """
    ui_impl.section("Ralph status")
    ui_impl.kv("Project", manifest.project_name)
    ui_impl.kv("Manifest", str(manifest_file))
    ui_impl.kv("Base branch", manifest.base_branch)

    if activity is not None and log_path is not None:
        ui_impl.kv("Progress log", str(log_path))
        if activity.run_id:
            ui_impl.kv("Run id", activity.run_id)
        if activity.last_event_ts:
            age = _age_label(activity.last_event_ts)
            state = "finished" if activity.finished else "in flight"
            ui_impl.kv(
                "Run state",
                f"{state} (last event {age})" if age else state,
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
        if comp.pr_url:
            ui_impl.kv("  pr", comp.pr_url)
        if comp.error:
            ui_impl.kv("  error", comp.error)

        comp_activity = (
            activity.components.get(comp.id) if activity is not None else None
        )
        if comp_activity is not None:
            if comp_activity.phase:
                ui_impl.kv("  phase", comp_activity.phase)
            attempt = comp_activity.attempt or comp.retries + 1
            ui_impl.kv("  attempt", str(attempt))
            if comp_activity.last_event:
                age = _age_label(comp_activity.last_event_ts)
                ui_impl.kv(
                    "  last event",
                    f"{comp_activity.last_event} ({age})"
                    if age else comp_activity.last_event,
                )
            if comp_activity.usage_calls:
                note = (
                    f" (lower bound: {comp_activity.unreported_calls} "
                    f"call(s) unreported)"
                    if comp_activity.unreported_calls else ""
                )
                ui_impl.kv(
                    "  usage",
                    f"{comp_activity.total_tokens} tokens, "
                    f"${comp_activity.cost_usd:.4f}, "
                    f"{comp_activity.usage_calls} calls{note}",
                )
        # Evidence paths: whatever this run left on disk for the
        # component (worktree kept after a failure, adversarial raw
        # outputs under .ralph/debug/).
        if root_dir is not None and activity is not None and activity.run_id:
            evidence = [
                path for path in (
                    root_dir / ".ralph" / "worktrees" / activity.run_id / comp.id,
                    root_dir / ".ralph" / "debug" / activity.run_id / comp.id,
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
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    help="Manifest file (default: scripts/ralph/manifest.json, falling "
         "back to scripts/ralph/run-manifest.json)",
)
@click.option(
    "--progress-log",
    "progress_log_path",
    type=click.Path(path_type=Path),
    help="Progress log to join onto the manifest "
         "(default: <root>/.ralph/progress.jsonl)",
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

    R3.2: joins the factory manifest with the ProgressLog (default
    .ralph/progress.jsonl): per component status, retries, branch,
    timestamps, plus phase, attempt, last-event age, usage totals and
    evidence paths for the latest run found in the log. Works
    manifest-only when no log exists.
    """
    import time as _time

    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    if manifest_path is not None:
        candidates = [manifest_path]
    else:
        # Factory runs persist to manifest.json; `ralph run` persists to
        # run-manifest.json (R0.5, H-15). Prefer the factory manifest.
        candidates = [
            root_dir / "scripts" / "ralph" / "manifest.json",
            root_dir / "scripts" / "ralph" / "run-manifest.json",
        ]

    log_path = progress_log_path or root_dir / ".ralph" / "progress.jsonl"

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

        events = read_progress_events(log_path)
        activity: RunActivity | None = None
        if events:
            activity = summarize_events(events, latest_run_id(events))

        _render_status(
            manifest, manifest_file, ui_impl,
            activity=activity, log_path=log_path if events else None,
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
    help="Manifest file (default: scripts/ralph/manifest.json)",
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
         "RALPH_FACTORY_KEEP_WORKTREES_ON_FAILURE / "
         "[factory].keep_worktrees_on_failure in ralph.toml)",
)
@click.option(
    "--force-lock",
    is_flag=True,
    help="Proceed even if another ralph invocation holds "
         ".ralph/factory.lock (may corrupt the other run's state)",
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
    import shutil as _shutil
    import subprocess as _sp

    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    manifest_file = (
        manifest_path if manifest_path is not None
        else root_dir / "scripts" / "ralph" / "manifest.json"
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

    comp = manifest.get_component(component_id)
    evidence_worktree = comp.evidence_worktree if comp else ""
    failed_branch = comp.branch_name if comp else ""

    try:
        reset_dependents = manifest.reset_for_retry(component_id)
    except ValueError as exc:
        ui_impl.err(str(exc))
        sys.exit(2)

    ui_impl.section("Retry plan")
    ui_impl.kv("Component", component_id)
    ui_impl.kv(
        "Cascade-skipped dependents reset",
        ", ".join(reset_dependents) if reset_dependents else "(none)",
    )
    ui_impl.kv("Manifest", str(manifest_file))

    # The failed attempt's worktree and branch are superseded by the
    # fresh attempt; remove them so provisioning and the stale-branch
    # preflight start clean. In single_pr mode every component shares
    # one branch carrying completed components' commits - never delete
    # it here.
    if evidence_worktree and Path(evidence_worktree).exists():
        _sp.run(
            ["git", "worktree", "remove", "--force", evidence_worktree],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        _shutil.rmtree(evidence_worktree, ignore_errors=True)
        _sp.run(
            ["git", "worktree", "prune"],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        ui_impl.info(
            f"Removed the failed attempt's evidence worktree: "
            f"{evidence_worktree}"
        )
    if failed_branch and not manifest.single_pr:
        branch_exists = _sp.run(
            ["git", "rev-parse", "--verify", "--quiet",
             f"refs/heads/{failed_branch}"],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        if branch_exists.returncode == 0:
            deleted = _sp.run(
                ["git", "branch", "-D", failed_branch],
                cwd=root_dir, capture_output=True, text=True, timeout=30,
            )
            if deleted.returncode == 0:
                ui_impl.info(
                    f"Deleted branch '{failed_branch}' from the failed "
                    f"attempt; the retry recreates it from "
                    f"'{manifest.base_branch}'"
                )
            else:
                ui_impl.err(
                    f"Could not delete branch '{failed_branch}': "
                    f"{deleted.stderr.strip()}"
                )
                ui_impl.info(
                    "Delete it manually (git branch -D "
                    f"{failed_branch}) and re-run; the factory refuses "
                    "to silently reuse stale branches (R0.5)."
                )
                sys.exit(1)
    elif manifest.single_pr:
        ui_impl.warn(
            "single_pr mode: the shared branch is left in place; if the "
            "run is refused at branch preflight, resolve it manually"
        )

    manifest.save(manifest_file)

    if not yes and ui_impl.can_prompt():
        choice = ui_impl.choose(
            f"Re-enter the factory to retry '{component_id}'?",
            ["Start", "Quit"],
            default=0,
        )
        if choice != 0:
            sys.exit(0)

    # Config assembly mirrors `ralph factory` with no flags: every phase
    # config resolves env > ralph.toml > defaults (R2.1 control plane).
    from ralph_py.contract import ContractConfig
    from ralph_py.feedforward import FeedforwardConfig
    from ralph_py.security import SecurityConfig
    from ralph_py.verify import VerifyConfig

    base_config = RalphConfig.load(root_dir)
    base_config.ui_mode = "plain"
    base_config.no_color = True
    if not base_config.prompt_file.exists():
        default_prompt = root_dir / "scripts" / "ralph" / "prompt.md"
        if default_prompt.exists():
            base_config.prompt_file = default_prompt
    _check_agent_preflight(base_config, ui_impl)

    factory_config = FactoryConfig.load(root_dir)
    factory_config.single_pr = manifest.single_pr
    factory_config.verify_config = VerifyConfig.load(root_dir)
    factory_config.security_config = SecurityConfig.load(root_dir)
    contract_resolved = ContractConfig.load(root_dir)
    factory_config.contract_config = (
        contract_resolved if contract_resolved.mode != "skip" else None
    )
    factory_config.feedforward_config = FeedforwardConfig.load(root_dir)
    factory_config.timeout_config = TimeoutConfig.load(root_dir)
    factory_config.progress_log_path = progress_log
    factory_config.force_lock = force_lock
    if keep_worktrees_on_failure:
        factory_config.keep_worktrees_on_failure = True

    result = run_factory(
        manifest, factory_config, base_config, ui_impl, root_dir,
        manifest_path=manifest_file,
    )
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
    from ralph_py.evolution import EvolutionConfig, EvolutionJournal

    root_dir = root.resolve() if root else Path.cwd()
    force_rich = os.environ.get("GUM_FORCE") == "1"
    ui_impl = get_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

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
        proposals_dir = root_dir / ".ralph" / "proposals"
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

    proposals_dir = root_dir / ".ralph" / "proposals"
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


_PROPOSAL_TITLE_RE = re.compile(r"^# (PROP-\d+): (.+)$")
_PROPOSAL_FIELD_RE = re.compile(r"^\*\*(Type|Target)\*\*: (.+)$")
_PROPOSAL_APPLIED_RE = re.compile(r"^\*\*Applied\*\*: (.+)$")


def _existing_proposal_titles(proposals_dir: Path) -> set[str]:
    """Titles of every proposal already saved to disk."""
    titles: set[str] = set()
    if not proposals_dir.is_dir():
        return titles
    for path in sorted(proposals_dir.glob("prop-*.md")):
        try:
            first_line = path.read_text().splitlines()[0]
        except (OSError, IndexError):
            continue
        m = _PROPOSAL_TITLE_RE.match(first_line)
        if m:
            titles.add(m.group(2))
    return titles


def _parse_proposal_file(path: Path) -> dict[str, str]:
    """Parse the structured fields save_proposals writes.

    Returns keys: id, title, type, target, convention (the blockquote
    body of the suggested change, "" when none), applied (timestamp or
    "")."""
    parsed = {
        "id": "", "title": "", "type": "", "target": "",
        "convention": "", "applied": "",
    }
    convention_lines: list[str] = []
    for line in path.read_text().splitlines():
        m = _PROPOSAL_TITLE_RE.match(line)
        if m and not parsed["id"]:
            parsed["id"], parsed["title"] = m.group(1), m.group(2)
            continue
        m = _PROPOSAL_FIELD_RE.match(line)
        if m:
            parsed[m.group(1).lower()] = m.group(2).strip()
            continue
        m = _PROPOSAL_APPLIED_RE.match(line)
        if m:
            parsed["applied"] = m.group(1).strip()
            continue
        if line.startswith("> "):
            convention_lines.append(line[2:].strip())
    parsed["convention"] = " ".join(convention_lines).strip()
    return parsed


def _append_to_agent_learnings(
    claude_md: Path, proposal_id: str, convention: str,
) -> bool:
    """Append one convention bullet to the end of the "## Agent
    Learnings" section of the project CLAUDE.md. Returns False (no
    write) when the file or the section is missing - the caller then
    falls back to honest manual instructions instead of guessing a
    location."""
    try:
        content = claude_md.read_text()
    except OSError:
        return False
    marker = "## Agent Learnings"
    idx = content.find(marker)
    if idx == -1:
        return False
    # End of the section = next level-2 header after it, else EOF.
    next_header = content.find("\n## ", idx + len(marker))
    insert_at = len(content) if next_header == -1 else next_header
    entry = f"- {convention} (applied from {proposal_id} by ralph evolve)\n"
    head = content[:insert_at]
    if not head.endswith("\n"):
        head += "\n"
    claude_md.write_text(head + entry + content[insert_at:])
    return True


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
    "applied" claims."""
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
        proposal = _parse_proposal_file(path)
        pid = proposal["id"] or path.stem.upper()
        if proposal["applied"]:
            ui_impl.info(
                f"{pid} already applied at {proposal['applied']}; skipping."
            )
            continue
        is_convention = (
            proposal["type"] == "computational"
            and proposal["target"] == "claude_md"
            and bool(proposal["convention"])
        )
        if not is_convention:
            ui_impl.info(f"{pid}: {proposal['title']}")
            ui_impl.warn(
                f"  Automated apply only covers convention-type proposals "
                f"(target claude_md). This one targets "
                f"'{proposal['target'] or 'unknown'}': review {path} and "
                f"apply it manually."
            )
            continue
        ui_impl.info(f"{pid}: {proposal['title']}")
        ui_impl.info(f"  Convention: {proposal['convention']}")
        if not evo_config.auto_apply_computational and not click.confirm(
            f"Append this convention to {claude_md}?", default=False,
        ):
            ui_impl.info(f"  {pid} not applied (declined).")
            continue
        if not _append_to_agent_learnings(
            claude_md, pid, proposal["convention"],
        ):
            ui_impl.err(
                f"  Could not apply {pid}: {claude_md} is missing or has "
                f"no '## Agent Learnings' section. Add the section or "
                f"apply manually from {path}."
            )
            failures += 1
            continue
        applied_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a") as f:
            f.write(f"\n**Applied**: {applied_at}\n")
        ui_impl.ok(f"  {pid} appended to {claude_md}.")
    return 1 if failures else 0


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
