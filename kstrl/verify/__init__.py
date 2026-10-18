"""Mechanical verification.

The configured checks, the diff checks, the policy envelope, the base
gates, the baselines and the failure signatures.
"""

from __future__ import annotations

from kstrl.verify.base_gates import (
    BaseGates,
    apply_acceptance,
    base_gates_path,
    measure_base_gates,
    reading_document,
    refusal_lines,
    warning_lines,
    write_record,
)
from kstrl.verify.failure_signatures import (
    INFRASTRUCTURE_CHECKS,
    UNENROLLED_CATEGORY,
    FailurePattern,
    PatternRouting,
    category_for_check,
    classify_check,
    normalize_error,
    route_patterns,
    signature_for_error,
    signatures_from_findings,
    signatures_from_verification,
    split_signature,
)
from kstrl.verify.guards import ScopeHazard, scope_entry_hazard
from kstrl.verify.policy import (
    ENFORCEMENT_MACHINERY_PATHS,
    POLICY_SECTION,
    PolicyConfig,
    count_diff_size,
    match_glob,
)
from kstrl.verify.verify import (
    DIFF_DEPENDENT_CHECKS,
    MechanicalVerification,
    latest_entry,
    narrow_to_undiffed,
    run_mechanical_verification,
    run_undiffed_verification,
    self_critique_progress_path,
)
from kstrl.verify.verify_commands import SHELL_COULD_NOT_RUN, check_stack_command, run_fast_checks
from kstrl.verify.verify_diff import SCOPE_UNREADABLE_CHECK, scope_unreadable_error
from kstrl.verify.verify_model import (
    LAYER0_NOT_MEASURED,
    VERIFY_SECTION,
    CheckResult,
    VerificationResult,
    VerifyConfig,
)

__all__ = [
    "BaseGates",
    "CheckResult",
    "DIFF_DEPENDENT_CHECKS",
    "ENFORCEMENT_MACHINERY_PATHS",
    "FailurePattern",
    "INFRASTRUCTURE_CHECKS",
    "LAYER0_NOT_MEASURED",
    "MechanicalVerification",
    "POLICY_SECTION",
    "PatternRouting",
    "PolicyConfig",
    "SCOPE_UNREADABLE_CHECK",
    "SHELL_COULD_NOT_RUN",
    "ScopeHazard",
    "UNENROLLED_CATEGORY",
    "VERIFY_SECTION",
    "VerificationResult",
    "VerifyConfig",
    "apply_acceptance",
    "base_gates_path",
    "category_for_check",
    "check_stack_command",
    "classify_check",
    "count_diff_size",
    "latest_entry",
    "match_glob",
    "measure_base_gates",
    "narrow_to_undiffed",
    "normalize_error",
    "reading_document",
    "refusal_lines",
    "route_patterns",
    "run_fast_checks",
    "run_mechanical_verification",
    "run_undiffed_verification",
    "scope_entry_hazard",
    "scope_unreadable_error",
    "self_critique_progress_path",
    "signature_for_error",
    "signatures_from_findings",
    "signatures_from_verification",
    "split_signature",
    "warning_lines",
    "write_record",
]
