"""The kstrl.toml keys that overlay a ``KstrlConfig`` field from a string.

Its own module because it is the part of ``kstrl/config.py`` that GROWS:
every new ``[paths]`` file is a row here, and ``config.py`` was one line
under the 800-line pre-commit ratchet when R10.9 came to add its row.
Measured: the field plus the row took it from 799 to 801 and
``scripts/precommit/file_length_ratchet.py`` returned ``CROSSED``. This
commit is the move alone; the row lands in the next one.

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
    ("agent", "type", "KSTRL_AGENT_TYPE", "agent_type", False),
    ("agent", "command", "AGENT_CMD", "agent_cmd", False),
    ("agent", "model", "MODEL", "model", False),
    ("agent", "reasoning_effort", "MODEL_REASONING_EFFORT", "model_reasoning_effort", False),
)
