"""Phase 2: Second-opinion review - a separate agent reviews the diff against the spec."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py import git
from ralph_py.decompose import _extract_json
from ralph_py.prd import PRD
from ralph_py.verify import VerificationResult

if TYPE_CHECKING:
    from ralph_py.agents.base import Agent
    from ralph_py.ui.base import UI


class ReviewVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ADVISORY = "advisory"


class ReviewMode(str, Enum):
    HARD = "hard"
    ADVISORY = "advisory"
    SKIP = "skip"


@dataclass
class CriterionReview:
    """Review verdict for a single acceptance criterion."""

    criterion: str
    verdict: str
    explanation: str
    suggestion: str = ""


@dataclass
class ReviewResult:
    """Aggregated review result across all stories."""

    passed: bool
    mode: str
    criteria: list[CriterionReview] = field(default_factory=list)
    overall_notes: str = ""
    raw_output: str = ""
    duration_seconds: float = 0.0

    def as_retry_context(self) -> str:
        """Format failing/advisory criteria for injection into retry prompt."""
        lines: list[str] = []
        for cr in self.criteria:
            if cr.verdict != ReviewVerdict.PASS.value:
                lines.append(f"- {cr.verdict.upper()}: \"{cr.criterion}\"")
                lines.append(f"  Explanation: {cr.explanation}")
                if cr.suggestion:
                    lines.append(f"  Suggestion: {cr.suggestion}")
        if self.overall_notes:
            lines.append(f"Overall: {self.overall_notes}")
        return "\n".join(lines)

    def as_pr_body_section(self) -> str:
        """Format all findings for PR description."""
        lines: list[str] = ["## Review Findings", ""]
        pass_count = sum(1 for c in self.criteria if c.verdict == ReviewVerdict.PASS.value)
        fail_count = sum(1 for c in self.criteria if c.verdict == ReviewVerdict.FAIL.value)
        adv_count = sum(
            1 for c in self.criteria if c.verdict == ReviewVerdict.ADVISORY.value
        )
        lines.append(
            f"**{pass_count} passed, {fail_count} failed, {adv_count} advisory**"
        )
        lines.append("")

        for cr in self.criteria:
            if cr.verdict == ReviewVerdict.PASS.value:
                icon = "pass"
            elif cr.verdict == ReviewVerdict.FAIL.value:
                icon = "FAIL"
            else:
                icon = "advisory"
            lines.append(f"- [{icon}] {cr.criterion}")
            if cr.verdict != ReviewVerdict.PASS.value:
                lines.append(f"  - {cr.explanation}")
                if cr.suggestion:
                    lines.append(f"  - Suggestion: {cr.suggestion}")

        if self.overall_notes:
            lines.append("")
            lines.append(f"**Notes**: {self.overall_notes}")

        return "\n".join(lines)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.criteria if c.verdict == ReviewVerdict.FAIL.value)

    @property
    def advisory_count(self) -> int:
        return sum(
            1 for c in self.criteria if c.verdict == ReviewVerdict.ADVISORY.value
        )


REVIEWER_PROMPT = """\
You are a senior code reviewer. Your job is to verify that a git diff correctly
implements the acceptance criteria in a PRD (Product Requirements Document).

You must output ONLY valid JSON (no Markdown, no code fences, no explanation).

Output schema:
{{
  "stories": [
    {{
      "storyId": "US-001",
      "storyTitle": "Short title",
      "criteria": [
        {{
          "criterion": "exact text from PRD acceptance criteria",
          "verdict": "pass|fail|advisory",
          "explanation": "evidence-based reason for this verdict",
          "suggestion": "what to fix (empty string if pass)"
        }}
      ]
    }}
  ],
  "overallNotes": "cross-cutting observations (empty string if none)"
}}

Verdict rules:
- "pass": the diff clearly implements this criterion
- "fail": the diff does NOT implement this criterion, or implements it incorrectly
- "advisory": the criterion appears implemented but there are quality concerns
  (poor error handling, missing edge cases, fragile patterns)

Evidence rules:
- Every verdict must reference specific files/lines from the diff
- Do not guess - if you cannot verify a criterion from the diff, mark it "fail"
- Be strict: working code that doesn't match the criterion's intent is a "fail"

================================================================================
PRD (acceptance criteria to verify)
================================================================================

{prd_content}

================================================================================
GIT DIFF (changes to review)
================================================================================

{diff_content}

================================================================================
MECHANICAL VERIFICATION RESULTS
================================================================================

{verification_summary}
"""


def build_review_prompt(
    prd_path: Path,
    worktree_path: Path,
    base_branch: str,
    verification_result: VerificationResult,
) -> str:
    """Assemble the full reviewer prompt."""
    prd = PRD.load(prd_path)
    prd_lines: list[str] = []
    for story in prd.user_stories:
        prd_lines.append(f"### {story.id}: {story.title}")
        for ac in story.acceptance_criteria:
            prd_lines.append(f"- {ac}")
        prd_lines.append("")

    diff_content = git.get_diff_content(base_branch, worktree_path)
    if len(diff_content) > 50000:
        diff_content = diff_content[:50000] + "\n... (diff truncated at 50KB)"

    verify_lines: list[str] = []
    for check in verification_result.checks:
        status = "PASS" if check.passed else "FAIL"
        verify_lines.append(f"- {check.name}: {status} - {check.message}")

    return REVIEWER_PROMPT.format(
        prd_content="\n".join(prd_lines),
        diff_content=diff_content,
        verification_summary="\n".join(verify_lines),
    )


def parse_review_output(raw_output: str) -> ReviewResult:
    """Parse structured JSON from reviewer agent output."""
    try:
        data = _extract_json(raw_output)
    except ValueError:
        return ReviewResult(
            passed=False,
            mode="",
            overall_notes="Failed to parse reviewer output as JSON",
            raw_output=raw_output[:2000],
        )

    criteria: list[CriterionReview] = []

    stories = data.get("stories", [])
    if not isinstance(stories, list):
        return ReviewResult(
            passed=False,
            mode="",
            overall_notes="Invalid review output: 'stories' is not an array",
            raw_output=raw_output[:2000],
        )

    for story in stories:
        if not isinstance(story, dict):
            continue
        for crit_data in story.get("criteria", []):
            if not isinstance(crit_data, dict):
                continue
            criteria.append(CriterionReview(
                criterion=str(crit_data.get("criterion", "")),
                verdict=str(crit_data.get("verdict", "fail")),
                explanation=str(crit_data.get("explanation", "")),
                suggestion=str(crit_data.get("suggestion", "")),
            ))

    has_failures = any(c.verdict == ReviewVerdict.FAIL.value for c in criteria)
    overall_notes = str(data.get("overallNotes", ""))

    return ReviewResult(
        passed=not has_failures,
        mode="",
        criteria=criteria,
        overall_notes=overall_notes,
        raw_output=raw_output[:2000],
    )


def run_review(
    agent: Agent,
    prd_path: Path,
    worktree_path: Path,
    base_branch: str,
    verification_result: VerificationResult,
    mode: ReviewMode,
    ui: UI,
    timeout: float = 600.0,
) -> ReviewResult:
    """Run the full review: build prompt, run agent, parse output.

    In advisory mode, all FAILs are downgraded and passed=True is returned.
    """
    if mode == ReviewMode.SKIP:
        return ReviewResult(passed=True, mode=mode.value)

    ui.info("  Running second-opinion review...")
    start = time.monotonic()

    prompt = build_review_prompt(
        prd_path, worktree_path, base_branch, verification_result,
    )

    output_lines: list[str] = []
    for line in agent.run(prompt, cwd=worktree_path, timeout=timeout):
        output_lines.append(line)

    raw_output = "\n".join(output_lines)
    result = parse_review_output(raw_output)
    result.mode = mode.value
    result.duration_seconds = time.monotonic() - start

    # In advisory mode, downgrade all FAILs and force pass
    if mode == ReviewMode.ADVISORY:
        for cr in result.criteria:
            if cr.verdict == ReviewVerdict.FAIL.value:
                cr.verdict = ReviewVerdict.ADVISORY.value
        result.passed = True

    status = "passed" if result.passed else "FAILED"
    ui.info(
        f"  Review {status}: "
        f"{result.fail_count} fail, {result.advisory_count} advisory"
    )

    return result
