"""The kstrl.toml keys that overlay a ``KstrlConfig`` field from a string.

Its own module because it is the part of ``kstrl/config.py`` that GROWS:
every new ``[paths]`` file is a row here, and ``config.py`` was one line
under the 800-line pre-commit ratchet when R10.9 came to add its row.
Measured: the field plus the row took it from 799 to 801 and
``scripts/precommit/file_length_ratchet.py`` returned ``CROSSED``.

It imports nothing, so no cycle is possible. ``kstrl.config`` re-exports
the name and the loaders read it from there; ``kstrl/config_report.py``
and ``scripts/gen_docs.py`` read it from here, which is where the table
lives. Both names are the SAME object and
``tests/test_string_keys_reach_every_surface.py`` pins that identity, so
"the table the loaders use" and "the table the documentation surfaces
use" cannot become two tables.
"""

from __future__ import annotations

#: Every kstrl.toml key that overlays one KstrlConfig field from a string:
#: (section, key, env var, field, is_path). A path row's value is resolved
#: against the root and its field default anchored there by
#: ``KstrlConfig.anchored``; every other row takes the string verbatim.
#: THE TWO DOORS DISAGREE ABOUT "" and both rules belong here, or a reader
#: of one will "fix" the other: the TOML door needs a NON-EMPTY string
#: (``branch = ""`` means "no override") while the env door applies any var
#: that is SET (test_an_empty_env_var_is_an_explicit_empty_value). One row,
#: not a hand-copied branch per key in the two overlays, the anchoring
#: block, ``config_report.show_sections`` and ``scripts/gen_docs.py``: a key
#: added to some of those differed silently by the door it came in through.
#: tests/test_config_toml.py checks the field names against the real
#: dataclass fields, because ``setattr`` on a typo invents an attribute instead of
#: raising. Unprefixed env names are compatibility; a new row takes KSTRL_.
STRING_KEYS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("paths", "prompt", "PROMPT_FILE", "prompt_file", True),
    ("paths", "prd", "PRD_FILE", "prd_file", True),
    ("paths", "progress", "PROGRESS_FILE", "progress_file", True),
    ("paths", "codebase_map", "CODEBASE_MAP_FILE", "codebase_map_file", True),
    ("paths", "golden_patterns", "KSTRL_GOLDEN_PATTERNS_FILE", "golden_patterns_file", True),
    ("paths", "memory", "KSTRL_MEMORY_FILE", "memory_file", True),
    ("agent", "type", "KSTRL_AGENT_TYPE", "agent_type", False),
    ("agent", "command", "AGENT_CMD", "agent_cmd", False),
    ("agent", "model", "MODEL", "model", False),
    ("agent", "reasoning_effort", "MODEL_REASONING_EFFORT", "model_reasoning_effort", False),
)

#: kstrl.toml sections renamed by #395. Consulted once at command entry;
#: a retired name REFUSES rather than being silently ignored the way an
#: unknown one is (tests/test_config_toml.py::test_from_toml_ignores_unknown_keys
#: pins that silence for names we never used). No alias layer: two
#: spellings live forever and the old one never dies.
RETIRED_SECTIONS: dict[str, str] = {"feedforward": "codebase_scan"}

#: (section, key) renamed by #395, mapped to the new KEY name.
RETIRED_KEYS: dict[tuple[str, str], str] = {
    ("factory", "setpoint_agreement"): "claim_agreement",
}

#: Environment variables renamed by #395.
RETIRED_ENV_VARS: dict[str, str] = {
    "KSTRL_FACTORY_SETPOINT_AGREEMENT": "KSTRL_FACTORY_CLAIM_AGREEMENT",
    "KSTRL_FEEDFORWARD_ENABLED": "KSTRL_CODEBASE_SCAN_ENABLED",
    "KSTRL_FEEDFORWARD_MODULE_MAP": "KSTRL_CODEBASE_SCAN_MODULE_MAP",
    "KSTRL_FEEDFORWARD_PUBLIC_INTERFACES": "KSTRL_CODEBASE_SCAN_PUBLIC_INTERFACES",
    "KSTRL_FEEDFORWARD_DEPENDENCY_GRAPH": "KSTRL_CODEBASE_SCAN_DEPENDENCY_GRAPH",
    "KSTRL_FEEDFORWARD_CONVENTIONS": "KSTRL_CODEBASE_SCAN_CONVENTIONS",
    "KSTRL_FEEDFORWARD_MAX_TOKENS": "KSTRL_CODEBASE_SCAN_MAX_TOKENS",
}
