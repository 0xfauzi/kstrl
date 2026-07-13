"""Configuration handling for Ralph."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _parse_bool(value: str | None) -> bool:
    """Parse boolean from environment variable."""
    if value is None:
        return False
    return bool(re.match(r"^(1|true|yes)$", value.lower()))


def _parse_paths(value: str | None) -> list[str]:
    """Parse comma-separated paths, trimming whitespace."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _resolve_path(value: str, root_dir: Path) -> Path:
    """Resolve a path string against root_dir if relative."""
    p = Path(value)
    if p.is_absolute():
        return p
    return root_dir / p


@dataclass
class RalphConfig:
    """Configuration for Ralph agentic loop."""

    max_iterations: int = 10
    prompt_file: Path = field(default_factory=lambda: Path("scripts/ralph/prompt.md"))
    prd_file: Path = field(default_factory=lambda: Path("scripts/ralph/prd.json"))
    progress_file: Path = field(default_factory=lambda: Path("scripts/ralph/progress.txt"))
    codebase_map_file: Path = field(
        default_factory=lambda: Path("scripts/ralph/codebase_map.md")
    )
    sleep_seconds: float = 2.0
    interactive: bool = False
    allowed_paths: list[str] = field(default_factory=list)

    # Branch config - None means use PRD, "" means skip
    ralph_branch: str | None = None
    ralph_branch_explicit: bool = False  # Was RALPH_BRANCH env var set?
    auto_checkout: bool = True

    # Agent config
    agent_cmd: str | None = None
    model: str | None = None
    model_reasoning_effort: str | None = None
    agent_type: str | None = None  # "claude-code", "codex", "auto", or None

    # Timeouts live in ralph_py.timeout.TimeoutConfig (the single source
    # for agent_iteration / component_total; R0.1). RalphConfig used to
    # duplicate them as dead fields - deliberately deleted, do not re-add.

    # UI config
    ui_mode: str = "auto"  # auto|rich|plain
    no_color: bool = False
    ascii_only: bool = False

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> RalphConfig:
        """Load configuration from environment variables only."""
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        # Default file paths are resolved against root_dir so the config is
        # immediately usable regardless of cwd at the call site.
        config.prompt_file = root_dir / "scripts/ralph/prompt.md"
        config.prd_file = root_dir / "scripts/ralph/prd.json"
        config.progress_file = root_dir / "scripts/ralph/progress.txt"
        config.codebase_map_file = root_dir / "scripts/ralph/codebase_map.md"
        _apply_env_overrides(config, root_dir)
        return config

    @classmethod
    def from_toml(cls, toml_path: Path, root_dir: Path | None = None) -> RalphConfig:
        """Load configuration from a ralph.toml file (no env overlay)."""
        if root_dir is None:
            root_dir = toml_path.parent if toml_path.is_absolute() else Path.cwd()
        config = cls()
        config.prompt_file = root_dir / "scripts/ralph/prompt.md"
        config.prd_file = root_dir / "scripts/ralph/prd.json"
        config.progress_file = root_dir / "scripts/ralph/progress.txt"
        config.codebase_map_file = root_dir / "scripts/ralph/codebase_map.md"
        if toml_path.exists():
            _apply_toml_overrides(config, toml_path, root_dir)
        return config

    @classmethod
    def load(
        cls,
        root_dir: Path | None = None,
        toml_path: Path | None = None,
    ) -> RalphConfig:
        """Load configuration with precedence: env > toml > dataclass defaults.

        If ``toml_path`` is omitted, ``<root_dir>/ralph.toml`` is auto-discovered.
        Missing TOML file is fine (defaults are used). Malformed TOML raises.
        """
        if root_dir is None:
            root_dir = Path.cwd()
        if toml_path is None:
            toml_path = root_dir / "ralph.toml"

        config = cls()
        config.prompt_file = root_dir / "scripts/ralph/prompt.md"
        config.prd_file = root_dir / "scripts/ralph/prd.json"
        config.progress_file = root_dir / "scripts/ralph/progress.txt"
        config.codebase_map_file = root_dir / "scripts/ralph/codebase_map.md"

        if toml_path.exists():
            _apply_toml_overrides(config, toml_path, root_dir)
        _apply_env_overrides(config, root_dir)
        return config

    def validate(self) -> list[str]:
        """Validate configuration, returning list of errors."""
        errors: list[str] = []

        if self.max_iterations < 0:
            errors.append(f"MAX_ITERATIONS must be non-negative (got: {self.max_iterations})")

        if not self.prompt_file.exists():
            errors.append(f"Prompt file not found: {self.prompt_file}")

        return errors


def _load_toml(path: Path) -> dict[str, Any]:
    """Load and parse a TOML file. Raises ValueError on malformed input."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in {path}: {exc}") from exc


def load_toml_section(toml_path: Path, section: str) -> dict[str, Any]:
    """Read a named section from a ralph.toml file.

    Shared by every config dataclass that has a corresponding
    ``[section]`` in the canonical ralph.toml. Returns ``{}`` when the
    file or the section is absent; raises ``ValueError`` with a clear
    message when the file is malformed so every loader behaves
    consistently. Sub-section keys that are not dicts (e.g. someone
    wrote ``factory = "hi"`` instead of ``[factory]``) return ``{}``
    rather than crashing later in the per-key cast.
    """
    if not toml_path.exists():
        return {}
    data = _load_toml(toml_path)
    section_data = data.get(section, {})
    if not isinstance(section_data, dict):
        return {}
    return section_data


def _apply_toml_overrides(
    config: RalphConfig, toml_path: Path, root_dir: Path,
) -> None:
    """Mutate config in place from a ralph.toml file.

    Maps the documented section structure (agent, run, paths, git, ui) onto
    the flat RalphConfig dataclass. Unknown keys are silently ignored.
    """
    data = _load_toml(toml_path)

    agent = data.get("agent")
    if isinstance(agent, dict):
        agent_type = agent.get("type")
        if isinstance(agent_type, str) and agent_type:
            config.agent_type = agent_type
        command = agent.get("command")
        if isinstance(command, str) and command:
            config.agent_cmd = command
        model = agent.get("model")
        if isinstance(model, str) and model:
            config.model = model
        reasoning = agent.get("reasoning_effort")
        if isinstance(reasoning, str) and reasoning:
            config.model_reasoning_effort = reasoning

    run = data.get("run")
    if isinstance(run, dict):
        if "max_iterations" in run:
            config.max_iterations = int(run["max_iterations"])
        if "sleep_seconds" in run:
            config.sleep_seconds = float(run["sleep_seconds"])
        if "interactive" in run:
            config.interactive = bool(run["interactive"])

    paths = data.get("paths")
    if isinstance(paths, dict):
        if isinstance(paths.get("prompt"), str) and paths["prompt"]:
            config.prompt_file = _resolve_path(paths["prompt"], root_dir)
        if isinstance(paths.get("prd"), str) and paths["prd"]:
            config.prd_file = _resolve_path(paths["prd"], root_dir)
        if isinstance(paths.get("progress"), str) and paths["progress"]:
            config.progress_file = _resolve_path(paths["progress"], root_dir)
        if isinstance(paths.get("codebase_map"), str) and paths["codebase_map"]:
            config.codebase_map_file = _resolve_path(paths["codebase_map"], root_dir)
        allowed = paths.get("allowed")
        if isinstance(allowed, list):
            config.allowed_paths = [str(p) for p in allowed if isinstance(p, str)]

    git_section = data.get("git")
    if isinstance(git_section, dict):
        if "branch" in git_section:
            branch = git_section["branch"]
            # Only treat the TOML branch as an explicit override when it
            # is non-empty. `branch = ""` in the shipped example means
            # "no override, fall back to PRD branchName", whereas the env
            # var `RALPH_BRANCH=""` (handled below) keeps its historical
            # meaning of "explicit skip".
            if isinstance(branch, str) and branch:
                config.ralph_branch = branch
                config.ralph_branch_explicit = True
        if "auto_checkout" in git_section:
            config.auto_checkout = bool(git_section["auto_checkout"])

    ui = data.get("ui")
    if isinstance(ui, dict):
        if "ascii" in ui:
            config.ascii_only = bool(ui["ascii"])


def _apply_env_overrides(config: RalphConfig, root_dir: Path) -> None:
    """Mutate config in place from environment variables.

    Only env vars that are explicitly set in the environment are applied -
    unset vars leave the existing config value untouched.
    """
    if "MAX_ITERATIONS" in os.environ:
        config.max_iterations = int(os.environ["MAX_ITERATIONS"])
    if "PROMPT_FILE" in os.environ:
        config.prompt_file = _resolve_path(os.environ["PROMPT_FILE"], root_dir)
    if "PRD_FILE" in os.environ:
        config.prd_file = _resolve_path(os.environ["PRD_FILE"], root_dir)
    if "PROGRESS_FILE" in os.environ:
        config.progress_file = _resolve_path(os.environ["PROGRESS_FILE"], root_dir)
    if "CODEBASE_MAP_FILE" in os.environ:
        config.codebase_map_file = _resolve_path(
            os.environ["CODEBASE_MAP_FILE"], root_dir
        )
    if "SLEEP_SECONDS" in os.environ:
        config.sleep_seconds = float(os.environ["SLEEP_SECONDS"])
    if "INTERACTIVE" in os.environ:
        config.interactive = _parse_bool(os.environ.get("INTERACTIVE"))
    if "ALLOWED_PATHS" in os.environ:
        config.allowed_paths = _parse_paths(os.environ.get("ALLOWED_PATHS"))
    if "RALPH_BRANCH" in os.environ:
        config.ralph_branch = os.environ["RALPH_BRANCH"]
        config.ralph_branch_explicit = True
    if "RALPH_AUTO_CHECKOUT" in os.environ:
        config.auto_checkout = _parse_bool(os.environ.get("RALPH_AUTO_CHECKOUT"))
    if "AGENT_CMD" in os.environ:
        config.agent_cmd = os.environ["AGENT_CMD"]
    if "MODEL" in os.environ:
        config.model = os.environ["MODEL"]
    if "MODEL_REASONING_EFFORT" in os.environ:
        config.model_reasoning_effort = os.environ["MODEL_REASONING_EFFORT"]
    if "RALPH_AGENT_TYPE" in os.environ:
        config.agent_type = os.environ["RALPH_AGENT_TYPE"]
    if "RALPH_UI" in os.environ:
        config.ui_mode = os.environ["RALPH_UI"]
    if "NO_COLOR" in os.environ:
        config.no_color = True
    if "RALPH_ASCII" in os.environ:
        config.ascii_only = _parse_bool(os.environ.get("RALPH_ASCII"))
