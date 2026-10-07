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
#: tests/test_string_keys_reach_every_surface.py checks the field names against the real
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
#: a retired name is refused with the name to use instead, where an
#: unknown one is refused as a name no setting reads (#525). No alias
#: layer: two spellings live forever and the old one never dies.
RETIRED_SECTIONS: dict[str, str] = {"feedforward": "codebase_scan"}

#: Why a verification command key or variable no longer exists (#696 slice
#: 4, the flag day): a confirmed ``[stack]`` is the only source of commands.
_RETIRED_FOR_STACK = (
    "retired: verification commands come only from a confirmed [stack]. Move the command "
    "under [stack.checks] (ks doctor files a proposed [stack] from the old [verify] commands)"
)

#: Why a codebase scan section no longer exists (#696 slice 6): each read one
#: source language's files or config.
_RETIRED_SCAN_SECTION = "retired: the codebase scan reads no source language; remove it"

#: Why a test-adequacy key no longer exists (#696 slice 8): Layer 0 read one
#: language's test files. The code reviewer judges whether a change weakened
#: the tests, and Phase 1 says Layer 0 was not measured from autonomy level 1.
_RETIRED_ADEQUACY = (
    "retired: kstrl reads no test file mechanically; the code reviewer judges whether "
    "a change weakened the tests. Remove the [adequacy] section"
)

#: Why a dead-code, mutation or coverage key no longer exists (#696 slice 8).
_RETIRED_TOOL_CHECK = (
    "retired: kstrl runs no dead-code, mutation or coverage tool. Remove it, and add "
    "the check you want to [stack.checks]"
)

#: Why a dependency or license policy key no longer exists (#696 slice 9):
#: each read one ecosystem's lockfiles or license registry.
_RETIRED_DEPENDENCY_POLICY = (
    "retired: kstrl reads no lockfile and no license registry, and the security reviewer "
    "lists every dependency a change adds; remove it"
)

#: Why an approved-fixtures key no longer exists (#700 slice 8): a fixture
#: lived in the PRD, where the engineer could read it, and was judged on
#: its output. An acceptance check lives outside the repository and is
#: judged on its exit status.
_RETIRED_FIXTURES = (
    "retired: kstrl runs no PRD fixture. Remove the [fixtures] section, and write each "
    "fixture as an acceptance check in a plan outside the repository (ks factory --acceptance)"
)

#: (section, key) retired by #395, #696 and #700, mapped to what replaced it: the
#: refusal reads "names [section] key, which was <this>".
RETIRED_KEYS: dict[tuple[str, str], str] = {
    ("factory", "setpoint_agreement"): "renamed to claim_agreement; rename the key",
    ("verify", "test_command"): _RETIRED_FOR_STACK,
    ("verify", "typecheck_command"): _RETIRED_FOR_STACK,
    ("verify", "lint_command"): _RETIRED_FOR_STACK,
    ("verify", "test_tool"): _RETIRED_FOR_STACK,
    ("verify", "typecheck_tool"): _RETIRED_FOR_STACK,
    ("verify", "lint_tool"): _RETIRED_FOR_STACK,
    ("factory", "worktree_setup_command"): (
        "retired: worktree setup comes only from a confirmed [stack]; move it to [stack] setup"
    ),
    ("contract", "test_command"): (
        "retired: Phase 3 runs every check of the confirmed [stack]; remove it"
    ),
    ("breaker", "test_command"): "retired: the no-progress breaker reads the diff only; remove it",
    ("breaker", "test_timeout"): "retired: the no-progress breaker reads the diff only; remove it",
    ("codebase_scan", "public_interfaces"): _RETIRED_SCAN_SECTION,
    ("codebase_scan", "dependency_graph"): _RETIRED_SCAN_SECTION,
    ("codebase_scan", "conventions"): _RETIRED_SCAN_SECTION,
    ("verify", "dead_code_cleanup"): _RETIRED_TOOL_CHECK,
    ("verify", "dead_code_command"): _RETIRED_TOOL_CHECK,
    ("verify", "mutation_testing"): _RETIRED_TOOL_CHECK,
    ("verify", "mutation_threshold"): _RETIRED_TOOL_CHECK,
    ("verify", "mutation_timeout"): _RETIRED_TOOL_CHECK,
    ("adequacy", "enabled"): _RETIRED_ADEQUACY,
    ("adequacy", "layer0"): _RETIRED_ADEQUACY,
    ("adequacy", "require_strong_oracle"): _RETIRED_ADEQUACY,
    ("adequacy", "flag_assertionless_tests"): _RETIRED_ADEQUACY,
    ("adequacy", "patch_coverage"): _RETIRED_TOOL_CHECK,
    ("adequacy", "diff_mutation"): _RETIRED_TOOL_CHECK,
    ("policy", "deps_allow_new"): _RETIRED_DEPENDENCY_POLICY,
    ("policy", "license_allow"): _RETIRED_DEPENDENCY_POLICY,
    ("policy", "license_deny_partial"): _RETIRED_DEPENDENCY_POLICY,
    ("policy", "license_unresolved"): _RETIRED_DEPENDENCY_POLICY,
    ("policy", "license_use_network"): _RETIRED_DEPENDENCY_POLICY,
    ("fixtures", "enabled"): _RETIRED_FIXTURES,
    ("fixtures", "snapshot_on_success"): _RETIRED_FIXTURES,
    ("fixtures", "snapshot_dir"): _RETIRED_FIXTURES,
    ("fixtures", "timeout"): _RETIRED_FIXTURES,
}

#: Environment variables retired by #395, #696 and #700, mapped to what replaced
#: each: the refusal reads "sets NAME, which was <this>".
RETIRED_ENV_VARS: dict[str, str] = {
    "KSTRL_FACTORY_SETPOINT_AGREEMENT": "renamed to KSTRL_FACTORY_CLAIM_AGREEMENT",
    "KSTRL_FEEDFORWARD_ENABLED": "renamed to KSTRL_CODEBASE_SCAN_ENABLED",
    "KSTRL_FEEDFORWARD_MODULE_MAP": "renamed to KSTRL_CODEBASE_SCAN_MODULE_MAP",
    "KSTRL_FEEDFORWARD_PUBLIC_INTERFACES": _RETIRED_SCAN_SECTION,
    "KSTRL_FEEDFORWARD_DEPENDENCY_GRAPH": _RETIRED_SCAN_SECTION,
    "KSTRL_FEEDFORWARD_CONVENTIONS": _RETIRED_SCAN_SECTION,
    "KSTRL_FEEDFORWARD_MAX_TOKENS": "renamed to KSTRL_CODEBASE_SCAN_MAX_TOKENS",
    "KSTRL_VERIFY_TEST_CMD": _RETIRED_FOR_STACK,
    "KSTRL_VERIFY_TYPECHECK_CMD": _RETIRED_FOR_STACK,
    "KSTRL_VERIFY_LINT_CMD": _RETIRED_FOR_STACK,
    "KSTRL_VERIFY_TEST_TOOL": _RETIRED_FOR_STACK,
    "KSTRL_VERIFY_TYPECHECK_TOOL": _RETIRED_FOR_STACK,
    "KSTRL_VERIFY_LINT_TOOL": _RETIRED_FOR_STACK,
    "KSTRL_FACTORY_WORKTREE_SETUP_COMMAND": (
        "retired: worktree setup comes only from a confirmed [stack]; use [stack] setup"
    ),
    "KSTRL_CONTRACT_TEST_CMD": "retired: Phase 3 runs every check of the confirmed [stack]",
    "KSTRL_BREAKER_TEST_CMD": "retired: the no-progress breaker reads the diff only",
    "KSTRL_BREAKER_TEST_TIMEOUT": "retired: the no-progress breaker reads the diff only",
    "KSTRL_CODEBASE_SCAN_PUBLIC_INTERFACES": _RETIRED_SCAN_SECTION,
    "KSTRL_CODEBASE_SCAN_DEPENDENCY_GRAPH": _RETIRED_SCAN_SECTION,
    "KSTRL_CODEBASE_SCAN_CONVENTIONS": _RETIRED_SCAN_SECTION,
    "KSTRL_DEAD_CODE_CLEANUP": _RETIRED_TOOL_CHECK,
    "KSTRL_DEAD_CODE_CMD": _RETIRED_TOOL_CHECK,
    "KSTRL_MUTATION_TESTING": _RETIRED_TOOL_CHECK,
    "KSTRL_MUTATION_THRESHOLD": _RETIRED_TOOL_CHECK,
    "KSTRL_MUTATION_TIMEOUT": _RETIRED_TOOL_CHECK,
    "KSTRL_ADEQUACY_ENABLED": _RETIRED_ADEQUACY,
    "KSTRL_ADEQUACY_LAYER0": _RETIRED_ADEQUACY,
    "KSTRL_POLICY_DEPS_ALLOW_NEW": _RETIRED_DEPENDENCY_POLICY,
    "KSTRL_POLICY_LICENSE_UNRESOLVED": _RETIRED_DEPENDENCY_POLICY,
    "KSTRL_POLICY_LICENSE_NET": _RETIRED_DEPENDENCY_POLICY,
    "KSTRL_FIXTURES_ENABLED": _RETIRED_FIXTURES,
    "KSTRL_FIXTURES_SNAPSHOT_ON_SUCCESS": _RETIRED_FIXTURES,
    "KSTRL_FIXTURES_SNAPSHOT_DIR": _RETIRED_FIXTURES,
    "KSTRL_FIXTURES_TIMEOUT": _RETIRED_FIXTURES,
}
