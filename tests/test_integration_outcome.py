"""The integration review's rules, through the real parser (#482)."""

from __future__ import annotations

import copy
import json

import pytest

from kstrl import git
from kstrl.contract import ContractResult
from kstrl.integration import (
    RECORD_ONLY_CONCERN_CATEGORIES,
    REGISTER_STORY_ID,
    IntegrationOutcome,
    cited_paths,
    integration_outcome,
    integration_stories,
)
from kstrl.review import (
    VALID_CONCERN_CATEGORIES,
    ReviewMode,
    ReviewResult,
    apply_coverage_check,
    parse_review_output,
)

BASE = "a" * 40
STORIES = integration_stories(BASE)
EXISTS = {"src/store.py", "src/api.py"}.__contains__


def _payload() -> dict[str, object]:
    return {
        "observedDiffstat": {"files": 2, "insertions": 7, "deletions": 0},
        "stories": [
            {
                "storyId": story.story_id,
                "storyTitle": story.title,
                "criteria": [
                    {
                        "criterion": story.criterion,
                        "verdict": "pass",
                        "explanation": "src/api.py:1 read and checked",
                        "suggestion": "",
                    }
                ],
            }
            for story in STORIES
        ],
        "concerns": [],
        "exhaustively_searched": True,
        "overallNotes": "",
    }


def _read(payload: dict[str, object]) -> ReviewResult:
    result = parse_review_output(json.dumps(payload), [s.story_id for s in STORIES])
    result.mode = ReviewMode.HARD.value
    return result


def _outcome(result: ReviewResult, test: ContractResult | None = None) -> IntegrationOutcome:
    return integration_outcome(test, result, STORIES, location_exists=EXISTS)


def test_a_clean_review_opens_nothing() -> None:
    outcome = _outcome(_read(_payload()))
    assert outcome.errors == ()
    assert outcome.opened == ()


def test_two_verdicts_for_one_story_are_red() -> None:
    payload = _payload()
    for story in payload["stories"]:
        if story["storyId"] == "IC3":
            story["criteria"].append(copy.deepcopy(story["criteria"][0]))
    outcome = _outcome(_read(payload))
    assert any("story IC3: 2 verdicts" in e for e in outcome.errors)
    assert outcome.opened == ()


def test_a_verdict_for_an_unexpected_story_is_red() -> None:
    payload = _payload()
    payload["stories"].append(
        {
            "storyId": "IC9",
            "storyTitle": "extra",
            "criteria": [
                {
                    "criterion": "extra criterion",
                    "verdict": "pass",
                    "explanation": "checked",
                    "suggestion": "",
                }
            ],
        }
    )
    outcome = _outcome(_read(payload))
    assert any("unexpected story id 'IC9'" in e for e in outcome.errors)


def test_criterion_text_that_differs_from_the_prd_is_red() -> None:
    payload = _payload()
    for story in payload["stories"]:
        if story["storyId"] == "IC4":
            story["criteria"][0]["criterion"] = "something else"
    outcome = _outcome(_read(payload))
    assert any("story IC4: criterion text differs" in e for e in outcome.errors)


def test_an_empty_explanation_is_red() -> None:
    payload = _payload()
    for story in payload["stories"]:
        if story["storyId"] == "IC1":
            story["criteria"][0]["explanation"] = "   "
    outcome = _outcome(_read(payload))
    assert any("story IC1: empty explanation" in e for e in outcome.errors)


def test_a_dropped_malformed_concern_is_red() -> None:
    payload = _payload()
    payload["concerns"] = [
        {
            "category": "nonsense",
            "severity": "fail",
            "location": "src/api.py:1",
            "explanation": "x",
        }
    ]
    result = _read(payload)
    assert result.dropped_concerns == 1
    outcome = _outcome(result)
    assert any("1 malformed concern" in e for e in outcome.errors)


@pytest.mark.parametrize("drop_key", [True, False])
def test_non_list_concerns_are_red(drop_key: bool) -> None:
    payload = _payload()
    if drop_key:
        del payload["concerns"]
    else:
        payload["concerns"] = "none"
    result = _read(payload)
    assert result.concerns_not_list is True
    outcome = _outcome(result)
    assert any("'concerns' is missing or is not a list" in e for e in outcome.errors)


def test_a_coverage_gap_is_red() -> None:
    payload = _payload()
    payload["stories"] = [s for s in payload["stories"] if s["storyId"] != "IC3"]
    result = _read(payload)
    assert result.infrastructure_error is True
    outcome = _outcome(result)
    assert any("infrastructure error" in e for e in outcome.errors)


def test_a_coverage_refusal_is_red() -> None:
    result = _read(_payload())
    apply_coverage_check(result, git.DiffStat(99, 0, 0), ReviewMode.HARD)
    outcome = _outcome(result)
    assert any("coverage refused" in e for e in outcome.errors)


def test_an_advisory_mode_result_is_red() -> None:
    result = _read(_payload())
    result.mode = "advisory"
    outcome = _outcome(result)
    assert any("advisory" in e and "hard" in e for e in outcome.errors)


def test_a_red_review_opens_nothing_even_when_the_tests_failed() -> None:
    payload = _payload()
    for story in payload["stories"]:
        if story["storyId"] == "IC3":
            story["criteria"].append(copy.deepcopy(story["criteria"][0]))
    test_result = ContractResult(False, 0, ["comp-a"], test_output="FAILED src/api.py")
    outcome = _outcome(_read(payload), test_result)
    assert outcome.opened == ()


def test_scope_creep_and_unrelated_change_fail_concerns_are_recorded_only() -> None:
    payload = _payload()
    payload["concerns"] = [
        {
            "category": "scope_creep",
            "severity": "fail",
            "location": "src/api.py:1",
            "explanation": "x",
        },
        {
            "category": "unrelated_change",
            "severity": "fail",
            "location": "src/api.py:2",
            "explanation": "y",
        },
    ]
    outcome = _outcome(_read(payload))
    assert outcome.opened == ()
    assert len(outcome.recorded) == 2
    assert all(r.kind == "concern" for r in outcome.recorded)


def test_an_error_handling_fail_concern_opens_one_finding() -> None:
    payload = _payload()
    payload["concerns"] = [
        {
            "category": "error_handling",
            "severity": "fail",
            "location": "src/api.py:1",
            "explanation": "x",
        }
    ]
    outcome = _outcome(_read(payload))
    assert len(outcome.opened) == 1
    finding = outcome.opened[0]
    assert finding.kind == "concern"
    assert finding.category == "error_handling"
    assert finding.locations == ("src/api.py",)
    assert finding.status == "open"


def test_an_advisory_verdict_is_recorded_and_opens_nothing() -> None:
    payload = _payload()
    for story in payload["stories"]:
        if story["storyId"] == "IC2":
            story["criteria"][0]["verdict"] = "advisory"
    outcome = _outcome(_read(payload))
    assert outcome.opened == ()
    assert len(outcome.recorded) == 1
    assert outcome.recorded[0].story_id == "IC2"
    assert outcome.recorded[0].severity == "advisory"


def test_a_failed_integrated_test_opens_a_finding_scoped_to_the_files_it_names() -> None:
    test_result = ContractResult(
        False, 0, ["comp-a"], test_output="FAILED tests/test_api.py::test_x - src/api.py:3"
    )
    outcome = _outcome(_read(_payload()), test_result)
    assert outcome.opened[0].kind == "test"
    assert outcome.opened[0].locations == ("src/api.py",)
    assert outcome.opened[0].missing_locations == ("tests/test_api.py",)
    assert outcome.opened[0].status == "open"


def test_ic5_fail_is_a_register_handoff() -> None:
    payload = _payload()
    for story in payload["stories"]:
        if story["storyId"] == "IC5":
            story["criteria"][0]["verdict"] = "fail"
    outcome = _outcome(_read(payload))
    assert outcome.opened[0].kind == "register"
    assert outcome.opened[0].status == "handoff"
    assert outcome.opened[0].locations == ()


def test_a_finding_with_no_existing_location_is_handed_off() -> None:
    payload = _payload()
    for story in payload["stories"]:
        if story["storyId"] == "IC1":
            story["criteria"][0]["verdict"] = "fail"
            story["criteria"][0]["explanation"] = "lib/missing.py:3 calls save wrongly"
    outcome = _outcome(_read(payload))
    assert outcome.opened[0].status == "handoff"


def test_the_record_only_categories_are_the_parsers_own() -> None:
    assert RECORD_ONLY_CONCERN_CATEGORIES <= VALID_CONCERN_CATEGORIES
    assert RECORD_ONLY_CONCERN_CATEGORIES == frozenset({"scope_creep", "unrelated_change"})


def test_cited_paths_keeps_repository_paths_in_order() -> None:
    text = (
        "see src/store.py:614-635 and tests/test_api.py::test_x - AssertionError, "
        "self.config.journal_path, ./src/store.py, https://github.com/o/r, 0.14s, "
        "/abs/x.py, ../up.py, scripts/kstrl/decisions.json"
    )
    assert cited_paths(text) == (
        "src/store.py",
        "tests/test_api.py",
        "scripts/kstrl/decisions.json",
    )


def test_the_criteria_template_parses_into_five_stories() -> None:
    ids = tuple(s.story_id for s in STORIES)
    assert ids == ("IC1", "IC2", "IC3", "IC4", "IC5")
    assert BASE in STORIES[3].criterion
    assert "decisions.json" in STORIES[4].criterion
    assert REGISTER_STORY_ID == "IC5"


def test_the_parser_counts_dropped_concerns_and_keeps_the_rest() -> None:
    payload = _payload()
    payload["concerns"] = [
        {
            "category": "dead_code",
            "severity": "advisory",
            "location": "src/api.py:1",
            "explanation": "unused",
        },
        "junk",
        {
            "category": "dead_code",
            "severity": "blocker",
            "location": "src/api.py:2",
            "explanation": "unused",
        },
        {
            "category": "dead_code",
            "severity": "advisory",
            "location": "src/api.py:3",
            "explanation": "",
        },
    ]
    result = _read(payload)
    assert result.dropped_concerns == 3
    assert len(result.concerns) == 1
    assert result.passed is True
    assert result.infrastructure_error is False


def test_a_list_of_concerns_is_not_flagged() -> None:
    result = _read(_payload())
    assert result.concerns_not_list is False
    assert result.dropped_concerns == 0
