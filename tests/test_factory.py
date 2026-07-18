"""Tests for factory module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ralph_py.config import RalphConfig
from ralph_py.factory import (
    ComponentResult,
    FactoryConfig,
    run_factory,
)
from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.review import ReviewMode, ReviewResult
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import CheckResult, VerificationResult, VerifyConfig


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


def _passing_verification() -> VerificationResult:
    return VerificationResult(
        passed=True,
        checks=[CheckResult("test_suite", True, "ok")],
    )


def _failing_verification() -> VerificationResult:
    return VerificationResult(
        passed=False,
        checks=[CheckResult("test_suite", False, "2 failures")],
    )


def _passing_review() -> ReviewResult:
    return ReviewResult(passed=True, mode="hard")


class TestFactoryConfig:
    """Tests for FactoryConfig."""

    def test_defaults(self) -> None:
        config = FactoryConfig()
        assert config.max_parallel == 4
        assert config.max_retries == 3
        assert config.use_worktrees is True
        assert config.create_prs is True
        assert config.review_mode == ReviewMode.HARD.value

    def test_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("FACTORY_MAX_PARALLEL", "8")
        monkeypatch.setenv("FACTORY_MAX_RETRIES", "5")
        config = FactoryConfig.from_env()
        assert config.max_parallel == 8
        assert config.max_retries == 5


class TestRunFactoryDAGValidation:
    """Tests for DAG validation in run_factory."""

    def test_rejects_cyclic_dag(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest([
            Component("a", "A", "", ["b"], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
        ])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, review_mode="skip",
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        result = run_factory(manifest, config, base, ui, root)
        assert result.exit_code == 1

    def test_empty_manifest_succeeds(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest([])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, review_mode="skip",
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        result = run_factory(manifest, config, base, ui, root)
        assert result.exit_code == 0


class TestRunFactoryExecution:
    """Tests for factory execution with mocked components."""

    def test_single_component_success(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest([
            Component(
                "comp-a", "Component A", "Desc",
                [], "scripts/ralph/feature/comp-a/prd.json",
                "ralph/factory/comp-a",
            ),
        ])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False, subprocess_timeout=5.0,
            ),
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        # Create PRD for the component
        feature_dir = root / "scripts" / "ralph" / "feature" / "comp-a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))

        success_result = ComponentResult("comp-a", success=True, iterations=3)

        with patch(
            "ralph_py.factory._run_component", return_value=success_result,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(manifest, config, base, ui, root)

        assert "comp-a" in result.completed
        assert result.exit_code == 0

    def test_component_failure_cascades(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest([
            Component("a", "A", "Desc A", [], "a.json", "b/a"),
            Component("b", "B", "Desc B", ["a"], "b.json", "b/b"),
        ])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            max_retries=0, retry_delay=0, review_mode="skip",
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

        prd_rel = "scripts/ralph/feature/a/prd.json"
        feature_dir = root / "scripts" / "ralph" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))

        manifest = _make_manifest([
            Component(
                "a", "A", "", [], prd_rel, "b/a",
                status=ComponentStatus.RUNNING.value,
            ),
        ])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False, subprocess_timeout=5.0,
            ),
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        success_result = ComponentResult("a", success=True, iterations=1)

        with patch(
            "ralph_py.factory._run_component", return_value=success_result,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(manifest, config, base, ui, root)

        assert "a" in result.completed

    def test_crash_recovery_resets_verifying(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        prd_rel = "scripts/ralph/feature/a/prd.json"
        feature_dir = root / "scripts" / "ralph" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))

        manifest = _make_manifest([
            Component(
                "a", "A", "", [], prd_rel, "b/a",
                status=ComponentStatus.VERIFYING.value,
            ),
        ])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False, subprocess_timeout=5.0,
            ),
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        success_result = ComponentResult("a", success=True, iterations=1)

        with patch(
            "ralph_py.factory._run_component", return_value=success_result,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(manifest, config, base, ui, root)

        assert "a" in result.completed

    def test_manifest_saved_during_execution(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        prd_rel = "scripts/ralph/feature/a/prd.json"
        feature_dir = root / "scripts" / "ralph" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))

        manifest = _make_manifest([
            Component("a", "A", "", [], prd_rel, "b/a"),
        ])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true", typecheck_command="true",
                lint_command="true", check_diff_scope=False,
                check_bad_patterns=False, subprocess_timeout=5.0,
            ),
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        # This duplicate PRD creation already exists above, remove the second one
        success_result = ComponentResult("a", success=True, iterations=1)
        manifest_path = root / "scripts" / "ralph" / "manifest.json"

        with patch(
            "ralph_py.factory._run_component", return_value=success_result,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
            run_factory(manifest, config, base, ui, root)

        assert manifest_path.exists()
        saved = json.loads(manifest_path.read_text())
        assert saved["components"][0]["status"] == "completed"

    def test_verification_failure_triggers_retry(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        prd_rel = "scripts/ralph/feature/a/prd.json"
        manifest = _make_manifest([
            Component("a", "A", "", [], prd_rel, "b/a"),
        ])
        config = FactoryConfig(
            use_worktrees=False, create_prs=False, max_parallel=1,
            max_retries=1, retry_delay=0, review_mode="skip",
            verify_config=VerifyConfig(
                test_command="false",  # tests will fail
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        # Create PRD with a non-passing story (verify will fail)
        feature_dir = root / "scripts" / "ralph" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))

        success_result = ComponentResult("a", success=True, iterations=1)

        with patch(
            "ralph_py.factory._run_component", return_value=success_result,
        ) as mock_run, patch("ralph_py.git.get_diff_content", return_value=""):
            result = run_factory(manifest, config, base, ui, root)

        # Should fail because tests fail, and retries are exhausted
        assert "a" in result.failed
        # R4.3: assert the retry actually happened, not just the final
        # failure. max_retries=1 means two attempts (initial + one
        # retry) before the component fails for good.
        assert mock_run.call_count == 2
        comp = manifest.get_component("a")
        assert comp is not None
        assert comp.retries == 1
