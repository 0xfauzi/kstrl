"""Resolved-config reporting: (section, key, value, source) rows.

Extracted from ``cli.config_show`` (TUI surface B1) so the plain
command and the config screen render the SAME dataset. This module is
click-free on purpose; presentation (click.echo lines, DataTable rows)
stays with the callers.

Source detection for env is behavioral: a value is tagged (env) when
removing the environment changes it. That requires temporarily
clearing ``os.environ`` - a PROCESS-WIDE side effect. Never call
``build_config_report`` while another thread is running a live
command; the TUI computes its report before app.run() and refreshes
only when no session is active.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

from kstrl.config import KstrlConfig, load_toml_section, resolve_config_file


@dataclass(frozen=True)
class ConfigRow:
    section: str
    key: str
    value: str  # pre-formatted via format_config_value
    source: str  # flag | env | toml | default


@dataclass(frozen=True)
class ConfigReport:
    root_dir: Path
    toml_path: Path
    toml_exists: bool
    rows: tuple[ConfigRow, ...]


# (toml section, [(toml key, KstrlConfig field)]) - the documented
# kstrl.toml surface for the base config.
SHOW_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
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
        ("branch", "kstrl_branch"),
        ("auto_checkout", "auto_checkout"),
    ]),
    ("ui", [
        ("ascii", "ascii_only"),
        ("ui_mode", "ui_mode"),
        ("no_color", "no_color"),
    ]),
]


def normalize_ui_mode(value: str) -> str:
    normalized = (value or "auto").strip().lower()
    if normalized == "gum":
        return "rich"
    if normalized in {"plain", "off", "no", "0"}:
        return "plain"
    if normalized not in {"auto", "rich", "plain"}:
        return "auto"
    return normalized


@contextmanager
def scrubbed_environ() -> Iterator[None]:
    """Temporarily clear os.environ so a loader sees toml + defaults only.

    A field whose value changes when the environment disappears was
    env-set. PROCESS-WIDE: see the module docstring's thread warning.
    """
    saved = dict(os.environ)
    os.environ.clear()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def format_config_value(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def kstrl_config_defaults(root_dir: Path) -> KstrlConfig:
    """Built-in KstrlConfig defaults with paths anchored like load()."""
    config = KstrlConfig()
    config.prompt_file = root_dir / "scripts/kstrl/prompt.md"
    config.prd_file = root_dir / "scripts/kstrl/prd.json"
    config.progress_file = root_dir / "scripts/kstrl/progress.txt"
    config.codebase_map_file = root_dir / "scripts/kstrl/codebase_map.md"
    return config


def _phase_sections() -> list[tuple[str, Any, list[str]]]:
    """(section, loader, knob fields) - the documented kstrl.toml
    surface for the factory-phase configs. Loaders import lazily; the
    report is not on any hot path."""
    from kstrl.contract import ContractConfig
    from kstrl.evolution import EvolutionConfig
    from kstrl.factory import FactoryConfig
    from kstrl.feedforward import FeedforwardConfig
    from kstrl.knowledge import KnowledgeConfig
    from kstrl.linear import LinearConfig
    from kstrl.observability import NotifyConfig
    from kstrl.security import SecurityConfig
    from kstrl.timeout import TimeoutConfig
    from kstrl.verify import VerifyConfig

    return [
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


def build_config_report(
    root_dir: Path,
    *,
    overlay: Callable[[KstrlConfig], set[str]] | None = None,
) -> ConfigReport:
    """Resolve every documented config value with its source.

    ``overlay`` is the CLI's flag layer: it mutates the resolved
    KstrlConfig and returns the field names it overrode (tagged
    ``flag``). Raises ValueError when a loader rejects the config -
    presentation of that error is the caller's job.
    """
    toml_path = resolve_config_file(root_dir)
    phase_sections = _phase_sections()

    resolved_base = KstrlConfig.load(root_dir)
    phase_resolved = {name: loader(root_dir) for name, loader, _ in phase_sections}
    with scrubbed_environ():
        noenv_base = KstrlConfig.load(root_dir)
        phase_noenv = {
            name: loader(root_dir) for name, loader, _ in phase_sections
        }
    phase_toml_keys = {
        name: set(load_toml_section(toml_path, name).keys())
        for name, _, _ in phase_sections
    }

    defaults_base = kstrl_config_defaults(root_dir)

    # Per-field sources for KstrlConfig, computed BEFORE the flag
    # overlay (a flag replaces whatever source the value had).
    base_sources: dict[str, str] = {}
    for f in dataclass_fields(KstrlConfig):
        if getattr(resolved_base, f.name) != getattr(noenv_base, f.name):
            base_sources[f.name] = "env"
        elif getattr(noenv_base, f.name) != getattr(defaults_base, f.name):
            base_sources[f.name] = "toml"
        else:
            base_sources[f.name] = "default"

    if overlay is not None:
        for name in overlay(resolved_base):
            base_sources[name] = "flag"
    resolved_base.ui_mode = normalize_ui_mode(resolved_base.ui_mode)

    rows: list[ConfigRow] = []
    for section, keys in SHOW_SECTIONS:
        for toml_key, field_name in keys:
            rows.append(ConfigRow(
                section=section,
                key=toml_key,
                value=format_config_value(getattr(resolved_base, field_name)),
                source=base_sources[field_name],
            ))
    for section, _, knob_fields in phase_sections:
        resolved = phase_resolved[section]
        noenv = phase_noenv[section]
        toml_keys = phase_toml_keys[section]
        for field_name in knob_fields:
            value = getattr(resolved, field_name)
            if value != getattr(noenv, field_name):
                source = "env"
            elif field_name in toml_keys:
                source = "toml"
            else:
                source = "default"
            rows.append(ConfigRow(
                section=section,
                key=field_name,
                value=format_config_value(value),
                source=source,
            ))

    return ConfigReport(
        root_dir=root_dir,
        toml_path=toml_path,
        toml_exists=toml_path.exists(),
        rows=tuple(rows),
    )
