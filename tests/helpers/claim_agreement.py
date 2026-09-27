"""Builders for R10.3 claim-agreement tests: a story, a PRD, a criterion
review, a review result, and a PRD written to disk.

Moved here from ``tests/test_claim_agreement.py`` when that module's
unit tests were retired; ``tests/test_claim_agreement_pipeline.py``
drives the rule through the pipeline and imports these so the two
sides cannot drift on what a story or a review looks like.
"""

from __future__ import annotations

from pathlib import Path

from kstrl.manifest import Component
from kstrl.prd import PRD, UserStory
from kstrl.review import CriterionReview, ReviewResult


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
