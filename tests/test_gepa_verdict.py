"""The GEPA report's verdict, driven through the real ``run_optimization`` (#217).

A reply the role's parser rejects scores 0.0, as a miss does. A seed whose
validation pass met a transport outage therefore scored below a candidate
that met none, and ``report.json`` said the candidate was better with
nothing a gate could use to tell why. The report now carries each
candidate's validation outcomes and a verdict that refuses a comparison
resting on a reply that measured nothing.

Real ``gepa.optimize``, real calibration fixtures and matchers, real
:class:`kstrl.gepa_adapter.ReflectionModel`; only the model replies are
scripted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kstrl.gepa_adapter import ReflectionModel, RoleFixture, run_optimization
from kstrl.review import REVIEWER_PROMPT
from tests.test_gepa_adapter import (
    MARKER,
    MarkerReviewer,
    ScriptedReflection,
    _by_id,
    _concern_reply,
    _fixtures,
    _ScriptedAgent,
)

#: How many role calls fail before the prompt-blind reviewer answers.
OUTAGE_CALLS = 3


class PromptBlindReviewer:
    """Unparseable for the first :data:`OUTAGE_CALLS` calls, then right on
    every fixture whatever its prompt says. No candidate can be better than
    the seed, so any "improvement" is the outage."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str, fixture: RoleFixture) -> str:
        self.calls += 1
        if self.calls <= OUTAGE_CALLS:
            return "upstream error: 529 overloaded"
        if fixture.negative:
            return json.dumps({"stories": [], "concerns": []})
        need = fixture.meta["must_detect"]
        return _concern_reply(need["category"], "fail", f"{need['evidence_path_contains']}:1")


def _fixture_set() -> list[RoleFixture]:
    return _by_id(
        _fixtures("concerns", "concerns_negative"),
        "concern-01-dead-code",
        "concern-03-scope-creep",
        "rev-neg-01-used-helper-refactor",
    )


def test_an_unparseable_reply_on_either_side_refuses_the_verdict(tmp_path: Path) -> None:
    """The outage lands on the seed's whole validation pass. The best
    candidate's val_score beats the seed's, and the verdict is refused,
    naming both validation fixtures and the outcome that measured nothing."""
    path = run_optimization(
        "reviewer",
        REVIEWER_PROMPT,
        _fixture_set(),
        runner=PromptBlindReviewer(),
        reflection_lm=ScriptedReflection(REVIEWER_PROMPT),
        max_metric_calls=10,
        run_dir=tmp_path / "run",
    )

    report = json.loads(path.read_text(encoding="utf-8"))
    seed = report["candidates"][0]
    best = report["candidates"][report["best_idx"]]
    assert report["best_idx"] == 1
    assert best["val_score"] > seed["val_score"], "the outage no longer inflates the candidate"
    assert seed["val_outcomes"] == {
        "concern-03-scope-creep": "unparseable reply",
        "rev-neg-01-used-helper-refactor": "unparseable reply",
    }
    assert best["val_outcomes"] == {
        "concern-03-scope-creep": "caught",
        "rev-neg-01-used-helper-refactor": "clean",
    }
    assert report["verdict"] == "refused"
    assert report["verdict_reasons"] == [
        "candidate 0: concern-03-scope-creep: unparseable reply",
        "candidate 0: rev-neg-01-used-helper-refactor: unparseable reply",
    ]


def test_a_clean_improvement_is_reported_improved(tmp_path: Path) -> None:
    """Every reply parses and the candidate catches what the seed missed,
    so the verdict is improved. The reflection model's usage is recorded."""
    reflection = ReflectionModel(
        agent=_ScriptedAgent(f"```\n{REVIEWER_PROMPT}\n{MARKER}\n```"),
        cwd=tmp_path,
        timeout=60.0,
        max_calls=5,
    )
    path = run_optimization(
        "reviewer",
        REVIEWER_PROMPT,
        _fixture_set(),
        runner=MarkerReviewer(),
        reflection_lm=reflection,
        max_metric_calls=10,
        run_dir=tmp_path / "run",
    )

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["best_idx"] == 1
    assert report["candidates"][0]["val_outcomes"] == {
        "concern-03-scope-creep": "missed",
        "rev-neg-01-used-helper-refactor": "clean",
    }
    assert report["candidates"][1]["val_outcomes"] == {
        "concern-03-scope-creep": "caught",
        "rev-neg-01-used-helper-refactor": "clean",
    }
    assert report["verdict"] == "improved"
    assert report["verdict_reasons"] == []
    assert report["reflection_usage"]["calls"] == 1
    assert report["reflection_usage"]["input_tokens"] == 100
    assert report["reflection_usage"]["output_tokens"] == 10


class OutageOnTheCandidateReviewer(MarkerReviewer):
    """:class:`MarkerReviewer`, except that a reply to a prompt carrying
    :data:`MARKER` on a negative fixture is an outage: every such reply, or
    only the first when ``first_only``. gepa scores a child on its train
    minibatch before its validation pass, and the negative is in both, so
    the first such reply is the minibatch's."""

    def __init__(self, *, first_only: bool) -> None:
        super().__init__()
        self.first_only = first_only
        self.outages = 0

    def __call__(self, prompt: str, fixture: RoleFixture) -> str:
        if fixture.negative and MARKER in prompt and not (self.first_only and self.outages):
            self.outages += 1
            return "upstream error: 529 overloaded"
        return super().__call__(prompt, fixture)


def _four_positive_run(tmp_path: Path, runner: OutageOnTheCandidateReviewer) -> dict[str, Any]:
    """Train is concern-01, concern-03 and the negative; validation is
    concern-02, concern-04 and the negative. With two positives on each
    side, the candidate outscores the seed even with the negative lost."""
    fixtures = _by_id(
        _fixtures("concerns", "concerns_negative"),
        "concern-01-dead-code",
        "concern-02-tautological-test",
        "concern-03-scope-creep",
        "concern-04-injection-empty-output",
        "rev-neg-01-used-helper-refactor",
    )
    path = run_optimization(
        "reviewer",
        REVIEWER_PROMPT,
        fixtures,
        runner=runner,
        reflection_lm=ScriptedReflection(REVIEWER_PROMPT),
        max_metric_calls=12,
        run_dir=tmp_path / "run",
    )
    report: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert report["best_idx"] == 1
    assert report["candidates"][0]["val_outcomes"] == {
        "concern-02-tautological-test": "missed",
        "concern-04-injection-empty-output": "missed",
        "rev-neg-01-used-helper-refactor": "clean",
    }
    return report


def test_an_unparseable_reply_on_the_best_candidate_refuses_the_verdict(tmp_path: Path) -> None:
    """The other side of the comparison: the seed's validation replies all
    parse, the best candidate's reply on the negative does not, and its
    val_score still beats the seed's. The verdict is refused and names the
    candidate, not the seed."""
    report = _four_positive_run(tmp_path, OutageOnTheCandidateReviewer(first_only=False))

    best = report["candidates"][1]
    assert best["val_score"] > report["candidates"][0]["val_score"]
    assert best["val_outcomes"] == {
        "concern-02-tautological-test": "caught",
        "concern-04-injection-empty-output": "caught",
        "rev-neg-01-used-helper-refactor": "unparseable reply",
    }
    assert report["verdict"] == "refused"
    assert report["verdict_reasons"] == [
        "candidate 1: rev-neg-01-used-helper-refactor: unparseable reply",
    ]


def test_an_outage_outside_the_validation_pass_is_not_a_refusal(tmp_path: Path) -> None:
    """The outage hits the candidate's train minibatch only. Its
    validation pass parses everywhere, so the verdict reads the
    validation pass and says improved."""
    runner = OutageOnTheCandidateReviewer(first_only=True)
    report = _four_positive_run(tmp_path, runner)

    assert runner.outages == 1, "the outage never happened, so this test measured nothing"
    assert report["candidates"][1]["val_outcomes"] == {
        "concern-02-tautological-test": "caught",
        "concern-04-injection-empty-output": "caught",
        "rev-neg-01-used-helper-refactor": "clean",
    }
    assert report["verdict"] == "improved"
    assert report["verdict_reasons"] == []
