"""#233 Part B: a convergence check across attempts.

Driven through the real ``ComponentPipeline.process_result``: the Phase 1
hook returns a failing verification whose linter check parsed N failures,
the real Phase 1 turns that into a ``PhaseFailure``, and the real
``_route_failure`` / ``retry_or_fail`` decide. What is asserted is the
component's recorded outcome, its finding stream and the evolution journal
row on disk.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl.evolution import FINDINGS_SUPERSEDED_EVENT
from kstrl.factory import ComponentResult, FactoryConfig
from kstrl.manifest import Component, ComponentStatus
from kstrl.parsers import ParsedFailure, ParsedOutput
from kstrl.pipeline import Transition
from kstrl.review import ReviewConcern, ReviewResult
from kstrl.security import SecurityConfig, SecurityFinding, SecurityResult
from kstrl.verify import CheckResult, VerificationResult
from tests.test_pipeline import _factory_config, _make_pipeline, _selection


def _failing(count: int) -> VerificationResult:
    """Phase 1 failing on the linter with ``count`` parsed failures."""
    return VerificationResult(
        passed=False,
        checks=[
            CheckResult(
                name="linter",
                passed=False,
                message="Linter failed (exit code 1)",
                parsed=ParsedOutput(
                    tool="ruff",
                    failures=[
                        ParsedFailure(file="a.py", line=n, code="E501") for n in range(count)
                    ],
                ),
            )
        ],
    )


def _drive(
    tmp_path: Path, counts: list[int | None], *, max_retries: int = 10, **config: object
) -> tuple[list[Transition], Component]:
    """Run one component through ``len(counts)`` failed attempts. An int is
    an attempt whose Phase 1 reported that many failures; None is an
    attempt whose engineer loop failed, so no gate counted anything.
    Returns the transitions and the component."""
    results: Iterator[VerificationResult] = iter([_failing(c) for c in counts if c is not None])
    pipeline, manifest, _, _ = _make_pipeline(
        tmp_path,
        config=_factory_config(max_retries=max_retries, **config),
        hooks_overrides={"run_mechanical_verification": lambda *a, **k: next(results)},
    )
    comp = manifest.get_component("comp-a")
    assert comp is not None
    transitions: list[Transition] = []
    for count in counts:
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=count is not None,
                iterations=1,
                duration_seconds=1.0,
                error=None if count is not None else "Did not complete",
                context_json=pipeline.component_contexts.get("comp-a"),
            ),
        )
        assert outcome is not None
        transitions.append(outcome.transition)
        if outcome.transition == Transition.FAILED:
            break
    return transitions, comp


def test_a_count_that_never_falls_fails_the_component_as_divergence(tmp_path: Path) -> None:
    transitions, comp = _drive(tmp_path, [2, 2, 2], convergence_attempts=2)

    assert transitions == [Transition.RETRYING, Transition.RETRYING, Transition.FAILED]
    assert comp.status == ComponentStatus.FAILED.value
    assert comp.failed_phase == "engineer"
    assert comp.failed_check == "convergence"
    divergence = [f for f in comp.findings if f.category == "divergence"]
    assert len(divergence) == 1
    finding = divergence[0]
    assert finding.phase == "engineer"
    assert finding.severity == "fail"
    assert "2 -> 2 -> 2" in finding.explanation


def test_one_decrease_resets_the_streak(tmp_path: Path) -> None:
    transitions, comp = _drive(tmp_path, [3, 2, 2], convergence_attempts=2)

    assert transitions == [Transition.RETRYING] * 3
    assert comp.failed_check == "linter"
    assert not [f for f in comp.findings if f.category == "divergence"]


def test_an_attempt_with_no_count_breaks_the_run(tmp_path: Path) -> None:
    """An engineer-loop failure between two gate failures measured
    nothing, so the run of readings starts again after it: 2, then no
    reading, then 2, 2 is two readings, not three."""
    transitions, comp = _drive(tmp_path, [2, None, 2, 2], convergence_attempts=2)

    assert transitions == [Transition.RETRYING] * 4
    assert not [f for f in comp.findings if f.category == "divergence"]


def test_the_default_never_fires(tmp_path: Path) -> None:
    assert FactoryConfig().convergence_attempts == 0
    transitions, _ = _drive(tmp_path, [2, 2, 2, 2, 2])

    assert transitions == [Transition.RETRYING] * 5


def test_the_journal_records_the_count_per_superseded_attempt(tmp_path: Path) -> None:
    _drive(tmp_path, [5, 3, 4])

    assert _journaled_counts(tmp_path) == [(1, 5), (2, 3), (3, 4)]


def _journaled_counts(root: Path) -> list[tuple[int, int | None]]:
    """(attempt, failure_count) of every superseded-attempt row on disk.
    Indexed, not ``.get``: a row without the key is a KeyError, so a
    missing key cannot pass as a null count."""
    journal = root / ".kstrl" / "evolution.jsonl"
    rows = [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        (r["attempt"], r["failure_count"])
        for r in rows
        if r.get("event_type") == FINDINGS_SUPERSEDED_EVENT
    ]


def test_an_exhausted_retry_budget_fails_on_the_gate_not_on_convergence(
    tmp_path: Path,
) -> None:
    """The check only refuses a retry that would otherwise be bought. With
    no retry left the component fails on its real gate, so the journal
    keeps the gate's identity."""
    transitions, comp = _drive(tmp_path, [2, 2, 2], max_retries=2, convergence_attempts=2)

    assert transitions == [Transition.RETRYING, Transition.RETRYING, Transition.FAILED]
    assert comp.failed_check == "linter"
    assert not [f for f in comp.findings if f.category == "divergence"]


@pytest.fixture
def _no_real_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewer phases read the shared diff and build an agent before
    they call the stubbed reviewer; tmp_path is not a git repository, so
    both are stubbed at their source modules, as tests/test_pipeline.py
    does for every pipeline test."""
    monkeypatch.setattr("kstrl.git.get_diff_content", lambda *a, **k: "diff --git a b\n")
    monkeypatch.setattr("kstrl.agents.get_agent", lambda *a, **k: object())


def _review(fails: int) -> ReviewResult:
    return ReviewResult(
        passed=False,
        mode="hard",
        concerns=[
            ReviewConcern(
                category="test_quality", severity="fail", location=f"a.py:{n}", explanation="x"
            )
            for n in range(fails)
        ],
    )


def _security(fails: int) -> SecurityResult:
    return SecurityResult(
        passed=False,
        mode="hard",
        findings=[
            SecurityFinding(
                category="injection", severity="high", location=f"a.py:{n}", explanation="x"
            )
            for n in range(fails)
        ],
    )


@pytest.mark.usefixtures("_no_real_diff")
@pytest.mark.parametrize("gate", ["review", "security"])
def test_a_reviewer_failure_journals_the_reviewers_fail_count(tmp_path: Path, gate: str) -> None:
    """Phase 2 and Phase 2.5 hand their own blocking-finding count to the
    check: two attempts failing with 3 and then 1 blocking findings."""
    readings = iter([3, 1])
    if gate == "review":
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="hard", max_retries=10),
            hooks_overrides={"run_review": lambda *a, **k: _review(next(readings))},
        )
    else:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="skip", security_config=SecurityConfig(mode="hard"), max_retries=10
            ),
            security_selection=_selection("security"),
            hooks_overrides={"run_security_review": lambda *a, **k: _security(next(readings))},
        )
    comp = manifest.get_component("comp-a")
    assert comp is not None
    for _ in range(2):
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=True,
                iterations=1,
                duration_seconds=1.0,
                context_json=pipeline.component_contexts.get("comp-a"),
            ),
        )
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
    assert comp.failed_phase == gate
    assert _journaled_counts(tmp_path) == [(1, 3), (2, 1)]


@pytest.mark.usefixtures("_no_real_diff")
def test_a_crashed_reviewer_breaks_the_run_and_journals_no_count(tmp_path: Path) -> None:
    """A reviewer that crashed counted nothing, so the attempt has no
    reading: null in the journal, and the run of readings starts again
    after it even though the phase did not change. Review counts 2, then
    a crash, then 2, 2 is two readings, so ``convergence_attempts=2``
    does not trip."""
    crashed = ReviewResult(passed=False, mode="hard", infrastructure_error=True)
    reviews = iter([_review(2), crashed, _review(2), _review(2)])
    pipeline, manifest, _, _ = _make_pipeline(
        tmp_path,
        config=_factory_config(review_mode="hard", max_retries=10, convergence_attempts=2),
        hooks_overrides={"run_review": lambda *a, **k: next(reviews)},
    )
    comp = manifest.get_component("comp-a")
    assert comp is not None
    transitions: list[Transition] = []
    for _ in range(4):
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=True,
                iterations=1,
                duration_seconds=1.0,
                context_json=pipeline.component_contexts.get("comp-a"),
            ),
        )
        assert outcome is not None
        transitions.append(outcome.transition)
    assert transitions == [Transition.RETRYING] * 4
    assert comp.failed_phase == "review"
    assert _journaled_counts(tmp_path) == [(1, 2), (2, None), (3, 2), (4, 2)]


@pytest.mark.usefixtures("_no_real_diff")
def test_a_count_from_a_later_gate_starts_the_run_again(tmp_path: Path) -> None:
    """Attempt 1 fails Phase 1 with 2 failures; attempts 2 to 4 pass Phase 1
    and fail review with 2 blocking concerns each. Reaching review is
    progress a count cannot show, so the run restarts at the phase change
    (2, then 2, 2 is two readings and does not trip) and trips inside
    review at attempt 4 (2, 2, 2)."""
    verifications = iter(
        [_failing(2)] + [VerificationResult(passed=True, checks=[]) for _ in range(3)]
    )
    pipeline, manifest, _, _ = _make_pipeline(
        tmp_path,
        config=_factory_config(review_mode="hard", max_retries=10, convergence_attempts=2),
        hooks_overrides={
            "run_mechanical_verification": lambda *a, **k: next(verifications),
            "run_review": lambda *a, **k: _review(2),
        },
    )
    comp = manifest.get_component("comp-a")
    assert comp is not None
    transitions: list[Transition] = []
    for _ in range(4):
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=True,
                iterations=1,
                duration_seconds=1.0,
                context_json=pipeline.component_contexts.get("comp-a"),
            ),
        )
        assert outcome is not None
        transitions.append(outcome.transition)
    assert transitions == [Transition.RETRYING] * 3 + [Transition.FAILED]
    assert comp.failed_check == "convergence"
    assert _journaled_counts(tmp_path) == [(1, 2), (2, 2), (3, 2)]


@pytest.mark.parametrize(
    ("toml_value", "env_value"),
    [("-1", None), ("true", None), ('"2"', None), (None, "-1"), (None, "two")],
)
def test_a_bad_convergence_attempts_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    toml_value: str | None,
    env_value: str | None,
) -> None:
    monkeypatch.delenv("KSTRL_FACTORY_CONVERGENCE_ATTEMPTS", raising=False)
    if toml_value is not None:
        (tmp_path / "kstrl.toml").write_text(
            f"[factory]\nconvergence_attempts = {toml_value}\n", encoding="utf-8"
        )
    if env_value is not None:
        monkeypatch.setenv("KSTRL_FACTORY_CONVERGENCE_ATTEMPTS", env_value)
    with pytest.raises(ValueError, match="convergence_attempts|CONVERGENCE_ATTEMPTS"):
        FactoryConfig.load(tmp_path)


def test_convergence_attempts_loads_from_toml_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KSTRL_FACTORY_CONVERGENCE_ATTEMPTS", raising=False)
    (tmp_path / "kstrl.toml").write_text("[factory]\nconvergence_attempts = 2\n", encoding="utf-8")
    assert FactoryConfig.load(tmp_path).convergence_attempts == 2
    monkeypatch.setenv("KSTRL_FACTORY_CONVERGENCE_ATTEMPTS", "3")
    assert FactoryConfig.load(tmp_path).convergence_attempts == 3
    assert FactoryConfig.from_env().convergence_attempts == 3


@pytest.mark.usefixtures("_no_real_diff")
def test_a_contract_reset_after_a_passing_attempt_starts_the_run_again(tmp_path: Path) -> None:
    """Attempts 1 and 2 fail Phase 1 with 2 failures, attempt 3 passes every
    gate, and the contract breaker resets it. Attempt 3 counted nothing, so
    attempt 4 failing with 2 again is one reading, not the third of a run."""
    results = iter(
        [_failing(2), _failing(2), VerificationResult(passed=True, checks=[]), _failing(2)]
    )
    pipeline, manifest, _, _ = _make_pipeline(
        tmp_path,
        config=_factory_config(max_retries=10, convergence_attempts=2),
        hooks_overrides={"run_mechanical_verification": lambda *a, **k: next(results)},
    )
    comp = manifest.get_component("comp-a")
    assert comp is not None
    transitions: list[Transition] = []
    for attempt in range(4):
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result(
            "comp-a",
            ComponentResult(
                "comp-a",
                success=True,
                iterations=1,
                duration_seconds=1.0,
                context_json=pipeline.component_contexts.get("comp-a"),
            ),
        )
        assert outcome is not None
        transitions.append(outcome.transition)
        if attempt == 2:
            # The factory's contract-breaker reset, as run_factory does it.
            pipeline.journal_superseded_findings(comp)
            comp.retries += 1
            comp.status = ComponentStatus.PENDING.value
            pipeline.record_contract_failure("comp-a", comp.retries, "contract output")
    assert transitions == [
        Transition.RETRYING,
        Transition.RETRYING,
        Transition.COMPLETED,
        Transition.RETRYING,
    ]
    assert comp.failed_check == "linter"
