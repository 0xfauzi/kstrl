"""Acceptance checks and contract tests.

The acceptance runner, the carried checks, the stack replay and the
isolation proof.
"""

from __future__ import annotations

from kstrl.acceptance.acceptance import (
    ACCEPTANCE_DIR,
    DESIGNER_FILE,
    FAIL,
    PASS,
    PLAN_FILE,
    TREE_ENV,
    BaseReading,
    PinnedPlan,
    judge_head,
    pin_plan,
    pinned_digest,
    plan_errors,
    replay_base,
    verdict_of,
    write_dir,
)
from kstrl.acceptance.acceptance_carried import Carried, carried_on_base, carry_passed
from kstrl.acceptance.acceptance_lines import halt_text, pr_isolation, pr_section
from kstrl.acceptance.contract import (
    CONTRACT_SECTION,
    ContractConfig,
    ContractMode,
    ContractResult,
    run_contract_testing,
)
from kstrl.acceptance.isolation import SETUP_ZONE, TEST_ZONE, prove_zones, write_record
from kstrl.acceptance.recheck import recheck_record
from kstrl.acceptance.replay import Replay, replay_stack

__all__ = [
    "ACCEPTANCE_DIR",
    "BaseReading",
    "CONTRACT_SECTION",
    "Carried",
    "ContractConfig",
    "ContractMode",
    "ContractResult",
    "DESIGNER_FILE",
    "FAIL",
    "PASS",
    "PLAN_FILE",
    "PinnedPlan",
    "Replay",
    "SETUP_ZONE",
    "TEST_ZONE",
    "TREE_ENV",
    "carried_on_base",
    "carry_passed",
    "halt_text",
    "judge_head",
    "pin_plan",
    "pinned_digest",
    "plan_errors",
    "pr_isolation",
    "pr_section",
    "prove_zones",
    "recheck_record",
    "replay_base",
    "replay_stack",
    "run_contract_testing",
    "verdict_of",
    "write_dir",
    "write_record",
]
