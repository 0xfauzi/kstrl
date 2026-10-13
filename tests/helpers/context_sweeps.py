"""Retry-context section reader and the bucket-rule sweep corpus.

Moved here from ``tests/test_context.py`` when its unit tests were
folded into the run-level retry-context tests (the retry prompt that
``run_loop`` hands the engineer is what the pipeline tests read). The
headings are the ones ``IterationContext.format_for_prompt`` writes
(#223 bucket rule: current, not re-measured, resolved or superseded).
"""

from __future__ import annotations

CURRENT = "## Current failures"
NOT_REMEASURED = "## Not re-measured"
RESOLVED = "## Resolved or superseded"


def section(text: str, heading: str) -> str:
    """The body under ``heading``, or "" when the section is absent.

    A section runs from its heading to the next heading, the closing
    instruction, or the end marker. Test failure texts are single-line,
    so no body line here is blank.
    """
    terminators = ("## ", "=== END", "Fix the current failures.")
    body: list[str] = []
    capturing = False
    for line in text.splitlines():
        if line.startswith(heading):
            capturing = True
            continue
        if capturing:
            if line.startswith(terminators):
                break
            body.append(line)
    if not capturing:
        return ""
    return "\n".join(body).strip()
