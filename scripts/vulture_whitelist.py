"""Names vulture reports at confidence 60 that are not dead code.

Each entry is one line `_.NAME` under a comment that gives the cause. vulture
treats an attribute access on `_` as a use of NAME. Before an entry is added,
`git grep -nw NAME` must show the reader vulture cannot see. A test-only entry
has no caller in kstrl. A record field that kstrl writes and only a test
reads names the writer. A function that only a test reads is removed, unless
the calibration suite reads it (#863 A2).

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

# the import system reads the __path__ of the package that the leash makes (kstrl/proc/leash.py)
_.__path__

# click.Group reads command_class by name
_.command_class

# click.Group reads group_class by name
_.group_class

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

# record field: writer at kstrl/learning/journal_reads.py:95
_.total_components

# record field: writer at kstrl/learning/journal_reads.py:96
_.affected_components

# PlannedIssue field: passed positionally at the two plan_sync sites: writer at
# kstrl/intake_github.py:445 and 453
_.authorization

# record field: set at construction and written to the intake ledger
_.labeled_at

# record field: set at construction and written to the intake ledger
_.last_edited_at

# provenance field of LoopBudget: set by factory_launch: writer at kstrl/factory_launch.py:279
_.prior_known_calls

# record field: set at construction: writer at kstrl/pipeline_knowledge.py:303, 327 and 393
_.utilization

# record field: set at construction; writer at kstrl/pr.py:315 to 375, no reader in kstrl
_.pushed

# RunState counter: written by the reducer at kstrl/events/reducer.py:383, no reader in kstrl
_.unknown_events

# Replay record field: written during the replay; writer at kstrl/replay.py:180, no reader in kstrl
_.lock_wait_seconds

# Replay record field: written during the replay; writer at kstrl/replay.py:304, no reader in kstrl
_.group_gone

# ProvenRung field: set by isolation; writer at kstrl/isolation.py:586, no reader in kstrl
_.backend_version

# ProvenRung field: set by isolation; writer at kstrl/isolation.py:588, no reader in kstrl
_.policy_sha256

# SettleResult counter: set and summed; writer at kstrl/serve.py:280, no reader in kstrl
_.inbox_items

# SettleResult counter: set and summed; writer at kstrl/serve.py:303, no reader in kstrl
_.sync_errors

# SettleResult counter: set and summed; writer at kstrl/serve.py:303, no reader in kstrl
_.synced

# SettleResult counter: set and summed; writer at kstrl/serve.py:519, no reader in kstrl
_.charged_usd


# Read by the calibration suite (tests/test_calibration.py), which is how the owner measures the
# prompts

# calibration suite: calibration.py
_.DEFAULT_CALIBRATION_RUNS

# calibration suite: calibration.py
_.required_kinds_satisfied

# calibration suite: calibration.py
_.exact_kinds_present

# calibration suite: calibration.py
_.build_report

# calibration suite: calibration.py
_.save_report

# calibration suite: calibration.py
_.reviewer_override_from_env

# calibration suite: calibration.py
_.reviewer_override_label

# calibration suite: calibration_baseline.py
_.CalibrationModelDriftWarning

# calibration suite: calibration_baseline.py
_.model_drift_message

# calibration suite: calibration_score.py
_.render_verification

# calibration suite: calibration_score.py
_.check_fixture_meta

# calibration suite: calibration_score.py
_.security_caught

# calibration suite: calibration_score.py
_.security_false_positive

# calibration suite: calibration_score.py
_.reviewer_caught

# calibration suite: calibration_score.py
_.reviewer_false_positive

# calibration suite: git_diff.py
_.pasted_change_source
