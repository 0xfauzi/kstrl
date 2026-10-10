"""Tests for the review phase driven through ``run_review`` on a real
review repository (the ``review_repo`` fixture): skip mode, hard mode
failing on a FAIL verdict, advisory mode downgrading both criterion
failures and concern failures.

Parsing of the reviewer's output (fenced JSON, malformed JSON, unknown
verdicts, concern categories) is exercised through ``run_factory`` in
``tests/test_review_gates.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from kstrl.review import (
    ReviewMode,
    ReviewVerdict,
    run_review,
)
from kstrl.ui.plain import PlainUI
from kstrl.verify_model import CheckResult, VerificationResult
from tests.conftest import ReviewRepo, with_observed_diffstat


class MockReviewAgent:
    """Mock agent that returns predetermined review JSON."""

    def __init__(self, output: str):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock-reviewer"

    def run(
        self,
        prompt: str,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        yield from self._output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


VALID_REVIEW_OUTPUT = json.dumps(
    {
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
    }
)


class TestRunReview:
    def test_skip_mode(self, tmp_path: Path) -> None:
        agent = MockReviewAgent("")
        ui = PlainUI(no_color=True)
        verification = VerificationResult(passed=True, checks=[])

        result = run_review(
            agent,
            tmp_path / "prd.json",
            tmp_path,
            "main",
            verification,
            ReviewMode.SKIP,
            ui,
        )
        assert result.passed is True
        assert result.mode == "skip"

    def test_hard_mode_with_failures(self, review_repo: ReviewRepo) -> None:
        # Create valid PRD for prompt building
        prd_path = review_repo.path / "prd.json"
        prd_path.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )

        agent = MockReviewAgent(with_observed_diffstat(VALID_REVIEW_OUTPUT, review_repo))
        ui = PlainUI(no_color=True)
        verification = VerificationResult(
            passed=True,
            checks=[CheckResult("test_suite", True, "ok")],
        )

        result = run_review(
            agent,
            prd_path,
            review_repo.path,
            review_repo.base_branch,
            verification,
            ReviewMode.HARD,
            ui,
        )
        assert result.passed is False
        assert result.mode == "hard"

    def test_advisory_mode_downgrades_failures(self, review_repo: ReviewRepo) -> None:
        prd_path = review_repo.path / "prd.json"
        prd_path.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )

        agent = MockReviewAgent(with_observed_diffstat(VALID_REVIEW_OUTPUT, review_repo))
        ui = PlainUI(no_color=True)
        verification = VerificationResult(
            passed=True,
            checks=[CheckResult("test_suite", True, "ok")],
        )

        result = run_review(
            agent,
            prd_path,
            review_repo.path,
            review_repo.base_branch,
            verification,
            ReviewMode.ADVISORY,
            ui,
        )
        assert result.passed is True
        assert result.mode == "advisory"
        # All FAILs should be downgraded to ADVISORY
        for cr in result.criteria:
            assert cr.verdict != ReviewVerdict.FAIL.value

    def test_advisory_mode_downgrades_concern_failures(
        self,
        review_repo: ReviewRepo,
    ) -> None:
        prd_path = review_repo.path / "prd.json"
        prd_path.write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        output = json.dumps(
            {
                "stories": [
                    {
                        "storyId": "US-001",
                        "storyTitle": "x",
                        "criteria": [
                            {
                                "criterion": "AC1",
                                "verdict": "pass",
                                "explanation": "ok",
                                "suggestion": "",
                            }
                        ],
                    }
                ],
                "concerns": [
                    {
                        "category": "security_concern",
                        "severity": "fail",
                        "location": "x:1",
                        "explanation": "bug",
                    }
                ],
            }
        )
        agent = MockReviewAgent(with_observed_diffstat(output, review_repo))
        ui = PlainUI(no_color=True)
        verification = VerificationResult(
            passed=True,
            checks=[CheckResult("test_suite", True, "ok")],
        )
        result = run_review(
            agent,
            prd_path,
            review_repo.path,
            review_repo.base_branch,
            verification,
            ReviewMode.ADVISORY,
            ui,
        )
        # Concern was downgraded; review passes; concern survives as advisory
        assert result.passed is True
        assert result.concerns[0].severity == "advisory"
