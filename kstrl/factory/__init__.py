"""One factory run.

The start steps, the launch, the workers, the worktrees, the acceptance and
integration rounds, the plan gate and the summary.
"""

from __future__ import annotations

from kstrl.factory.acceptance_design import remove_designed_check
from kstrl.factory.commandrun import CommandRun, open_command_run
from kstrl.factory.factory import run_factory
from kstrl.factory.factory_preflight import (
    BaseRefusedError,
    base_check_before_architect,
    base_refused_before_architect,
    report_preflight,
)
from kstrl.factory.factory_worker import engineer_context_prefix, report_operator_files
from kstrl.factory.plan_gate import PLAN_GATE_KEY, spec_pin_errors
from kstrl.factory.retry_plan import (
    RESUME_REFUSAL,
    plan_resume,
    prepare_retry,
    preview_retry,
    print_resume_plan,
    print_retry_plan,
    retry_confirm_header,
)

__all__ = [
    "BaseRefusedError",
    "CommandRun",
    "PLAN_GATE_KEY",
    "RESUME_REFUSAL",
    "base_check_before_architect",
    "base_refused_before_architect",
    "engineer_context_prefix",
    "open_command_run",
    "plan_resume",
    "prepare_retry",
    "preview_retry",
    "print_resume_plan",
    "print_retry_plan",
    "remove_designed_check",
    "report_operator_files",
    "report_preflight",
    "retry_confirm_header",
    "run_factory",
    "spec_pin_errors",
]
