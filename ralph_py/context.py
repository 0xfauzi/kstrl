"""Context accumulation for retry prompts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IterationRecord:
    """Record of a single iteration attempt."""

    iteration: int
    success: bool
    error: str | None = None
    summary: str = ""


@dataclass
class IterationContext:
    """Accumulated context across retries for a component.

    Tracks failures from iterations, verification, review, and contract testing.
    Serializable to JSON for transport across process boundaries.
    """

    records: list[IterationRecord] = field(default_factory=list)
    review_findings: list[str] = field(default_factory=list)
    verification_failures: list[str] = field(default_factory=list)
    contract_failures: list[str] = field(default_factory=list)

    def add_iteration(self, record: IterationRecord) -> None:
        self.records.append(record)

    def add_review_finding(self, finding: str) -> None:
        if finding:
            self.review_findings.append(finding)

    def add_verification_failure(self, failure: str) -> None:
        if failure:
            self.verification_failures.append(failure)

    def add_contract_failure(self, failure: str) -> None:
        if failure:
            self.contract_failures.append(failure)

    def format_for_prompt(self) -> str:
        """Format accumulated context as text to prepend to the agent prompt."""
        sections: list[str] = []

        attempt = len(self.records) + 1
        sections.append(f"=== PREVIOUS ATTEMPT CONTEXT (Attempt {attempt}) ===")

        if self.records:
            sections.append("")
            sections.append("## Iteration History")
            for rec in self.records:
                status = "completed" if rec.success else "FAILED"
                line = f"- Iteration {rec.iteration}: {status}"
                if rec.error:
                    line += f" - {rec.error}"
                if rec.summary:
                    line += f" ({rec.summary})"
                sections.append(line)

        if self.verification_failures:
            sections.append("")
            sections.append("## Verification Failures")
            for failure in self.verification_failures:
                sections.append(failure)

        if self.review_findings:
            sections.append("")
            sections.append("## Review Findings")
            for finding in self.review_findings:
                sections.append(finding)

        if self.contract_failures:
            sections.append("")
            sections.append("## Contract Test Failures")
            for failure in self.contract_failures:
                sections.append(failure)

        sections.append("")
        sections.append("Fix ALL issues listed above before completing.")
        sections.append("=== END PREVIOUS CONTEXT ===")

        return "\n".join(sections)

    def to_json(self) -> str:
        """Serialize to JSON string for ProcessPoolExecutor transport."""
        data: dict[str, Any] = {
            "records": [
                {
                    "iteration": r.iteration,
                    "success": r.success,
                    "error": r.error,
                    "summary": r.summary,
                }
                for r in self.records
            ],
            "review_findings": self.review_findings,
            "verification_failures": self.verification_failures,
            "contract_failures": self.contract_failures,
        }
        return json.dumps(data)

    @classmethod
    def from_json(cls, data: str) -> IterationContext:
        """Deserialize from JSON string."""
        if not data or data == "{}":
            return cls()
        parsed = json.loads(data)
        ctx = cls(
            review_findings=parsed.get("review_findings", []),
            verification_failures=parsed.get("verification_failures", []),
            contract_failures=parsed.get("contract_failures", []),
        )
        for rec_data in parsed.get("records", []):
            ctx.records.append(IterationRecord(
                iteration=rec_data["iteration"],
                success=rec_data["success"],
                error=rec_data.get("error"),
                summary=rec_data.get("summary", ""),
            ))
        return ctx
