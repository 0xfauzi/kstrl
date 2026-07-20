"""Tests for the kstrl.config TOML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.config import KstrlConfig


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# from_toml: mapping table
# ---------------------------------------------------------------------------


def test_from_toml_maps_agent_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "ralph.toml"
    _write_toml(
        toml_path,
        """
[agent]
type = "codex"
command = "my-agent --stdin"
model = "o3"
reasoning_effort = "high"
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.agent_type == "codex"
    assert config.agent_cmd == "my-agent --stdin"
    assert config.model == "o3"
    assert config.model_reasoning_effort == "high"


def test_from_toml_maps_run_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "ralph.toml"
    _write_toml(
        toml_path,
        """
[run]
max_iterations = 42
sleep_seconds = 5
interactive = true
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.max_iterations == 42
    assert config.sleep_seconds == 5.0
    assert config.interactive is True


def test_from_toml_maps_paths_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "ralph.toml"
    _write_toml(
        toml_path,
        """
[paths]
prompt = "custom/prompt.md"
prd = "custom/prd.json"
progress = "custom/progress.txt"
codebase_map = "custom/map.md"
allowed = ["src/", "tests/"]
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.prompt_file == tmp_path / "custom/prompt.md"
    assert config.prd_file == tmp_path / "custom/prd.json"
    assert config.progress_file == tmp_path / "custom/progress.txt"
    assert config.codebase_map_file == tmp_path / "custom/map.md"
    assert config.allowed_paths == ["src/", "tests/"]


def test_from_toml_maps_git_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "ralph.toml"
    _write_toml(
        toml_path,
        """
[git]
branch = "feature/x"
auto_checkout = false
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.kstrl_branch == "feature/x"
    assert config.kstrl_branch_explicit is True
    assert config.auto_checkout is False


def test_from_toml_maps_ui_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "ralph.toml"
    _write_toml(
        toml_path,
        """
[ui]
ascii = true
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.ascii_only is True


def test_from_toml_empty_file_uses_defaults(tmp_path: Path) -> None:
    toml_path = tmp_path / "ralph.toml"
    _write_toml(toml_path, "")
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.max_iterations == 10
    assert config.sleep_seconds == 2.0
    assert config.agent_type is None


def test_from_toml_missing_file_uses_defaults(tmp_path: Path) -> None:
    config = KstrlConfig.from_toml(tmp_path / "nonexistent.toml", tmp_path)
    assert config.max_iterations == 10
    assert config.agent_cmd is None


def test_from_toml_malformed_raises_clear_error(tmp_path: Path) -> None:
    toml_path = tmp_path / "ralph.toml"
    _write_toml(toml_path, "this is not = valid = toml = [\n")
    with pytest.raises(ValueError, match="Invalid TOML"):
        KstrlConfig.from_toml(toml_path, tmp_path)


def test_from_toml_resolves_absolute_paths(tmp_path: Path) -> None:
    toml_path = tmp_path / "ralph.toml"
    abs_prompt = tmp_path / "elsewhere" / "p.md"
    _write_toml(
        toml_path,
        f"""
[paths]
prompt = "{abs_prompt}"
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.prompt_file == abs_prompt


def test_from_toml_ignores_unknown_keys(tmp_path: Path) -> None:
    toml_path = tmp_path / "ralph.toml"
    _write_toml(
        toml_path,
        """
[agent]
type = "claude"
unknown_field = "ignored"

[unknown_section]
foo = "bar"
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.agent_type == "claude"


# ---------------------------------------------------------------------------
# load: env > toml > defaults precedence
# ---------------------------------------------------------------------------


def test_load_env_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml_path = tmp_path / "ralph.toml"
    _write_toml(
        toml_path,
        """
[run]
max_iterations = 25

[agent]
model = "sonnet"
""",
    )
    monkeypatch.setenv("MAX_ITERATIONS", "99")
    monkeypatch.setenv("MODEL", "opus")
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 99
    assert config.model == "opus"


def test_load_toml_wins_over_defaults_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear env vars that might leak from the test environment
    for var in ("MAX_ITERATIONS", "MODEL", "SLEEP_SECONDS", "INTERACTIVE"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "ralph.toml"
    _write_toml(
        toml_path,
        """
[run]
max_iterations = 25
""",
    )
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 25


def test_load_defaults_when_no_toml_and_no_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "MAX_ITERATIONS", "MODEL", "SLEEP_SECONDS", "INTERACTIVE",
        "ALLOWED_PATHS", "AGENT_CMD", "MODEL_REASONING_EFFORT",
        "RALPH_AGENT_TYPE", "RALPH_BRANCH", "RALPH_ASCII",
    ):
        monkeypatch.delenv(var, raising=False)
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 10
    assert config.sleep_seconds == 2.0
    assert config.agent_type is None
    assert config.agent_cmd is None
    assert config.kstrl_branch is None
    assert config.kstrl_branch_explicit is False


def test_load_auto_discovers_ralph_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("MAX_ITERATIONS",):
        monkeypatch.delenv(var, raising=False)
    _write_toml(
        tmp_path / "ralph.toml",
        """
[run]
max_iterations = 7
""",
    )
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 7


def test_load_missing_toml_falls_back_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("MAX_ITERATIONS",):
        monkeypatch.delenv(var, raising=False)
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 10


def test_load_env_branch_marks_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RALPH_BRANCH", "")
    config = KstrlConfig.load(tmp_path)
    assert config.kstrl_branch == ""
    assert config.kstrl_branch_explicit is True


def test_load_env_paths_resolved_against_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_FILE", "custom/prompt.md")
    config = KstrlConfig.load(tmp_path)
    assert config.prompt_file == tmp_path / "custom/prompt.md"


def test_load_toml_empty_branch_does_not_mark_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kstrl.toml.example documents `branch = ""` as 'empty = use PRD
    branchName'. An empty TOML branch must therefore NOT mark explicit,
    so loop._determine_branch falls through to PRD lookup instead of
    skipping checkout. Env var RALPH_BRANCH="" retains its historical
    explicit-skip meaning - that path is tested elsewhere."""
    for var in ("RALPH_BRANCH",):
        monkeypatch.delenv(var, raising=False)
    _write_toml(
        tmp_path / "ralph.toml",
        """
[git]
branch = ""
""",
    )
    config = KstrlConfig.load(tmp_path)
    assert config.kstrl_branch is None
    assert config.kstrl_branch_explicit is False


def test_load_toml_nonempty_branch_marks_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("RALPH_BRANCH",):
        monkeypatch.delenv(var, raising=False)
    _write_toml(
        tmp_path / "ralph.toml",
        """
[git]
branch = "feature/foo"
""",
    )
    config = KstrlConfig.load(tmp_path)
    assert config.kstrl_branch == "feature/foo"
    assert config.kstrl_branch_explicit is True


# ---------------------------------------------------------------------------
# from_env: backwards compatibility
# ---------------------------------------------------------------------------


def test_from_env_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_ITERATIONS", "13")
    monkeypatch.setenv("MODEL", "haiku")
    config = KstrlConfig.from_env(tmp_path)
    assert config.max_iterations == 13
    assert config.model == "haiku"
    assert config.prompt_file == tmp_path / "scripts/kstrl/prompt.md"


def test_from_env_does_not_read_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("MAX_ITERATIONS",):
        monkeypatch.delenv(var, raising=False)
    _write_toml(
        tmp_path / "ralph.toml",
        """
[run]
max_iterations = 999
""",
    )
    # Change cwd to tmp_path so any auto-discovery would pick up the toml
    monkeypatch.chdir(tmp_path)
    config = KstrlConfig.from_env(tmp_path)
    # from_env must ignore toml
    assert config.max_iterations == 10
