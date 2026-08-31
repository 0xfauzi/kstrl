"""An untrustworthy scope is refused BEFORE the engineer runs (#294).

``check_scope_unreadable`` is Phase 1's backstop, and Phase 1 runs after
the engineer loop. By the time it refuses, up to ``max_iterations`` LLM
calls have already been spent on a verdict nothing in the worktree could
have changed. #293's ``_preflight_component_scope`` covers the common
case by refusing the whole run up front, but it only inspects components
that are PENDING when it runs, and the contract breaker resets a
COMPLETED component to PENDING inside the scheduling loop, long after
the preflight returned.

This file pins the gate that closes that window: the scheduler re-checks
the component's plan-time scope immediately before ``begin_attempt``, so
the reset component never launches. Two consequences it also pins,
because neither is obvious from the gate itself:

- Turning verification off no longer turns the refusal off.
  ``--no-verify`` returns from ``_phase_verify`` before the ungated check
  can run, so before this gate a component with no trustworthy scope
  could merge with Phase 1's guard inert. The gate is outside Phase 1.
- The failure is the COMPONENT's, not the run's. The preflight halts the
  whole run because nothing has merged yet; here siblings already have,
  and discarding them to punish one component would be the worse trade.

The route into the gate is reproduced honestly rather than asserted: the
component is COMPLETED while the preflight runs and PENDING when the
scheduler reaches it, which is the contract breaker's exact shape.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl import events as ev
from kstrl import factory
from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, FactoryResult, run_factory
from kstrl.fixtures import FixturesConfig
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import SCOPE_UNREADABLE_CHECK, VerifyConfig
from tests.helpers.component_prd import PASSING_STORY, write_component_prd


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    """A repo where comp-b has a pre-run PRD and comp-a does NOT.

    The missing file is the whole fixture: the plan-time snapshot (#269)
    resolves comp-a to ``unresolved`` and comp-b to a real scope, so one
    component is refused and its sibling is not.
    """
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "seed")
    write_component_prd(
        root,
        "scripts/kstrl/feature/comp-b/prd.json",
        stories=[PASSING_STORY],
    )


def _component(comp_id: str) -> Component:
    return Component(
        comp_id,
        comp_id.title(),
        "Desc",
        [],
        f"scripts/kstrl/feature/{comp_id}/prd.json",
        f"kstrl/factory/{comp_id}",
    )


def _manifest() -> Manifest:
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=[_component("comp-a"), _component("comp-b")],
    )


def _base_config(root: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts" / "kstrl" / "prompt.md",
        prd_file=root / "scripts" / "kstrl" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _factory_config(tmp_path: Path, **overrides: Any) -> FactoryConfig:
    defaults: dict[str, Any] = dict(
        max_parallel=1,
        max_retries=0,
        retry_delay=0,
        create_prs=False,
        use_worktrees=True,
        review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true",
            typecheck_command="true",
            lint_command="true",
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
        fixtures_config=FixturesConfig(),
        progress_log_path=tmp_path / "progress.jsonl",
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


def _run_with_comp_a_reset_after_preflight(
    tmp_path: Path,
    manifest: Manifest,
    config: FactoryConfig,
    launched: list[str],
) -> FactoryResult:
    """Drive a real run where comp-a reaches the scheduler PENDING but
    was not PENDING when the preflight looked.

    That is the contract breaker's shape, reproduced at the one point
    that matters here rather than standing up contract testing to
    provoke it. ``_preflight_component_scope`` skips a non-PENDING
    component by design; flipping the status back afterwards puts comp-a
    in front of the scheduling gate exactly as a reset does.
    """
    comp_a = manifest.get_component("comp-a")
    assert comp_a is not None
    real_preflights = factory._run_preflights

    def _preflights(*args: Any, **kwargs: Any) -> Any:
        comp_a.status = ComponentStatus.COMPLETED.value
        try:
            return real_preflights(*args, **kwargs)
        finally:
            comp_a.status = ComponentStatus.PENDING.value

    def _setup(comp_id: str, *args: Any, **kwargs: Any) -> Path:
        wt = tmp_path / ".kstrl" / "worktrees" / "run" / comp_id
        write_component_prd(
            wt,
            f"scripts/kstrl/feature/{comp_id}/prd.json",
            stories=[PASSING_STORY],
        )
        return wt

    def _worker(component_id: str, *args: Any, **kwargs: Any) -> ComponentResult:
        launched.append(component_id)
        return ComponentResult(component_id, success=True, iterations=1)

    with (
        patch("kstrl.factory._run_preflights", side_effect=_preflights),
        patch("kstrl.factory._setup_worktree", side_effect=_setup),
        patch("kstrl.factory._run_component", side_effect=_worker),
        patch("kstrl.git.get_diff_content", return_value=""),
    ):
        return run_factory(
            manifest,
            config,
            _base_config(tmp_path),
            PlainUI(no_color=True, file=io.StringIO()),
            tmp_path,
            manifest_path=tmp_path / "manifest.json",
        )


class TestTheGateRefusesBeforeSpending:
    def test_no_engineer_runs_for_the_component_with_no_scope(
        self,
        tmp_path: Path,
    ) -> None:
        """The measurement the gate exists for. Without it comp-a runs a
        full engineer loop and Phase 1 refuses afterwards, so ``launched``
        reads ``["comp-a", "comp-b"]`` and the whole cost of comp-a's
        attempt is spent on a verdict fixed before the run started."""
        _init_repo(tmp_path)
        manifest = _manifest()
        launched: list[str] = []
        result = _run_with_comp_a_reset_after_preflight(
            tmp_path,
            manifest,
            _factory_config(tmp_path),
            launched,
        )

        assert launched == ["comp-b"]
        assert result.failed == ["comp-a"]
        assert result.completed == ["comp-b"]

    def test_the_refusal_is_recorded_under_its_own_name(self, tmp_path: Path) -> None:
        """``failed_phase`` is ``scope``, not ``verify``: nothing in
        Phase 1 ran. ``failed_check`` still carries the check name, so
        the journal, ``ks evolve`` and ``ks autonomy`` classify a gate
        refusal and a Phase 1 refusal identically."""
        _init_repo(tmp_path)
        manifest = _manifest()
        _run_with_comp_a_reset_after_preflight(
            tmp_path,
            manifest,
            _factory_config(tmp_path),
            [],
        )

        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.status == ComponentStatus.FAILED.value
        assert comp_a.failed_phase == "scope"
        assert comp_a.failed_check == SCOPE_UNREADABLE_CHECK

    def test_the_recorded_error_names_the_cause_and_a_remedy_that_works(
        self,
        tmp_path: Path,
    ) -> None:
        """``comp.error`` is what the ComponentFailed event, the
        notification hook and the HALTED_RUN inbox item all carry, so it
        has to stand alone: which of the two faults this is, and that the
        flag an operator would reach for first is not the fix."""
        _init_repo(tmp_path)
        manifest = _manifest()
        _run_with_comp_a_reset_after_preflight(
            tmp_path,
            manifest,
            _factory_config(tmp_path),
            [],
        )

        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert "Component scope could not be read; retrying cannot change it" in comp_a.error
        assert "prd.json" in comp_a.error
        assert "diff_scope" not in comp_a.error

    def test_verification_off_does_not_switch_the_refusal_off(
        self,
        tmp_path: Path,
    ) -> None:
        """The gate is outside Phase 1, which is the point of putting it
        in the scheduler. ``--no-verify`` is both halves of what the CLI
        sets, and it makes ``_phase_verify`` return before any check
        runs, so before this gate comp-a would have gone on to succeed
        with the ungated guard inert."""
        _init_repo(tmp_path)
        manifest = _manifest()
        launched: list[str] = []
        result = _run_with_comp_a_reset_after_preflight(
            tmp_path,
            manifest,
            _factory_config(tmp_path, verify_config=None, skip_verification=True),
            launched,
        )

        assert launched == ["comp-b"]
        assert result.failed == ["comp-a"]
        comp_a = manifest.get_component("comp-a")
        assert comp_a is not None
        assert comp_a.failed_check == SCOPE_UNREADABLE_CHECK

    def test_the_sibling_is_not_stranded(self, tmp_path: Path) -> None:
        """A pass that transitions a component without launching has to
        re-derive the ready set. The preflight's answer to an
        untrustworthy scope is to refuse the whole run, which is right
        before anything merges and wrong here."""
        _init_repo(tmp_path)
        manifest = _manifest()
        _run_with_comp_a_reset_after_preflight(
            tmp_path,
            manifest,
            _factory_config(tmp_path),
            [],
        )

        comp_b = manifest.get_component("comp-b")
        assert comp_b is not None
        assert comp_b.status == ComponentStatus.COMPLETED.value
        run_dir = sorted((tmp_path / ".kstrl" / "runs").iterdir())[-1]
        events = ev.read_events(run_dir / "events.jsonl")
        starts = [
            event
            for event in events
            if isinstance(event, ev.PhaseStarted) and event.phase == "engineer"
        ]
        assert [event.component for event in starts] == ["comp-b"]
