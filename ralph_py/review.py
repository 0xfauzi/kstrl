"""Phase 2: Second-opinion review - a separate agent reviews the diff against the spec."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
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
from ralph_py.findings import Finding, dump_raw_debug
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

# R1.1: whitelist of criterion verdicts accepted from the reviewer,
# compared case-insensitively after stripping. The prompt schema
# promises pass|fail|advisory; anything else ("Blocked", "n/a", a
# missing key) is a parse failure - infrastructure error, never a
# silently non-blocking advisory.
VALID_CRITERION_VERDICTS = frozenset({
    ReviewVerdict.PASS.value,
    ReviewVerdict.FAIL.value,
    ReviewVerdict.ADVISORY.value,
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
    # R1.4 (H-16): True when the reviewed diff was truncated at the
    # prompt cap, i.e. the verdict covers only a prefix of the change.
    # Advisory mode may pass a partial review, but the pass must be
    # visibly partial (PR body annotation + an advisory concern);
    # hard mode never accepts a partial review - the factory chunks
    # the diff and runs one pass per chunk instead.
    partial: bool = False

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
        if self.partial:
            lines.append("")
            lines.append(
                "**PARTIAL REVIEW (R1.4): the diff exceeded the prompt "
                "size cap; only a truncated prefix was reviewed**"
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
    hoists git.get_diff_content to component scope, strips the
    Self-Critique block ONCE, and shares the result with Phase 2 and
    2.5). A provided diff is used as-is apart from the size cap: the
    caller owns Self-Critique hygiene (R1.4 - one strip in the factory
    instead of one per phase). When None, the diff is fetched AND
    stripped here (E2).
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


def parse_review_output(
    raw_output: str,
    expected_story_ids: Sequence[str] | None = None,
    *,
    debug_dir: Path | None = None,
) -> ReviewResult:
    """Parse structured JSON from reviewer agent output.

    ``expected_story_ids`` enables the R1.1 criterion-coverage gate:
    every PRD story id must receive at least one criterion verdict or
    the result is an infrastructure error - a partial or empty review
    (``{"stories": [], "concerns": []}``) is a review that did not
    happen, not a clean pass (CRIT-5). Matching is by story id
    (case-insensitive, whitespace-stripped), never by criterion text.
    ``None`` skips the check for callers that have no PRD.

    ``debug_dir`` enables a full raw-output dump on parse failure via
    :func:`ralph_py.findings.dump_raw_debug`; the result's
    ``raw_output`` field stays truncated to 2000 chars to bound
    manifest/journal size.
    """

    def _infra(notes: str, label: str) -> ReviewResult:
        dump_path = dump_raw_debug(debug_dir, "review", raw_output, label)
        if dump_path:
            notes = f"{notes} [full raw output: {dump_path}]"
        return ReviewResult(
            passed=False,
            mode="",
            overall_notes=notes,
            raw_output=raw_output[:2000],
            infrastructure_error=True,
        )

    try:
        data = _extract_json(raw_output)
    except ValueError:
        return _infra("Failed to parse reviewer output as JSON", "no_json")

    # R1.2: _extract_json returns whatever json.loads produced - null,
    # a list, a bare string. Anything but an object would crash the
    # .get() calls below with AttributeError.
    if not isinstance(data, dict):
        return _infra(
            f"Review output was not a JSON object (got {type(data).__name__})",
            "non_dict_json",
        )

    criteria: list[CriterionReview] = []

    stories = data.get("stories", [])
    if not isinstance(stories, list):
        return _infra(
            "Invalid review output: 'stories' is not an array",
            "stories_not_array",
        )

    covered_story_ids: set[str] = set()
    invalid_verdicts: list[str] = []
    for story in stories:
        if not isinstance(story, dict):
            continue
        story_id = str(story.get("storyId", "")).strip()
        raw_criteria = story.get("criteria", [])
        if not isinstance(raw_criteria, list):
            continue
        for crit_data in raw_criteria:
            if not isinstance(crit_data, dict):
                continue
            # R1.1: normalize then whitelist. Verbatim storage meant
            # "FAIL" matched neither the fail gate nor the pass gate
            # and became a non-blocking advisory-alike.
            verdict = str(crit_data.get("verdict", "")).strip().lower()
            if verdict not in VALID_CRITERION_VERDICTS:
                invalid_verdicts.append(
                    str(crit_data.get("verdict", ""))[:40] or "<missing>"
                )
                continue
            criteria.append(CriterionReview(
                criterion=str(crit_data.get("criterion", "")),
                verdict=verdict,
                explanation=str(crit_data.get("explanation", "")),
                suggestion=str(crit_data.get("suggestion", "")),
            ))
            if story_id:
                covered_story_ids.add(story_id.lower())

    if invalid_verdicts:
        return _infra(
            "Review output contained unrecognized verdicts: "
            + ", ".join(repr(v) for v in invalid_verdicts)
            + " (valid: pass/fail/advisory)",
            "invalid_verdict",
        )

    if expected_story_ids:
        missing = [
            sid for sid in expected_story_ids
            if sid.strip().lower() not in covered_story_ids
        ]
        if missing:
            return _infra(
                "Review coverage incomplete: no verdict for story ids "
                + ", ".join(missing)
                + " (CRIT-5: a partial or empty review cannot pass)",
                "coverage_gap",
            )

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
    *,
    debug_dir: Path | None = None,
) -> ReviewResult:
    """Run the full review: build prompt, run agent, parse output.

    In advisory mode, all FAILs are downgraded and passed=True is returned.

    Never raises: any agent/prompt failure degrades to a ReviewResult
    with ``infrastructure_error=True`` so one broken reviewer fails one
    component instead of aborting the whole factory run (R1.2, mirrors
    ``run_security_review``).
    """
    if mode == ReviewMode.SKIP:
        return ReviewResult(passed=True, mode=mode.value)

    ui.info("  Running second-opinion review...")
    start = time.monotonic()

    truncated = False
    try:
        if diff_content is None:
            # Same fallback contract as build_review_prompt: fetch AND
            # strip the Self-Critique block (E2) when the caller did
            # not provide a pre-stripped diff.
            diff_content = git.get_diff_content(base_branch, worktree_path)
            diff_content = git.strip_self_critique_from_diff(diff_content)
        # R1.4: anything past the cap is invisible to the reviewer.
        truncated = len(diff_content) > git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT
        prompt = build_review_prompt(
            prd_path, worktree_path, base_branch, verification_result,
            diff_content=diff_content,
        )
        # R1.1: the coverage gate needs the ground-truth story ids from
        # the PRD, not whatever ids the reviewer chose to mention.
        expected_story_ids = [
            story.id for story in PRD.load(prd_path).user_stories
        ]
        output_lines = collect_agent_output(
            agent, prompt, cwd=worktree_path, timeout=timeout,
        )
    except AgentOutputTooLarge as exc:
        # Hostile/buggy agent flooding output. The review never
        # happened: infrastructure error (H-13), which hard mode blocks
        # on; advisory passes but the infra finding stays visible.
        ui.warn(f"  Reviewer agent output too large: {exc}")
        result = ReviewResult(
            passed=mode != ReviewMode.HARD,
            mode=mode.value,
            overall_notes=f"Reviewer agent output too large: {exc}",
            infrastructure_error=True,
        )
        result.duration_seconds = time.monotonic() - start
        return result
    except Exception as exc:  # noqa: BLE001
        # Reviewer agent crashed (or the PRD/diff could not be read).
        # Degrade to a per-component infrastructure failure - never
        # propagate and abort the run (R1.2).
        ui.warn(f"  Reviewer agent failed: {exc}")
        result = ReviewResult(
            passed=mode != ReviewMode.HARD,
            mode=mode.value,
            overall_notes=f"Reviewer agent failed: {exc}",
            infrastructure_error=True,
        )
        result.duration_seconds = time.monotonic() - start
        return result

    raw_output = _select_agent_output(agent, output_lines)
    result = parse_review_output(
        raw_output, expected_story_ids, debug_dir=debug_dir,
    )
    result.mode = mode.value
    result.duration_seconds = time.monotonic() - start

    if truncated and not result.infrastructure_error:
        # R1.4: the verdict covers only a prefix of the diff. Advisory
        # mode may pass, but visibly: flag the result and inject an
        # advisory concern so the PR body, findings stream, and retry
        # context all say "partial".
        result.partial = True
        result.concerns.append(ReviewConcern(
            category="other",
            severity="advisory",
            location="",
            explanation=(
                "Partial review (R1.4): the diff exceeded the "
                f"{git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT // 1000}KB prompt "
                "cap and only the truncated prefix was reviewed; "
                "anything past the cut is unreviewed."
            ),
            suggestion=(
                "Split the component or reduce the diff so the full "
                "change fits one review pass."
            ),
        ))
        if mode == ReviewMode.HARD:
            # Backstop, not the primary path: the factory chunks
            # oversized diffs before calling us (run_chunked_review).
            # Reaching this branch means a caller bypassed that policy,
            # and hard mode must never approve a partially visible diff
            # (H-16: the unreviewed tail would merge). Fail closed as
            # infrastructure - the review did not fully happen.
            result.passed = False
            result.infrastructure_error = True
            result.overall_notes = (
                "Hard-mode review received an oversized diff without "
                "chunking; the unreviewed tail cannot be approved "
                "(R1.4). " + result.overall_notes
            ).strip()

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
    partial_note = " (PARTIAL: diff truncated)" if result.partial else ""
    ui.info(
        f"  Review {status}{partial_note}: "
        f"{result.fail_count} fail, {result.advisory_count} advisory"
    )

    return result


def merge_review_results(
    results: list[ReviewResult], mode: str,
) -> ReviewResult:
    """R1.4: merge the per-chunk results of a chunked review into one.

    Policy (H-16): any chunk failure fails the merged result; criteria
    and concerns concatenate; any chunk infrastructure error marks the
    merged result as an infrastructure error (the review did not fully
    happen). ``exhaustively_searched`` survives only when every chunk
    claimed it (hint, never a gate).

    Known limitation, documented on purpose: when a chunk infra-errors,
    ``as_findings()`` renders only the infrastructure finding (its
    contract: an errored review has no trustworthy findings), while the
    concatenated criteria/concerns stay visible in the PR body and
    retry context via ``as_pr_body_section``/``as_retry_context``.
    """
    if not results:
        raise ValueError("merge_review_results requires at least one result")
    n = len(results)
    merged = ReviewResult(
        passed=all(r.passed for r in results),
        mode=mode,
        infrastructure_error=any(r.infrastructure_error for r in results),
        exhaustively_searched=all(r.exhaustively_searched for r in results),
        partial=any(r.partial for r in results),
    )
    notes = [f"Chunked review: {n} passes over an oversized diff (R1.4)."]
    raw_parts: list[str] = []
    for i, r in enumerate(results, 1):
        merged.criteria.extend(r.criteria)
        merged.concerns.extend(r.concerns)
        merged.duration_seconds += r.duration_seconds
        if r.overall_notes:
            notes.append(f"[chunk {i}/{n}] {r.overall_notes}")
        raw_parts.append(f"--- chunk {i}/{n} ---\n{r.raw_output}")
    merged.overall_notes = "\n".join(notes)
    merged.raw_output = "\n".join(raw_parts)
    return merged


def run_chunked_review(
    agent: Agent,
    prd_path: Path,
    worktree_path: Path,
    base_branch: str,
    verification_result: VerificationResult,
    mode: ReviewMode,
    ui: UI,
    diff_chunks: list[str],
    timeout: float = 600.0,
    *,
    budget_remaining: int | None = None,
    consume_budget: Callable[[], None] | None = None,
    debug_dir: Path | None = None,
) -> ReviewResult:
    """R1.4 (H-16): review an oversized diff chunk by chunk, one agent
    pass per chunk, and merge the verdicts.

    Every pass counts against the adversarial budget via
    ``consume_budget``. When ``budget_remaining`` cannot cover one pass
    per chunk, NO pass runs and an infrastructure-error result is
    returned: a diff that cannot be fully reviewed must fail loudly,
    never pass partially. ``budget_remaining=None`` means unbounded.
    """
    n = len(diff_chunks)
    if n == 0:
        raise ValueError("run_chunked_review requires at least one chunk")
    if budget_remaining is not None and budget_remaining < n:
        return ReviewResult(
            passed=False,
            mode=mode.value,
            overall_notes=(
                f"Chunked review needs {n} adversarial calls for "
                f"{n} diff chunks but only {budget_remaining} remain in "
                "max_adversarial_calls; refusing to review the diff "
                "partially (R1.4)"
            ),
            infrastructure_error=True,
        )
    results: list[ReviewResult] = []
    for i, chunk in enumerate(diff_chunks, 1):
        if consume_budget is not None:
            consume_budget()
        ui.info(f"    Review chunk {i}/{n}...")
        results.append(run_review(
            agent, prd_path, worktree_path, base_branch,
            verification_result, mode, ui,
            timeout=timeout,
            diff_content=chunk,
            # Per-chunk subdir: dump_raw_debug writes fixed filenames,
            # so sharing one dir would overwrite earlier chunks' dumps.
            debug_dir=debug_dir / f"chunk-{i}" if debug_dir else None,
        ))
    return merge_review_results(results, mode.value)
