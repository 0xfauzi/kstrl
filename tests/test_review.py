"""Tests for review module."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ralph_py.review import (
    CriterionReview,
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
        assert "1 passed" in body
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
