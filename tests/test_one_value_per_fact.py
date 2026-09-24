"""#450: one fact, one value, on every surface that records it.

Found by the cross-surface reconciler on the #432 repository and again
on the #453 build. Two facts were recorded twice with different values.

Component duration. ``component_completed`` carried the engineer loop's
duration while the manifest and the journal carried the whole attempt
(R6.4), and ``progress.jsonl`` rounded the event's value a second time.

Review finding counts. ``review_result`` carried criterion counts only,
while the "Review ..." log line, the reviewer's ``finding_recorded`` rows
and the divergence reading counted criteria and concerns together. The
merge-gate inbox item counted every review-phase finding, claim
disagreements included. Phase 2.5's ``advisory_count`` counted every
security finding, the critical and high ones ``fail_count`` had already
counted.

The end-to-end tests drive the real ``run_factory`` with the LLM seams
stubbed and read the surfaces off disk. The census tests pin every place
the two events are built, so a second writer shows up as a delta.
"""

from __future__ import annotations

import ast
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl import events as ev
from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.findings import CLAIM_DISAGREEMENT_CATEGORY
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.manifest import Component, Manifest
from kstrl.review import CriterionReview, ReviewConcern, ReviewResult
from kstrl.security import SecurityConfig, SecurityFinding, SecurityResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig
from tests.helpers.astwalk import Sees, assert_census, label, package_sources, parsed
from tests.helpers.component_prd import PASSING_STORY, write_component_prd
from tests.test_attempt_iteration_readings import _flattened_targets, owner_row
from tests.test_inbox import _init_git_repo

COMP = "comp-a"
PRD_REL = f"scripts/kstrl/feature/{COMP}/prd.json"
#: The engineer loop's duration as the stub worker reports it. Chosen so
#: it cannot equal the attempt's measured wall-clock time.
ENGINEER_SECONDS = 1234.5
ENGINEER_ITERATIONS = 3


class _NoPromptUI(PlainUI):
    """Non-interactive regardless of how pytest wires stdin, so the merge
    gate parks instead of prompting."""

    def can_prompt(self) -> bool:
        return False


def _project(root: Path) -> Path:
    """A committed repository (identity set by ``_init_git_repo``) holding
    one component PRD whose single story the engineer marked done."""
    _init_git_repo(root)
    kstrl_dir = root / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("p", encoding="utf-8")
    write_component_prd(root, PRD_REL, stories=[PASSING_STORY])
    (root / "kstrl.toml").write_text("[knowledge]\nenabled = false\n", encoding="utf-8")
    return root


def _run(
    root: Path,
    *,
    review: ReviewResult,
    security: SecurityResult | None = None,
    pause_before_pr_merge: bool = False,
) -> str:
    """One real ``run_factory`` over one component; returns the UI text."""
    out = io.StringIO()
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[Component(COMP, "A", "D", [], PRD_REL, f"kstrl/factory/{COMP}")],
    )
    config = FactoryConfig(
        use_worktrees=False,
        create_prs=pause_before_pr_merge,
        pause_before_pr_merge=pause_before_pr_merge,
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        review_mode=review.mode,
        security_config=SecurityConfig(mode=security.mode) if security is not None else None,
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
    )
    base = KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )
    worker = ComponentResult(
        COMP,
        success=True,
        iterations=ENGINEER_ITERATIONS,
        duration_seconds=ENGINEER_SECONDS,
    )
    passing = VerificationResult(passed=True, checks=[CheckResult("test_suite", True, "ok")])
    with (
        patch("kstrl.factory._run_component", return_value=worker),
        patch("kstrl.factory.run_mechanical_verification", return_value=passing),
        patch("kstrl.factory.run_review", return_value=review),
        patch("kstrl.factory.run_security_review", return_value=security),
        patch("kstrl.git.get_diff_content", return_value="diff --git a b\n"),
        patch("kstrl.pr.is_gh_available", return_value=False),
    ):
        run_factory(
            manifest,
            config,
            base,
            _NoPromptUI(no_color=True, file=out),
            root,
            manifest_path=root / "scripts" / "kstrl" / "manifest.json",
        )
    return out.getvalue()


def _events(root: Path) -> list[ev.Event]:
    run_dir = sorted((root / ".kstrl" / "runs").iterdir())[-1]
    return list(ev.read_events(run_dir / "events.jsonl"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _review_event(events: list[ev.Event], *, security: bool) -> ev.ReviewResultEvent:
    found = [
        e
        for e in events
        if isinstance(e, ev.ReviewResultEvent) and e.mode.startswith("security") is security
    ]
    assert len(found) == 1, found
    return found[0]


def _reviewer_rows(events: list[ev.Event], phase: str) -> list[ev.FindingRecorded]:
    """The rows the reviewer's own findings produced. A claim disagreement
    is also recorded under phase ``review``, but it is the pipeline's
    comparison of the reviewer against the PRD, not a reviewer finding."""
    return [
        e
        for e in events
        if isinstance(e, ev.FindingRecorded)
        and e.phase == phase
        and e.category != CLAIM_DISAGREEMENT_CATEGORY
    ]


def _counts(rows: list[ev.FindingRecorded]) -> tuple[int, int]:
    return (
        sum(1 for r in rows if r.severity == "fail"),
        sum(1 for r in rows if r.severity == "advisory"),
    )


def _passing_review() -> ReviewResult:
    return ReviewResult(
        passed=True,
        mode="hard",
        criteria=[CriterionReview("AC1", "pass", "ok", story_id="US-001")],
        concerns=[
            ReviewConcern("test_quality", "advisory", "a.py:1", "weak"),
            ReviewConcern("error_handling", "advisory", "a.py:2", "swallowed"),
        ],
    )


class TestComponentDuration:
    def test_every_surface_carries_the_attempt_duration(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        output = _run(root, review=_passing_review())
        events = _events(root)

        completed = [e for e in events if isinstance(e, ev.ComponentCompleted)]
        assert len(completed) == 1
        manifest = Manifest.load(root / "scripts" / "kstrl" / "manifest.json")
        comp = manifest.get_component(COMP)
        assert comp is not None
        journal_rows = [
            e
            for e in _jsonl(root / ".kstrl" / "evolution.jsonl")
            if e.get("event_type") == "component_result"
        ]
        progress_rows = [
            e
            for e in _jsonl(root / ".kstrl" / "progress.jsonl")
            if e["event"] == "component_completed"
        ]
        assert len(journal_rows) == 1 and len(progress_rows) == 1

        attempt = comp.duration_seconds
        assert completed[0].duration_seconds == attempt
        assert journal_rows[0]["duration_seconds"] == attempt
        assert progress_rows[0]["data"]["duration_seconds"] == attempt
        assert completed[0].iterations == comp.iteration_count == ENGINEER_ITERATIONS
        assert f"COMPLETED: {COMP} ({ENGINEER_ITERATIONS} iterations, {attempt:.0f}s)" in output

        # The engineer loop's own duration is still recorded, once, on
        # the engineer phase's bracket closer.
        engineer = [e for e in events if isinstance(e, ev.PhaseCompleted) and e.phase == "engineer"]
        assert [e.duration_seconds for e in engineer] == [ENGINEER_SECONDS]
        assert attempt != ENGINEER_SECONDS


class TestReviewCounts:
    @pytest.mark.parametrize(
        ("review", "expected"),
        [
            pytest.param(_passing_review(), (0, 2), id="advisory-concerns"),
            pytest.param(
                ReviewResult(
                    passed=False,
                    mode="hard",
                    criteria=[CriterionReview("AC1", "pass", "ok", story_id="US-001")],
                    concerns=[
                        ReviewConcern("error_handling", "fail", "a.py:2", "swallowed"),
                        ReviewConcern("test_quality", "advisory", "a.py:1", "weak"),
                    ],
                ),
                (1, 1),
                id="failing-concern",
            ),
        ],
    )
    def test_the_event_counts_the_reviewers_finding_rows(
        self, tmp_path: Path, review: ReviewResult, expected: tuple[int, int]
    ) -> None:
        root = _project(tmp_path)
        _run(root, review=review)
        events = _events(root)
        event = _review_event(events, security=False)
        assert (event.fail_count, event.advisory_count) == expected
        assert _counts(_reviewer_rows(events, "review")) == expected
        # The "Review ..." log line prints these two properties.
        assert (review.fail_count, review.advisory_count) == expected

    def test_security_counts_partition_the_security_rows(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        security = SecurityResult(
            passed=True,
            mode="advisory",
            findings=[
                SecurityFinding("injection", "high", "a.py:3", "unescaped"),
                SecurityFinding("other", "low", "a.py:4", "noisy"),
            ],
        )
        _run(root, review=_passing_review(), security=security)
        events = _events(root)
        event = _review_event(events, security=True)
        rows = _reviewer_rows(events, "security")
        assert (event.fail_count, event.advisory_count) == (1, 1)
        assert event.fail_count + event.advisory_count == len(rows) == 2
        assert event.fail_count == sum(1 for r in rows if r.severity in ("critical", "high"))


class TestMergeGateEvidence:
    def test_the_item_carries_the_review_event_counts(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        # An advisory criterion on a story the engineer marked done also
        # records a claim disagreement under phase review. The old count
        # included it; the review event does not.
        review = ReviewResult(
            passed=True,
            mode="hard",
            criteria=[CriterionReview("AC1", "advisory", "thin", story_id="US-001")],
            concerns=[
                ReviewConcern("test_quality", "advisory", "a.py:1", "weak"),
                ReviewConcern("error_handling", "advisory", "a.py:2", "swallowed"),
            ],
        )
        _run(root, review=review, pause_before_pr_merge=True)
        events = _events(root)
        event = _review_event(events, security=False)
        claims = [
            e
            for e in events
            if isinstance(e, ev.FindingRecorded) and e.category == CLAIM_DISAGREEMENT_CATEGORY
        ]
        assert len(claims) == 1
        gates = [
            i for i in Inbox(root, InboxConfig()).open_items() if i.kind == ItemKind.MERGE_GATE
        ]
        assert len(gates) == 1
        assert gates[0].evidence["review_fail_count"] == event.fail_count == 0
        assert gates[0].evidence["review_advisory_count"] == event.advisory_count == 3
        assert "review_findings" not in gates[0].evidence

    def test_a_review_with_no_reading_states_no_count(self, tmp_path: Path) -> None:
        """An advisory-mode reviewer that crashed passes with
        ``infrastructure_error``. Its 0/0 is not a reading, so the item
        must not show it as one."""
        root = _project(tmp_path)
        crashed = ReviewResult(
            passed=True,
            mode="advisory",
            overall_notes="Review agent crashed: boom",
            infrastructure_error=True,
        )
        _run(root, review=crashed, pause_before_pr_merge=True)
        gates = [
            i for i in Inbox(root, InboxConfig()).open_items() if i.kind == ItemKind.MERGE_GATE
        ]
        assert len(gates) == 1
        assert "review_fail_count" not in gates[0].evidence
        assert "review_advisory_count" not in gates[0].evidence


# --- census: every place the two events are built -------------------------


def _builds(name: str) -> Sees:
    return lambda node: (
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    )


#: Every ``ComponentCompleted(...)`` in ``kstrl/``, by owning function.
EXPECTED_COMPLETED_SITES: dict[str, int] = {
    # The two factory sites. Both must read the manifest's fields; see
    # test_factory_completion_events_read_the_manifest_fields.
    "pipeline.py:complete": 1,
    "pipeline.py:repoll_merge_pending": 1,
    # Runs with no manifest Component and no journal row, so the event is
    # the only record of the duration it carries.
    "cli.py:_understand_core": 1,
    "decompose.py:_decompose_spec_impl": 1,
    "feature_cmd.py:run_feature": 2,
}

#: Every ``ReviewResultEvent(...)`` in ``kstrl/``, by owning function.
EXPECTED_REVIEW_RESULT_SITES: dict[str, int] = {
    "pipeline.py:_phase_review": 1,
    "pipeline.py:_phase_security": 1,
}

#: Every assignment to an attribute spelled ``duration_seconds`` in
#: ``kstrl/``, by owning function. ``Component.duration_seconds`` has one
#: writer, ``_end_attempt``, which stamps the whole attempt. Before #450
#: ``process_result`` also wrote the engineer loop's duration into it, so
#: a manifest saved mid-attempt carried the other value. A keyword
#: argument (``Component(duration_seconds=...)``) is not an assignment and
#: is not seen; ``Manifest.load`` builds the field that way from disk.
EXPECTED_DURATION_WRITE_SITES: dict[str, int] = {
    "pipeline.py:_end_attempt": 1,  # the fact: the attempt's wall-clock time
    "manifest.py:reset_for_retry": 1,  # the --reset path zeroes it
    "review.py:run_review": 3,  # ReviewResult's own timing
    "security.py:run_security_review": 1,  # SecurityResult's own timing
    "agents/base.py:add_record": 1,  # usage totals, not a component
    "agents/base.py:merge": 1,  # usage totals, not a component
}


def _writes_duration(node: ast.AST) -> bool:
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        targets = [node.target]
    else:
        return False
    return any(
        isinstance(t, ast.Attribute) and t.attr == "duration_seconds"
        for t in _flattened_targets(targets)
    )


def _sites(name: str) -> Iterator[tuple[Path, ast.Call]]:
    sees = _builds(name)
    for source_file in package_sources():
        for node in ast.walk(parsed(source_file)):
            if sees(node):
                assert isinstance(node, ast.Call)
                yield source_file, node


def _keywords(node: ast.Call) -> dict[str | None, str]:
    return {kw.arg: ast.unparse(kw.value) for kw in node.keywords}


class TestOneWriterPerField:
    def test_every_component_completed_site_is_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=_builds("ComponentCompleted"),
            key=owner_row,
            expected=EXPECTED_COMPLETED_SITES,
            control=(
                "ev.ComponentCompleted(component='c')\n",
                "ComponentCompleted(component='c')\n",
            ),
            message=(
                "The set of places that build ComponentCompleted changed. A factory "
                "site must pass comp.duration_seconds and comp.iteration_count, the "
                "fields the manifest and journal carry (#450); add the row with a reason."
            ),
        )

    def test_every_review_result_site_is_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=_builds("ReviewResultEvent"),
            key=owner_row,
            expected=EXPECTED_REVIEW_RESULT_SITES,
            control=(
                "ev.ReviewResultEvent(component='c')\n",
                "ReviewResultEvent(component='c')\n",
            ),
            message=(
                "The set of places that build ReviewResultEvent changed. Its counts "
                "must read the result's fail_count and advisory_count (#450); add the "
                "row with a reason."
            ),
        )

    def test_every_duration_write_is_pinned(self) -> None:
        assert_census(
            sources=package_sources(),
            sees=_writes_duration,
            key=owner_row,
            expected=EXPECTED_DURATION_WRITE_SITES,
            control=(
                "comp.duration_seconds = 1.0\n",  # Assign
                "comp.duration_seconds += 1.0\n",  # AugAssign
                "comp.duration_seconds: float = 1.0\n",  # AnnAssign
                "a, comp.duration_seconds = f()\n",  # Assign, tuple-unpacked
            ),
            message=(
                "The set of places that assign a duration_seconds attribute changed. "
                "Component.duration_seconds has one writer, _end_attempt (#450); a "
                "second writer puts a second value on the manifest. Add the row with "
                "a reason only if it writes some other object's field."
            ),
        )

    def test_factory_completion_events_read_the_manifest_fields(self) -> None:
        checked = 0
        for source_file, node in _sites("ComponentCompleted"):
            if label(source_file) != "pipeline.py":
                continue
            kwargs = _keywords(node)
            where = f"{label(source_file)}:{node.lineno}"
            assert kwargs.get("duration_seconds") == "comp.duration_seconds", where
            assert kwargs.get("iterations") == "comp.iteration_count", where
            checked += 1
        assert checked == 2, f"checked {checked} pipeline site(s); the census pins 2"

    def test_review_result_counts_read_the_result_properties(self) -> None:
        checked = 0
        for source_file, node in _sites("ReviewResultEvent"):
            kwargs = _keywords(node)
            where = f"{label(source_file)}:{node.lineno}"
            fail = kwargs.get("fail_count", "")
            base = fail.removesuffix(".fail_count")
            assert base in ("review_result", "sec_result"), (where, fail)
            assert kwargs.get("advisory_count") == f"{base}.advisory_count", where
            checked += 1
        assert checked == 2, f"checked {checked} site(s); the census pins 2"
