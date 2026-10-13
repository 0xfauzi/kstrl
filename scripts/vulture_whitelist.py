"""Names vulture reports at confidence 60 that are not dead code, or that only a test reads.

Each entry is one line `_.NAME` under a comment that gives the cause. vulture
treats an attribute access on `_` as a use of NAME. Before an entry is added,
`git grep -nw NAME` must show the reader vulture cannot see. A test-only entry
is not a verdict that the code is needed: kstrl has no caller for it, and a
later slice can delete it together with the test that reads it.

This file is listed in `[tool.vulture] paths` in pyproject.toml. It is never
imported.
"""

from __future__ import annotations


class _Anything:
    def __getattr__(self, name: str) -> _Anything:
        return self


_ = _Anything()


# Read by a framework, a type checker or the string value of a name

# names the command-module imports so ruff F401 sees a reader
_.COMMAND_MODULES

# click.Group reads command_class by name
_.command_class

# click.Group reads group_class by name
_.group_class

# removed by #870 (A4); delete this entry if that PR is merged
_.validate

# csv.Dialect subclass: the csv module reads the attribute
_.quotechar

# csv.Dialect subclass: the csv module reads the attribute
_.doublequote

# csv.Dialect subclass: the csv module reads the attribute
_.escapechar

# csv.Dialect subclass: the csv module reads the attribute
_.lineterminator

# csv.Dialect subclass: the csv module reads the attribute
_.quoting

# csv.Dialect subclass: the csv module reads the attribute
_.skipinitialspace

# static assertion inside TYPE_CHECKING that mypy reads
_._health_breach_seam_accepts_the_contract

# StrEnum member: read as the string value 'linear' from a queue file
_.LINEAR


# Record, event and counter fields that kstrl writes and does not read

# property read by tests only (tests/test_claude_engineer_write_guard_e2e.py shadows the name)
_.empty

# property read by tests only
_.stall_count

# property read by tests only
_.recording

# dataclass field of an event: set by the event reader, serialised into events.jsonl
_.type_name

# event field: serialised into events.jsonl
_.advisory

# event field: serialised into events.jsonl
_.release_ref_rule

# event field: serialised into events.jsonl
_.blocking_findings

# event field: serialised into events.jsonl
_.blocked

# event field: serialised into events.jsonl
_.scope_source

# event field: serialised into events.jsonl
_.elapsed_seconds

# event field: serialised into events.jsonl
_.facts_written

# record field: constructed by journal_reads, read by tests
_.total_components

# record field: constructed by journal_reads, read by tests
_.affected_components

# PlannedIssue field: passed positionally at the two plan_sync sites, no reader in kstrl
_.authorization

# record field: set at construction and written to the intake ledger
_.labeled_at

# record field: set at construction and written to the intake ledger
_.last_edited_at

# provenance field of LoopBudget: set by factory_launch, read by tests
_.prior_known_calls

# record field: set at construction, read by tests
_.utilization

# record field: set at construction, read by tests
_.pushed

# RunState counter: written by the reducer, read by tests
_.unknown_events

# Replay record field: written during the replay, no reader in kstrl
_.lock_wait_seconds

# Replay record field: written during the replay, no reader in kstrl
_.group_gone

# ProvenRung field: set by isolation, no reader in kstrl
_.backend_version

# ProvenRung field: set by isolation, no reader in kstrl
_.policy_sha256

# SettleResult counter: set and summed, read by tests
_.inbox_items

# SettleResult counter: set and summed, read by tests
_.sync_errors

# SettleResult counter: set and summed, read by tests
_.synced

# SettleResult counter: set and summed, read by tests
_.charged_usd


# Read only by tests: no caller in kstrl

# tests only: agents/liveness.py
_.reset_probe_cache

# tests only: agents/prompt_record.py
_.read_prompt_records

# tests only: autonomy_replay.py
_.replay_file

# tests only: calibration.py
_.DEFAULT_CALIBRATION_RUNS

# tests only: calibration.py
_.required_kinds_satisfied

# tests only: calibration.py
_.exact_kinds_present

# tests only: calibration.py
_.build_report

# tests only: calibration.py
_.save_report

# tests only: calibration.py
_.reviewer_override_from_env

# tests only: calibration.py
_.reviewer_override_label

# tests only: calibration_baseline.py
_.CalibrationModelDriftWarning

# tests only: calibration_baseline.py
_.model_drift_message

# tests only: calibration_score.py
_.render_verification

# tests only: calibration_score.py
_.check_fixture_meta

# tests only: calibration_score.py
_.security_caught

# tests only: calibration_score.py
_.security_false_positive

# tests only: calibration_score.py
_.reviewer_caught

# tests only: calibration_score.py
_.reviewer_false_positive

# tests only: config.py
_.from_toml

# tests only: findings.py
_.finding_attempt

# tests only: git.py
_.create_branch_from

# tests only: git.py
_.delete_branch

# tests only: git.py
_.checkout_existing

# tests only: git_diff.py
_.as_payload

# tests only: git_diff.py
_.pasted_change_source

# tests only: git_diff.py
_.DEFAULT_PROMPT_DIFF_CHAR_LIMIT

# tests only: health.py
_.health_status

# tests only: inbox.py
_.unparseable_line_count

# tests only: intake_issue.py
_.forget

# tests only: journal_reads.py
_.extract_failure_patterns

# tests only: knowledge.py
_.read_dependency_scope_telemetry

# tests only: loop_budget.py
_.halt_reason

# tests only: observability.py
_.summarize_events

# tests only: pipeline_ledger.py
_.coverage_notes

# tests only: runid.py
_.KNOWN_KINDS

# tests only: serve_process.py
_.process_group_alive

# tests only: statedir.py
_.clear_xdg_state_home_cache

# tests only: timeout.py
_.run_with_timeout
