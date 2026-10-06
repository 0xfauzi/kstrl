"""`ks init` scaffolds a kstrl.toml whose every key a loader reads.

Moved out of ``tests/test_config_control_plane.py`` on #603, when adding
two ``[factory]`` keys took that file past the 800-line ratchet. The
tests are unchanged.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

import kstrl.cli as cli_mod
from kstrl.evolution import EvolutionConfig
from kstrl.factory import FactoryConfig
from kstrl.feedforward import CodebaseScanConfig
from kstrl.init_cmd import DEFAULT_KSTRL_TOML
from kstrl.knowledge import KnowledgeConfig
from kstrl.verify import VerifyConfig

EXPECTED_SCAFFOLD_SECTIONS = {
    "agent",
    "run",
    "paths",
    "git",
    "ui",
    "factory",
    "verify",
    "policy",
    "autonomy",
    "divergence",
    "inbox",
    "security",
    "contract",
    "codebase_scan",
    "knowledge",
    "evolution",
    "timeout",
    "queue",
    "serve",
    "intake_github",
}

#: The uncommented scaffold also holds the `[stack]` table, which the scaffold
#: ships commented out: kstrl runs nothing until a person fills it in.
UNCOMMENTED_SECTIONS = EXPECTED_SCAFFOLD_SECTIONS | {"stack"}

# Keys each loader actually consumes, mirrored by hand so a typo'd or
# phantom key in the scaffold fails membership below.
EXPECTED_SCAFFOLD_KEYS = {
    "agent": {"type", "command", "model", "reasoning_effort"},
    "run": {"max_iterations", "sleep_seconds", "interactive"},
    "paths": {"prompt", "prd", "progress", "codebase_map", "golden_patterns", "memory", "allowed"},
    "git": {"branch", "auto_checkout"},
    "ui": {"ascii"},
    "factory": {
        "max_parallel",
        "max_retries",
        "retry_delay",
        "use_worktrees",
        "single_pr",
        "create_prs",
        "review_mode",
        "review_timeout_seconds",
        "architect_timeout_seconds",
        "merge_timeout",
        "max_adversarial_calls",
        "max_total_tokens",
        "max_cost_usd",
        "pause_before_pr_merge",
    },
    "stack": {"instructions", "setup", "env", "checks"},
    "verify": {
        "check_diff_scope",
        "check_bad_patterns",
        "subprocess_timeout",
        "require_self_critique",
        "self_critique_min_bullets",
        "progress_file_path",
    },
    "policy": {
        "enabled",
        "paths_deny",
        "max_files_changed",
        "max_lines_changed",
        "deps_allow_new",
        "secret_patterns",
        "enforcement_paths_extra",
        "license_allow",
        "license_deny_partial",
        "license_unresolved",
        "license_use_network",
        "deploy",
    },
    "autonomy": {"enabled", "max_level"},
    "divergence": {
        "mode",
        "growth_steps",
    },
    "inbox": {
        "enabled",
        "open_item_cap",
        "snooze_hours",
        "notify_action_required",
    },
    "security": {
        "mode",
        "fail_threshold",
        "timeout_seconds",
        "agent_cmd",
        "agent_type",
        "model",
    },
    "contract": {"mode", "timeout"},
    "codebase_scan": {
        "enabled",
        "module_map",
        "max_context_tokens",
    },
    "knowledge": {
        "enabled",
        "max_core_tokens",
        "max_dependency_tokens",
        "max_sibling_tokens",
        "distill_timeout_seconds",
        "distill_model",
        "max_facts_per_distill",
        "dependency_scope",
    },
    "evolution": {
        "enabled",
        "journal_path",
        "experiments_path",
        "min_pattern_frequency",
        "lookback_runs",
    },
    "timeout": {"agent_iteration", "component_total", "scheduler_backstop_margin"},
    "queue": {"max_attempts", "lease_ttl_seconds"},
    "serve": {
        "poll_interval_seconds",
        "daily_budget_usd",
        "max_consecutive_poison",
        "caffeinate",
        "factory_timeout_seconds",
        "allow_uncovered_cost",
        "max_open_prs",
    },
    "intake_github": {
        "enabled",
        "repo",
        "queued_label",
        "label_prefix",
        "max_items_per_sync",
        "default_priority",
        "comment_on_result",
        "dry_run",
        "timeout_seconds",
        "allowed_actors",
        "steer_enabled",
    },
}


def _uncomment_scaffold(text: str) -> str:
    """Uncomment every `# key = value` line and every `# [stack...]` header."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# [stack"):
            lines.append(stripped[2:].split("#", 1)[0].rstrip())
        elif stripped in ('# instructions = ""', '# tests = ""') or stripped.startswith(
            ('# instructions = "" ', '# tests = "" ')
        ):
            # The stack's required keys ship empty, which a stack refuses:
            # fill them so the loaders see a stack that parses.
            key = stripped[2:].split(" =", 1)[0]
            lines.append(
                f'{key} = "{"Built by a kstrl test." if key == "instructions" else "true"}"'
            )
        elif (
            stripped.startswith("# ")
            and " = " in stripped
            and not (stripped.startswith(("# Resolved", "# kstrl")) or stripped[2:3].isupper())
        ):
            lines.append(stripped[2:])
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


class TestInitScaffold:
    def test_init_creates_kstrl_toml(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli_mod.cli, ["init", str(tmp_path), "--ui", "plain"])
        assert result.exit_code == 0, result.output
        toml_path = tmp_path / "kstrl.toml"
        assert toml_path.exists()
        data = tomllib.loads(toml_path.read_text())
        assert set(data.keys()) == EXPECTED_SCAFFOLD_SECTIONS
        # All keys commented out: scaffolding changes no effective value.
        assert all(section == {} for section in data.values())

    def test_the_daemon_sections_scaffold_their_real_defaults(self, tmp_path: Path) -> None:
        """#452: the scaffold had no [queue], [serve] or [intake_github]
        section. Each line shows the built-in default, so uncommenting
        every one of them changes no value."""
        from kstrl.intake_github import GitHubIntakeConfig
        from kstrl.serve import ServeConfig
        from kstrl.workqueue import QueueConfig

        (tmp_path / "kstrl.toml").write_text(
            _uncomment_scaffold(DEFAULT_KSTRL_TOML), encoding="utf-8"
        )
        assert QueueConfig.load(tmp_path) == QueueConfig()
        assert ServeConfig.load(tmp_path) == ServeConfig()
        assert GitHubIntakeConfig.load(tmp_path) == GitHubIntakeConfig()

    def test_the_header_says_where_the_other_keys_are(self) -> None:
        """#452: the header claimed every key was listed, and most
        sections were not. It now names the keys it lists and where the
        rest are."""
        header = DEFAULT_KSTRL_TOML.split("\n\n", 1)[0]
        assert "Every key" not in header
        assert "configuration reference" in header

    def test_init_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_parallel = 2\n")
        runner = CliRunner()
        result = runner.invoke(cli_mod.cli, ["init", str(tmp_path), "--ui", "plain"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "kstrl.toml").read_text() == "[factory]\nmax_parallel = 2\n"

    def test_scaffold_keys_are_real(self) -> None:
        # Uncomment every key and check each against the loader key sets;
        # a scaffold key the loaders do not read fails here.
        data = tomllib.loads(_uncomment_scaffold(DEFAULT_KSTRL_TOML))
        assert set(data.keys()) == UNCOMMENTED_SECTIONS
        for section, keys in data.items():
            unexpected = set(keys) - EXPECTED_SCAFFOLD_KEYS[section]
            assert not unexpected, (
                f"[{section}] scaffold keys not consumed by any loader: {sorted(unexpected)}"
            )

    def test_the_uncommented_scaffold_passes_the_entry_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#525: every scaffold key is one a loader reads, so uncommenting
        any of them never meets the entry check's refusal."""
        from kstrl.config_preflight import collect_config_problems

        for name in [k for k in os.environ if k.startswith("KSTRL_")]:
            monkeypatch.delenv(name)
        (tmp_path / "kstrl.toml").write_text(
            _uncomment_scaffold(DEFAULT_KSTRL_TOML), encoding="utf-8"
        )
        assert collect_config_problems(tmp_path, lambda _message: None) == []

    def test_uncommented_scaffold_loads_through_every_loader(self, tmp_path: Path) -> None:
        from kstrl.config import KstrlConfig
        from kstrl.contract import ContractConfig
        from kstrl.security import SecurityConfig
        from kstrl.timeout import TimeoutConfig

        (tmp_path / "kstrl.toml").write_text(_uncomment_scaffold(DEFAULT_KSTRL_TOML))
        KstrlConfig.load(tmp_path)
        FactoryConfig.load(tmp_path)
        VerifyConfig.load(tmp_path)
        SecurityConfig.load(tmp_path)
        ContractConfig.load(tmp_path)
        CodebaseScanConfig.load(tmp_path)
        EvolutionConfig.load(tmp_path)
        KnowledgeConfig.load(tmp_path)
        TimeoutConfig.load(tmp_path)
