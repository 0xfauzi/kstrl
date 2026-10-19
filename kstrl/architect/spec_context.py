"""The spec the plan was made from, as the engineer is given it (owner decision of 2026-10-19).

The architect turns the spec into PRDs and a decision register, and a
detail it leaves out of both (a type the spec names, an exact output) was
lost to the engineer while the verification designer, which reads the
whole spec, still checked it: a user run halted on a held-out check that
expected an ``int64`` the engineer was never shown.
"""

from __future__ import annotations

#: H3: engineer-facing context, enrolled in tests/test_prompt_versions.py.
#: The calibration suite scores no engineer-facing fixture, so it carries
#: the H3 obligation and no H2 obligation the suite can discharge.
SPEC_CONTEXT_PROMPT_VERSION = "1.0.0"

SPEC_CONTEXT_PROMPT = """\
## The Spec

The specification this plan was made from, as the operator wrote it. Your
PRD's stories are the part of it this component builds; build only those,
because other components build the rest. Where the spec states a detail
your PRD leaves open, such as a name, a type or an exact output, follow the
spec. Where your PRD or an architect decision below settles something
differently from the spec, follow the PRD and the decision, and name the
difference in your Self-Critique.

{spec}
"""


def build_spec_context(spec: str) -> str:
    """The spec block of an engineer's context, or "" when the plan names no spec."""
    return SPEC_CONTEXT_PROMPT.format(spec=spec.strip()) if spec.strip() else ""
