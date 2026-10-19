"""The bytes kstrl reads and writes.

The atomic write, the terminated append, the JSON read, the state
directory, the run id and the untrusted-data delimiters.
"""

from __future__ import annotations

from kstrl.io.appendio import (
    JOURNAL_REPAIR_EVENT,
    REPAIR_DETAIL,
    append_records,
    append_terminated,
    open_for_append,
)
from kstrl.io.atomicio import atomic_write_json, atomic_write_text
from kstrl.io.delimiters import generate_data_delimiter
from kstrl.io.jsonread import read_json, read_json_file
from kstrl.io.runid import mint_run_id, run_kind, run_sort_key
from kstrl.io.statedir import (
    CONTROL_APP_NAME,
    CONTROL_AUTONOMY,
    CONTROL_CI_CHECKS,
    CONTROL_GITHUB_PROCESSED,
    CONTROL_INBOX,
    CONTROL_PAUSE,
    CONTROL_PR_COUNT_STREAK,
    CONTROL_SIGNALS,
    CONTROL_SPEND,
    STATE_DIR_NAME,
    ControlStateError,
    control_dir,
    control_file,
    control_is_external,
    control_lock,
    control_untrusted_reason,
    ensure_control_state,
    legacy_control_refusal,
    legacy_control_remaining,
    plan_prd_path,
    pre_run_prd_path,
    state_dir,
    state_dir_carve_out,
    xdg_state_home,
)

__all__ = [
    "CONTROL_APP_NAME",
    "CONTROL_AUTONOMY",
    "CONTROL_CI_CHECKS",
    "CONTROL_GITHUB_PROCESSED",
    "CONTROL_INBOX",
    "CONTROL_PAUSE",
    "CONTROL_PR_COUNT_STREAK",
    "CONTROL_SIGNALS",
    "CONTROL_SPEND",
    "ControlStateError",
    "JOURNAL_REPAIR_EVENT",
    "REPAIR_DETAIL",
    "STATE_DIR_NAME",
    "append_records",
    "append_terminated",
    "atomic_write_json",
    "atomic_write_text",
    "control_dir",
    "control_file",
    "control_is_external",
    "control_lock",
    "control_untrusted_reason",
    "ensure_control_state",
    "generate_data_delimiter",
    "legacy_control_refusal",
    "legacy_control_remaining",
    "mint_run_id",
    "open_for_append",
    "plan_prd_path",
    "pre_run_prd_path",
    "read_json",
    "read_json_file",
    "run_kind",
    "run_sort_key",
    "state_dir",
    "state_dir_carve_out",
    "xdg_state_home",
]
