"""One component of a factory run.

The attempts, the checks, the review phases, the transitions, the merge and
the PR, and the run settings.
"""

from __future__ import annotations

from kstrl.pipeline.context import IterationContext
from kstrl.pipeline.divergence import DIVERGENCE_SECTION
from kstrl.pipeline.factory_agents import (
    AdversarialAgentSelection,
    cli_family,
    resolve_engineer_sandbox,
    select_reviewers,
    warn_unsandboxable_reviewers,
)
from kstrl.pipeline.factory_config import (
    FACTORY_SECTION,
    VALID_REVIEW_MODES,
    FactoryConfig,
    merge_gate_unreachable_warning,
    review_enabled,
    warn_unreachable_gates,
)
from kstrl.pipeline.factory_result import (
    ComponentResult,
    FactoryResult,
    clear_partial_usage,
    ids_in_status,
    report_awaiting_approval,
    resolve_exit_code,
    run_is_clean,
    salvage_aborted_usage,
    write_partial_usage,
)
from kstrl.pipeline.launch_record import (
    REMOVED_OPTIONS,
    FlagValue,
    LaunchRecord,
    LaunchRecordError,
    flags_argv,
    launch_record_path,
    option_argv,
    read_launch_record,
    replayable_flags,
    run_limits,
    write_launch_record,
)
from kstrl.pipeline.pipeline import ComponentPipeline
from kstrl.pipeline.pipeline_merge import HITL_REJECT_CHECK, PR_CLOSED_CHECK
from kstrl.pipeline.pipeline_state import PipelineHooks, iso_now
from kstrl.pipeline.pr import GH_TIMEOUT, create_prs_in_order, create_single_pr
from kstrl.pipeline.pr_body import PR_FOOTER_MARKER
from kstrl.pipeline.release import (
    RELEASE_CONFIG_SECTION,
    RELEASE_REF_RULE,
    ReleaseInputs,
    release_ref_from,
    release_withheld,
)
from kstrl.pipeline.runenvelope import RunEnvelope
from kstrl.pipeline.runstate import RunState

__all__ = [
    "AdversarialAgentSelection",
    "ComponentPipeline",
    "ComponentResult",
    "DIVERGENCE_SECTION",
    "FACTORY_SECTION",
    "FactoryConfig",
    "FactoryResult",
    "FlagValue",
    "GH_TIMEOUT",
    "HITL_REJECT_CHECK",
    "IterationContext",
    "LaunchRecord",
    "LaunchRecordError",
    "PR_CLOSED_CHECK",
    "PR_FOOTER_MARKER",
    "PipelineHooks",
    "RELEASE_CONFIG_SECTION",
    "RELEASE_REF_RULE",
    "REMOVED_OPTIONS",
    "ReleaseInputs",
    "RunEnvelope",
    "RunState",
    "VALID_REVIEW_MODES",
    "clear_partial_usage",
    "cli_family",
    "create_prs_in_order",
    "create_single_pr",
    "flags_argv",
    "ids_in_status",
    "iso_now",
    "launch_record_path",
    "merge_gate_unreachable_warning",
    "option_argv",
    "read_launch_record",
    "release_ref_from",
    "release_withheld",
    "replayable_flags",
    "report_awaiting_approval",
    "resolve_engineer_sandbox",
    "resolve_exit_code",
    "review_enabled",
    "run_is_clean",
    "run_limits",
    "salvage_aborted_usage",
    "select_reviewers",
    "warn_unreachable_gates",
    "warn_unsandboxable_reviewers",
    "write_launch_record",
    "write_partial_usage",
]
