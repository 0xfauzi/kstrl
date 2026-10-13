"""The plan that a run executes.

The manifest DAG, the PRDs, the requirements, the scope and the Finding
record.
"""

from __future__ import annotations

from kstrl.plan.findings import (
    CLAIM_DISAGREEMENT_CATEGORY,
    POLICY_CATEGORY_PREFIX,
    WAIVER_TAG_PREFIX,
    Finding,
    dump_raw_debug,
    finding_model,
    finding_waiver,
    render_findings_markdown,
    tag_finding_with_attempt,
    tag_finding_with_model,
)
from kstrl.plan.manifest import (
    ADVERSARIAL_BUDGET_CHECK,
    COMPONENT_STATUS_VALUES,
    MERGE_GATE_PARK_KEY,
    Component,
    ComponentStatus,
    Manifest,
    park_dedupe_key,
)
from kstrl.plan.manifest_merge import has_merged
from kstrl.plan.names import role_component_key, validate_branch_name, validate_component_id
from kstrl.plan.prd import PRD, UserStory, prd_text_for_prompt
from kstrl.plan.requirements import (
    KIND_NON_GOAL,
    KIND_REQUIREMENT,
    SpecRequirement,
    built_by,
    enum_field_error,
    parse_requirements,
    required_field_error,
    requirements_payload_errors,
)
from kstrl.plan.scope import ComponentScope, RunScope

__all__ = [
    "ADVERSARIAL_BUDGET_CHECK",
    "CLAIM_DISAGREEMENT_CATEGORY",
    "COMPONENT_STATUS_VALUES",
    "Component",
    "ComponentScope",
    "ComponentStatus",
    "Finding",
    "KIND_NON_GOAL",
    "KIND_REQUIREMENT",
    "MERGE_GATE_PARK_KEY",
    "Manifest",
    "POLICY_CATEGORY_PREFIX",
    "PRD",
    "RunScope",
    "SpecRequirement",
    "UserStory",
    "WAIVER_TAG_PREFIX",
    "built_by",
    "dump_raw_debug",
    "enum_field_error",
    "finding_model",
    "finding_waiver",
    "has_merged",
    "park_dedupe_key",
    "parse_requirements",
    "prd_text_for_prompt",
    "render_findings_markdown",
    "required_field_error",
    "requirements_payload_errors",
    "role_component_key",
    "tag_finding_with_attempt",
    "tag_finding_with_model",
    "validate_branch_name",
    "validate_component_id",
]
