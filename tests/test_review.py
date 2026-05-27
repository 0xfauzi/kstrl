"""Tests for review module."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ralph_py.review import (
    CriterionReview,
    ReviewConcern,
    ReviewMode,
    ReviewResult,
    ReviewVerdict,
    parse_review_output,
    run_review,
)
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import CheckResult, VerificationResult


class MockReviewAgent:
    """Mock agent that returns predetermined review JSON."""

    def __init__(self, output: str):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock-reviewer"

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None,
    ) -> Iterator[str]:
        yield from self._output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


VALID_REVIEW_OUTPUT = json.dumps({
    "stories": [
        {
            "storyId": "US-001",
            "storyTitle": "Create users table",
            "criteria": [
                {
                    "criterion": "Users table exists",
                    "verdict": "pass",
                    "explanation": "CREATE TABLE users found in migration",
                    "suggestion": "",
                },
                {
                    "criterion": "Email index exists",
                    "verdict": "fail",
                    "explanation": "No index on email column found in diff",
                    "suggestion": "Add CREATE UNIQUE INDEX idx_users_email",
                },
            ],
        }
    ],
    "overallNotes": "Migration looks incomplete",
})


class TestParseReviewOutput:
    def test_valid_output(self) -> None:
        result = parse_review_output(VALID_REVIEW_OUTPUT)
        assert len(result.criteria) == 2
        assert result.criteria[0].verdict == "pass"
        assert result.criteria[1].verdict == "fail"
        assert result.overall_notes == "Migration looks incomplete"

    def test_passed_when_no_failures(self) -> None:
        data = json.dumps({
            "stories": [{"storyId": "US-001", "storyTitle": "Test", "criteria": [
                {"criterion": "AC1", "verdict": "pass", "explanation": "ok", "suggestion": ""},
            ]}],
            "overallNotes": "",
        })
        result = parse_review_output(data)
        assert result.passed is True

    def test_failed_when_failures_exist(self) -> None:
        result = parse_review_output(VALID_REVIEW_OUTPUT)
        assert result.passed is False

    def test_invalid_json(self) -> None:
        result = parse_review_output("not json at all")
        assert result.passed is False
        assert "parse" in result.overall_notes.lower()

    def test_json_in_code_fence(self) -> None:
        wrapped = f"```json\n{VALID_REVIEW_OUTPUT}\n```"
        result = parse_review_output(wrapped)
        assert len(result.criteria) == 2


class TestReviewResult:
    def test_as_retry_context(self) -> None:
        result = ReviewResult(
            passed=False,
            mode="hard",
            criteria=[
                CriterionReview("AC1", "pass", "ok"),
                CriterionReview("AC2", "fail", "missing", "add it"),
            ],
        )
        ctx = result.as_retry_context()
        assert "FAIL" in ctx
        assert "AC2" in ctx
        assert "add it" in ctx
        assert "AC1" not in ctx  # pass excluded

    def test_as_pr_body_section(self) -> None:
        result = ReviewResult(
            passed=True,
            mode="advisory",
            criteria=[
                CriterionReview("AC1", "pass", "ok"),
                CriterionReview("AC2", "advisory", "could improve", "refactor"),
            ],
            overall_notes="Looks decent",
        )
        body = result.as_pr_body_section()
        assert "Review Findings" in body
        assert "1 criteria passed" in body
        assert "1 advisory" in body
        assert "Looks decent" in body

    def test_fail_count(self) -> None:
        result = ReviewResult(
            passed=False,
            mode="hard",
            criteria=[
                CriterionReview("AC1", "pass", "ok"),
                CriterionReview("AC2", "fail", "bad"),
                CriterionReview("AC3", "fail", "also bad"),
            ],
        )
        assert result.fail_count == 2
        assert result.advisory_count == 0


class TestConcerns:
    """Tests for the new cross-cutting reviewer concerns surface."""

    def test_parse_extracts_concerns(self) -> None:
        output = json.dumps({
            "stories": [],
            "concerns": [
                {
                    "category": "security_concern",
                    "severity": "fail",
                    "location": "src/auth.py:42-58",
                    "explanation": "Password compared with == (timing oracle)",
                    "suggestion": "Use hmac.compare_digest",
                },
                {
                    "category": "test_quality",
                    "severity": "advisory",
                    "location": "tests/test_auth.py:101",
                    "explanation": "assert True - tautological",
                },
            ],
            "exhaustively_searched": True,
        })
        result = parse_review_output(output)
        assert len(result.concerns) == 2
        assert result.concerns[0].category == "security_concern"
        assert result.exhaustively_searched is True
        assert result.passed is False  # one concern is fail-severity

    def test_concern_fail_blocks_overall_pass(self) -> None:
        output = json.dumps({
            "stories": [{
                "storyId": "US-001",
                "storyTitle": "x",
                "criteria": [{
                    "criterion": "AC1",
                    "verdict": "pass",
                    "explanation": "ok",
                    "suggestion": "",
                }],
            }],
            "concerns": [{
                "category": "dead_code",
                "severity": "fail",
                "location": "src/x.py:10",
                "explanation": "Function f never called",
            }],
        })
        result = parse_review_output(output)
        assert result.passed is False
        assert result.fail_count == 1
        assert result.advisory_count == 0

    def test_advisory_concerns_do_not_block(self) -> None:
        output = json.dumps({
            "stories": [{
                "storyId": "US-001",
                "storyTitle": "x",
                "criteria": [{
                    "criterion": "AC1",
                    "verdict": "pass",
                    "explanation": "ok",
                    "suggestion": "",
                }],
            }],
            "concerns": [{
                "category": "copy_paste",
                "severity": "advisory",
                "location": "src/x.py:10",
                "explanation": "Duplicates helper foo()",
            }],
        })
        result = parse_review_output(output)
        assert result.passed is True
        assert result.advisory_count == 1

    def test_invalid_concern_categories_dropped(self) -> None:
        output = json.dumps({
            "stories": [],
            "concerns": [
                {
                    "category": "made_up_category",
                    "severity": "fail",
                    "location": "x:1",
                    "explanation": "bogus",
                },
                {
                    "category": "security_concern",
                    "severity": "fail",
                    "location": "x:2",
                    "explanation": "legit",
                },
            ],
        })
        result = parse_review_output(output)
        assert len(result.concerns) == 1
        assert result.concerns[0].category == "security_concern"

    def test_invalid_severity_dropped(self) -> None:
        output = json.dumps({
            "stories": [],
            "concerns": [{
                "category": "dead_code",
                "severity": "blocker",  # not a valid severity
                "location": "x:1",
                "explanation": "x",
            }],
        })
        result = parse_review_output(output)
        assert result.concerns == []

    def test_exhaustively_searched_default_false_when_missing(self) -> None:
        output = json.dumps({"stories": [], "concerns": []})
        result = parse_review_output(output)
        assert result.exhaustively_searched is False

    def test_concerns_appear_in_pr_body(self) -> None:
        result = ReviewResult(
            passed=False,
            mode="hard",
            criteria=[CriterionReview("AC1", "pass", "ok")],
            concerns=[
                ReviewConcern(
                    category="security_concern",
                    severity="fail",
                    location="src/x.py:1-5",
                    explanation="hardcoded API key",
                    suggestion="move to env var",
                ),
            ],
        )
        body = result.as_pr_body_section()
        assert "Reviewer concerns" in body
        assert "security_concern" in body
        assert "hardcoded API key" in body
        assert "0 additional concerns" not in body
        assert "1 additional concerns" in body

    def test_advisory_mode_downgrades_concern_failures(
        self, tmp_path: Path,
    ) -> None:
        prd_path = tmp_path / "prd.json"
        prd_path.write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"], "priority": 1,
                "passes": True, "notes": "",
            }],
        }))
        output = json.dumps({
            "stories": [{
                "storyId": "US-001",
                "storyTitle": "x",
                "criteria": [{
                    "criterion": "AC1",
                    "verdict": "pass",
                    "explanation": "ok",
                    "suggestion": "",
                }],
            }],
            "concerns": [{
                "category": "security_concern",
                "severity": "fail",
                "location": "x:1",
                "explanation": "bug",
            }],
        })
        agent = MockReviewAgent(output)
        ui = PlainUI(no_color=True)
        verification = VerificationResult(
            passed=True,
            checks=[CheckResult("test_suite", True, "ok")],
        )
        result = run_review(
            agent, prd_path, tmp_path, "main",
            verification, ReviewMode.ADVISORY, ui,
        )
        # Concern was downgraded; review passes; concern survives as advisory
        assert result.passed is True
        assert result.concerns[0].severity == "advisory"


class TestRunReview:
    def test_skip_mode(self, tmp_path: Path) -> None:
        agent = MockReviewAgent("")
        ui = PlainUI(no_color=True)
        verification = VerificationResult(passed=True, checks=[])

        result = run_review(
            agent, tmp_path / "prd.json", tmp_path, "main",
            verification, ReviewMode.SKIP, ui,
        )
        assert result.passed is True
        assert result.mode == "skip"

    def test_hard_mode_with_failures(self, tmp_path: Path) -> None:
        # Create valid PRD for prompt building
        prd_path = tmp_path / "prd.json"
        prd_path.write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"], "priority": 1,
                "passes": True, "notes": "",
            }],
        }))

        agent = MockReviewAgent(VALID_REVIEW_OUTPUT)
        ui = PlainUI(no_color=True)
        verification = VerificationResult(
            passed=True,
            checks=[CheckResult("test_suite", True, "ok")],
        )

        result = run_review(
            agent, prd_path, tmp_path, "main",
            verification, ReviewMode.HARD, ui,
        )
        assert result.passed is False
        assert result.mode == "hard"

    def test_advisory_mode_downgrades_failures(self, tmp_path: Path) -> None:
        prd_path = tmp_path / "prd.json"
        prd_path.write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"], "priority": 1,
                "passes": True, "notes": "",
            }],
        }))

        agent = MockReviewAgent(VALID_REVIEW_OUTPUT)
        ui = PlainUI(no_color=True)
        verification = VerificationResult(
            passed=True,
            checks=[CheckResult("test_suite", True, "ok")],
        )

        result = run_review(
            agent, prd_path, tmp_path, "main",
            verification, ReviewMode.ADVISORY, ui,
        )
        assert result.passed is True
        assert result.mode == "advisory"
        # All FAILs should be downgraded to ADVISORY
        for cr in result.criteria:
            assert cr.verdict != ReviewVerdict.FAIL.value
