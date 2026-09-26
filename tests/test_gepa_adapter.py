"""The GEPA seam, driven end to end with scripted models and no spend (#530).

Every test runs the real adapter, the real role builders and the real
calibration matchers. Only the two model calls are scripted: the role
reply (a :class:`kstrl.gepa_adapter.RoleRunner`) and the reflection model.
``run_optimization`` runs the real ``gepa.optimize`` on real calibration
fixtures from ``tests/adversarial_fixtures``.
"""

from __future__ import annotations

import json
import os
import pickle
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import tests.test_calibration as tc
from kstrl import calibration_score, gepa_adapter
from kstrl.agents.base import UsageRecord
from kstrl.agents.proc import TIMEOUT_MESSAGE_PREFIX
from kstrl.gepa_adapter import (
    GEPA_REFLECTION_PROMPT,
    KstrlGepaAdapter,
    ReflectionModel,
    RoleFixture,
    run_optimization,
    split_fixtures,
)
from kstrl.review import REVIEWER_PROMPT
from kstrl.security import SECURITY_PROMPT
from tests.helpers.calibration_repo_fixture import load_fixtures

#: The line the scripted reflection model appends to the seed prompt. The
#: scripted reviewer catches a planted defect only when its prompt holds it.
MARKER = "Report every helper that nothing calls."


def _fixtures(*subdirs: str) -> list[RoleFixture]:
    return [
        RoleFixture(meta["fixture_id"], meta, artifact.read_text(encoding="utf-8"))
        for subdir in subdirs
        for artifact, meta in load_fixtures(subdir, ".diff")
    ]


def _by_id(fixtures: list[RoleFixture], *ids: str) -> list[RoleFixture]:
    table = {f.fixture_id: f for f in fixtures}
    return [table[i] for i in ids]


def _concern_reply(category: str, severity: str, location: str) -> str:
    concern = {
        "category": category,
        "severity": severity,
        "location": location,
        "explanation": "scripted concern",
    }
    return json.dumps({"stories": [], "concerns": [concern]})


def _finding_reply(category: str, severity: str, location: str) -> str:
    finding = {
        "category": category,
        "severity": severity,
        "location": location,
        "explanation": "scripted finding",
    }
    return json.dumps({"findings": [finding]})


class CannedRunner:
    """A role reply per fixture id; records every prompt it is sent."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, prompt: str, fixture: RoleFixture) -> str:
        self.prompts.append((fixture.fixture_id, prompt))
        return self.replies[fixture.fixture_id]


class MarkerReviewer:
    """Catches a positive fixture's planted defect only when the prompt
    carries :data:`MARKER`; reports nothing otherwise."""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, prompt: str, fixture: RoleFixture) -> str:
        self.prompts.append((fixture.fixture_id, prompt))
        if fixture.negative or MARKER not in prompt:
            return json.dumps({"stories": [], "concerns": []})
        need = fixture.meta["must_detect"]
        return _concern_reply(need["category"], "fail", f"{need['evidence_path_contains']}:1")


class ScriptedReflection:
    """Returns the seed prompt plus :data:`MARKER` in a fenced block."""

    def __init__(self, seed: str) -> None:
        self.seed = seed
        self.prompts: list[str] = []

    def __call__(self, prompt: str | list[dict[str, Any]]) -> str:
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        return f"```\n{self.seed}\n{MARKER}\n```"


def _optimize(tmp_path: Path, reviewer: MarkerReviewer, reflection: ScriptedReflection) -> Path:
    fixtures = _by_id(
        _fixtures("concerns", "concerns_negative"),
        "concern-01-dead-code",
        "concern-03-scope-creep",
        "rev-neg-01-used-helper-refactor",
    )
    return run_optimization(
        "reviewer",
        REVIEWER_PROMPT,
        fixtures,
        runner=reviewer,
        reflection_lm=reflection,
        max_metric_calls=10,
        run_dir=tmp_path / "run",
    )


@pytest.mark.parametrize(
    "role,subdirs",
    [
        ("reviewer", ("concerns", "concerns_negative")),
        ("security", ("security", "security_negative")),
    ],
)
def test_every_split_carries_the_negatives(
    role: str, subdirs: tuple[str, str], tmp_path: Path
) -> None:
    """Negatives go to train AND validation; positives are split, not copied."""
    fixtures = _fixtures(*subdirs)
    negatives = {f.fixture_id for f in fixtures if f.negative}
    positives = {f.fixture_id for f in fixtures if not f.negative}
    assert negatives and len(positives) >= 2, f"{role}: the fixture set changed shape"

    train, validation = split_fixtures(role, fixtures)
    train_ids = {f.fixture_id for f in train}
    validation_ids = {f.fixture_id for f in validation}

    assert negatives <= train_ids, f"{role}: train is missing a negative"
    assert negatives <= validation_ids, f"{role}: validation is missing a negative"
    assert (train_ids - negatives) | (validation_ids - negatives) == positives
    assert not (train_ids - negatives) & (validation_ids - negatives)
    assert train_ids - negatives and validation_ids - negatives

    with pytest.raises(ValueError, match="negative"):
        split_fixtures(role, [f for f in fixtures if not f.negative])

    # The entry point refuses the same set before it creates anything.
    runner = CannedRunner({})
    with pytest.raises(ValueError, match="negative"):
        run_optimization(
            role,
            REVIEWER_PROMPT,
            [f for f in fixtures if not f.negative],
            runner=runner,
            reflection_lm=ScriptedReflection(REVIEWER_PROMPT),
            max_metric_calls=10,
            run_dir=tmp_path / "run",
        )
    assert not (tmp_path / "run").exists(), "a refused fixture set left a run_dir behind"
    assert runner.prompts == []


def test_existing_run_dir_is_refused(tmp_path: Path) -> None:
    """gepa resumes from a run_dir by unpickling gepa_state.bin. The one
    planted here runs os.mkdir when unpickled, so a sentinel directory that
    appears means the library loaded a file this call did not write."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sentinel = tmp_path / "unpickled"

    class _Payload:
        def __reduce__(self) -> tuple[Any, tuple[str]]:
            return (os.mkdir, (str(sentinel),))

    (run_dir / "gepa_state.bin").write_bytes(pickle.dumps(_Payload()))
    reviewer = MarkerReviewer()

    with pytest.raises(FileExistsError, match="unpickling"):
        run_optimization(
            "reviewer",
            REVIEWER_PROMPT,
            _by_id(
                _fixtures("concerns", "concerns_negative"),
                "concern-01-dead-code",
                "concern-03-scope-creep",
                "rev-neg-01-used-helper-refactor",
            ),
            runner=reviewer,
            reflection_lm=ScriptedReflection(REVIEWER_PROMPT),
            max_metric_calls=10,
            run_dir=run_dir,
        )

    assert not sentinel.exists(), "gepa unpickled a run_dir this call did not create"
    assert reviewer.prompts == [], "a refused run still called the role"


def test_reflection_uses_the_enrolled_template(tmp_path: Path) -> None:
    """The reflection model receives GEPA_REFLECTION_PROMPT, filled with the
    seed prompt and the scored failures, and not gepa's default template."""
    reflection = ScriptedReflection(REVIEWER_PROMPT)
    _optimize(tmp_path, MarkerReviewer(), reflection)

    assert len(reflection.prompts) == 1
    sent = reflection.prompts[0]
    tokens = set(re.findall(r"KSTRL-DATA-[0-9a-f]{32}", sent))
    assert len(tokens) == 1, f"expected one delimiter token, found {tokens}"
    filled = GEPA_REFLECTION_PROMPT.format(data_delimiter=tokens.pop())
    head, rest = filled.split("<curr_param>")
    middle, tail = rest.split("<side_info>")
    assert sent.startswith(head + REVIEWER_PROMPT + middle)
    assert sent.endswith(tail)
    failures = sent[len(head + REVIEWER_PROMPT + middle) : -len(tail)]
    assert "concern-01-dead-code" in failures
    assert "missed" in failures


def test_evaluate_scores_with_the_calibration_matchers() -> None:
    """Severity decides each of these scores, and the matchers the adapter
    grades with are the objects the calibration suite grades with."""
    for name in (
        "reviewer_caught",
        "reviewer_false_positive",
        "security_caught",
        "security_false_positive",
    ):
        assert getattr(tc, name) is getattr(calibration_score, name)
        assert getattr(gepa_adapter, name) is getattr(calibration_score, name)

    reviewer_batch = _by_id(
        _fixtures("concerns", "concerns_negative"),
        "concern-02-tautological-test",
        "rev-neg-01-used-helper-refactor",
        "rev-neg-02-thorough-tests",
    )
    reviewer = KstrlGepaAdapter(
        "reviewer",
        CannedRunner(
            {
                # test_quality at "advisory" does not meet the fixture's "fail" floor.
                "concern-02-tautological-test": _concern_reply(
                    "test_quality", "advisory", "tests/test_calculator.py:3"
                ),
                # dead_code is forbidden, but only a blocking concern counts.
                "rev-neg-01-used-helper-refactor": _concern_reply(
                    "dead_code", "advisory", "src/sandbox/parser.py:1"
                ),
                # test_quality is forbidden and this one blocks.
                "rev-neg-02-thorough-tests": _concern_reply(
                    "test_quality", "fail", "tests/test_calculator.py:1"
                ),
            }
        ),
    )
    batch = reviewer.evaluate(reviewer_batch, {"reviewer": REVIEWER_PROMPT}, capture_traces=True)
    assert batch.scores == [0.0, 1.0, 0.0]
    assert batch.objective_scores == [
        {"detection": 0.0},
        {"false_positive": 1.0},
        {"false_positive": 0.0},
    ]
    records = reviewer.make_reflective_dataset({"reviewer": REVIEWER_PROMPT}, batch, ["reviewer"])
    assert [(r["fixture"], r["outcome"]) for r in records["reviewer"]] == [
        ("concern-02-tautological-test", "missed"),
        ("rev-neg-02-thorough-tests", "false positive"),
    ]
    assert records["reviewer"][0]["must_detect"] == reviewer_batch[0].meta["must_detect"]
    assert records["reviewer"][0]["findings"][0]["severity"] == "advisory"

    security_batch = _by_id(_fixtures("security"), "sec-01-sql-injection")
    security = KstrlGepaAdapter(
        "security",
        # injection at "medium" is below the fixture's "high" floor.
        CannedRunner(
            {"sec-01-sql-injection": _finding_reply("injection", "medium", "src/users.py:5")}
        ),
    )
    assert security.evaluate(security_batch, {"security": SECURITY_PROMPT}).scores == [0.0]


@pytest.mark.parametrize(
    "role,subdir,fixture_id,seed",
    [
        ("reviewer", "concerns_negative", "rev-neg-01-used-helper-refactor", REVIEWER_PROMPT),
        ("security", "security_negative", "sec-neg-01-parameterized-dynamic-sql", SECURITY_PROMPT),
    ],
    ids=["reviewer", "security"],
)
def test_unparseable_reply_scores_zero_on_a_negative(
    role: str, subdir: str, fixture_id: str, seed: str
) -> None:
    """A reply the parser rejects measured nothing. On a negative fixture it
    must not score as a clean pass, or a prompt that breaks the output
    format scores perfectly on every negative."""
    fixture = _by_id(_fixtures(subdir), fixture_id)
    adapter = KstrlGepaAdapter(role, CannedRunner({fixture_id: "no JSON here"}))
    batch = adapter.evaluate(fixture, {role: seed}, capture_traces=True)
    assert batch.scores == [0.0]
    records = adapter.make_reflective_dataset({role: seed}, batch, [role])
    assert records[role][0]["outcome"] == "unparseable reply"


@pytest.mark.parametrize(
    "role,template,error",
    [
        ("reviewer", "Review {the_change}.", "KeyError('the_change')"),
        ("reviewer", "Review {0}.", "IndexError"),
        ("reviewer", "Review {.", "ValueError"),
        ("security", "Review {prd_content.x}.", "AttributeError"),
    ],
)
def test_a_candidate_that_does_not_render_scores_zero_and_runs_nothing(
    role: str, template: str, error: str
) -> None:
    """A proposed prompt is model-written text. One whose placeholders the
    role's builder cannot fill is a failed candidate, scored 0.0 with the
    reason, and the role is never called with it. str.format raises four
    different types for four malformed templates, so a guard narrower than
    Exception lets one of them stop the whole optimization."""
    subdir = "concerns_negative" if role == "reviewer" else "security_negative"
    fixture = [f for f in _fixtures(subdir) if f.negative][:1]
    runner = CannedRunner({})
    adapter = KstrlGepaAdapter(role, runner)
    candidate = {role: template}
    batch = adapter.evaluate(fixture, candidate, capture_traces=True)
    assert batch.scores == [0.0]
    assert runner.prompts == []
    records = adapter.make_reflective_dataset(candidate, batch, [role])
    assert records[role][0]["outcome"] == "prompt did not render"
    assert error in records[role][0]["reason"]


def test_optimize_end_to_end_with_scripted_models(tmp_path: Path) -> None:
    """Seed misses both planted defects; the reflection adds MARKER; the
    candidate catches both and keeps the negative clean, so it is accepted
    on train and chosen on validation."""
    reviewer = MarkerReviewer()
    reflection = ScriptedReflection(REVIEWER_PROMPT)
    report_path = _optimize(tmp_path, reviewer, reflection)

    assert report_path == tmp_path / "run" / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["role"] == "reviewer"
    assert report["gepa_version"] == "0.1.4"
    assert report["reflection_prompt_version"] == gepa_adapter.GEPA_REFLECTION_PROMPT_VERSION
    assert report["splits"] == {
        "train": ["concern-01-dead-code", "rev-neg-01-used-helper-refactor"],
        "validation": ["concern-03-scope-creep", "rev-neg-01-used-helper-refactor"],
    }
    assert report["total_metric_calls"] == 10
    assert report["best_idx"] == 1
    seed, proposed = report["candidates"]
    assert seed["prompt"] == REVIEWER_PROMPT
    assert seed["val_score"] == 0.5
    assert seed["val_objectives"] == {"detection": 0.0, "false_positive": 1.0}
    assert proposed["prompt"].endswith(MARKER)
    assert proposed["parents"] == [0]
    assert proposed["val_score"] == 1.0
    assert proposed["val_objectives"] == {"detection": 1.0, "false_positive": 1.0}
    assert len(reflection.prompts) == 1
    assert len(reviewer.prompts) == 10


class _ScriptedAgent:
    """An Agent whose every run yields one reply and one usage record."""

    name = "scripted"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.runs = 0
        self.final_message: str | None = None
        self.usage_records: list[UsageRecord] = []

    def run(
        self, prompt: str, cwd: Path | None = None, timeout: float | None = None
    ) -> Iterator[str]:
        self.runs += 1
        self.final_message = self.reply
        self.usage_records.append(UsageRecord(input_tokens=100, output_tokens=10))
        yield self.reply


def test_reflection_model_records_usage_and_stops_at_its_cap(tmp_path: Path) -> None:
    agent = _ScriptedAgent("```\nnew instructions\n```")
    model = ReflectionModel(agent=agent, cwd=tmp_path, timeout=60.0, max_calls=2)

    assert model("first") == "```\nnew instructions\n```"
    assert model("second") == "```\nnew instructions\n```"
    with pytest.raises(RuntimeError, match="reflection budget of 2 calls is spent"):
        model("third")

    assert agent.runs == 2
    assert model.usage.calls == 2
    assert model.usage.input_tokens == 200
    assert model.usage.output_tokens == 20


@pytest.mark.parametrize(
    "last_line,error",
    [
        (f"{TIMEOUT_MESSAGE_PREFIX} after 60.0s", "timed out"),
        ("", "empty reply"),
    ],
)
def test_reflection_model_refuses_a_timed_out_or_empty_reply(
    tmp_path: Path, last_line: str, error: str
) -> None:
    """gepa takes an unfenced reply whole as the new prompt, so returning a
    timeout line or an empty string would turn it into a candidate. Both
    raise instead, and the call and its usage are still counted."""
    agent = _ScriptedAgent(last_line)
    model = ReflectionModel(agent=agent, cwd=tmp_path, timeout=60.0, max_calls=2)

    with pytest.raises(RuntimeError, match=error):
        model("revise this")

    assert model.calls == 1
    assert model.usage.calls == 1
