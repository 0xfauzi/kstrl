"""Spec decomposition.

The architect prompt and reply, the spec audit, the decision register, the
owner answers and the Linear sync.
"""

from __future__ import annotations

from kstrl.architect.decisions import (
    DecisionRegister,
    DecisionRegisterError,
    bind_register,
    build_decisions_context,
    escalation_naming,
    read_decisions,
)
from kstrl.architect.decompose import decompose_spec
from kstrl.architect.decompose_audit import SpecBlockerError
from kstrl.architect.decompose_prompt import load_spec_input, spec_digest
from kstrl.architect.linear import LINEAR_SECTION, LinearConfig, build_linear_sink
from kstrl.architect.owner_answers import OwnerAnswerError
from kstrl.architect.spec_context import build_spec_context
from kstrl.architect.spec_issues import VALID_KINDS

__all__ = [
    "DecisionRegister",
    "DecisionRegisterError",
    "LINEAR_SECTION",
    "LinearConfig",
    "OwnerAnswerError",
    "SpecBlockerError",
    "VALID_KINDS",
    "bind_register",
    "build_decisions_context",
    "build_linear_sink",
    "build_spec_context",
    "decompose_spec",
    "escalation_naming",
    "load_spec_input",
    "read_decisions",
    "spec_digest",
]
