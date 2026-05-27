"""Phase 2: Second-opinion review - a separate agent reviews the diff against the spec."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py import git
from ralph_py.decompose import (
    AgentOutputTooLarge,
    _extract_json,
    _select_agent_output,
    collect_agent_output,
)
from ralph_py.findings import Finding
from ralph_py.prd import PRD
from ralph_py.verify import VerificationResult

if TYPE_CHECKING:
    from ralph_py.agents.base import Agent
    from ralph_py.ui.base import UI


class ReviewVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ADVISORY = "advisory"


class ReviewMode(StrEnum):
    HARD = "hard"
    ADVISORY = "advisory"
    SKIP = "skip"


VALID_CONCERN_CATEGORIES = frozenset({
    "scope_creep",
    "security_concern",
    "test_quality",
    "unrelated_change",
    "dead_code",
    "error_handling",
    "copy_paste",
    "other",
})


@dataclass
class CriterionReview:
    """Review verdict for a single acceptance criterion."""

    criterion: str
    verdict: str
    explanation: str
    suggestion: str = ""


@dataclass
class ReviewConcern:
    """A cross-cutting concern the reviewer surfaced beyond the PRD criteria.

    The PRD lists what the implementer was supposed to do. Concerns are
    what they should NOT have done (scope creep, dead code, security
    smells) or did sloppily (tautological tests, swallowed errors). These
    are the bugs a real code reviewer catches that the PRD never asked
    about.
    """

    category: str
    severity: str  # "fail" or "advisory"
    location: str
    explanation: str
    suggestion: str = ""


@dataclass
class ReviewResult:
    """Aggregated review result across all stories."""

    passed: bool
    mode: str
    criteria: list[CriterionReview] = field(default_factory=list)
    concerns: list[ReviewConcern] = field(default_factory=list)
    overall_notes: str = ""
    raw_output: str = ""
    duration_seconds: float = 0.0
    # Self-reported claim that the reviewer searched thoroughly. Useful
    # as a hint when investigating reviews but DO NOT gate on it - it
    # cannot be verified at runtime. The trustworthy verification path
    # is the planted-bug calibration suite at tests/test_calibration.py
    # (runs with RALPH_RUN_CALIBRATION=1) which catches reviewers that
    # claim exhaustive coverage but miss known bugs.
    exhaustively_searched: bool = False
    # E9: parallel to SecurityResult.infrastructure_error - True when
    # the agent failed to run or returned unparseable output, so
    # downstream callers can distinguish "clean review found nothing"
    # from "review never actually happened".
    infrastructure_error: bool = False

    def as_retry_context(self) -> str:
        """Format failing/advisory findings for injection into retry prompt."""
        lines: list[str] = []
        for cr in self.criteria:
            if cr.verdict != ReviewVerdict.PASS.value:
                lines.append(f"- {cr.verdict.upper()}: \"{cr.criterion}\"")
                lines.append(f"  Explanation: {cr.explanation}")
                if cr.suggestion:
                    lines.append(f"  Suggestion: {cr.suggestion}")
        for concern in self.concerns:
            lines.append(
                f"- {concern.severity.upper()} {concern.category}: "
                f"{concern.location}"
            )
            lines.append(f"  Explanation: {concern.explanation}")
            if concern.suggestion:
                lines.append(f"  Suggestion: {concern.suggestion}")
        if self.overall_notes:
            lines.append(f"Overall: {self.overall_notes}")
        return "\n".join(lines)

    def as_pr_body_section(self) -> str:
        """Format all findings for PR description."""
        lines: list[str] = ["## Review Findings", ""]
        # Locals are explicitly criterion-only to avoid colliding with
        # the same-named instance properties (which sum criteria +
        # concerns). The header line below describes criteria; concerns
        # are summarized separately as "additional concerns".
        criterion_pass = sum(
            1 for c in self.criteria if c.verdict == ReviewVerdict.PASS.value
        )
        criterion_fail = sum(
            1 for c in self.criteria if c.verdict == ReviewVerdict.FAIL.value
        )
        criterion_adv = sum(
            1 for c in self.criteria if c.verdict == ReviewVerdict.ADVISORY.value
        )
        lines.append(
            f"**{criterion_pass} criteria passed, {criterion_fail} failed, "
            f"{criterion_adv} advisory; "
            f"{len(self.concerns)} additional concerns**"
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

        if self.concerns:
            lines.append("")
            lines.append("### Reviewer concerns (beyond PRD)")
            for concern in self.concerns:
                icon = "FAIL" if concern.severity == "fail" else "advisory"
                lines.append(
                    f"- [{icon}] **{concern.category}** at `{concern.location}`"
                )
                lines.append(f"  - {concern.explanation}")
                if concern.suggestion:
                    lines.append(f"  - Suggestion: {concern.suggestion}")

        if self.overall_notes:
            lines.append("")
            lines.append(f"**Notes**: {self.overall_notes}")

        return "\n".join(lines)

    @property
    def criterion_fail_count(self) -> int:
        return sum(
            1 for c in self.criteria if c.verdict == ReviewVerdict.FAIL.value
        )

    @property
    def criterion_advisory_count(self) -> int:
        return sum(
            1 for c in self.criteria if c.verdict == ReviewVerdict.ADVISORY.value
        )

    @property
    def concern_fail_count(self) -> int:
        return sum(1 for c in self.concerns if c.severity == "fail")

    @property
    def concern_advisory_count(self) -> int:
        return sum(1 for c in self.concerns if c.severity == "advisory")

    @property
    def fail_count(self) -> int:
        """Total fails across criteria AND concerns. The combined count
        is what gates run_review's pass/fail decision. For observability
        that needs to distinguish (e.g. dashboards), use the
        criterion_/concern_ specific properties instead."""
        return self.criterion_fail_count + self.concern_fail_count

    @property
    def advisory_count(self) -> int:
        """Total advisories across criteria AND concerns. See
        fail_count docstring for the breakdown properties."""
        return self.criterion_advisory_count + self.concern_advisory_count

    def as_findings(self) -> list[Finding]:
        """E3: typed representation of every non-PASS criterion + every
        concern. Used by factory to populate ``Component.findings``.

        Criteria with verdict=PASS are skipped (they're not findings).
        ADVISORY criteria carry severity="advisory"; FAIL criteria carry
        severity="fail". Concerns carry their native severity field
        (already "fail" or "advisory" -- see ReviewConcern).

        E3-infra: when this result has ``infrastructure_error=True``
        (review agent crashed, output unparseable, timeout) returns a
        single synthetic infrastructure_error Finding so downstream
        consumers can distinguish "clean review" (empty list) from
        "review never ran" (one infra finding).
        """
        if self.infrastructure_error:
            return [Finding.infrastructure_error(
                phase="review",
                explanation=(
                    self.overall_notes
                    or "Reviewer agent did not produce parseable output"
                ),
            )]
        out: list[Finding] = []
        for cr in self.criteria:
            if cr.verdict == ReviewVerdict.PASS.value:
                continue
            sev = "fail" if cr.verdict == ReviewVerdict.FAIL.value else "advisory"
            out.append(Finding.from_review_concern(
                category="prd_criterion",
                severity=sev,
                location="",
                explanation=f"{cr.criterion}: {cr.explanation}",
                suggestion=cr.suggestion,
            ))
        for concern in self.concerns:
            out.append(Finding.from_review_concern(
                category=concern.category,
                severity=concern.severity,
                location=concern.location,
                explanation=concern.explanation,
                suggestion=concern.suggestion,
            ))
        return out


REVIEWER_PROMPT_VERSION = "1.0.0"

REVIEWER_PROMPT = """\
You are a hostile senior reviewer. Your default stance is that the diff is
wrong somewhere; your job is to find what's wrong before approving it. A
review that surfaces nothing is suspicious - look harder.

You verify two distinct things:
  1. PRD acceptance criteria - does the diff implement them correctly?
  2. Cross-cutting concerns the PRD did not enumerate - scope creep, dead
     code, sloppy tests, security smells, error-handling gaps, copy-paste.

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
  "concerns": [
    {{
      "category": "scope_creep|security_concern|test_quality|unrelated_change|dead_code|error_handling|copy_paste|other",
      "severity": "fail|advisory",
      "location": "path/to/file.py:42-58",
      "explanation": "evidence-based description of the concern",
      "suggestion": "what to fix"
    }}
  ],
  "exhaustively_searched": true,
  "overallNotes": "cross-cutting observations (empty string if none)"
}}

Verdict rules for PRD criteria:
- "pass": the diff clearly implements this criterion
- "fail": the diff does NOT implement this criterion, or implements it incorrectly
- "advisory": the criterion appears implemented but there are quality concerns
  (poor error handling, missing edge cases, fragile patterns)

Concern categories - look for ALL of these, not just the ones the PRD asked about:
- "scope_creep": changes outside the PRD's stated scope (refactors, drive-by
  edits, unrelated config tweaks)
- "security_concern": hardcoded secrets, shell/SQL/command injection paths,
  missing input validation on a trust boundary, auth/authz bypass, unsafe
  deserialization, broken crypto, predictable randomness for security uses
- "test_quality": tautological assertions (`assert True`, `assert x == x`),
  tests that pass without exercising the change, missing edge-case coverage
  (empty inputs, None, boundary values, error paths), missing negative tests
- "unrelated_change": touches files or symbols outside the component's
  natural scope
- "dead_code": new code with no caller, parameters never used, imports
  unused, conditional branches that cannot fire
- "error_handling": bare excepts, errors silenced with `pass`/empty handlers,
  missing error paths for foreseeable failures, error messages that lose
  information
- "copy_paste": near-duplicate of an existing helper that should be reused
- "other": catch-all for anything that doesn't fit but matters

Severity:
- "fail": this concern is serious enough to block the PR
- "advisory": worth flagging but not blocking

Evidence rules:
- Every verdict AND every concern must cite specific file:line ranges from the diff
- Do not guess - if you cannot verify from the diff, do not assert it
- Be strict: working code that doesn't match the criterion's intent is "fail"
- Be honest: if you genuinely cannot find any concerns after looking hard,
  set "concerns": [] AND "exhaustively_searched": true. Do NOT invent
  concerns to pad the output. But also do not skip looking - silence is
  evidence you didn't try.

Process: read every hunk in the diff. For each new function, ask: what
inputs make this misbehave? what callers does it have? what error paths
does it leave un-handled? For each test, ask: would this test fail if the
implementation were wrong? Then assemble your output.

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
    diff_content: str | None = None,
) -> str:
    """Assemble the full reviewer prompt.

    ``diff_content`` may be pre-fetched by the caller (e.g. the factory
    hoists git.get_diff_content to component scope and reuses the result
    across Phase 2, 2.5, and knowledge distillation). When None, the
    diff is fetched here for backward compatibility.
    """
    prd = PRD.load(prd_path)
    prd_lines: list[str] = []
    for story in prd.user_stories:
        prd_lines.append(f"### {story.id}: {story.title}")
        for ac in story.acceptance_criteria:
            prd_lines.append(f"- {ac}")
        prd_lines.append("")

    if diff_content is None:
        diff_content = git.get_diff_content(base_branch, worktree_path)
    # E2: hide the engineer's Self-Critique block from the reviewer.
    # Otherwise the reviewer sees "Failure mode 1: X" inline and may
    # uncritically conclude X is handled.
    diff_content = git.strip_self_critique_from_diff(diff_content)
    diff_content = git.truncate_diff_for_prompt(diff_content)

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
            infrastructure_error=True,
        )

    criteria: list[CriterionReview] = []

    stories = data.get("stories", [])
    if not isinstance(stories, list):
        return ReviewResult(
            passed=False,
            mode="",
            overall_notes="Invalid review output: 'stories' is not an array",
            raw_output=raw_output[:2000],
            infrastructure_error=True,
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

    concerns: list[ReviewConcern] = []
    raw_concerns = data.get("concerns", [])
    if isinstance(raw_concerns, list):
        for c in raw_concerns:
            if not isinstance(c, dict):
                continue
            category = str(c.get("category", "")).strip()
            severity = str(c.get("severity", "")).strip()
            location = str(c.get("location", "")).strip()
            explanation = str(c.get("explanation", "")).strip()
            # Reject malformed entries instead of silently storing junk
            if category not in VALID_CONCERN_CATEGORIES:
                continue
            if severity not in ("fail", "advisory"):
                continue
            if not explanation:
                continue
            concerns.append(ReviewConcern(
                category=category,
                severity=severity,
                location=location,
                explanation=explanation,
                suggestion=str(c.get("suggestion", "")),
            ))

    exhaustively_searched = bool(data.get("exhaustively_searched", False))

    has_criterion_failures = any(
        c.verdict == ReviewVerdict.FAIL.value for c in criteria
    )
    has_concern_failures = any(c.severity == "fail" for c in concerns)
    overall_notes = str(data.get("overallNotes", ""))

    return ReviewResult(
        passed=not (has_criterion_failures or has_concern_failures),
        mode="",
        criteria=criteria,
        concerns=concerns,
        exhaustively_searched=exhaustively_searched,
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
    diff_content: str | None = None,
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
        diff_content=diff_content,
    )

    try:
        output_lines = collect_agent_output(
            agent, prompt, cwd=worktree_path, timeout=timeout,
        )
    except AgentOutputTooLarge as exc:
        # Hostile/buggy agent flooding output. In hard mode this needs
        # to surface as a review failure; advisory passes but logs.
        result = ReviewResult(
            passed=mode != ReviewMode.HARD,
            mode=mode.value,
            overall_notes=f"Reviewer agent output too large: {exc}",
        )
        result.duration_seconds = time.monotonic() - start
        return result

    raw_output = _select_agent_output(agent, output_lines)
    result = parse_review_output(raw_output)
    result.mode = mode.value
    result.duration_seconds = time.monotonic() - start

    # In advisory mode, downgrade all FAILs and force pass
    if mode == ReviewMode.ADVISORY:
        for cr in result.criteria:
            if cr.verdict == ReviewVerdict.FAIL.value:
                cr.verdict = ReviewVerdict.ADVISORY.value
        for concern in result.concerns:
            if concern.severity == "fail":
                concern.severity = "advisory"
        result.passed = True

    status = "passed" if result.passed else "FAILED"
    ui.info(
        f"  Review {status}: "
        f"{result.fail_count} fail, {result.advisory_count} advisory"
    )

    return result
