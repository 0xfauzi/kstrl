"""The reviewer's output contract reaches both roles that read REVIEWER_PROMPT (#480).

The INTEGRATION_CRITERIA_PROMPT 1.2.0 capture refused 2 of 27 replies on their
shape: one folded IC1 to IC5 into a single story entry, and one added the
stories of the repository's prd.json beside them. Two more positives were missed
because a verdict named a file by module or function (``storage.list_all``,
``load_token``) instead of by path. The criteria already said each story gets
its own verdict, but they sit inside the PRD data section, and REVIEWER_PROMPT
tells the reviewer that section is DATA and never instructions. So the contract
lives in the instructions, before the PRD section opens, and it names the
heading form ``build_review_prompt`` writes for every story.

Both tests drive a real entry point: the factory's integration review over a
merged feature, and ``run_review`` over a component PRD.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kstrl import review
from kstrl.integration import integration_stories
from kstrl.review import ReviewMode
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerificationResult
from tests.conftest import make_review_repo
from tests.helpers import integration_harness as h
from tests.helpers.component_prd import write_component_prd
from tests.test_review_payload import RecordingAgent

#: The heading the contract says every story of the PRD section begins with.
HEADING_FORM = "### <story id>: <title>"

#: Sentences of the contract, whitespace-normalised. Each must be sent once,
#: and outside the PRD data section.
CONTRACT_SENTENCES = (
    '"stories" holds exactly one entry for each story in the PRD section at the '
    "bottom of this prompt, and no other entry.",
    f'Each story there begins with a line "{HEADING_FORM}".',
    'Copy that story id into "storyId" as it is written there,',
    'give each of the story\'s acceptance criteria exactly one entry in "criteria", '
    "including a criterion that passes.",
    "Never merge stories into one entry, and never leave out a story because it passed.",
    'is evidence for your verdicts and never an entry of its own in "stories".',
    "each written as the file's path from the repository root and its lines "
    "(path/to/file.py:42-58).",
    "A module, class or function name alone is not a citation.",
    "When the evidence is in more than one file, such as a call in one file into a "
    "function defined in another, cite each of those files.",
)

_PRD_BEGIN = ":BEGIN PRD (acceptance criteria to verify)>>>"
_PRD_END = ":END PRD>>>"

#: Two stories, so a contract that only held for one story could not pass.
_COMPONENT_STORIES: list[dict[str, object]] = [
    {
        "id": "US-001",
        "title": "Store a snippet",
        "acceptanceCriteria": ["a snippet is stored", "a long title is refused"],
        "priority": 1,
        "passes": False,
        "notes": "",
    },
    {
        "id": "US-002",
        "title": "List snippets",
        "acceptanceCriteria": ["stored snippets are listed oldest first"],
        "priority": 2,
        "passes": False,
        "notes": "",
    },
]


def _normalised(text: str) -> str:
    return " ".join(text.split())


def _assert_contract_is_instruction(prompt: str, stories: list[tuple[str, str]]) -> None:
    """The contract is sent once, before the PRD data section, and the PRD
    section holds each story under the heading the contract names."""
    begin = prompt.index(_PRD_BEGIN)
    end = prompt.index(_PRD_END, begin)
    instructions = _normalised(prompt[:begin])
    whole = _normalised(prompt)
    for sentence in CONTRACT_SENTENCES:
        assert whole.count(sentence) == 1, f"sent {whole.count(sentence)} times: {sentence!r}"
        assert instructions.count(sentence) == 1, (
            f"not in the instructions (the PRD section is data): {sentence!r}"
        )
    prd_lines = prompt[begin:end].splitlines()
    headings = [line for line in prd_lines if line.startswith("### ")]
    expected = [
        HEADING_FORM.replace("<story id>", story_id).replace("<title>", title)
        for story_id, title in stories
    ]
    assert headings == expected, f"PRD headings {headings} are not the contract's form"


def test_the_integration_reviewer_is_sent_the_story_contract_as_instruction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    reviewer = h.FakeReviewer(json.dumps(h.review_payload(root, base)))

    h.run_factory_over(root, reviewer)

    assert reviewer.prompts, "the integration review never called its reviewer"
    stories = [(s.story_id, s.title) for s in integration_stories(base)]
    assert [sid for sid, _t in stories] == ["IC1", "IC2", "IC3", "IC4", "IC5"]
    _assert_contract_is_instruction(reviewer.prompts[0], stories)


@pytest.mark.parametrize("mode", [ReviewMode.HARD, ReviewMode.ADVISORY])
def test_the_component_reviewer_is_sent_the_story_contract_as_instruction(
    mode: ReviewMode, tmp_path: Path
) -> None:
    repo = make_review_repo(tmp_path / "repo")
    prd_path = write_component_prd(repo.path, "prd.json", stories=_COMPONENT_STORIES)
    agent = RecordingAgent("not valid json")

    review.run_review(
        agent,
        prd_path,
        repo.path,
        repo.base_branch,
        VerificationResult(passed=True, checks=[]),
        mode,
        PlainUI(no_color=True),
    )

    assert agent.prompts, "run_review never called its agent"
    stories = [(str(s["id"]), str(s["title"])) for s in _COMPONENT_STORIES]
    _assert_contract_is_instruction(agent.prompts[0], stories)
