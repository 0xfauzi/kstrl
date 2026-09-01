"""Driving Phase 1 and its failure routing from a test.

Three test modules were each building the same two things by hand: a
``VerifyConfig`` with only the cheap gates on, and a ``ComponentPipeline``
wired to stub hooks so one phase can be driven directly. The #294 tests
originally reached into ``tests.test_progress_scope`` for the private
``_pipeline`` and ``_component``, which imports and executes a
1600-line test module to obtain two factories and makes renaming a
private helper there break an unrelated file. ``tests/helpers/`` is
where the repo puts this.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.config import KstrlConfig
from kstrl.events import CallbackSink, Event, EventBus, V1CompatSink
from kstrl.factory import (
    AdversarialAgentSelection,
    ComponentResult,
    FactoryConfig,
    FactoryResult,
)
from kstrl.knowledge import KnowledgeConfig
from kstrl.manifest import Component, Manifest
from kstrl.observability import NotifyConfig, NotifyHooks, ProgressLog
from kstrl.pipeline import ComponentPipeline, FailureAction, PipelineHooks, VerifyPhaseResult
from kstrl.review import ReviewResult
from kstrl.scope import RunScope
from kstrl.security import SecurityResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerificationResult, VerifyConfig, run_mechanical_verification

if TYPE_CHECKING:
    from kstrl.verify import CheckResult

#: Only the gates that cost nothing: three ``true`` commands, no PRD, no
#: pattern scan. Everything else in Phase 1 defaults off.
CHEAP_GATES = VerifyConfig(
    test_command="true",
    typecheck_command="true",
    lint_command="true",
    check_bad_patterns=False,
    subprocess_timeout=30.0,
)


def component(comp_id: str = "comp-a") -> Component:
    return Component(
        comp_id,
        comp_id.title(),
        "Desc",
        [],
        f"scripts/kstrl/{comp_id}/prd.json",
        f"kstrl/factory/{comp_id}",
    )


def verify_with_cheap_gates(
    root: Path,
    *,
    allowed_paths_error: str | None,
    allowed_paths: list[str] | None = None,
) -> VerificationResult:
    """Phase 1 over ``root`` with only the cheap gates on.

    No git repository is set up, and none is needed: with an
    ``allowed_paths_error`` ``_scope_checks`` returns before any git
    call, ``prd_path`` is None, and every other diff-reading check is
    off. Measured: building a repo with a real diff first changes no
    assertion and roughly doubles the runtime.
    """
    return run_mechanical_verification(
        root,
        None,
        "main",
        allowed_paths,
        CHEAP_GATES,
        allowed_paths_error=allowed_paths_error,
    )


def _pipeline(
    root: Path,
    comp: Component,
    verification: VerificationResult,
    *,
    ui: PlainUI | None = None,
) -> ComponentPipeline:
    """``ui`` is for a caller that needs to READ the narration: the
    default throws it away. Events are captured the way the rest of the
    suite does it, with ``pipeline.bus.add_sink`` after construction -
    ``ComponentPipeline.__init__`` emits nothing, so a sink attached
    then sees every event a sink passed here would."""
    manifest = Manifest(
        version="1",
        spec_file="spec.md",
        project_name="t",
        base_branch="main",
        single_pr=False,
        components=[comp],
    )
    hooks = PipelineHooks(
        run_mechanical_verification=lambda *a, **k: verification,
        run_review=lambda *a, **k: ReviewResult(passed=True, mode="advisory"),
        run_security_review=lambda *a, **k: SecurityResult(passed=True, mode="advisory"),
        distill_facts=lambda *a, **k: (1, "1 fact written"),
        measure_fact_utilization=lambda *a, **k: {"injected": 0, "referenced": 0},
        cleanup_worktree=lambda *a, **k: None,
    )
    return ComponentPipeline(
        manifest=manifest,
        manifest_path=root / "manifest.json",
        factory_config=FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            max_retries=3,
            retry_delay=0,
            review_mode="skip",
        ),
        base_config=KstrlConfig(),
        ui=ui or PlainUI(no_color=True, file=io.StringIO()),
        root_dir=root,
        run_id="run-test",
        bus=EventBus(
            V1CompatSink(ProgressLog(root / "progress.jsonl", run_id="run-test")),
            run_id="run-test",
        ),
        journal_path=None,
        review_selection=AdversarialAgentSelection(
            phase="review",
            agent_cmd=None,
            agent_type=None,
            model=None,
            reasoning=None,
            source="explicit",
            identity="test-review",
        ),
        security_selection=None,
        knowledge_config=KnowledgeConfig(enabled=False),
        factory_result=FactoryResult(),
        notify=NotifyHooks(NotifyConfig(), run_id="run-test", project="t"),
        hooks=hooks,
        run_scope=RunScope({}),
        worktree_paths={comp.id: root},
        component_contexts={},
        fresh_base_retry_ids=set(),
        component_failure_signatures={},
    )


def phase_verify_action(root: Path, failing: list[CheckResult]) -> tuple[FailureAction, str]:
    """``(action, error)`` the REAL ``_phase_verify`` routes to.

    Drives ``ComponentPipeline._phase_verify`` rather than re-deriving
    the condition in the test: a test that reimplements the branch it
    checks passes when the branch is deleted. Through
    :func:`phase_verify_surfaces`, so the shape of that call - which
    ``ComponentResult`` fields Phase 1 needs - is written once in this
    file and not twice.
    """
    surfaces = phase_verify_surfaces(root, VerificationResult(passed=False, checks=failing))
    assert surfaces.result.failure is not None
    return surfaces.result.failure.action, surfaces.result.failure.error


@dataclass(frozen=True)
class PhaseVerifySurfaces:
    """Everything one ``_phase_verify`` call left behind.

    ``phase_verify_action`` answers "what did Phase 1 route to".
    This answers "what did an operator and an event consumer see",
    which is the question #306's not-measured sidecar exists for.
    """

    result: VerifyPhaseResult
    narration: str
    events: list[Event]


def phase_verify_surfaces(
    root: Path,
    verification: VerificationResult,
) -> PhaseVerifySurfaces:
    """Drive the REAL ``_phase_verify`` over ``verification``.

    The hook is stubbed to return ``verification`` unchanged, so the
    aggregation is not re-run here; what is exercised is everything
    Phase 1 does with the object afterwards.
    """
    comp = component()
    narration = io.StringIO()
    events: list[Event] = []
    pipeline = _pipeline(root, comp, verification, ui=PlainUI(no_color=True, file=narration))
    pipeline.bus.add_sink(CallbackSink(events.append))
    result = pipeline._phase_verify(
        comp,
        ComponentResult(comp.id, success=True, iterations=1, duration_seconds=1.0),
        root,
    )
    return PhaseVerifySurfaces(result, narration.getvalue(), events)
