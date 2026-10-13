"""The learning loop.

The evolution journal, the autonomy ladder and the health trend.
"""

from __future__ import annotations

from kstrl.learning.autonomy import (
    DEMOTION_TRIGGER_LABELS,
    AutonomyError,
    AutonomyLevel,
    AutonomyState,
    DemotionTrigger,
    FlagBundle,
    flag_bundle_for,
)
from kstrl.learning.autonomy_config import (
    AUTONOMY_SECTION,
    AutonomyConfig,
    control_relocation_error,
    manual_override_notes,
    resolve_runtime_level,
    resolved_flag_bundle,
    strict_bool,
)
from kstrl.learning.autonomy_replay import RunRecord, load_runs, replay
from kstrl.learning.autonomy_transitions import (
    apply_demotion,
    commit_transition,
    entry_signal_blockers,
    promotion_authority_error,
    save_ladder_state,
)
from kstrl.learning.evolution import EvolutionJournal, classify_merges
from kstrl.learning.evolution_config import EVOLUTION_SECTION, EvolutionConfig
from kstrl.learning.evolve_report import readiness_lines
from kstrl.learning.health import breach_lines, health_report, readings_from
from kstrl.learning.journal_rows import (
    FINDINGS_SUPERSEDED_EVENT,
    INTEGRATION_RESULT_EVENT,
    JOURNAL_SCHEMA_VERSION,
    SPEC_ISSUES_EVENT,
    LegacyJournalRowError,
    entry_str,
)

__all__ = [
    "AUTONOMY_SECTION",
    "AutonomyConfig",
    "AutonomyError",
    "AutonomyLevel",
    "AutonomyState",
    "DEMOTION_TRIGGER_LABELS",
    "DemotionTrigger",
    "EVOLUTION_SECTION",
    "EvolutionConfig",
    "EvolutionJournal",
    "FINDINGS_SUPERSEDED_EVENT",
    "FlagBundle",
    "INTEGRATION_RESULT_EVENT",
    "JOURNAL_SCHEMA_VERSION",
    "LegacyJournalRowError",
    "RunRecord",
    "SPEC_ISSUES_EVENT",
    "apply_demotion",
    "breach_lines",
    "classify_merges",
    "commit_transition",
    "control_relocation_error",
    "entry_signal_blockers",
    "entry_str",
    "flag_bundle_for",
    "health_report",
    "load_runs",
    "manual_override_notes",
    "promotion_authority_error",
    "readiness_lines",
    "readings_from",
    "replay",
    "resolve_runtime_level",
    "resolved_flag_bundle",
    "save_ladder_state",
    "strict_bool",
]
