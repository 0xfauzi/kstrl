"""#524: Phase 2.5 counts exactly the findings that decided its verdict.

Before #524 the verdict failed hard mode on any finding at or above
``[security] fail_threshold`` while ``SecurityResult.fail_count`` counted
critical plus high whatever the threshold. At ``fail_threshold =
"medium"`` an attempt that failed on three medium findings reported
``fail_count=0`` to the ``review_result`` event and to the #233
convergence check.

The first test drives the real ``run_security_review`` over a real git
repository with a canned reviewer reply, for every threshold, and holds
the verdict, the count and the log line to one expected number written
out by hand. The second drives the real pipeline around it and reads the
two readers of the count off disk: the ``review_result`` row in
``progress.jsonl`` and the convergence reading in the evolution journal.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from kstrl.evolution import FINDINGS_SUPERSEDED_EVENT
from kstrl.factory import ComponentResult
from kstrl.observability import read_progress_events
from kstrl.pipeline import Transition
from kstrl.security import SecurityConfig, SecurityResult, run_security_review
from kstrl.ui.base import UI
from kstrl.ui.plain import PlainUI
from tests.conftest import ReviewRepo, make_review_repo, with_observed_diffstat
from tests.test_convergence_check import _journaled_counts
from tests.test_pipeline import _factory_config, _make_pipeline, _selection
from tests.test_security import MockSecurityAgent

ALL_FOUR = ["critical", "high", "medium", "low"]


def _reply(repo: ReviewRepo, severities: list[str]) -> str:
    """A reviewer reply with one finding per severity, in order, carrying
    ``repo``'s real diffstat so the coverage check stays silent."""
    payload = {
        "findings": [
            {
                "category": "injection",
                "severity": severity,
                "location": f"a.py:{n}",
                "explanation": "unescaped input reaches the query",
            }
            for n, severity in enumerate(severities)
        ],
        "exhaustively_searched": True,
    }
    return with_observed_diffstat(json.dumps(payload), repo)


def _repo(tmp_path: Path) -> ReviewRepo:
    repo = make_review_repo(tmp_path / "repo")
    repo.prd_path.write_text('{"branchName": "t", "userStories": []}', encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    ("mode", "threshold", "severities", "expected_fail"),
    [
        ("hard", "critical", ALL_FOUR, 1),
        ("hard", "high", ALL_FOUR, 2),
        ("hard", "medium", ALL_FOUR, 3),
        ("hard", "low", ALL_FOUR, 4),
        ("hard", "medium", ["medium", "medium", "medium"], 3),
        ("hard", "critical", ["high"], 0),
        ("hard", "medium", ["low"], 0),
        ("advisory", "low", ALL_FOUR, 0),
        ("advisory", "critical", ALL_FOUR, 0),
    ],
)
def test_the_verdict_the_count_and_the_log_line_agree(
    tmp_path: Path, mode: str, threshold: str, severities: list[str], expected_fail: int
) -> None:
    repo = _repo(tmp_path)
    out = io.StringIO()
    result = run_security_review(
        MockSecurityAgent(_reply(repo, severities)),
        repo.prd_path,
        repo.path,
        repo.base_branch,
        SecurityConfig(mode=mode, fail_threshold=threshold),
        PlainUI(no_color=True, file=out),
    )
    expected_advisory = len(severities) - expected_fail
    assert result.infrastructure_error is False
    assert len(result.findings) == len(severities)
    assert (result.fail_count, result.advisory_count) == (expected_fail, expected_advisory)
    assert result.passed is (expected_fail == 0)
    assert f"{expected_fail} fail, {expected_advisory} advisory" in out.getvalue()


@pytest.fixture
def _no_real_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline reads the shared diff and builds an agent before it
    calls the reviewer hook; the pipeline root is not a git repository,
    so both are stubbed at their source modules, as in
    tests/test_convergence_check.py."""
    monkeypatch.setattr("kstrl.git.get_diff_content", lambda *a, **k: "diff --git a b\n")
    monkeypatch.setattr("kstrl.agents.get_agent", lambda *a, **k: object())


def _drive(
    tmp_path: Path, threshold: str, replies: list[list[str]]
) -> tuple[list[Transition], list[dict[str, Any]], Path, str]:
    """One component through ``len(replies)`` attempts of the real pipeline.
    Phase 2.5 is the real ``run_security_review`` judged against the
    ``SecurityConfig`` the pipeline hands it; only the reviewer's reply is
    canned. Returns the transitions, the ``review_result`` rows of
    ``progress.jsonl``, the pipeline root and the UI text."""
    repo = _repo(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    pending = iter(replies)

    def real_security_review(
        _agent: object,
        _prd: Path,
        _wt: Path,
        _base: str,
        config: SecurityConfig,
        ui: UI,
        **_kwargs: object,
    ) -> SecurityResult:
        agent = MockSecurityAgent(_reply(repo, next(pending)))
        return run_security_review(agent, repo.prd_path, repo.path, repo.base_branch, config, ui)

    out = io.StringIO()
    pipeline, manifest, _, _ = _make_pipeline(
        root,
        config=_factory_config(
            review_mode="skip",
            security_config=SecurityConfig(mode="hard", fail_threshold=threshold),
            max_retries=10,
        ),
        ui=PlainUI(no_color=True, file=out),
        security_selection=_selection("security"),
        hooks_overrides={"run_security_review": real_security_review},
    )
    comp = manifest.get_component("comp-a")
    assert comp is not None
    transitions: list[Transition] = []
    for _ in replies:
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
    rows = [
        e["data"]
        for e in read_progress_events(root / "progress.jsonl")
        if e.get("event") == "review_result"
    ]
    return transitions, rows, root, out.getvalue()


@pytest.mark.usefixtures("_no_real_diff")
def test_a_failure_below_high_reaches_the_event_and_the_convergence_reading(
    tmp_path: Path,
) -> None:
    """``fail_threshold = "medium"``: attempts failing on three medium
    findings beside one low one, then on one medium finding. Before #524
    both readers recorded 0 for both. The low finding keeps the count
    apart from the number of findings, so a log line that counts every
    finding says 4 and fails."""
    transitions, rows, root, text = _drive(
        tmp_path, "medium", [["medium", "medium", "medium", "low"], ["medium"]]
    )
    assert transitions == [Transition.RETRYING, Transition.RETRYING]
    assert [(r["passed"], r["fail_count"], r["advisory_count"]) for r in rows] == [
        (False, 3, 1),
        (False, 1, 0),
    ]
    assert _journaled_counts(root) == [(1, 3), (2, 1)]
    assert "Phase 2.5 FAILED for comp-a: 3 failures" in text


@pytest.mark.usefixtures("_no_real_diff")
def test_a_pass_above_the_finding_reports_no_failure(tmp_path: Path) -> None:
    """``fail_threshold = "critical"``: one high finding passes the gate,
    so the event counts it advisory. Before #524 it said fail_count=1."""
    transitions, rows, _, _ = _drive(tmp_path, "critical", [["high"]])
    assert transitions == [Transition.COMPLETED]
    assert [(r["passed"], r["fail_count"], r["advisory_count"]) for r in rows] == [(True, 0, 1)]


@pytest.mark.usefixtures("_no_real_diff")
def test_a_failure_below_high_journals_the_categories_that_failed(tmp_path: Path) -> None:
    """``fail_threshold = "medium"``: the journal's failure signatures name
    the category that failed the gate. Before, they were chosen by a fixed
    critical-plus-high rule, so a medium failure journaled only the
    error-text fallback ``security:security-review-failed``."""
    _, _, root, _ = _drive(tmp_path, "medium", [["medium", "low"]])
    rows = [
        json.loads(line)
        for line in (root / ".kstrl" / "evolution.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    signatures = [
        r["failure_signatures"] for r in rows if r.get("event_type") == FINDINGS_SUPERSEDED_EVENT
    ]
    assert signatures == [["security:injection"]]
