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
    #: Sections whose loader rejected the configuration, so no row could
    #: be built for them. The report is still returned: see
    #: :func:`build_config_report`.
    unresolved: tuple[str, ...] = ()


# (toml section, [(toml key, KstrlConfig field)]) - the documented
# kstrl.toml surface for the base config.
SHOW_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "agent",
        [
            ("type", "agent_type"),
            ("command", "agent_cmd"),
            ("model", "model"),
            ("reasoning_effort", "model_reasoning_effort"),
        ],
    ),
    (
        "run",
        [
            ("max_iterations", "max_iterations"),
            ("sleep_seconds", "sleep_seconds"),
            ("interactive", "interactive"),
        ],
    ),
    (
        "paths",
        [
            ("prompt", "prompt_file"),
            ("prd", "prd_file"),
            ("progress", "progress_file"),
            ("codebase_map", "codebase_map_file"),
            ("allowed", "allowed_paths"),
        ],
    ),
    (
        "git",
        [
            ("branch", "kstrl_branch"),
            ("auto_checkout", "auto_checkout"),
        ],
    ),
    (
        "ui",
        [
            ("ascii", "ascii_only"),
            ("ui_mode", "ui_mode"),
            ("no_color", "no_color"),
        ],
    ),
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


# Rows whose None means something more specific than "no value", and
# where printing bare None (or, worse, a materialized default) would
# misreport what a run will do. R8 review finding 1: `ks config show`
# printed <root>/scripts/kstrl/progress.txt as the effective progress
# path, which is exactly the out-of-scope location the factory no longer
# uses - an operator copying it into kstrl.toml would recreate the
# defect the sentinel removed.
UNSET_RENDERINGS: dict[tuple[str, str], str] = {
    ("paths", "progress"): "<unset: each component writes beside its own PRD>",
}


def format_config_value(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def format_row_value(section: str, key: str, value: Any) -> str:
    """``format_config_value`` plus the per-row unset renderings."""
    if value is None and (section, key) in UNSET_RENDERINGS:
        return UNSET_RENDERINGS[(section, key)]
    return format_config_value(value)


def kstrl_config_defaults(root_dir: Path) -> KstrlConfig:
    """Built-in KstrlConfig defaults with paths anchored like load()."""
    config = KstrlConfig()
    config.prompt_file = root_dir / "scripts/kstrl/prompt.md"
    config.prd_file = root_dir / "scripts/kstrl/prd.json"
    config.codebase_map_file = root_dir / "scripts/kstrl/codebase_map.md"
    return config


def _phase_sections() -> list[tuple[str, Any, list[str]]]:
    """(section, loader, knob fields) - the documented kstrl.toml
    surface for the factory-phase configs. Loaders import lazily; the
    report is not on any hot path.

    The LOADER for each section comes from
    ``config_preflight.config_sections()``, which is the one registry of
    section to loader and is kept complete by an AST test. Only the knob
    lists are local, because they are about what this report RENDERS
    rather than about what a section is loaded by. A second copy of the
    loader table is what the comment on ``verify`` below is already an
    account of, one level down.
    """
    from kstrl.config_preflight import config_sections
    from kstrl.timeout import TimeoutConfig
    from kstrl.verify import VerifyConfig

    loaders = {name: entry.loader for entry in config_sections() for name in entry.sections}

    knobs: list[tuple[str, list[str]]] = [
        (
            "factory",
            [
                "max_parallel",
                "max_retries",
                "retry_delay",
                "use_worktrees",
                "single_pr",
                "create_prs",
                "review_mode",
                "merge_timeout",
                "max_adversarial_calls",
                "max_total_tokens",
                "max_cost_usd",
                "pause_before_pr_merge",
                "progress_log_enabled",
                "keep_worktrees_on_failure",
            ],
        ),
        # Derived, not hand-listed. The hand-written copy of this list
        # went stale the moment #258 added the three `*_tool` keys: they
        # reached VerifyConfig, gen_docs, the README and env-vars.md and
        # not this list, so `ks config` and the config screen showed no
        # row for the one setting an operator reaches for when a gate is
        # parsed by the wrong toolchain. Every scalar field of
        # VerifyConfig IS a documented kstrl.toml key, which gen_docs
        # already enforces, so the field list is the key list and a
        # second copy of it can only ever be wrong.
        ("verify", [f.name for f in dataclass_fields(VerifyConfig)]),
        (
            "security",
            [
                "mode",
                "fail_threshold",
                "timeout_seconds",
                "agent_cmd",
                "agent_type",
                "model",
            ],
        ),
        ("contract", ["mode", "test_command", "timeout"]),
        (
            "feedforward",
            [
                "enabled",
                "module_map",
                "public_interfaces",
                "dependency_graph",
                "conventions",
                "max_context_tokens",
            ],
        ),
        (
            "knowledge",
            [
                "enabled",
                "max_core_tokens",
                "max_dependency_tokens",
                "max_sibling_tokens",
                "distill_timeout_seconds",
                "distill_model",
                "max_facts_per_distill",
                "dependency_scope",
            ],
        ),
        (
            "evolution",
            [
                "enabled",
                "journal_path",
                "experiments_path",
                "min_pattern_frequency",
                "lookback_runs",
                "auto_propose",
                "auto_apply_computational",
            ],
        ),
        ("timeout", [f.name for f in dataclass_fields(TimeoutConfig)]),
        (
            "notify",
            [
                "on_complete",
                "on_first_failure",
                "on_inbox_item",
                "hook_timeout",
            ],
        ),
        (
            "linear",
            [
                "enabled",
                "team_id",
                "token_env",
                "auth_mode",
                "api_url",
                "dry_run",
                "timeout_seconds",
                "min_request_interval",
            ],
        ),
    ]
    return [(name, loaders[name], fields) for name, fields in knobs]


def _base_sources(
    resolved: KstrlConfig, noenv: KstrlConfig, defaults: KstrlConfig
) -> dict[str, str]:
    """Per-field source for KstrlConfig, computed BEFORE the flag overlay.

    A flag replaces whatever source the value had, so it has to be
    applied after this, not folded into it.
    """
    sources: dict[str, str] = {}
    for f in dataclass_fields(KstrlConfig):
        if getattr(resolved, f.name) != getattr(noenv, f.name):
            sources[f.name] = "env"
        elif getattr(noenv, f.name) != getattr(defaults, f.name):
            sources[f.name] = "toml"
        else:
            sources[f.name] = "default"
    return sources


def _base_rows(resolved: KstrlConfig, sources: dict[str, str]) -> list[ConfigRow]:
    """Rows for the sections KstrlConfig fans out over."""
    return [
        ConfigRow(
            section=section,
            key=toml_key,
            value=format_row_value(section, toml_key, getattr(resolved, field_name)),
            source=sources[field_name],
        )
        for section, keys in SHOW_SECTIONS
        for toml_key, field_name in keys
    ]


def _phase_rows(
    section: str,
    knob_fields: list[str],
    resolved: Any,
    noenv: Any,
    toml_keys: set[str],
) -> list[ConfigRow]:
    """Rows for one phase config, each tagged with where its value came from."""
    rows: list[ConfigRow] = []
    for field_name in knob_fields:
        value = getattr(resolved, field_name)
        if value != getattr(noenv, field_name):
            source = "env"
        elif field_name in toml_keys:
            source = "toml"
        else:
            source = "default"
        rows.append(
            ConfigRow(
                section=section,
                key=field_name,
                value=format_config_value(value),
                source=source,
            )
        )
    return rows


def build_config_report(
    root_dir: Path,
    *,
    overlay: Callable[[KstrlConfig], set[str]] | None = None,
) -> ConfigReport:
    """Resolve every documented config value with its source.

    ``overlay`` is the CLI's flag layer: it mutates the resolved
    KstrlConfig and returns the field names it overrode (tagged
    ``flag``).

    A PHASE section whose loader rejects the config costs that section's
    rows and is named in ``unresolved``; it does not cost the report.
    One typo used to abort the whole thing before a single row printed,
    which made ``ks config show`` the LEAST informative surface in the
    CLI at the exact moment it is the one an operator opens: every other
    command named the section, the key and the value, and this one said
    ``error: could not convert string to float: 'many'``.

    The base config still raises, because a malformed document or a bad
    ``[run]`` value leaves nothing to render beside the failure.
    """
    toml_path = resolve_config_file(root_dir)
    phase_sections = _phase_sections()
    unresolved: list[str] = []

    def _resolve(name: str, loader: Any) -> Any:
        try:
            return loader(root_dir)
        except (ValueError, TypeError, RuntimeError):
            # The same rejection set config_preflight._REJECTIONS states.
            # WHY it was rejected is that module's job to report; this
            # one records only that the rows cannot be built.
            if name not in unresolved:
                unresolved.append(name)
            return None

    resolved_base = KstrlConfig.load(root_dir)
    phase_resolved = {name: _resolve(name, loader) for name, loader, _ in phase_sections}
    with scrubbed_environ():
        noenv_base = KstrlConfig.load(root_dir)
        phase_noenv = {name: _resolve(name, loader) for name, loader, _ in phase_sections}
    phase_toml_keys = {
        name: set(load_toml_section(toml_path, name).keys()) for name, _, _ in phase_sections
    }

    defaults_base = kstrl_config_defaults(root_dir)

    base_sources = _base_sources(resolved_base, noenv_base, defaults_base)
    if overlay is not None:
        for name in overlay(resolved_base):
            base_sources[name] = "flag"
    resolved_base.ui_mode = normalize_ui_mode(resolved_base.ui_mode)

    rows = _base_rows(resolved_base, base_sources)
    for section, _, knob_fields in phase_sections:
        if section in unresolved:
            continue
        rows.extend(
            _phase_rows(
                section,
                knob_fields,
                phase_resolved[section],
                phase_noenv[section],
                phase_toml_keys[section],
            )
        )

    return ConfigReport(
        root_dir=root_dir,
        toml_path=toml_path,
        toml_exists=toml_path.exists(),
        rows=tuple(rows),
        unresolved=tuple(unresolved),
    )
