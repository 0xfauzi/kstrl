"""The settings of kstrl.

The `[agent]`, `[run]` and `[paths]` sections, the environment variables,
the number rules, the TOML parse and the rules of a config section.
"""

from __future__ import annotations

from kstrl.config.config import (
    AGENT_TYPE_ALIASES,
    KSTRL_SECTION,
    VALID_AGENT_TYPES,
    KstrlConfig,
    canonical_agent_type,
    component_progress_path,
    parse_bool,
    parse_paths,
    reconcile_progress_config,
    relative_to_root,
    resolve_config_file,
    validate_agent_type,
)
from kstrl.config.config_keys import (
    RETIRED_ENV_VALUES,
    RETIRED_ENV_VARS,
    RETIRED_KEYS,
    RETIRED_SECTIONS,
    RETIRED_VALUES,
    STRING_KEYS,
)
from kstrl.config.config_numbers import (
    LIMIT_FLOAT,
    LIMIT_INT,
    SIGNED,
    BudgetConfigError,
    check_number,
    check_numbers,
)
from kstrl.config.config_resolve import (
    REJECTIONS,
    SURFACE_REJECTIONS,
    environ_lock,
    raise_if_defect,
    resolve_or_report,
    scrubbed_environ,
    section_detail,
)
from kstrl.config.config_section import ConfigSection
from kstrl.config.config_toml import (
    ConfigError,
    RecordedTable,
    load_toml_document,
    load_toml_section,
    record_reads,
    section_table,
    toml_parse_scope,
)

__all__ = [
    "AGENT_TYPE_ALIASES",
    "BudgetConfigError",
    "ConfigError",
    "ConfigSection",
    "KSTRL_SECTION",
    "KstrlConfig",
    "LIMIT_FLOAT",
    "LIMIT_INT",
    "REJECTIONS",
    "RETIRED_ENV_VALUES",
    "RETIRED_ENV_VARS",
    "RETIRED_KEYS",
    "RETIRED_SECTIONS",
    "RETIRED_VALUES",
    "RecordedTable",
    "SIGNED",
    "STRING_KEYS",
    "SURFACE_REJECTIONS",
    "VALID_AGENT_TYPES",
    "canonical_agent_type",
    "check_number",
    "check_numbers",
    "component_progress_path",
    "environ_lock",
    "load_toml_document",
    "load_toml_section",
    "parse_bool",
    "parse_paths",
    "raise_if_defect",
    "reconcile_progress_config",
    "record_reads",
    "relative_to_root",
    "resolve_config_file",
    "resolve_or_report",
    "scrubbed_environ",
    "section_detail",
    "section_table",
    "toml_parse_scope",
    "validate_agent_type",
]
