"""R7.3 PR 2: unified scheduling loop regression tests.

The sequential and parallel scheduler paths are now ONE loop (an
_InlineExecutor stands in for the process pool when max_parallel <= 1).
These tests pin the loop-shape behaviors that a naive unification would
lose: a pass that transitions components without launching anything
(provisioning failure, budget gate) must re-derive the ready set
instead of stopping while schedulable components remain.

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
    _InlineExecutor,
    run_factory,
)
from kstrl.fixtures import FixturesConfig
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "seed")


def _component(comp_id: str, deps: list[str] | None = None) -> Component:
    return Component(
        comp_id, comp_id.title(), "Desc", deps or [],
        f"scripts/ralph/feature/{comp_id}/prd.json",
        f"ralph/factory/{comp_id}",
    )


def _manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1", spec_file="spec.md", project_name="test",
        base_branch="main", single_pr=False, components=components,
    )


def _base_config(root: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root / "scripts" / "ralph" / "prompt.md",
        prd_file=root / "scripts" / "ralph" / "prd.json",
        sleep_seconds=0,
        agent_cmd="echo test",
        ralph_branch="",
        ralph_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _factory_config(tmp_path: Path, **overrides: Any) -> FactoryConfig:
    defaults: dict[str, Any] = dict(
        max_parallel=1, max_retries=0, retry_delay=0,
        create_prs=False, use_worktrees=True, review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=5.0,
        ),
        fixtures_config=FixturesConfig(),
        progress_log_path=tmp_path / "progress.jsonl",
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


class TestInlineExecutor:
    def test_submit_resolves_result(self) -> None:
        future = _InlineExecutor().submit(lambda: ComponentResult("a", True))
        assert future.done()
        assert future.result().component_id == "a"

    def test_submit_captures_exception_for_result_time(self) -> None:
        def _boom() -> ComponentResult:
            raise RuntimeError("worker exploded")

        future = _InlineExecutor().submit(_boom)
        assert future.done()
        # The exception surfaces at result(), where a pool worker's
        # would - the shared loop's except Exception handles both.
        try:
            future.result()
        except RuntimeError as exc:
            assert "worker exploded" in str(exc)
        else:  # pragma: no cover - the assert above must fire
            raise AssertionError("expected RuntimeError")


class TestUnifiedSchedulingLoop:
    def test_provisioning_failure_does_not_strand_siblings(
        self, tmp_path: Path,
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
            wt = tmp_path / ".ralph" / "worktrees" / "run" / comp_id
            prd = wt / "scripts" / "ralph" / "feature" / comp_id / "prd.json"
            prd.parent.mkdir(parents=True, exist_ok=True)
            prd.write_text(
                '{"branchName": "test", "userStories": [{"id": "US-001", '
                '"title": "T", "acceptanceCriteria": ["AC1"], "priority": 1, '
                '"passes": true, "notes": ""}]}'
            )
            return wt

        with patch(
            "kstrl.factory._setup_worktree", side_effect=_setup,
        ), patch(
            "kstrl.factory._run_component", return_value=success_b,
        ), patch(
            "kstrl.git.get_diff_content", return_value="",
        ):
            result = run_factory(
                manifest, config, _base_config(tmp_path),
                PlainUI(no_color=True, file=io.StringIO()), tmp_path,
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
        run_dir = sorted((tmp_path / ".ralph" / "runs").iterdir())[-1]
        events = ev.read_events(run_dir / "events.jsonl")
        engineer_starts = [
            event for event in events
            if isinstance(event, ev.PhaseStarted)
            and event.phase == "engineer"
        ]
        assert [event.component for event in engineer_starts] == ["comp-b"]
