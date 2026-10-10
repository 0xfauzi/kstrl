"""Claim agreement: the engineer's ``passes`` flags against the reviewer's verdicts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kstrl.findings import (
    CLAIM_DISAGREEMENT_CATEGORY,
    Finding,
    tag_finding_with_model,
)
from kstrl.prd import PRD
from kstrl.review import ReviewResult, ReviewVerdict, normalize_story_id

if TYPE_CHECKING:
    from kstrl.factory import FactoryConfig


def claim_disagreements(
    prd: PRD,
    review: ReviewResult,
    *,
    severity: str,
) -> list[Finding]:
    """R10.3: one Finding per story where the two checks disagree.

    The engineer agent is the only writer of ``passes`` in the PRD: it
    does the work and then files the report on the work. The reviewer is
    a second, independent reading of the same question. This returns one
    finding for every story the engineer marked done that the reviewer
    did not independently mark pass, whether the reviewer marked it fail,
    marked it advisory, never covered it at all, or passed only some of
    its acceptance criteria.

    That last case is the one the coverage gate in ``parse_review_output``
    does not catch: it checks that every STORY got a verdict, not that
    every CRITERION did. A reviewer that returns one passing criterion
    for a two-criterion story reads as a clean pass there, so the claim
    would be confirmed on half the evidence. Confirmation here needs both
    a pass and a verdict per acceptance criterion.

    Returns ``[]`` when ``review.infrastructure_error`` is set. The
    reviewer crashed or returned unparseable output, so there is no
    second reading; absence of a measurement is not disagreement, and
    reporting it as one would manufacture findings out of an outage. The
    outage itself is already recorded as an infrastructure finding by
    ``as_findings``.

    A story with ``passes`` false never produces a finding. The agent
    made no claim about it, so there is nothing to disagree with.

    Every finding is tagged with the reviewing model identity. The
    comparison is mechanical, but the evidence is one model's verdict,
    and R7.1 exists so a finding can be attributed to the family that
    raised it.
    """
    if review.infrastructure_error:
        return []
    verdicts = review.story_verdicts()
    out: list[Finding] = []
    for story in prd.user_stories:
        if story.passes is not True:
            continue
        verdict = verdicts.get(normalize_story_id(story.id))
        judged = review.judged_criterion_count(story.id)
        expected = len(story.acceptance_criteria)
        if verdict == ReviewVerdict.PASS.value and judged >= expected:
            continue
        unmet = review.non_pass_criteria(story.id)
        # The suggestion carries the reviewer's own criterion texts when
        # it judged any of them unmet. When it judged none unmet but did
        # not judge them all, there is no criterion text to hand over,
        # so the suggestion says what is missing instead. A story with
        # no verdict at all needs neither: the explanation already says
        # "not covered" and repeating the count adds nothing.
        gap = ""
        if verdict is None:
            reads = "not covered"
        elif verdict != ReviewVerdict.PASS.value:
            reads = verdict
        else:
            # Every verdict returned was a pass, but not every
            # acceptance criterion got one. "The reviewer passed this
            # story" and "the reviewer passed the part of it that it
            # looked at" are different facts, and only the first
            # confirms the engineer's claim.
            reads = f"pass on only {judged} of {expected} acceptance criteria"
            gap = (
                f"The reviewer returned {judged} verdict(s) for "
                f"{expected} acceptance criteria, so the story is "
                "unconfirmed rather than judged unmet."
            )
        out.append(
            Finding.from_review_concern(
                category=CLAIM_DISAGREEMENT_CATEGORY,
                severity=severity,
                location=story.id,
                explanation=(
                    f"Story {story.id} is marked passes=true by the engineer "
                    f"but the reviewer's verdict is {reads}"
                ),
                suggestion="; ".join(cr.criterion for cr in unmet) or gap,
            )
        )
    return [tag_finding_with_model(f, review.reviewer_model) for f in out]


#: H3 (#303): fragments claim_retry_context assembles; versioned as one
#: body (docs/adversarial-roadmap.md, H3a sweep row).
CLAIM_RETRY_PROMPT_VERSION = "1.0.0"

CLAIM_RETRY_PROMPT = (
    "Set-point disagreement: you marked the stories below done, but "
    "the reviewer did not confirm them. {did} Implement each one "
    "properly and set the flag again only once its acceptance "
    "criteria are genuinely met."
)
CLAIM_REVERTED_PROMPT = "Their `passes` flags have been reset to false in the PRD."
CLAIM_NOT_REVERTED_PROMPT = (
    "Their `passes` flags could NOT be reset automatically: set each "
    "one back to false yourself before doing anything else."
)
CLAIM_PARTIALLY_JUDGED_PROMPT = (
    "  - Every criterion the reviewer judged passed, but it "
    "did not judge them all. Nothing here says the story is "
    "wrong; it says the story is unconfirmed."
)
CLAIM_NO_VERDICT_PROMPT = (
    "  - The reviewer returned no verdict for this story, so "
    "there is no criterion-level evidence to act on. Check "
    "the story against its acceptance criteria yourself."
)


def _claim_lines_for_finding(finding: Finding, review: ReviewResult) -> list[str]:
    """The detail lines for one disagreement, for :func:`_claim_disagreement_lines`.

    Two different situations reach the "no unmet criteria" branch and
    the agent must not be handed the wrong one. "Nothing was judged" and
    "everything judged passed, but not everything was judged" both leave
    ``unmet`` empty, and the second used to be described as the first -
    contradicting the finding printed directly above it, which had just
    said "pass on only 1 of 2".
    """
    lines = [f"- {finding.explanation}"]
    judged = review.criteria_for(finding.location)
    unmet = [cr for cr in judged if cr.verdict != ReviewVerdict.PASS.value]
    if not unmet:
        lines.append(CLAIM_PARTIALLY_JUDGED_PROMPT if judged else CLAIM_NO_VERDICT_PROMPT)
        return lines
    for cr in unmet:
        lines.append(f"  - [{cr.verdict}] {cr.criterion}")
        if cr.explanation:
            lines.append(f"    - Reviewer: {cr.explanation}")
        if cr.suggestion:
            lines.append(f"    - Suggestion: {cr.suggestion}")
    return lines


def _claim_disagreement_lines(
    disagreements: list[Finding],
    review: ReviewResult,
) -> list[str]:
    """The per-finding detail lines for :func:`claim_retry_context`.

    Extracted out of that function so its branching is not counted
    against the caller's complexity; the caller still owns the header
    line and the empty-disagreements short circuit.
    """
    lines: list[str] = []
    for finding in disagreements:
        lines.extend(_claim_lines_for_finding(finding, review))
    return lines


def claim_retry_context(
    disagreements: list[Finding],
    review: ReviewResult,
    *,
    reverted: bool = True,
) -> str:
    """R10.3: the claim findings as text for the engineer's retry.

    Says what the harness did as well as what it found, because the
    agent will otherwise re-read a PRD it does not expect to have
    changed under it. ``reverted=False`` when the rewritten PRD could
    not be saved: the agent is then asked to reset the flags itself,
    rather than being told about a change that did not happen.

    It renders the reviewer's own explanation and suggestion per unmet
    criterion, not just the criterion text. The criterion text alone
    tells the agent nothing it did not already read in the PRD, and this
    is the one retry path where the reviewer's reasoning does not reach
    the agent by another route: ``as_retry_context`` is added only when
    the review FAILED, and a claim block happens on a review that
    passed.
    """
    if not disagreements:
        return ""
    did = CLAIM_REVERTED_PROMPT if reverted else CLAIM_NOT_REVERTED_PROMPT
    lines = [
        CLAIM_RETRY_PROMPT.format(did=did),
    ]
    lines.extend(_claim_disagreement_lines(disagreements, review))
    return "\n".join(lines)


def revert_unconfirmed_stories(
    prd: PRD,
    review: ReviewResult,
    disagreements: list[Finding],
    *,
    attempt: int,
) -> list[str]:
    """R10.3: reset ``passes`` on every story in *disagreements*.

    Mutates *prd* in place and returns the ids it reverted; the caller
    saves. This is what makes the PRD, the record of what is done, agree
    with the check rather than with the claim. It also feeds back into
    the engineer's own story selection: the prompt tells it to pick the
    highest-priority story where ``passes`` is false, so a reverted
    story is picked up again on the next attempt without the retry text
    having to name it.

    Each revert leaves a note saying who reverted it, on which attempt,
    and the first criterion the reviewer would not pass, so the PRD
    carries the audit trail even if the findings are never read.
    """
    ids = [f.location for f in disagreements if f.location]
    wanted = {normalize_story_id(i) for i in ids}
    reverted: list[str] = []
    for story in prd.user_stories:
        if normalize_story_id(story.id) not in wanted:
            continue
        unmet = review.non_pass_criteria(story.id)
        detail = unmet[0].criterion if unmet else "story not covered by review"
        note = f"reverted by reviewer (attempt {attempt}): {detail}"
        story.passes = False
        story.notes = f"{story.notes}\n{note}" if story.notes else note
        reverted.append(story.id)
    return reverted


def claim_blocks(config: FactoryConfig, autonomy_level: int) -> bool:
    """Whether a claim disagreement should FAIL the component.

    The config can opt in early (``[factory] claim_agreement = "block"``
    with the ladder off), and the autonomy ladder can force it on from L1
    upward, but neither can turn it off once the other wants it. Autonomy is allowed to tighten a
    gate and never to loosen one.

    The gate ships advisory so that its first output is a measurement
    rather than a wall. Graduating the default to "block" is the
    operator's judgement, made after reading real findings, and is
    deliberately not encoded as a number here.
    """
    if config.claim_agreement == "block":
        return True
    # autonomy_level == 0 means the ladder is off, so only the explicit
    # config opt-in above can block. With the ladder on, a run that has
    # earned any autonomy at all should not be taking the agent's word
    # for done: the whole point of the ladder is that less human
    # attention is spent per run, which makes the second check matter
    # more, not less.
    return autonomy_level >= 1
