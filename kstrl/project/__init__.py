"""The project of the operator.

The [stack] section, the ks init scaffolds and their ledger, the operator
context files and the codebase scan.
"""

from __future__ import annotations

from kstrl.project.feedforward import (
    CODEBASE_SCAN_SECTION,
    CodebaseScanConfig,
    build_codebase_scan_context,
)
from kstrl.project.init_cmd import run_init
from kstrl.project.operator_context import (
    GOLDEN_PATTERNS,
    MEMORY,
    OperatorFile,
    append_guidance_record,
    load_operator_file,
    memory_text_refusal,
    operator_file_notices,
    operator_file_spec,
)
from kstrl.project.scaffold_ledger import staleness_notice
from kstrl.project.scaffold_templates import (
    DEFAULT_CODEBASE_MAP,
    DEFAULT_FEATURE_UNDERSTAND,
    DEFAULT_PROMPT,
)
from kstrl.project.stack import (
    NO_STACK,
    REPLAY_BASE_CONTRADICTION,
    REPLAY_BOUNDARY_REFUSED,
    REPLAY_CHECK_NOT_RUNNABLE,
    REPLAY_SETUP_FAILED,
    REPLAY_UP_FAILED,
    REPLAY_UP_TIMEOUT,
    STACK_KEY,
    STACK_KIND,
    STACK_OPTIONS,
    STACK_SECTION,
    Stack,
    StackError,
    StackRefused,
    confirmed_stack,
    decide_at_prompt,
    file_stack_item,
    legacy_proposal,
    load_stack,
    replay_refuses,
    stack_errors,
    stack_from_table,
    stack_in_force,
    stack_paths,
    stack_text_digest,
    stack_toml,
    unconfirmed_lines,
)

__all__ = [
    "CODEBASE_SCAN_SECTION",
    "CodebaseScanConfig",
    "DEFAULT_CODEBASE_MAP",
    "DEFAULT_FEATURE_UNDERSTAND",
    "DEFAULT_PROMPT",
    "GOLDEN_PATTERNS",
    "MEMORY",
    "NO_STACK",
    "OperatorFile",
    "REPLAY_BASE_CONTRADICTION",
    "REPLAY_BOUNDARY_REFUSED",
    "REPLAY_CHECK_NOT_RUNNABLE",
    "REPLAY_SETUP_FAILED",
    "REPLAY_UP_FAILED",
    "REPLAY_UP_TIMEOUT",
    "STACK_KEY",
    "STACK_KIND",
    "STACK_OPTIONS",
    "STACK_SECTION",
    "Stack",
    "StackError",
    "StackRefused",
    "append_guidance_record",
    "build_codebase_scan_context",
    "confirmed_stack",
    "decide_at_prompt",
    "file_stack_item",
    "legacy_proposal",
    "load_operator_file",
    "load_stack",
    "memory_text_refusal",
    "operator_file_notices",
    "operator_file_spec",
    "replay_refuses",
    "run_init",
    "stack_errors",
    "stack_from_table",
    "stack_in_force",
    "stack_paths",
    "stack_text_digest",
    "stack_toml",
    "staleness_notice",
    "unconfirmed_lines",
]
