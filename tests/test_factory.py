"""Tests for factory module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ralph_py.config import RalphConfig
from ralph_py.factory import (
    ComponentResult,
    FactoryConfig,
    _verify_component,
    run_factory,
)
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.ui.plain import PlainUI


def _make_manifest(
    components: list[Component] | None = None,
) -> Manifest:
    """Build a test manifest."""
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test",
        base_branch="main",
        single_pr=False,
        components=components or [],
    )


def _make_base_config(root_dir: Path) -> RalphConfig:
    """Build a base config for factory tests."""
    prompt = root_dir / "scripts" / "ralph" / "prompt.md"
    prd = root_dir / "scripts" / "ralph" / "prd.json"
    return RalphConfig(
        prompt_file=prompt,
        prd_file=prd,
        sleep_seconds=0,
        agent_cmd="echo test",
        ralph_branch="",
        ralph_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _setup_project(tmp_path: Path) -> Path:
    """Create minimal project structure for factory tests."""
    ralph_dir = tmp_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    return tmp_path


class TestFactoryConfig:
    """Tests for FactoryConfig."""

    def test_defaults(self) -> None:
        config = FactoryConfig()
        assert config.max_parallel == 4
        assert config.max_retries == 3
        assert config.use_worktrees is True
        assert config.create_prs is True

    def test_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("FACTORY_MAX_PARALLEL", "8")
        monkeypatch.setenv("FACTORY_MAX_RETRIES", "5")
        config = FactoryConfig.from_env()
        assert config.max_parallel == 8
        assert config.max_retries == 5


class TestVerifyComponent:
    """Tests for _verify_component."""

    def test_no_command_returns_true(self, tmp_path: Path) -> None:
        assert _verify_component(tmp_path, None) is True

    def test_passing_command(self, tmp_path: Path) -> None:
        assert _verify_component(tmp_path, "true") is True

    def test_failing_command(self, tmp_path: Path) -> None:
        assert _verify_component(tmp_path, "false") is False


class TestRunFactoryDAGValidation:
    """Tests for DAG validation in run_factory."""

    def test_rejects_cyclic_dag(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest([
            Component("a", "A", "", ["b"], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
        ])
        config = FactoryConfig(use_worktrees=False, create_prs=False)
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        result = run_factory(manifest, config, base, ui, root)
        assert result.exit_code == 1

    def test_empty_manifest_succeeds(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest([])
        config = FactoryConfig(use_worktrees=False, create_prs=False)
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        result = run_factory(manifest, config, base, ui, root)
        assert result.exit_code == 0


class TestRunFactoryExecution:
    """Tests for factory execution with mocked _run_component."""

    def test_single_component_success(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        # Create PRD for the component
        feature_dir = root / "scripts" / "ralph" / "feature" / "comp-a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            '{"branchName": "test", "userStories": []}'
        )

        manifest = _make_manifest([
            Component(
                "comp-a", "Component A", "Desc",
                [], "scripts/ralph/feature/comp-a/prd.json",
                "ralph/factory/comp-a",
            ),
        ])
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        success_result = ComponentResult("comp-a", success=True, iterations=3)

        with patch("ralph_py.factory._run_component", return_value=success_result):
            result = run_factory(manifest, config, base, ui, root)

        assert "comp-a" in result.completed
        assert result.exit_code == 0

    def test_component_failure_cascades(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        manifest = _make_manifest([
            Component(
                "a", "A", "Desc A", [],
                "a.json", "b/a",
            ),
            Component(
                "b", "B", "Desc B", ["a"],
                "b.json", "b/b",
            ),
        ])
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            max_retries=0,
            retry_delay=0,
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        fail_result = ComponentResult("a", success=False, error="test failure")

        with patch("ralph_py.factory._run_component", return_value=fail_result):
            result = run_factory(manifest, config, base, ui, root)

        assert "a" in result.failed
        assert "b" in result.skipped
        assert result.exit_code == 1

    def test_crash_recovery_resets_running(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        manifest = _make_manifest([
            Component(
                "a", "A", "", [],
                "a.json", "b/a",
                status=ComponentStatus.RUNNING.value,
            ),
        ])
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        success_result = ComponentResult("a", success=True, iterations=1)

        with patch("ralph_py.factory._run_component", return_value=success_result):
            result = run_factory(manifest, config, base, ui, root)

        # Component should have been reset from RUNNING -> PENDING -> completed
        assert "a" in result.completed

    def test_retry_on_failure(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        manifest = _make_manifest([
            Component(
                "a", "A", "", [],
                "a.json", "b/a",
            ),
        ])
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            max_retries=2,
            retry_delay=0,
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        call_count = 0

        def mock_run_component(
            component_id, prd_path_str, worktree_path_str,
            prompt_file_str, agent_cmd, model, reasoning, agent_type, sleep_seconds,
        ):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return ComponentResult(component_id, success=False, error="fail")
            return ComponentResult(component_id, success=True, iterations=1)

        with patch("ralph_py.factory._run_component", side_effect=mock_run_component):
            result = run_factory(manifest, config, base, ui, root)

        assert "a" in result.completed
        assert call_count == 3  # 2 failures + 1 success

    def test_manifest_saved_during_execution(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        manifest = _make_manifest([
            Component(
                "a", "A", "", [],
                "a.json", "b/a",
            ),
        ])
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        success_result = ComponentResult("a", success=True, iterations=1)
        manifest_path = root / "scripts" / "ralph" / "manifest.json"

        with patch("ralph_py.factory._run_component", return_value=success_result):
            run_factory(manifest, config, base, ui, root)

        assert manifest_path.exists()
        saved = json.loads(manifest_path.read_text())
        assert saved["components"][0]["status"] == "completed"
