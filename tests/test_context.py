"""Tests for context module."""

from __future__ import annotations

from kstrl.context import IterationContext, IterationRecord


class TestIterationContext:
    def test_empty_context(self) -> None:
        ctx = IterationContext()
        assert ctx.records == []
        assert ctx.review_findings == []

    def test_add_iteration(self) -> None:
        ctx = IterationContext()
        ctx.add_iteration(IterationRecord(iteration=1, success=False, error="tests failed"))
        assert len(ctx.records) == 1
        assert ctx.records[0].error == "tests failed"

    def test_add_review_finding(self) -> None:
        ctx = IterationContext()
        ctx.add_review_finding("US-001: missing index")
        assert ctx.review_findings == ["US-001: missing index"]

    def test_add_empty_string_ignored(self) -> None:
        ctx = IterationContext()
        ctx.add_review_finding("")
        ctx.add_verification_failure("")
        ctx.add_contract_failure("")
        assert ctx.review_findings == []
        assert ctx.verification_failures == []
        assert ctx.contract_failures == []

    def test_format_for_prompt_empty(self) -> None:
        ctx = IterationContext()
        text = ctx.format_for_prompt()
        assert "PREVIOUS ATTEMPT CONTEXT" in text
        assert "Attempt 1" in text

    def test_format_for_prompt_with_failures(self) -> None:
        ctx = IterationContext()
        ctx.add_iteration(IterationRecord(1, False, "tests failed"))
        ctx.add_verification_failure("- check_test_suite: FAIL - 2 errors")
        ctx.add_review_finding("- US-001: FAIL - missing index")
        ctx.add_contract_failure("- Integration test failed after merging api component")

        text = ctx.format_for_prompt()
        assert "Iteration History" in text
        assert "Verification Failures" in text
        assert "Review Findings" in text
        assert "Contract Test Failures" in text
        assert "Fix ALL issues" in text
        assert "tests failed" in text

    def test_format_attempt_number_increments(self) -> None:
        ctx = IterationContext()
        ctx.add_iteration(IterationRecord(1, False, "fail"))
        ctx.add_iteration(IterationRecord(2, False, "fail again"))
        text = ctx.format_for_prompt()
        assert "Attempt 3" in text  # next attempt after 2 records

    def test_json_roundtrip(self) -> None:
        ctx = IterationContext()
        ctx.add_iteration(IterationRecord(1, False, "err", "summary"))
        ctx.add_review_finding("finding-1")
        ctx.add_verification_failure("check failed")
        ctx.add_contract_failure("integration broke")

        json_str = ctx.to_json()
        restored = IterationContext.from_json(json_str)

        assert len(restored.records) == 1
        assert restored.records[0].iteration == 1
        assert restored.records[0].error == "err"
        assert restored.records[0].summary == "summary"
        assert restored.review_findings == ["finding-1"]
        assert restored.verification_failures == ["check failed"]
        assert restored.contract_failures == ["integration broke"]

    def test_from_json_empty(self) -> None:
        ctx = IterationContext.from_json("")
        assert ctx.records == []

    def test_from_json_empty_object(self) -> None:
        ctx = IterationContext.from_json("{}")
        assert ctx.records == []
