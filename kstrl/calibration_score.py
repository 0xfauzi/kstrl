"""The calibration scorer: the matchers that grade a role's reply (#530).

Moved out of ``tests/test_calibration.py`` unchanged, so the paid
calibration suite and the prompt optimizer in :mod:`kstrl.gepa_adapter`
grade a reply with the same functions. Two scorers would let an
optimizer improve against one of them while calibration reads the other.
``tests/test_calibration.py`` imports every name here back, and the unit
tests that pin the matchers (``tests/test_calibration_matchers.py``)
reach them through that import.

The only edits made by the move are type parameters (``dict`` became
``dict[str, Any]``) that ``mypy --strict`` requires of ``kstrl/``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from kstrl.review import ReviewResult
from kstrl.security import SecurityResult

# Realistic mechanical-verification context. The old ``_VERIFICATION_STUB``
# used check names the harness never emits ("tests", "lint"); the real
# reviewer prompt is fed one ``- <check.name>: <PASS|FAIL> - <message>``
# line per check by review.build_review_prompt, where the names come from
# verify.py (test_suite / typecheck / linter / diff_scope / self_critique).
# A clean, passing diff produces this.
_DEFAULT_VERIFICATION: tuple[dict[str, Any], ...] = (
    {"name": "test_suite", "passed": True, "message": "Tests passed"},
    {"name": "typecheck", "passed": True, "message": "Typecheck passed"},
    {"name": "linter", "passed": True, "message": "Linter passed"},
    {"name": "diff_scope", "passed": True, "message": "all files within scope"},
    {"name": "self_critique", "passed": True, "message": "failure modes listed"},
)


def render_verification(meta: dict[str, Any]) -> str:
    """Render a fixture's verification context into the exact shape the
    factory feeds Phase 2 review: one ``- <name>: <PASS|FAIL> - <message>``
    line per check (see review.build_review_prompt).

    Uses the fixture's ``verification`` list when present, so a fixture
    can encode a realistic mixed pass/fail context; otherwise falls back
    to a production-shaped all-pass default. This replaces the old
    all-PASS ``_VERIFICATION_STUB`` (R5.2 context realism) whose check
    names matched no real verifier, so measured reviewer detection now
    transfers to production.
    """
    checks: Sequence[dict[str, Any]] = meta.get("verification") or _DEFAULT_VERIFICATION
    lines: list[str] = []
    for check in checks:
        status = "PASS" if check.get("passed", True) else "FAIL"
        name = str(check.get("name", "check"))
        message = str(check.get("message", ""))
        lines.append(f"- {name}: {status} - {message}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Severity ordering shared with security.py
# ---------------------------------------------------------------------------


_SEV_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _meets_severity(actual: str, threshold: str) -> bool:
    return _SEV_RANK.get(actual, 0) >= _SEV_RANK.get(threshold, 0)


# ---------------------------------------------------------------------------
# Matcher helpers (F5-matchers)
#
# Extracted as top-level functions so they can be unit-tested against
# synthetic inputs in tests/test_calibration_matchers.py without
# requiring a real LLM call. The paid tests in tests/test_calibration.py feed the same
# helpers with output produced by a live agent under
# KSTRL_RUN_CALIBRATION=1. Identical matcher means: a passing unit
# test = the matcher works against the assumptions encoded in the
# fixture meta; a passing integration test = the matcher AND the LLM
# both held up.
# ---------------------------------------------------------------------------


def _acceptable_categories(requirement: dict[str, Any]) -> set[str]:
    """Categories that count as a hit for a positive fixture.

    Supports either a single ``category`` (legacy fixtures) or a
    ``category_any_of`` list. The list form exists for the R5.2 subtle
    bugs whose genuine finding could be labelled under more than one
    taxonomy bucket -- e.g. a signature-comparison timing oracle is
    defensibly ``broken_crypto`` OR ``information_disclosure`` OR
    ``auth_bypass``. Accepting any of them measures *whether the model
    saw the flaw*, not whether it guessed our preferred label; the
    ``evidence_path_contains`` gate still pins the finding to the right
    file so this does not become a free pass.
    """
    if "category_any_of" in requirement:
        return {str(c) for c in requirement["category_any_of"]}
    return {str(requirement["category"])}


def security_caught(
    result: SecurityResult,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the security matcher.

    A finding counts as catching the planted bug when its category is
    among the acceptable categories, severity meets the threshold, and
    (optionally) location contains the expected path substring. First
    match wins; subsequent findings are ignored even if more severe --
    this mirrors how the review path would gate (one matching finding
    is enough).
    """
    acceptable = _acceptable_categories(requirement)
    for finding in result.findings:
        if finding.category not in acceptable:
            continue
        if not _meets_severity(finding.severity, requirement["severity_at_least"]):
            continue
        if requirement.get("evidence_path_contains"):
            if requirement["evidence_path_contains"] not in finding.location:
                continue
        return True, f"{finding.severity} {finding.category} at {finding.location}"
    return False, ""


def security_false_positive(
    result: SecurityResult,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(is_false_positive, detail)`` for a security NEGATIVE
    fixture.

    ``requirement`` is the fixture's ``must_not_flag`` block:
    ``{"categories": [...], "severity_at_least": "high"}``. A false
    positive is any finding whose category is forbidden AND whose
    severity meets the floor (default ``high`` -- the level at which a
    security finding is actionable enough to erode a hard-mode halt).
    Sub-floor findings are ignored so a low-severity nit does not
    inflate the FP rate.
    """
    forbidden = {str(c) for c in requirement.get("categories", [])}
    floor = str(requirement.get("severity_at_least", "high"))
    for finding in result.findings:
        if finding.category not in forbidden:
            continue
        if not _meets_severity(finding.severity, floor):
            continue
        return True, f"{finding.severity} {finding.category} at {finding.location}"
    return False, ""


def reviewer_caught(
    result: ReviewResult,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(caught, detail)`` for the reviewer matcher.

    A concern counts as catching the planted issue when its category is
    among the acceptable categories, and (optionally) severity is
    ``"fail"`` when the fixture demands ``severity_at_least == "fail"``,
    and (optionally) location contains the expected path substring.
    """
    acceptable = _acceptable_categories(requirement)
    for concern in result.concerns:
        if concern.category not in acceptable:
            continue
        if requirement.get("severity_at_least") == "fail":
            if concern.severity != "fail":
                continue
        if requirement.get("evidence_path_contains"):
            if requirement["evidence_path_contains"] not in concern.location:
                continue
        return True, (f"{concern.severity} {concern.category} at {concern.location}")
    return False, ""


def reviewer_false_positive(
    result: ReviewResult,
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(is_false_positive, detail)`` for a reviewer NEGATIVE
    fixture.

    ``requirement`` is the fixture's ``must_not_flag`` block. A false
    positive is a concern whose category is forbidden. When the floor is
    ``fail`` (the default), only *blocking* concerns count: an advisory
    concern does not halt the PR, so it does not erode halt credibility.
    Any other floor also counts advisory concerns.
    """
    forbidden = {str(c) for c in requirement.get("categories", [])}
    floor = str(requirement.get("severity_at_least", "fail"))
    for concern in result.concerns:
        if concern.category not in forbidden:
            continue
        if floor == "fail" and concern.severity != "fail":
            continue
        return True, (f"{concern.severity} {concern.category} at {concern.location}")
    return False, ""
