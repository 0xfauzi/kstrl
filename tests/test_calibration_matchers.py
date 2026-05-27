"""F5-matchers: unit tests for the calibration matcher helpers.

These tests exercise ``security_caught`` / ``reviewer_caught`` /
``architect_caught`` against synthetic inputs *without* calling any LLM.
Their job is to catch the kind of latent bug that the F5 baseline run
surfaced -- the security matcher used to reference a nonexistent
``finding.evidence`` attribute and had never been executed end-to-end
until a real calibration run lit it up.

Why this matters: the calibration suite is gated behind
``RALPH_RUN_CALIBRATION=1`` and requires real LLM tokens, so its
matcher code is naturally under-tested. Unit-testing the matchers
guarantees they at least *evaluate correctly* against the input shape
they expect, even when nobody runs the LLM-driven integration.

Tests are organized by role: TestSecurityMatcher / TestReviewerMatcher /
TestArchitectMatcher.
"""

from __future__ import annotations

from ralph_py.decompose import SpecIssue
from ralph_py.review import (
    CriterionReview,
    ReviewConcern,
    ReviewResult,
    ReviewVerdict,
)
from ralph_py.security import SecurityFinding, SecurityMode, SecurityResult
from tests.test_calibration import (
    architect_caught,
    reviewer_caught,
    security_caught,
)


def _security_result(*findings: SecurityFinding) -> SecurityResult:
    return SecurityResult(
        passed=False,
        mode=SecurityMode.HARD.value,
        findings=list(findings),
    )


def _review_result(*concerns: ReviewConcern) -> ReviewResult:
    return ReviewResult(
        passed=False,
        mode="hard",
        criteria=[
            CriterionReview(
                criterion="placeholder",
                verdict=ReviewVerdict.PASS.value,
                explanation="ok",
            ),
        ],
        concerns=list(concerns),
    )


# ---------------------------------------------------------------------------
# Security matcher
# ---------------------------------------------------------------------------


class TestSecurityMatcher:
    def test_matches_when_category_severity_and_path_all_match(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="injection", severity="critical",
                location="src/users.py:11-13",
                explanation="raw sql in get_user",
            ),
        )
        caught, detail = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
            "evidence_path_contains": "src/users.py",
        })
        assert caught
        assert "critical injection at src/users.py:11-13" in detail

    def test_no_match_when_category_differs(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="xss", severity="critical",
                location="src/users.py:11", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert not caught

    def test_no_match_when_severity_below_threshold(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="injection", severity="medium",
                location="src/users.py:11", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert not caught

    def test_no_match_when_location_does_not_contain_expected_path(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="injection", severity="critical",
                location="src/other.py:1", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
            "evidence_path_contains": "src/users.py",
        })
        assert not caught

    def test_path_optional_when_not_required(self) -> None:
        """``evidence_path_contains`` is optional. Omitting it means
        the matcher accepts any location."""
        result = _security_result(
            SecurityFinding(
                category="injection", severity="critical",
                location="anywhere.py", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert caught

    def test_first_matching_finding_wins(self) -> None:
        """When multiple findings match, the first one is returned. This
        mirrors how a real gate would decide -- one matching finding is
        enough to flag the planted bug."""
        result = _security_result(
            SecurityFinding(
                category="injection", severity="high",
                location="src/users.py:11", explanation="first",
            ),
            SecurityFinding(
                category="injection", severity="critical",
                location="src/users.py:42", explanation="second, more severe",
            ),
        )
        caught, detail = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert caught
        # First match returned, NOT the more-severe second one.
        assert "high injection at src/users.py:11" in detail

    def test_skips_non_matching_finding_and_keeps_searching(self) -> None:
        result = _security_result(
            SecurityFinding(
                category="xss", severity="critical",
                location="src/other.py:1", explanation="distractor",
            ),
            SecurityFinding(
                category="injection", severity="critical",
                location="src/users.py:11", explanation="the planted bug",
            ),
        )
        caught, detail = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert caught
        assert "src/users.py:11" in detail

    def test_no_match_against_empty_findings(self) -> None:
        result = _security_result()
        caught, detail = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert not caught
        assert detail == ""

    def test_unknown_severity_in_finding_treated_as_minimal(self) -> None:
        """A SecurityFinding with a severity string not in
        ``{critical, high, medium, low}`` (e.g., the LLM hallucinated
        ``severe`` or ``major``) is treated as rank 0 and therefore
        cannot meet a non-trivial threshold."""
        result = _security_result(
            SecurityFinding(
                category="injection", severity="severe",  # not in taxonomy
                location="src/users.py:1", explanation="...",
            ),
        )
        caught, _ = security_caught(result, {
            "category": "injection",
            "severity_at_least": "high",
        })
        assert not caught


# ---------------------------------------------------------------------------
# Reviewer matcher
# ---------------------------------------------------------------------------


class TestReviewerMatcher:
    def test_matches_concern_with_correct_category_and_severity(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="dead_code", severity="fail",
                location="src/parser.py:15-31",
                explanation="unused branch",
            ),
        )
        caught, detail = reviewer_caught(result, {
            "category": "dead_code",
            "severity_at_least": "fail",
        })
        assert caught
        assert "fail dead_code" in detail

    def test_no_match_when_category_differs(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="scope_creep", severity="fail",
                location="src/parser.py", explanation="...",
            ),
        )
        caught, _ = reviewer_caught(result, {
            "category": "dead_code",
            "severity_at_least": "fail",
        })
        assert not caught

    def test_no_match_when_fail_required_but_concern_is_advisory(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="dead_code", severity="advisory",
                location="src/parser.py", explanation="...",
            ),
        )
        caught, _ = reviewer_caught(result, {
            "category": "dead_code",
            "severity_at_least": "fail",
        })
        assert not caught

    def test_advisory_severity_accepted_when_no_floor(self) -> None:
        """When the fixture does not require ``severity_at_least=fail``,
        an advisory concern is acceptable."""
        result = _review_result(
            ReviewConcern(
                category="dead_code", severity="advisory",
                location="src/parser.py", explanation="...",
            ),
        )
        caught, _ = reviewer_caught(result, {"category": "dead_code"})
        assert caught

    def test_path_filter_applied(self) -> None:
        result = _review_result(
            ReviewConcern(
                category="test_quality", severity="fail",
                location="tests/test_other.py:1",
                explanation="distractor",
            ),
            ReviewConcern(
                category="test_quality", severity="fail",
                location="tests/test_calculator.py:6-8",
                explanation="tautological",
            ),
        )
        caught, detail = reviewer_caught(result, {
            "category": "test_quality",
            "severity_at_least": "fail",
            "evidence_path_contains": "test_calculator.py",
        })
        assert caught
        assert "test_calculator.py" in detail

    def test_no_match_against_empty_concerns(self) -> None:
        result = _review_result()
        caught, detail = reviewer_caught(result, {"category": "dead_code"})
        assert not caught
        assert detail == ""


# ---------------------------------------------------------------------------
# Architect matcher
# ---------------------------------------------------------------------------


class TestArchitectMatcher:
    def test_matches_when_count_kinds_and_severity_all_satisfied(self) -> None:
        issues = [
            SpecIssue(
                kind="missing_detail", severity="blocker",
                summary="no auth story",
            ),
            SpecIssue(
                kind="undefined_failure_mode", severity="major",
                summary="missing error path",
            ),
        ]
        caught, detail = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": ["missing_detail", "undefined_failure_mode"],
            "blocker_or_major": True,
        })
        assert caught
        assert "2 issues" in detail

    def test_no_match_when_count_below_minimum(self) -> None:
        issues = [
            SpecIssue(kind="missing_detail", severity="blocker", summary="..."),
        ]
        caught, _ = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": [],
        })
        assert not caught

    def test_no_match_when_required_kind_not_present(self) -> None:
        """This is the spec-01 case from the F5 baseline -- 8 blocker
        issues but no ``undefined_failure_mode``. Strict matcher fails."""
        issues = [
            SpecIssue(kind="missing_detail", severity="blocker", summary="..."),
            SpecIssue(kind="missing_detail", severity="blocker", summary="..."),
            SpecIssue(kind="ambiguity", severity="blocker", summary="..."),
        ]
        caught, _ = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": ["undefined_failure_mode", "missing_detail"],
        })
        assert not caught

    def test_no_match_when_blocker_or_major_required_but_only_minor(self) -> None:
        issues = [
            SpecIssue(kind="ambiguity", severity="minor", summary="..."),
            SpecIssue(kind="ambiguity", severity="minor", summary="..."),
        ]
        caught, _ = architect_caught(issues, {
            "spec_issues_min": 2,
            "must_include_kind": ["ambiguity"],
            "blocker_or_major": True,
        })
        assert not caught

    def test_matches_when_blocker_or_major_satisfied_by_either(self) -> None:
        major_only = [
            SpecIssue(kind="ambiguity", severity="major", summary="..."),
            SpecIssue(kind="ambiguity", severity="minor", summary="..."),
        ]
        caught, _ = architect_caught(major_only, {
            "spec_issues_min": 2,
            "must_include_kind": ["ambiguity"],
            "blocker_or_major": True,
        })
        assert caught

    def test_required_kinds_field_optional(self) -> None:
        """When ``must_include_kind`` is empty, the matcher passes
        regardless of which kinds appear."""
        issues = [
            SpecIssue(kind="other", severity="blocker", summary="..."),
            SpecIssue(kind="ambiguity", severity="major", summary="..."),
        ]
        caught, _ = architect_caught(issues, {
            "spec_issues_min": 1,
        })
        assert caught

    def test_no_match_against_empty_issues(self) -> None:
        caught, detail = architect_caught([], {"spec_issues_min": 1})
        assert not caught
        assert "got 0 issues" in detail

    def test_default_min_count_is_one(self) -> None:
        """When ``spec_issues_min`` is omitted, at least one issue is
        still required."""
        issues = [SpecIssue(kind="ambiguity", severity="major", summary="...")]
        caught, _ = architect_caught(issues, {})
        assert caught

        empty: list[SpecIssue] = []
        caught_empty, _ = architect_caught(empty, {})
        assert not caught_empty
