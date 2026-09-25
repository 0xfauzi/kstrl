"""#518: an integration verdict is joined to its story by the story id alone.

The reviewer echoes each criterion's text, and real replies reword it or cut
it short: 17 of 27 integration calibration runs, and 1 of 6 E2 replays, were
refused on "criterion text differs from the PRD's" before any finding was
scored. The id in the reply's schema is the join; the echoed text is kept in
the evidence and never compared. A reply that does not name every story id
exactly once is still refused.

Every test drives the real ``run_factory`` through
``tests/helpers/integration_harness.py`` or ``tests/helpers/integration_loop.py``:
a real repository, the real Phase 3 check and temporary worktree, the real
``run_review`` parser, with only the reviewer CLI stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.helpers import integration_harness as h
from tests.helpers import integration_loop as lp

IC2_FAIL_TEXT = f"{h.STORE}:1 re-applies request rules to stored rows"


def _state(root: Path) -> dict[str, Any]:
    return json.loads(h.state_file(root).read_text(encoding="utf-8"))


def _evidence(root: Path) -> dict[str, Any]:
    return json.loads(h.evidence_files(root)[-1].read_text(encoding="utf-8"))


def test_an_ic5_echo_missing_its_second_sentence_is_scored(tmp_path: Path) -> None:
    """The E2 run-6 reply: IC5's second sentence dropped, IC2 failed."""
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    for story in payload["stories"]:
        if story["storyId"] == "IC5":
            criterion = story["criteria"][0]["criterion"]
            assert " If decisions.json does not exist" in criterion
            story["criteria"][0]["criterion"] = criterion.split(" If decisions.json")[0]
    h.set_verdict(payload, "IC2", "fail", IC2_FAIL_TEXT)

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    evidence = _evidence(root)
    assert evidence["errors"] == []
    state = _state(root)
    assert [(f["storyId"], f["kind"], f["status"]) for f in state["findings"]] == [
        ("IC2", "criterion", "open")
    ]
    assert state["findings"][0]["locations"] == [h.STORE]
    assert state["stops"][-1]["outcome"] == "open_findings"


def test_every_criterion_echoed_in_other_words_is_scored(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    for story in payload["stories"]:
        story["criteria"][0]["criterion"] = f"{story['storyTitle']}, in the reviewer's words"

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    evidence = _evidence(root)
    assert evidence["errors"] == []
    echoed = [c["criterion"] for c in evidence["review"]["criteria"]]
    assert all(text.endswith(", in the reviewer's words") for text in echoed)
    assert len(echoed) == 5
    state = _state(root)
    assert state["findings"] == []
    assert state["stops"][-1]["outcome"] == "clean"


def test_a_reply_with_no_verdict_for_one_criterion_is_still_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    payload["stories"] = [s for s in payload["stories"] if s["storyId"] != "IC4"]

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    errors = _evidence(root)["errors"]
    assert any("no verdict for story ids IC4 " in e for e in errors)
    state = _state(root)
    assert state["findings"] == []
    assert state["stops"][-1]["outcome"] == "red"


def test_five_stories_returned_as_one_story_are_still_refused(tmp_path: Path) -> None:
    """The E1 run-8 reply: every criterion echoed exactly, all under one story
    id "IC". No verdict names a PRD story id, so none can be joined, and the
    exact text is not used as a second key."""
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    criteria = [c for story in payload["stories"] for c in story["criteria"]]
    payload["stories"] = [{"storyId": "IC", "storyTitle": "Integration", "criteria": criteria}]

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    errors = _evidence(root)["errors"]
    assert any("no verdict for story ids IC1, IC2, IC3, IC4, IC5 " in e for e in errors)
    state = _state(root)
    assert state["findings"] == []
    assert state["stops"][-1]["outcome"] == "red"


def test_two_verdicts_for_one_story_are_still_refused(tmp_path: Path) -> None:
    """The calibration shape "story IC1: 2 verdicts": the id joins both, and
    a story whose PRD holds one criterion takes exactly one verdict."""
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    first = payload["stories"][0]
    assert first["storyId"] == "IC1"
    first["criteria"].append({**first["criteria"][0], "criterion": "IC1, second half"})

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    assert _evidence(root)["errors"] == ["story IC1: 2 verdicts; exactly one is required"]
    state = _state(root)
    assert state["findings"] == []
    assert state["stops"][-1]["outcome"] == "red"


def test_a_reworded_verdict_with_no_explanation_is_still_refused(tmp_path: Path) -> None:
    """Rewording is accepted; a verdict that explains nothing did not judge
    its criterion, whatever text it echoes."""
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    payload = h.review_payload(root, base)
    for story in payload["stories"]:
        if story["storyId"] == "IC3":
            story["criteria"][0]["criterion"] = "IC3, in the reviewer's words"
    h.set_verdict(payload, "IC3", "pass", "   ")

    h.run_factory_over(root, h.FakeReviewer(json.dumps(payload)))

    assert _evidence(root)["errors"] == ["story IC3: empty explanation"]
    state = _state(root)
    assert state["findings"] == []
    assert state["stops"][-1]["outcome"] == "red"


class _RewordingReviewer(lp.ScriptedReviewer):
    """The scripted integration reviewer, echoing every criterion in its own words."""

    def _payload(self, verdicts: lp.Verdicts, cwd: Path) -> dict[str, Any]:
        payload = super()._payload(verdicts, cwd)
        for story in payload["stories"]:
            for criterion in story["criteria"]:
                criterion["criterion"] = f"{story['storyId']} as the reviewer read it"
        return payload


def test_a_carried_finding_echoed_in_other_words_closes_on_its_pass(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = lp.loop_feature(root)
    reviewer = _RewordingReviewer(base, [lp.IC2_FAIL, {}])
    rig = lp.Rig(root, reviewer)

    result, _out = lp.run_loop(root, rig)

    assert reviewer.calls == 2
    assert "IF-1" in reviewer.prompts[1]
    state = lp.state(root)
    assert state["findings"][0]["status"] == "closed"
    assert state["stops"][-1]["outcome"] == "clean"
    assert result.exit_code == 0
