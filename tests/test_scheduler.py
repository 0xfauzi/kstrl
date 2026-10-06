"""R7.3 PR 2: unified scheduling loop regression test.

The sequential and parallel scheduler paths are ONE loop (an inline
executor stands in for the process pool when max_parallel <= 1). This
test pins the loop-shape behaviour that a naive unification would
lose: a pass that transitions components without launching anything
(provisioning failure) must re-derive the ready set instead of
stopping while schedulable components remain, driven through
``run_factory`` on a real repo.

The budget-gate fail-all behavior is already pinned by
tests/test_usage_meter.py (comp-b never launches: the scheduling gate
fails it loudly too); the in-process execution of the sequential mode
is pinned by every test that patches kstrl.factory._run_component
with an unpicklable MagicMock.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl import events as ev
from kstrl.config import KstrlConfig
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    run_factory,
)
from kstrl.fixtures import FixturesConfig
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.helpers import gitrepo
from tests.helpers.component_prd import PASSING_STORY, write_component_prd
from tests.helpers.stack_confirmation import in_process_stack


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-b", "main")
    gitrepo.set_identity(root)
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "seed")
    # The pre-run copies the plan-time scope snapshot reads (#269).
    # Without them the run is refused before scheduling (#293 review),
    # which is exactly what this test needs NOT to happen. Same stories
    # as the worktree copy the worktree stub writes, or Phase 1 reads
    # the difference as the engineer rewriting its own story set.
    for comp_id in ("comp-a", "comp-b"):
        write_component_prd(
            root,
            f"scripts/kstrl/feature/{comp_id}/prd.json",
            stories=[PASSING_STORY],
        )


def _component(comp_id: str, deps: list[str] | None = None) -> Component:
    return Component(
        comp_id,
        comp_id.title(),
        "Desc",
        deps or [],
        f"scripts/kstrl/feature/{comp_id}/prd.json",
        f"kstrl/factory/{comp_id}",
    )


def _manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1",
        spec_file="",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=components,
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
        project_stack=in_process_stack({"tests": "true", "typecheck": "true", "lint": "true"}),
        verify_config=VerifyConfig(
            project_stack=in_process_stack({"tests": "true", "typecheck": "true", "lint": "true"}),
            check_diff_scope=False,
            check_bad_patterns=False,
            subprocess_timeout=5.0,
        ),
        fixtures_config=FixturesConfig(),
        progress_log_path=tmp_path / "progress.jsonl",
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


class TestUnifiedSchedulingLoop:
    def test_provisioning_failure_does_not_strand_siblings(
        self,
        tmp_path: Path,
    ) -> None:
        """comp-a's worktree setup fails; comp-b is INDEPENDENT and must
        still be scheduled. A pass that only transitioned components
        without launching (provisioning failure) has to re-derive the
        ready set - the old sequential loop did this via its per-
        component while-pass, and the unified loop must not lose it."""
        _init_repo(tmp_path)
        manifest = _manifest([_component("comp-a"), _component("comp-b")])
        config = _factory_config(tmp_path)
        success_b = ComponentResult("comp-b", success=True, iterations=1)

        real_setup_calls: list[str] = []

        def _setup(comp_id: str, *args: Any, **kwargs: Any) -> Path:
            real_setup_calls.append(comp_id)
            if comp_id == "comp-a":
                raise RuntimeError("worktree add failed (simulated)")
            wt = tmp_path / ".kstrl" / "worktrees" / "run" / comp_id
            write_component_prd(
                wt,
                f"scripts/kstrl/feature/{comp_id}/prd.json",
                stories=[PASSING_STORY],
            )
            return wt

        with (
            patch(
                "kstrl.factory._setup_worktree",
                side_effect=_setup,
            ),
            patch(
                "kstrl.factory._run_component",
                return_value=success_b,
            ),
            patch(
                "kstrl.git.get_diff_content",
                return_value="",
            ),
        ):
            result = run_factory(
                manifest,
                config,
                _base_config(tmp_path),
                PlainUI(no_color=True, file=io.StringIO()),
                tmp_path,
                manifest_path=tmp_path / "manifest.json",
            )

        comp_a = manifest.get_component("comp-a")
        comp_b = manifest.get_component("comp-b")
        assert comp_a is not None and comp_b is not None
        assert comp_a.status == ComponentStatus.FAILED.value
        assert comp_a.failed_phase == "provisioning"
        assert comp_b.status == ComponentStatus.COMPLETED.value
        assert result.failed == ["comp-a"]
        assert result.completed == ["comp-b"]
        assert real_setup_calls == ["comp-a", "comp-b"]
        run_dir = sorted((tmp_path / ".kstrl" / "runs").iterdir())[-1]
        events = ev.read_events(run_dir / "events.jsonl")
        engineer_starts = [
            event
            for event in events
            if isinstance(event, ev.PhaseStarted) and event.phase == "engineer"
        ]
        assert [event.component for event in engineer_starts] == ["comp-b"]
