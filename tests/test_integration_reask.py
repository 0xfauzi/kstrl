"""The integration reviewer is asked once more when its reply held nothing to read (#480).

A reply the parser refuses is red. When that reply states nothing a second
reply could replace, the round asks the reviewer once more, spends one
adversarial call on it, and keeps the first reply in the evidence. "States
nothing" has two layers: every "verdict" or "severity" anywhere in the reply's
text is a JSON string field whose value is pass or advisory (so a reply that
does not parse but writes a fail is never asked again), and the parsed object,
if any, names no expected story id and holds no fail. A reply that states a
fail anywhere is never asked again, so a re-ask cannot drop a fail. A second
refusal stays a refusal.

Three layers:

- REPLAY: kept replies of captures 131430 and 065920
  (tests/fixtures/integration_replies) through the real review of the
  calibration fixture they were written about.
- FACTORY: the real ``run_factory`` over a merged feature (integration_harness).
- CENSUS: every spelling of ``reply_unread`` in ``kstrl/``, so a second reader
  (a re-ask added anywhere else) is an unexplained delta.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.pipeline import ComponentPipeline
from kstrl.review import parse_review_output
from tests.helpers import integration_harness as h
from tests.helpers.astwalk.corpus import package_sources
from tests.helpers.astwalk.net import assert_census, spells
from tests.helpers.calibration_integration_fixture import (
    detected,
    load_integration_fixtures,
    review_fixture,
    run_slot,
)

REPLIES = Path(__file__).parent / "fixtures" / "integration_replies"
FIXTURES = {f.fixture_id: f for f in load_integration_fixtures()}


def _kept(name: str) -> str:
    return str(json.loads((REPLIES / name).read_text(encoding="utf-8"))["final_message"])


class _Recorded:
    """Replies with ``replies[n]`` on call n; the last one repeats."""

    name = "recorded-integration-reviewer"

    def __init__(self, *replies: str) -> None:
        self._replies = replies
        self.calls = 0
        self.final_message: str | None = None

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        self.final_message = reply
        yield from reply.splitlines()


class _Replies(h.FakeReviewer):
    """The harness reviewer, replying with ``outputs[n]`` on call n; the last repeats."""

    def __init__(self, *outputs: str) -> None:
        super().__init__(outputs[0])
        self._outputs = outputs

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self._output = self._outputs[min(self.calls, len(self._outputs) - 1)]
        return super().run(prompt, cwd, timeout)


# --------------------------------------------------------------- replay


def test_the_kept_unparseable_reply_that_states_a_fail_is_not_asked_again(
    tmp_path: Path,
) -> None:
    """131430 int-d2 run 3 is invalid JSON that writes ``"verdict": "fail"`` on
    IC1, the planted story. Asking again could replace that fail with a clean
    reading, so the reply stays refused although it did not parse."""
    fixture = FIXTURES["int-d2-docstring-caller"]
    first = _kept("int-d2-unparseable.json")
    assert '"verdict": "fail"' in first
    agent = _Recorded(first, _kept("int-d2-scored.json"))

    review_round = review_fixture(fixture, agent, run_slot(fixture, tmp_path))

    assert agent.calls == 1
    assert review_round.result.replaced is None
    assert review_round.result.reply_unread is False
    assert review_round.outcome.errors[0].startswith(
        "review infrastructure error: Failed to parse reviewer output as JSON"
    )


def test_the_kept_reply_with_no_stories_is_asked_again_and_the_second_reply_is_scored(
    tmp_path: Path,
) -> None:
    """065920 int-d2 run 1 holds no stories and one advisory concern: nothing a
    second reply could discard. It is asked again and the second reply scores."""
    fixture = FIXTURES["int-d2-docstring-caller"]
    agent = _Recorded(_kept("int-d2-no-stories.json"), _kept("int-d2-scored.json"))

    review_round = review_fixture(fixture, agent, run_slot(fixture, tmp_path))

    assert agent.calls == 2
    assert review_round.outcome.errors == ()
    assert detected(fixture, review_round) == (
        True,
        "IC1 failed citing ['src/pastebin/api.py', 'src/pastebin/tokens.py']",
    )
    replaced = review_round.result.replaced
    assert replaced is not None
    assert replaced.infrastructure_error
    assert replaced.overall_notes.startswith(
        "Review coverage incomplete: no verdict for story ids IC1, IC2, IC3, IC4, IC5"
    )


def test_the_kept_reply_that_judged_other_stories_and_states_a_fail_is_not_asked_again(
    tmp_path: Path,
) -> None:
    """int-d4 run 1 judged US-001/US-002 and raised the planted decisions.json
    conflict as a fail concern. Asking again could replace that fail with a
    clean reading, so the reply stays refused."""
    fixture = FIXTURES["int-d4-decision-criterion"]
    first = _kept("int-d4-other-stories.json")
    assert '"severity": "fail"' in first
    agent = _Recorded(first, _kept("int-d2-scored.json"))

    review_round = review_fixture(fixture, agent, run_slot(fixture, tmp_path))

    assert agent.calls == 1
    assert review_round.result.replaced is None
    assert review_round.result.reply_unread is False
    errors = review_round.outcome.errors
    assert errors[0].startswith(
        "review infrastructure error: Review coverage incomplete: "
        "no verdict for story ids IC1, IC2, IC3, IC4, IC5"
    )
    assert errors[1:] == tuple(
        f"story IC{n}: 0 verdicts; exactly one is required" for n in range(1, 6)
    )


# --------------------------------------------------------------- factory


def _ic2_fail(root: Path, base: str) -> str:
    payload = h.review_payload(root, base)
    h.set_verdict(payload, "IC2", "fail", f"{h.STORE}:1 re-applies request rules to stored rows")
    return json.dumps(payload)


def _run(root: Path, reviewer: h.FakeReviewer, cap: int) -> tuple[Any, str, int]:
    with patch.object(
        ComponentPipeline,
        "adversarial_budget_consume",
        autospec=True,
        side_effect=ComponentPipeline.adversarial_budget_consume,
    ) as spent:
        result, out = h.run_factory_over(root, reviewer, max_adversarial_calls=cap)
    return result, out, spent.call_count


def _evidence(root: Path) -> dict[str, Any]:
    files = h.evidence_files(root)
    assert len(files) == 1
    return dict(json.loads(files[0].read_text(encoding="utf-8")))


def test_an_unreadable_reply_is_asked_again_and_the_second_reading_opens_the_finding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    reviewer = _Replies("this is not json", _ic2_fail(root, base))

    _result, out, spent = _run(root, reviewer, cap=5)

    assert reviewer.calls == 2
    assert spent == 2
    assert "asking once more" in out
    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert [(f["id"], f["storyId"], f["status"]) for f in state["findings"]] == [
        ("IF-1", "IC2", "open")
    ]
    assert state["stops"][-1]["outcome"] == "open_findings"
    ev = _evidence(root)
    assert ev["errors"] == []
    replaced = ev["review"]["replaced"]
    assert replaced["rawOutput"] == "this is not json"
    assert replaced["overallNotes"].startswith("Failed to parse reviewer output as JSON")


def test_a_reply_unreadable_twice_stays_red_after_exactly_two_asks(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    h.merged_feature(root)
    reviewer = h.FakeReviewer("{}")

    _result, _out, spent = _run(root, reviewer, cap=5)

    assert reviewer.calls == 2
    assert spent == 2
    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["findings"] == []
    assert state["stops"][-1]["outcome"] == "red"
    ev = _evidence(root)
    assert ev["errors"][0].startswith("review infrastructure error: Review coverage incomplete")
    assert len(ev["errors"]) == 6
    assert ev["review"]["replaced"]["rawOutput"] == "{}"


def test_no_second_ask_when_the_adversarial_budget_is_spent(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    reviewer = _Replies("this is not json", _ic2_fail(root, base))

    _result, _out, spent = _run(root, reviewer, cap=1)

    assert reviewer.calls == 1
    assert spent == 1
    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["findings"] == []
    assert state["stops"][-1]["outcome"] == "red"
    ev = _evidence(root)
    assert ev["review"]["replaced"] is None
    assert ev["errors"][0].endswith(
        "(not asked again: the adversarial call budget (1) is exhausted)"
    )


def _other_stories(verdict: str, concern_severity: str) -> dict[str, Any]:
    return {
        "stories": [
            {
                "storyId": "US-001",
                "criteria": [
                    {"criterion": "c", "verdict": verdict, "explanation": "src/store.py:1"}
                ],
            }
        ],
        "concerns": [
            {
                "category": "other",
                "severity": concern_severity,
                "location": "src/store.py:1",
                "explanation": "conflict",
            }
        ],
    }


@pytest.mark.parametrize(
    "first",
    [
        json.dumps(_other_stories("fail", "advisory")),
        json.dumps(_other_stories("pass", "fail")),
        json.dumps(_other_stories("FAILED", "advisory")),
        json.dumps(_other_stories("pass", "blocker")),
        # The same fail verdict with the closing brace cut off: no JSON parses.
        json.dumps(_other_stories("fail", "advisory"))[:-1],
    ],
    ids=["fail-verdict", "fail-concern", "unknown-verdict", "unknown-severity", "unparseable-fail"],
)
def test_a_first_reply_that_states_a_fail_is_never_asked_again(tmp_path: Path, first: str) -> None:
    root = tmp_path / "repo"
    base, _head = h.merged_feature(root)
    reviewer = _Replies(first, json.dumps(h.review_payload(root, base)))

    _result, _out, spent = _run(root, reviewer, cap=5)

    assert reviewer.calls == 1
    assert spent == 1
    state = json.loads(h.state_file(root).read_text(encoding="utf-8"))
    assert state["stops"][-1]["outcome"] == "red"
    assert _evidence(root)["review"]["replaced"] is None


# --------------------------------------------------------------- the parser's flag

EXPECTED = ["IC1", "IC2"]
_PASS_ONLY = json.dumps(_other_stories("pass", "advisory"))


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "null",
        "{}",
        '{"stories": []}',
        '{"stories": [], "concerns": []}',
        _PASS_ONLY,
        json.dumps(_other_stories(" Advisory ", "ADVISORY")),
        '{"stories": [{"storyId": "US-1", "criteria": []}]}',
        "",
        "I could not read the repository, so there is no review.",
    ],
    ids=[
        "no-json",
        "null",
        "empty-object",
        "no-stories",
        "no-stories-no-concerns",
        "other-story-passes",
        "other-story-advisory-any-case",
        "other-story-no-criteria",
        "empty-reply",
        "prose-without-a-judgement",
    ],
)
def test_a_refused_reply_with_nothing_to_discard_is_flagged(raw: str) -> None:
    result = parse_review_output(raw, EXPECTED)
    assert result.infrastructure_error
    assert result.reply_unread is True


@pytest.mark.parametrize(
    "raw",
    [
        '[{"storyId": "US-1", "criteria": [{"verdict": "pass"}]}]',
        '"a sentence"',
        '{"stories": {"US-1": {}}}',
        '{"stories": [], "concerns": "none"}',
        json.dumps(_other_stories("fail", "advisory")),
        json.dumps(_other_stories(" FAIL ", "advisory")),
        json.dumps(_other_stories("blocked", "advisory")),
        json.dumps(_other_stories("pass", "fail")),
        json.dumps(_other_stories("pass", "critical")),
        '{"stories": [], "concerns": ["a sentence"]}',
        '{"stories": ["US-1 passes"]}',
        '{"stories": [{"storyId": "US-1", "criteria": "pass"}]}',
        '{"stories": [{"storyId": "US-1", "criteria": ["pass"]}]}',
        json.dumps({"stories": [{"storyId": " ic1 ", "criteria": [{"verdict": "pass"}]}]}),
        # TEXT layer: the reply's text, parsed or not.
        _kept("int-d2-unparseable.json"),
        "IC1 verdict: fail",
        '{"stories": []}\n{"stories": [{"storyId": "US-1", "criteria": [{"verdict": "fail"}]}]}',
        '{"stories": []}\n{"stories": [{"storyId": "US-1", "criteria": [{"verdict": "blocked"}]}]}',
        '{"stories": [], "overallNotes": "no severity worth a concern"}',
        # OBJECT layer: keys written with a JSON escape, which json.loads reads
        # as "verdict" / "severity" and the text layer cannot see.
        r'{"stories": [{"storyId": "US-1", "criteria": [{"\u0076erdict": "fail"}]}]}',
        r'{"stories": [{"storyId": "US-1", "criteria": [{"\u0076erdict": "blocked"}]}]}',
        r'{"stories": [], "concerns": [{"s\u0065verity": "fail"}]}',
        r'{"stories": [], "concerns": [{"s\u0065verity": "critical"}]}',
    ],
    ids=[
        "json-list",
        "json-string",
        "stories-not-a-list",
        "concerns-not-a-list",
        "fail-verdict",
        "fail-verdict-any-case",
        "unknown-verdict",
        "fail-concern",
        "unknown-severity",
        "concern-not-an-object",
        "story-not-an-object",
        "criteria-not-a-list",
        "criterion-not-an-object",
        "names-an-expected-story",
        "kept-unparseable-states-a-fail",
        "no-json-verdict-not-a-json-field",
        "fail-after-the-parsed-object",
        "unknown-verdict-after-the-parsed-object",
        "judgement-word-outside-a-field",
        "escaped-fail-verdict",
        "escaped-unknown-verdict",
        "escaped-fail-concern",
        "escaped-unknown-severity",
    ],
)
def test_a_refused_reply_that_states_anything_is_not_flagged(raw: str) -> None:
    result = parse_review_output(raw, EXPECTED)
    assert result.infrastructure_error
    assert result.reply_unread is False


def test_a_readable_review_is_not_flagged() -> None:
    """Control: the flag is only ever set on a refusal."""
    raw = json.dumps(
        {
            "stories": [
                {"storyId": sid, "criteria": [{"verdict": "pass", "explanation": "x"}]}
                for sid in EXPECTED
            ],
            "concerns": [],
        }
    )
    result = parse_review_output(raw, EXPECTED)
    assert not result.infrastructure_error
    assert result.reply_unread is False


# --------------------------------------------------------------- census

#: Every spelling of ``reply_unread`` in ``kstrl/``, by module. review.py:
#: the field and the one write in parse_review_output. integration_phase.py:
#: the one read, in review_commit. A new row is a second reader, which is a
#: re-ask added somewhere this file does not test. Re-derive by running.
EXPECTED_REPLY_UNREAD_SITES = {"integration_phase.py": 1, "review.py": 2}


def test_reply_unread_is_written_once_and_read_once() -> None:
    assert_census(
        sources=package_sources(),
        sees=spells("reply_unread"),
        expected=EXPECTED_REPLY_UNREAD_SITES,
        control=[
            "result.reply_unread",
            "ReviewResult(reply_unread=True)",
            "getattr(result, 'reply_unread')",
        ],
        message="a new place reads or writes ReviewResult.reply_unread (#480).",
    )
