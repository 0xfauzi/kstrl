"""R7.3: ComponentPipeline state-machine tests, in isolation.

Drives the pipeline directly - stub hooks, no subprocesses, no LLM
calls, no scheduler - and asserts every transition the roadmap names:
retry, retry exhaustion, cascade-skip, MERGE_PENDING (park and re-poll),
HITL checkpoint reject/retry, and both budget-exhaustion walls
(adversarial call cap and token cap). The review's critical bugs lived
exactly in these transitions while they were closures inside
run_factory; these tests are the regression net that extraction bought.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from ralph_py.agents.base import UsageRecord, UsageTotals
from ralph_py.config import RalphConfig
from ralph_py.events import CallbackSink, Event, EventBus, PhaseCompleted, V1CompatSink
from ralph_py.factory import (
    AdversarialAgentSelection,
    ComponentResult,
    FactoryConfig,
    FactoryResult,
)
from ralph_py.fixtures import FixturesConfig
from ralph_py.knowledge import KnowledgeConfig
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.observability import NotifyConfig, NotifyHooks, ProgressLog
from ralph_py.pipeline import (
    CheckpointDecision,
    ComponentPipeline,
    PipelineHooks,
    PrDisposition,
    Transition,
)
from ralph_py.pr import PrOutcome
from ralph_py.review import ReviewResult
from ralph_py.security import SecurityConfig, SecurityResult
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import CheckResult, VerificationResult, VerifyConfig


class _ChoiceUI(PlainUI):
    """Interactive-capable UI with a scripted HITL checkpoint answer."""

    def __init__(self, choice: int) -> None:
        super().__init__(no_color=True, file=io.StringIO())
        self._choice = choice

    def can_prompt(self) -> bool:
        return True

    def choose(
        self, header: str, options: list[str], default: int = 0,
    ) -> int:
        return self._choice


def _component(comp_id: str, deps: list[str] | None = None) -> Component:
    return Component(
        comp_id, comp_id.title(), "Desc", deps or [],
        f"scripts/ralph/feature/{comp_id}/prd.json",
        f"ralph/factory/{comp_id}",
    )


def _make_manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=components,
    )


def _base_config(root: Path) -> RalphConfig:
    return RalphConfig(
        prompt_file=root / "scripts" / "ralph" / "prompt.md",
        prd_file=root / "scripts" / "ralph" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        ralph_branch="",
        ralph_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _factory_config(**overrides: Any) -> FactoryConfig:
    defaults: dict[str, Any] = dict(
        max_parallel=1, max_retries=1, retry_delay=0,
        create_prs=False, use_worktrees=False, review_mode="skip",
        verify_config=VerifyConfig(), fixtures_config=FixturesConfig(),
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


def _selection(phase: str) -> AdversarialAgentSelection:
    return AdversarialAgentSelection(
        phase=phase, agent_cmd=None, agent_type=None, model=None,
        reasoning=None, source="explicit", identity=f"test-{phase}",
    )


def _recording_hooks(
    calls: list[str], **overrides: Any,
) -> PipelineHooks:
    """Hooks whose stubs append their name to ``calls`` when invoked."""

    def _rec(name: str, ret: Any) -> Any:
        def _f(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return ret
        return _f

    defaults: dict[str, Any] = dict(
        run_mechanical_verification=_rec(
            "verify", VerificationResult(passed=True, checks=[]),
        ),
        run_review=_rec("review", ReviewResult(passed=True, mode="advisory")),
        run_chunked_review=_rec(
            "chunked_review", ReviewResult(passed=True, mode="hard"),
        ),
        run_security_review=_rec(
            "security", SecurityResult(passed=True, mode="advisory"),
        ),
        run_chunked_security_review=_rec(
            "chunked_security", SecurityResult(passed=True, mode="hard"),
        ),
        distill_facts=_rec("distill", (1, "1 fact written")),
        build_knowledge_context=_rec("knowledge_ctx", ""),
        measure_fact_utilization=_rec(
            "utilization", {"injected": 0, "referenced": 0},
        ),
        cleanup_worktree=_rec("cleanup_worktree", None),
    )
    defaults.update(overrides)
    return PipelineHooks(**defaults)


def _make_pipeline(
    tmp_path: Path,
    *,
    components: list[Component] | None = None,
    config: FactoryConfig | None = None,
    ui: PlainUI | None = None,
    knowledge: KnowledgeConfig | None = None,
    security_selection: AdversarialAgentSelection | None = None,
    calls: list[str] | None = None,
    hooks_overrides: dict[str, Any] | None = None,
) -> tuple[ComponentPipeline, Manifest, FactoryResult, list[str]]:
    comps = components if components is not None else [
        _component("comp-a"), _component("comp-b", deps=["comp-a"]),
    ]
    manifest = _make_manifest(comps)
    factory_config = config or _factory_config()
    factory_result = FactoryResult()
    call_log = calls if calls is not None else []
    ui = ui or PlainUI(no_color=True, file=io.StringIO())
    pipeline = ComponentPipeline(
        manifest=manifest,
        manifest_path=tmp_path / "manifest.json",
        factory_config=factory_config,
        base_config=_base_config(tmp_path),
        ui=ui,
        root_dir=tmp_path,
        run_id="run-test",
        bus=EventBus(
            V1CompatSink(ProgressLog(tmp_path / "progress.jsonl", run_id="run-test")),
            run_id="run-test",
        ),
        journal_path=tmp_path / "progress.jsonl",
        notify=NotifyHooks(
            NotifyConfig(), run_id="run-test", project="test", warn=ui.warn,
        ),
        review_selection=_selection("review"),
        security_selection=security_selection,
        knowledge_config=knowledge or KnowledgeConfig(enabled=False),
        factory_result=factory_result,
        hooks=_recording_hooks(call_log, **(hooks_overrides or {})),
        worktree_paths={},
        component_contexts={},
        fresh_base_retry_ids=set(),
        component_failure_signatures={},
    )
    return pipeline, manifest, factory_result, call_log


def _success(comp_id: str, usage: UsageTotals | None = None) -> ComponentResult:
    return ComponentResult(
        comp_id, success=True, iterations=2, duration_seconds=1.0,
        usage=usage,
    )


def _usage(total: int) -> UsageTotals:
    totals = UsageTotals()
    totals.add_record(UsageRecord(
        input_tokens=total // 2, output_tokens=total - total // 2,
        total_tokens=total, duration_seconds=1.0,
        source="claude-stream-json",
    ))
    return totals


def _events(tmp_path: Path) -> list[dict[str, Any]]:
    return ProgressLog(tmp_path / "progress.jsonl").read_events()


@pytest.fixture(autouse=True)
def _no_real_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline is exercised without git: the shared-diff fetch and
    the agent factory are stubbed at their source modules (the same
    seams the factory-level tests use)."""
    monkeypatch.setattr(
        "ralph_py.git.get_diff_content", lambda *a, **k: "diff --git a b\n",
    )
    monkeypatch.setattr(
        "ralph_py.agents.get_agent", lambda *a, **k: object(),
    )


class TestEngineerTransitions:
    def test_unknown_component_returns_none(self, tmp_path: Path) -> None:
        pipeline, _, _, _ = _make_pipeline(tmp_path)
        assert pipeline.process_result("ghost", _success("ghost")) is None

    def test_engineer_failure_retries_with_context(
        self, tmp_path: Path,
    ) -> None:
        pipeline, manifest, result, _ = _make_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", ComponentResult(
            "comp-a", success=False, iterations=3, error="Did not complete",
        ))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.status == ComponentStatus.PENDING.value
        assert comp.retries == 1
        assert comp.failed_phase == "engineer"
        assert comp.failed_check == "loop"
        assert "Did not complete" in pipeline.component_contexts["comp-a"]
        assert result.failed == []

    def test_retry_exhaustion_fails_and_cascade_skips(
        self, tmp_path: Path,
    ) -> None:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=_factory_config(max_retries=1),
        )
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        pipeline.begin_attempt(comp)
        first = pipeline.process_result("comp-a", ComponentResult(
            "comp-a", success=False, error="boom",
        ))
        assert first is not None and first.transition == Transition.RETRYING
        pipeline.begin_attempt(comp)
        second = pipeline.process_result("comp-a", ComponentResult(
            "comp-a", success=False, error="boom again",
        ))
        assert second is not None and second.transition == Transition.FAILED
        assert comp.status == ComponentStatus.FAILED.value
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]
        assert result.skipped == ["comp-b"]

    def test_timeout_failure_marks_worktree_hygiene(
        self, tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path, config=_factory_config(use_worktrees=True),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", ComponentResult(
            "comp-a", success=False,
            error="component timeout: exceeded 5.0s wall clock",
        ))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert "comp-a" in pipeline.fresh_base_retry_ids
        assert "worktree recreated from base" in comp.error

    def test_token_budget_at_engineer_checkpoint_fails(
        self, tmp_path: Path,
    ) -> None:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=_factory_config(max_total_tokens=100),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result(
            "comp-a", _success("comp-a", usage=_usage(150)),
        )
        assert outcome is not None
        assert outcome.transition == Transition.FAILED
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "token_budget"
        # Loud, typed, journaled: the synthetic finding and the event.
        assert any(
            f.is_infrastructure_error for f in comp.findings
        )
        assert any(
            e["event"] == "budget_exceeded" for e in _events(tmp_path)
        )
        assert result.failed == ["comp-a"]

    def test_scheduling_gate_budget_failure(self, tmp_path: Path) -> None:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=_factory_config(max_total_tokens=10),
        )
        pipeline.run_usage.merge(_usage(50))
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert pipeline.token_budget_exceeded()
        assert pipeline.fail_for_budget(comp, "scheduling") == Transition.FAILED
        assert comp.failed_phase == "scheduling"
        assert result.failed == ["comp-a"]


class TestVerifyAndDiffTransitions:
    def test_success_path_completes(self, tmp_path: Path) -> None:
        pipeline, manifest, result, calls = _make_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.verify is not None and outcome.verify.ran
        assert outcome.review is not None and not outcome.review.ran
        assert outcome.checkpoint == CheckpointDecision.NOT_PROMPTED
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.SKIPPED
        assert comp.status == ComponentStatus.COMPLETED.value
        assert result.completed == ["comp-a"]
        assert calls == ["verify"]

    def test_verify_failure_retries(self, tmp_path: Path) -> None:
        failing = VerificationResult(passed=False, checks=[
            CheckResult(name="tests", passed=False, message="1 failed"),
        ])
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            hooks_overrides={
                "run_mechanical_verification": lambda *a, **k: failing,
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_phase == "verify"
        assert comp.failed_check == "tests"
        assert comp.verification_passed is False
        assert "tests" in pipeline.component_contexts["comp-a"]

    def test_skip_verification_records_skip_and_completes(
        self, tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path, config=_factory_config(skip_verification=True),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.verify is not None and not outcome.verify.ran
        assert comp.verification_passed is None
        assert "verify" not in calls
        assert any(f.is_phase_skip for f in comp.findings)

    def test_diff_fetch_failure_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ralph_py.git import GitDiffError

        def _boom(*args: Any, **kwargs: Any) -> str:
            raise GitDiffError("git diff exploded")

        monkeypatch.setattr("ralph_py.git.get_diff_content", _boom)
        pipeline, manifest, _, _ = _make_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_phase == "diff"
        assert comp.failed_check == "git_diff"

    def test_unsplittable_hard_mode_diff_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ralph_py.git import DiffUnsplittableError

        monkeypatch.setattr(
            "ralph_py.git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT", 10,
        )

        def _unsplittable(*args: Any, **kwargs: Any) -> list[str]:
            raise DiffUnsplittableError("one file exceeds the cap")

        monkeypatch.setattr(
            "ralph_py.git.split_diff_for_prompt", _unsplittable,
        )
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path, config=_factory_config(review_mode="hard"),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_phase == "review"
        assert comp.failed_check == "diff_chunking"


class TestReviewAndSecurityTransitions:
    def test_review_failure_retries(self, tmp_path: Path) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="hard"),
            hooks_overrides={
                "run_review": lambda *a, **k: ReviewResult(
                    passed=False, mode="hard",
                ),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_phase == "review"
        assert comp.failed_check == "criteria"
        assert comp.review_passed is False

    def test_review_budget_exhausted_skips_but_completes(
        self, tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="advisory", max_adversarial_calls=1,
            ),
        )
        pipeline.adversarial_budget_consume()
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.review is not None and not outcome.review.ran
        assert comp.review_passed is None
        assert "review" not in calls
        assert any(f.is_phase_skip for f in comp.findings)

    def test_chunk_budget_insufficient_fails_without_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "ralph_py.git.DEFAULT_PROMPT_DIFF_CHAR_LIMIT", 10,
        )
        monkeypatch.setattr(
            "ralph_py.git.split_diff_for_prompt",
            lambda *a, **k: ["c1", "c2", "c3"],
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="hard", max_adversarial_calls=1, max_retries=3,
            ),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        # R1.4: retrying cannot recover adversarial budget - fail direct,
        # with NO retry consumed even though retries remain.
        assert outcome.transition == Transition.FAILED
        assert comp.retries == 0
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "adversarial_budget"
        assert comp.review_passed is False
        assert result.failed == ["comp-a"]

    def test_security_failure_retries(self, tmp_path: Path) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="skip",
                security_config=SecurityConfig(mode="hard"),
            ),
            security_selection=_selection("security"),
            hooks_overrides={
                "run_security_review": lambda *a, **k: SecurityResult(
                    passed=False, mode="hard",
                ),
            },
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.failed_phase == "security"
        assert comp.failed_check == "findings"

    def test_review_crash_in_advisory_mode_degrades_and_completes(
        self, tmp_path: Path,
    ) -> None:
        def _crash(*args: Any, **kwargs: Any) -> ReviewResult:
            raise RuntimeError("reviewer exploded")

        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(review_mode="advisory"),
            hooks_overrides={"run_review": _crash},
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        # Advisory mode: the crash degrades to an infra-annotated pass,
        # but the infra marker survives in the typed result.
        assert outcome.transition == Transition.COMPLETED
        assert outcome.review is not None
        assert outcome.review.result is not None
        assert outcome.review.result.infrastructure_error


class TestCheckpointAndPrTransitions:
    def _pr_config(self, **overrides: Any) -> FactoryConfig:
        return _factory_config(create_prs=True, **overrides)

    def test_checkpoint_reject_fails_component(
        self, tmp_path: Path,
    ) -> None:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(pause_before_pr_merge=True, max_retries=3),
            ui=_ChoiceUI(choice=1),
        )
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.checkpoint == CheckpointDecision.REJECTED
        assert outcome.transition == Transition.FAILED
        # R2.6: reject is terminal - no retry consumed, dependents skipped.
        assert comp.retries == 0
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "hitl_reject"
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]

    def test_checkpoint_retry_consumes_a_retry(self, tmp_path: Path) -> None:
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=self._pr_config(pause_before_pr_merge=True),
            ui=_ChoiceUI(choice=2),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.checkpoint == CheckpointDecision.RETRY
        assert outcome.transition == Transition.RETRYING
        assert comp.retries == 1
        assert comp.status == ComponentStatus.PENDING.value
        assert comp.failed_check == "hitl_retry"
        assert "Human reviewer requested changes" in (
            pipeline.component_contexts["comp-a"]
        )

    def test_merge_pending_parks_component(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "ralph_py.pr.push_create_and_merge_pr",
            lambda *a, **k: PrOutcome(
                pushed=True, pr_number=7, pr_url="https://x/pull/7",
                merged=False, merge_pending=True,
                error="merge not confirmed",
            ),
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=self._pr_config(),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.MERGE_PENDING
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.MERGE_PENDING
        assert comp.status == ComponentStatus.MERGE_PENDING.value
        # Parked, not terminal: no completion stamp, in neither bucket.
        assert comp.completed_at == ""
        assert result.completed == []
        assert result.failed == []
        assert result.pr_urls == ["https://x/pull/7"]
        assert any(
            e["event"] == "merge_pending" for e in _events(tmp_path)
        )

    def test_pr_flow_failure_fails_and_cascade_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "ralph_py.pr.push_create_and_merge_pr",
            lambda *a, **k: PrOutcome(
                pushed=False, merged=False, error="push rejected",
            ),
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=self._pr_config(),
        )
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.FAILED
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_phase == "pr"
        assert comp.failed_check == "pr_flow"
        assert comp.error == "push rejected"
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]

    def test_confirmed_merge_completes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "ralph_py.pr.push_create_and_merge_pr",
            lambda *a, **k: PrOutcome(
                pushed=True, pr_number=8, pr_url="https://x/pull/8",
                merged=True,
            ),
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=self._pr_config(),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.MERGED
        assert comp.status == ComponentStatus.COMPLETED.value
        assert result.completed == ["comp-a"]
        assert result.pr_urls == ["https://x/pull/8"]

    def test_no_gh_completes_without_pr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: False)
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=self._pr_config(),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.NO_GH
        assert result.completed == ["comp-a"]
        assert result.pr_urls == []


class TestMergeConflictDoctrine:
    """R7.5: a CONFLICTING PR re-runs the component against the freshly
    merged base (re-run, don't rebase) instead of failing terminally."""

    def _pr_config(self, **overrides: Any) -> FactoryConfig:
        return _factory_config(create_prs=True, **overrides)

    def _conflict_outcome(self) -> PrOutcome:
        return PrOutcome(
            pushed=True, pr_number=7, pr_url="https://x/pull/7",
            merged=False, merge_conflict=True,
            error="PR #7 conflicts with main",
        )

    def test_conflict_routes_to_fresh_base_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "ralph_py.pr.push_create_and_merge_pr",
            lambda *a, **k: self._conflict_outcome(),
        )
        closed: list[tuple[int, str]] = []

        def fake_close(pr_number: int, branch: str, cwd: Path) -> None:
            closed.append((pr_number, branch))
            return None

        monkeypatch.setattr("ralph_py.pr.close_pr_for_rerun", fake_close)
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=self._pr_config(max_retries=1, use_worktrees=True),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        comp.pr_number = 7
        comp.pr_url = "https://x/pull/7"
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert outcome.pr is not None
        assert outcome.pr.disposition == PrDisposition.CONFLICT
        # Scheduled for a re-run, not failed.
        assert comp.status == ComponentStatus.PENDING.value
        assert comp.retries == 1
        assert result.failed == []
        # The re-run recreates worktree AND branch from origin/<base>.
        assert "comp-a" in pipeline.fresh_base_retry_ids
        assert "[conflict retry" in comp.error
        # The old PR was closed and its pointers cleared, so the retry
        # creates a fresh PR instead of re-polling the closed one.
        assert closed == [(7, comp.branch_name)]
        assert comp.pr_number is None
        assert comp.pr_url == ""
        assert pipeline.component_failure_signatures["comp-a"] == [
            "pr:merge-conflict",
        ]
        # The next attempt's context explains the re-run.
        assert "freshly merged base" in pipeline.component_contexts["comp-a"]

    def test_conflict_with_retries_exhausted_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "ralph_py.pr.push_create_and_merge_pr",
            lambda *a, **k: self._conflict_outcome(),
        )
        monkeypatch.setattr(
            "ralph_py.pr.close_pr_for_rerun", lambda *a: None,
        )
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=self._pr_config(max_retries=0),
        )
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.FAILED
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "merge_conflict"
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]

    def test_conflict_close_failure_is_nonfatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "ralph_py.pr.push_create_and_merge_pr",
            lambda *a, **k: self._conflict_outcome(),
        )
        monkeypatch.setattr(
            "ralph_py.pr.close_pr_for_rerun",
            lambda *a: "gh pr close #7 failed",
        )
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path, config=self._pr_config(max_retries=1),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        comp.pr_number = 7
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.RETRYING
        assert comp.status == ComponentStatus.PENDING.value


class TestMergePendingRepoll:
    def _parked(self, tmp_path: Path) -> tuple[
        ComponentPipeline, Manifest, FactoryResult,
    ]:
        pipeline, manifest, result, _ = _make_pipeline(
            tmp_path, config=_factory_config(create_prs=True),
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        comp.status = ComponentStatus.MERGE_PENDING.value
        comp.pr_number = 7
        comp.pr_url = "https://x/pull/7"
        return pipeline, manifest, result

    def test_repoll_merged_completes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "ralph_py.pr.wait_for_merge", lambda *a, **k: "merged",
        )
        monkeypatch.setattr(
            "ralph_py.git.fetch_base_branch", lambda *a, **k: None,
        )
        pipeline, manifest, result = self._parked(tmp_path)
        pipeline.repoll_merge_pending()
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.status == ComponentStatus.COMPLETED.value
        assert result.completed == ["comp-a"]

    def test_repoll_closed_fails_and_cascade_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "ralph_py.pr.wait_for_merge", lambda *a, **k: "closed",
        )
        pipeline, manifest, result = self._parked(tmp_path)
        pipeline.repoll_merge_pending()
        comp = manifest.get_component("comp-a")
        dep = manifest.get_component("comp-b")
        assert comp is not None and dep is not None
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "pr_closed"
        assert dep.status == ComponentStatus.SKIPPED.value
        assert result.failed == ["comp-a"]
        assert result.skipped == ["comp-b"]

    def test_repoll_unconfirmed_stays_parked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)
        monkeypatch.setattr(
            "ralph_py.pr.wait_for_merge", lambda *a, **k: "unknown",
        )
        pipeline, manifest, result = self._parked(tmp_path)
        pipeline.repoll_merge_pending()
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.status == ComponentStatus.MERGE_PENDING.value
        assert result.completed == []
        assert result.failed == []


class TestSchedulerFacingTransitions:
    def test_provisioning_failure_via_fail(self, tmp_path: Path) -> None:
        pipeline, manifest, result, _ = _make_pipeline(tmp_path)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        transition = pipeline.fail(
            comp, "worktree add failed",
            phase="provisioning", check="worktree_setup",
        )
        assert transition == Transition.FAILED
        assert comp.failed_phase == "provisioning"
        assert result.failed == ["comp-a"]

    def test_scheduler_backstop_failure(self, tmp_path: Path) -> None:
        pipeline, manifest, result, _ = _make_pipeline(tmp_path)
        captured: list[Event] = []
        pipeline.bus.add_sink(CallbackSink(captured.append))
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        pipeline.worktree_paths["comp-a"] = tmp_path / "wt-a"
        pipeline.fail_scheduler_backstop("comp-a", 120.0)
        assert comp.status == ComponentStatus.FAILED.value
        assert comp.failed_check == "scheduler_backstop"
        assert comp.error == "component timeout"
        assert comp.evidence_worktree == str(tmp_path / "wt-a")
        assert result.failed == ["comp-a"]
        # The worktree entry survives: a leaked worker may still own it.
        assert "comp-a" in pipeline.worktree_paths
        completed = [e for e in captured if isinstance(e, PhaseCompleted)]
        assert len(completed) == 1
        assert completed[0].phase == "engineer"
        assert completed[0].passed is False
        assert completed[0].detail == "component timeout"


class TestDistillPlacement:
    def test_distiller_runs_pre_pr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The R7.3 placement decision, as a test: distillation happens
        after the review gates and BEFORE the PR step, so the distilled
        diff is the component's true delta."""
        calls: list[str] = []
        monkeypatch.setattr("ralph_py.pr.is_gh_available", lambda: True)

        def _pr(*args: Any, **kwargs: Any) -> PrOutcome:
            calls.append("pr")
            return PrOutcome(
                pushed=True, pr_number=9, pr_url="https://x/pull/9",
                merged=True,
            )

        monkeypatch.setattr("ralph_py.pr.push_create_and_merge_pr", _pr)
        pipeline, manifest, _, _ = _make_pipeline(
            tmp_path,
            config=_factory_config(create_prs=True),
            knowledge=KnowledgeConfig(
                enabled=True, knowledge_root=tmp_path / "knowledge",
            ),
            calls=calls,
        )
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.distill is not None and outcome.distill.ran
        assert "distill" in calls and "pr" in calls
        assert calls.index("distill") < calls.index("pr")

    def test_distill_skipped_on_exhausted_adversarial_budget(
        self, tmp_path: Path,
    ) -> None:
        pipeline, manifest, _, calls = _make_pipeline(
            tmp_path,
            config=_factory_config(
                review_mode="skip", max_adversarial_calls=1,
            ),
            knowledge=KnowledgeConfig(
                enabled=True, knowledge_root=tmp_path / "knowledge",
            ),
        )
        pipeline.adversarial_budget_consume()
        comp = manifest.get_component("comp-a")
        assert comp is not None
        pipeline.begin_attempt(comp)
        outcome = pipeline.process_result("comp-a", _success("comp-a"))
        assert outcome is not None
        assert outcome.transition == Transition.COMPLETED
        assert outcome.distill is not None and not outcome.distill.ran
        assert "distill" not in calls
        assert any(f.is_phase_skip for f in comp.findings)
