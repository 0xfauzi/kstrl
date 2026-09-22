"""R10.3 claim agreement: a story is done only when both checks say so.

The engineer agent is the only writer of the PRD's ``passes`` flag: the
thing that does the work also files the report on the work. The reviewer
is a second, independent reading of the same question, and it has been
available and ignored since R1.1. These tests pin the rule that the two
must agree, and the two modes it ships in - record only, or revert the
flag and retry.

The pipeline-level tests (``tests/test_claim_agreement_pipeline.py``)
import the harness from tests.test_pipeline rather than rebuilding it,
which is also where the reviewer stub seam lives (``_recording_hooks``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kstrl.evolution import signatures_from_findings
from kstrl.factory import FactoryConfig
from kstrl.findings import CLAIM_DISAGREEMENT_CATEGORY, finding_model
from kstrl.manifest import Component
from kstrl.pr import _generate_pr_body
from kstrl.prd import PRD, UserStory
from kstrl.review import (
    CriterionReview,
    ReviewResult,
    claim_blocks,
    claim_disagreements,
    claim_retry_context,
    parse_review_output,
    revert_unconfirmed_stories,
)
from tests.test_pipeline import _component

# --------------------------------------------------------------------
# builders
# --------------------------------------------------------------------


def _story(
    story_id: str,
    *,
    passes: bool,
    criteria: list[str] | None = None,
    notes: str = "",
) -> UserStory:
    return UserStory(
        id=story_id,
        title=f"Story {story_id}",
        acceptance_criteria=criteria or [f"{story_id} works"],
        priority=1,
        passes=passes,
        notes=notes,
    )


def _prd(*stories: UserStory) -> PRD:
    return PRD(branch_name="kstrl/factory/comp-a", user_stories=list(stories))


def _criterion(
    story_id: str,
    verdict: str,
    criterion: str = "does the thing",
) -> CriterionReview:
    return CriterionReview(
        criterion=criterion,
        verdict=verdict,
        explanation=f"{verdict} because",
        suggestion="" if verdict == "pass" else "fix it",
        story_id=story_id,
    )


def _review(
    *criteria: CriterionReview,
    passed: bool = True,
    infra: bool = False,
    model: str = "",
) -> ReviewResult:
    return ReviewResult(
        passed=passed,
        mode="advisory",
        criteria=list(criteria),
        infrastructure_error=infra,
        reviewer_model=model,
    )


def _write_prd(root: Path, comp: Component, prd: PRD) -> Path:
    path = root / comp.prd_path
    path.parent.mkdir(parents=True, exist_ok=True)
    prd.save(path)
    return path


# --------------------------------------------------------------------
# 1. the parser keeps the story id
# --------------------------------------------------------------------


class TestParserKeepsStoryId:
    def test_parse_review_output_keeps_story_id(self) -> None:
        raw = json.dumps(
            {
                "stories": [
                    {
                        "storyId": "US-001",
                        "criteria": [
                            {
                                "criterion": "a",
                                "verdict": "pass",
                                "explanation": "ok",
                                "suggestion": "",
                            },
                        ],
                    },
                    {
                        "storyId": "US-002",
                        "criteria": [
                            {
                                "criterion": "b",
                                "verdict": "fail",
                                "explanation": "no",
                                "suggestion": "do b",
                            },
                        ],
                    },
                ],
                "concerns": [],
                "overallNotes": "",
            }
        )
        result = parse_review_output(raw)
        assert not result.infrastructure_error, result.overall_notes
        assert [c.story_id for c in result.criteria] == ["US-001", "US-002"]

    def test_story_id_is_stored_stripped_but_not_lowercased(self) -> None:
        """The raw value stays inspectable; normalising happens at
        lookup, which is what lets a reviewer's case drift still match."""
        raw = json.dumps(
            {
                "stories": [
                    {
                        "storyId": "  us-001  ",
                        "criteria": [
                            {
                                "criterion": "a",
                                "verdict": "pass",
                                "explanation": "ok",
                                "suggestion": "",
                            }
                        ],
                    }
                ],
                "concerns": [],
                "overallNotes": "",
            }
        )
        result = parse_review_output(raw)
        assert result.criteria[0].story_id == "us-001"


# --------------------------------------------------------------------
# 2. rolling criterion verdicts up into a story verdict
# --------------------------------------------------------------------


class TestStoryVerdicts:
    def test_story_verdicts_fail_dominates(self) -> None:
        review = _review(
            _criterion("A", "pass"),
            _criterion("A", "fail"),
            _criterion("A", "advisory"),
        )
        assert review.story_verdicts() == {"a": "fail"}

    def test_story_verdicts_advisory_when_no_fail(self) -> None:
        review = _review(_criterion("A", "pass"), _criterion("A", "advisory"))
        assert review.story_verdicts() == {"a": "advisory"}

    def test_story_verdicts_pass_only_when_all_pass(self) -> None:
        review = _review(_criterion("A", "pass"), _criterion("A", "pass"))
        assert review.story_verdicts() == {"a": "pass"}

    def test_story_verdicts_ignores_empty_story_id(self) -> None:
        review = _review(_criterion("", "fail"), _criterion("A", "pass"))
        assert review.story_verdicts() == {"a": "pass"}

    def test_story_verdicts_ignores_an_unrecognised_verdict(self) -> None:
        """The parser cannot emit one, so this only guards a
        hand-built result. Skipping is the safe direction: the story
        ends with no reading and therefore reads as uncovered, not as
        confirmed."""
        review = _review(_criterion("A", "Blocked"))
        assert review.story_verdicts() == {}

    def test_uncovered_story_is_absent_not_defaulted(self) -> None:
        assert _review(_criterion("A", "pass")).story_verdicts().get("b") is None

    def test_non_pass_criteria_filters_by_story(self) -> None:
        review = _review(
            _criterion("A", "fail", "a-crit"),
            _criterion("B", "fail", "b-crit"),
            _criterion("A", "pass", "a-ok"),
        )
        assert [c.criterion for c in review.non_pass_criteria("A")] == ["a-crit"]


# --------------------------------------------------------------------
# 3. the disagreement check
# --------------------------------------------------------------------


class TestClaimDisagreements:
    def test_disagreement_on_advisory_verdict(self) -> None:
        prd = _prd(_story("A", passes=True), _story("B", passes=True))
        review = _review(
            _criterion("A", "pass"),
            _criterion("B", "advisory", "b-crit"),
        )
        found = claim_disagreements(prd, review, severity="advisory")
        assert len(found) == 1
        assert found[0].location == "B"
        assert found[0].category == CLAIM_DISAGREEMENT_CATEGORY
        assert found[0].severity == "advisory"
        assert found[0].phase == "review"
        assert "advisory" in found[0].explanation
        assert found[0].suggestion == "b-crit"

    def test_disagreement_on_uncovered_story(self) -> None:
        prd = _prd(_story("A", passes=True), _story("B", passes=True))
        review = _review(_criterion("A", "pass"))
        found = claim_disagreements(prd, review, severity="advisory")
        assert [f.location for f in found] == ["B"]
        assert "not covered" in found[0].explanation
        assert found[0].suggestion == ""

    def test_no_disagreement_for_unclaimed_story(self) -> None:
        prd = _prd(_story("A", passes=True), _story("B", passes=False))
        review = _review(_criterion("A", "pass"), _criterion("B", "fail"))
        assert claim_disagreements(prd, review, severity="advisory") == []

    def test_no_disagreement_on_infra_error(self) -> None:
        prd = _prd(_story("A", passes=True))
        review = _review(passed=False, infra=True)
        assert claim_disagreements(prd, review, severity="advisory") == []

    def test_story_id_matching_is_case_insensitive(self) -> None:
        """The reviewer writing "us-001" for a PRD's "US-001" is
        agreement, not an uncovered story."""
        prd = _prd(_story("US-001", passes=True))
        review = _review(_criterion("us-001", "pass"))
        assert claim_disagreements(prd, review, severity="advisory") == []

    def test_severity_is_the_callers_choice(self) -> None:
        prd = _prd(_story("A", passes=True))
        found = claim_disagreements(prd, _review(), severity="fail")
        assert [f.severity for f in found] == ["fail"]

    def test_findings_carry_the_reviewing_model(self) -> None:
        prd = _prd(_story("A", passes=True))
        review = _review(model="codex (gpt-5)")
        found = claim_disagreements(prd, review, severity="advisory")
        assert finding_model(found[0]) == "codex (gpt-5)"


class TestCriterionCoverage:
    """Round-1 review, P1: a pass on half the criteria is not a pass.

    ``parse_review_output``'s coverage gate checks that every STORY got
    a verdict, never that every CRITERION did, so a reviewer returning
    one passing criterion for a two-criterion story reads as a clean
    pass there. Confirming the engineer's claim on that would be
    confirming it on half the evidence.
    """

    def test_partial_criteria_do_not_confirm_a_story(self) -> None:
        prd = _prd(_story("A", passes=True, criteria=["crit A", "crit B"]))
        review = _review(_criterion("A", "pass", "crit A"))
        found = claim_disagreements(prd, review, severity="advisory")
        assert [f.location for f in found] == ["A"]
        assert "1 of 2 acceptance criteria" in found[0].explanation
        assert "unconfirmed rather than judged unmet" in found[0].suggestion

    def test_the_existing_coverage_gate_does_not_catch_this(self) -> None:
        """The hole this closes is real, not hypothetical."""
        raw = json.dumps(
            {
                "stories": [
                    {
                        "storyId": "A",
                        "criteria": [
                            {
                                "criterion": "crit A",
                                "verdict": "pass",
                                "explanation": "ok",
                                "suggestion": "",
                            },
                        ],
                    }
                ],
                "concerns": [],
                "overallNotes": "",
            }
        )
        parsed = parse_review_output(raw, ["A"])
        assert parsed.infrastructure_error is False
        assert parsed.story_verdicts() == {"a": "pass"}

    def test_full_criteria_all_passing_do_confirm(self) -> None:
        prd = _prd(_story("A", passes=True, criteria=["crit A", "crit B"]))
        review = _review(
            _criterion("A", "pass", "crit A"),
            _criterion("A", "pass", "crit B"),
        )
        assert claim_disagreements(prd, review, severity="advisory") == []

    def test_duplicate_criteria_across_chunks_are_counted_once(self) -> None:
        """A chunked review merges passes, so the same criterion can come
        back twice. Counting raw entries would call a two-criterion story
        fully judged on one criterion judged twice."""
        prd = _prd(_story("A", passes=True, criteria=["crit A", "crit B"]))
        review = _review(
            _criterion("A", "pass", "crit A"),
            _criterion("A", "pass", "crit A"),
        )
        assert review.judged_criterion_count("A") == 1
        assert len(claim_disagreements(prd, review, severity="advisory")) == 1

    def test_extra_verdicts_do_not_create_a_disagreement(self) -> None:
        prd = _prd(_story("A", passes=True, criteria=["crit A"]))
        review = _review(
            _criterion("A", "pass", "crit A"),
            _criterion("A", "pass", "extra"),
        )
        assert claim_disagreements(prd, review, severity="advisory") == []

    def test_a_failed_criterion_still_reads_as_the_verdict(self) -> None:
        """Incomplete coverage must not mask a judged failure."""
        prd = _prd(_story("A", passes=True, criteria=["crit A", "crit B"]))
        review = _review(_criterion("A", "fail", "crit A"))
        found = claim_disagreements(prd, review, severity="fail")
        assert "verdict is fail" in found[0].explanation
        assert found[0].suggestion == "crit A"


class TestClaimBlocks:
    @pytest.mark.parametrize(
        ("mode", "level", "expected"),
        [
            ("advisory", 0, False),
            ("block", 0, True),
            ("advisory", 1, True),
            ("advisory", 2, True),
        ],
    )
    def test_claim_blocks_rules(
        self,
        mode: str,
        level: int,
        expected: bool,
    ) -> None:
        config = FactoryConfig(claim_agreement=mode)
        assert claim_blocks(config, level) is expected

    def test_matches_the_published_lesson_table(self) -> None:
        """docs/lessons/verify/pr-221/claim_agreement.py publishes
        ``blocks(mode, level) = mode == "block" or level >= 1`` as a
        claim about this code. If they ever disagree the lesson page is
        wrong, so the agreement is asserted rather than assumed."""
        for mode in ("advisory", "block"):
            for level in range(5):
                config = FactoryConfig(claim_agreement=mode)
                assert claim_blocks(config, level) is (mode == "block" or level >= 1)


# --------------------------------------------------------------------
# 4. reverting the flag
# --------------------------------------------------------------------


class TestRevert:
    def test_revert_resets_the_flag_and_notes_why(self) -> None:
        prd = _prd(_story("A", passes=True), _story("B", passes=True))
        review = _review(
            _criterion("A", "pass"),
            _criterion("B", "advisory", "b-crit"),
        )
        found = claim_disagreements(prd, review, severity="fail")
        assert revert_unconfirmed_stories(
            prd,
            review,
            found,
            attempt=2,
        ) == ["B"]
        by_id = {s.id: s for s in prd.user_stories}
        assert by_id["A"].passes is True
        assert by_id["B"].passes is False
        assert by_id["B"].notes == ("reverted by reviewer (attempt 2): b-crit")

    def test_revert_of_an_uncovered_story_says_so(self) -> None:
        prd = _prd(_story("A", passes=True, notes="earlier note"))
        found = claim_disagreements(prd, _review(), severity="fail")
        revert_unconfirmed_stories(prd, _review(), found, attempt=1)
        assert prd.user_stories[0].notes == (
            "earlier note\nreverted by reviewer (attempt 1): story not covered by review"
        )


class TestRetryContext:
    """Measured on a real run: the criterion text alone did not help.

    In block mode the reviewer's reasoning reaches the agent by no other
    route, because ``as_retry_context`` is added only when the review
    FAILED and a claim block happens on a review that passed. A real
    factory run retried with the criterion text only and repeated the
    same defect; that is n=1 and not proof of causation, but handing the
    agent evidence it otherwise never sees costs nothing.
    """

    def test_carries_the_reviewers_evidence_not_just_the_criterion(
        self,
    ) -> None:
        prd = _prd(_story("A", passes=True))
        review = _review(
            CriterionReview(
                criterion="raises on names over 64 characters",
                verdict="advisory",
                explanation="__init__.py:7-11 strips before measuring",
                suggestion="measure before stripping",
                story_id="A",
            )
        )
        found = claim_disagreements(prd, review, severity="fail")
        text = claim_retry_context(found, review)
        assert "raises on names over 64 characters" in text
        assert "__init__.py:7-11 strips before measuring" in text
        assert "measure before stripping" in text

    def test_says_the_flags_were_reset(self) -> None:
        prd = _prd(_story("A", passes=True))
        review = _review(_criterion("A", "fail"))
        found = claim_disagreements(prd, review, severity="fail")
        assert "have been reset to false" in claim_retry_context(
            found,
            review,
        )

    def test_does_not_claim_a_revert_that_did_not_happen(self) -> None:
        prd = _prd(_story("A", passes=True))
        review = _review(_criterion("A", "fail"))
        found = claim_disagreements(prd, review, severity="fail")
        text = claim_retry_context(found, review, reverted=False)
        assert "could NOT be reset automatically" in text
        assert "have been reset to false" not in text

    def test_partial_coverage_is_not_described_as_no_verdict(self) -> None:
        """Round-2 review, P3. A story where every judged criterion
        passed but not every criterion was judged leaves `unmet` empty,
        exactly like a story with no verdict at all. Saying "returned no
        verdict" for the first contradicts the finding printed directly
        above it, which had just said "pass on only 1 of 2"."""
        prd = _prd(_story("A", passes=True, criteria=["crit A", "crit B"]))
        review = _review(_criterion("A", "pass", "crit A"))
        found = claim_disagreements(prd, review, severity="fail")
        text = claim_retry_context(found, review)
        assert "pass on only 1 of 2" in text
        assert "did not judge them all" in text
        assert "returned no verdict" not in text

    def test_uncovered_story_says_there_is_no_evidence(self) -> None:
        prd = _prd(_story("A", passes=True))
        review = _review()
        found = claim_disagreements(prd, review, severity="fail")
        text = claim_retry_context(found, review)
        assert "no verdict for this story" in text

    def test_empty_when_nothing_disagrees(self) -> None:
        assert claim_retry_context([], _review()) == ""


class TestPrdSave:
    def test_save_of_an_unchanged_prd_is_byte_identical(
        self,
        tmp_path: Path,
    ) -> None:
        """The factory writes PRDs with the same two-space indent and
        trailing newline PRD.save emits (decompose's atomic JSON
        writer), so a load-save cycle must not reformat the file. The
        revert has to change one flag and nothing else.
        """
        path = tmp_path / "prd.json"
        _prd(_story("A", passes=True), _story("B", passes=True)).save(path)
        original = path.read_bytes()

        PRD.load(path).save(path)
        assert path.read_bytes() == original

    def test_revert_changes_only_the_reverted_story(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "prd.json"
        _prd(_story("A", passes=True), _story("B", passes=True)).save(path)
        before = json.loads(path.read_text())

        prd = PRD.load(path)
        review = _review(_criterion("A", "pass"), _criterion("B", "fail"))
        found = claim_disagreements(prd, review, severity="fail")
        revert_unconfirmed_stories(prd, review, found, attempt=1)
        prd.save(path)

        after = json.loads(path.read_text())
        assert after["userStories"][0] == before["userStories"][0]
        assert after["userStories"][1]["passes"] is False
        assert after["branchName"] == before["branchName"]


# --------------------------------------------------------------------
# 5. the journal signature and the pull-request body
# --------------------------------------------------------------------


class TestFindingReachesTheRecord:
    def test_blocking_finding_produces_the_journal_signature(self) -> None:
        prd = _prd(_story("A", passes=True))
        found = claim_disagreements(prd, _review(), severity="fail")
        assert signatures_from_findings("review", found) == [
            "review:claim_disagreement",
        ]

    def test_advisory_finding_produces_no_signature(self) -> None:
        """signatures_from_findings only emits for fail/critical/high,
        so in advisory mode the journal carries the finding itself (via
        Component.findings) but no failure signature. Stated here so the
        asymmetry is a decision on the record rather than a surprise."""
        prd = _prd(_story("A", passes=True))
        found = claim_disagreements(prd, _review(), severity="advisory")
        assert signatures_from_findings("review", found) == []

    def test_finding_renders_in_the_pr_body(self, tmp_path: Path) -> None:
        """An advisory-only gate whose output never reaches the pull
        request is decoration. The review_findings string cannot carry
        it: that renders criteria and concerns, and this is neither."""
        from kstrl.manifest import Manifest

        comp = _component("comp-a")
        prd = _prd(_story("US-002", passes=True))
        comp.findings = claim_disagreements(
            prd,
            _review(_criterion("US-002", "advisory", "handles empties")),
            severity="advisory",
        )
        manifest = Manifest(
            version="1",
            spec_file="spec.md",
            project_name="test",
            base_branch="main",
            single_pr=False,
            components=[comp],
        )
        body = _generate_pr_body(comp, manifest)
        assert CLAIM_DISAGREEMENT_CATEGORY in body
        assert "US-002" in body
