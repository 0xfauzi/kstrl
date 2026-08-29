"""Tests for factory module."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kstrl.config import KstrlConfig
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    FactoryResult,
    merge_gate_unreachable_warning,
    resolve_exit_code,
    run_factory,
)
from kstrl.knowledge import Fact, write_facts
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.review import ReviewMode, ReviewResult
from kstrl.ui.plain import PlainUI
from kstrl.verify import CheckResult, VerificationResult, VerifyConfig


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


def _make_base_config(root_dir: Path) -> KstrlConfig:
    """Build a base config for factory tests."""
    prompt = root_dir / "scripts" / "kstrl" / "prompt.md"
    prd = root_dir / "scripts" / "kstrl" / "prd.json"
    return KstrlConfig(
        prompt_file=prompt,
        prd_file=prd,
        sleep_seconds=0,
        agent_cmd="echo test",
        kstrl_branch="",
        kstrl_branch_explicit=True,
        ui_mode="plain",
        no_color=True,
    )


def _setup_project(tmp_path: Path) -> Path:
    """Create minimal project structure for factory tests."""
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    (kstrl_dir / "prd.json").write_text('{"branchName": "test", "userStories": []}')
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


class TestMergeGateUnreachableWarning:
    """Issue #207 (PR #211 review P1/P3): warn when the FINAL resolved
    config has the merge gate on while _phase_checkpoint is unreachable.

    Checked post-autonomy-resolution in run_factory, because the L1/L2
    bundle can flip pause_before_pr_merge on when no config flag set it.
    """

    def test_silent_when_gate_off(self) -> None:
        assert merge_gate_unreachable_warning(FactoryConfig()) is None
        assert merge_gate_unreachable_warning(FactoryConfig(create_prs=False)) is None

    def test_silent_when_gate_reachable(self) -> None:
        assert (
            merge_gate_unreachable_warning(
                FactoryConfig(pause_before_pr_merge=True, create_prs=True)
            )
            is None
        )

    def test_warns_when_prs_disabled(self) -> None:
        warning = merge_gate_unreachable_warning(
            FactoryConfig(pause_before_pr_merge=True, create_prs=False)
        )
        assert warning is not None
        assert "pause_before_pr_merge" in warning
        assert "create_prs" in warning
        assert "merge gate can never run" in warning

    def test_warns_in_single_pr_mode(self) -> None:
        """Review P3: single_pr mode creates one aggregate PR via
        create_single_pr with no checkpoint, so the gate is equally
        unreachable even though create_prs is on."""
        warning = merge_gate_unreachable_warning(
            FactoryConfig(
                pause_before_pr_merge=True,
                create_prs=True,
                single_pr=True,
            )
        )
        assert warning is not None
        assert "single_pr" in warning
        assert "merge gate never runs" in warning


class TestRunFactoryDAGValidation:
    """Tests for DAG validation in run_factory."""

    def test_rejects_cyclic_dag(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest(
            [
                Component("a", "A", "", ["b"], "a.json", "b/a"),
                Component("b", "B", "", ["a"], "b.json", "b/b"),
            ]
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            review_mode="skip",
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        result = run_factory(manifest, config, base, ui, root)
        assert result.exit_code == 1

    def test_empty_manifest_succeeds(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest([])
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            review_mode="skip",
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        result = run_factory(manifest, config, base, ui, root)
        assert result.exit_code == 0


class TestRunFactoryExecution:
    """Tests for factory execution with mocked components."""

    def test_single_component_success(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest(
            [
                Component(
                    "comp-a",
                    "Component A",
                    "Desc",
                    [],
                    "scripts/kstrl/feature/comp-a/prd.json",
                    "kstrl/factory/comp-a",
                ),
            ]
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        # Create PRD for the component
        feature_dir = root / "scripts" / "kstrl" / "feature" / "comp-a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )

        success_result = ComponentResult("comp-a", success=True, iterations=3)

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success_result,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(manifest, config, base, ui, root)

        assert "comp-a" in result.completed
        assert result.exit_code == 0

    def test_component_failure_cascades(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest(
            [
                Component("a", "A", "Desc A", [], "a.json", "b/a"),
                Component("b", "B", "Desc B", ["a"], "b.json", "b/b"),
            ]
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            max_retries=0,
            retry_delay=0,
            review_mode="skip",
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        fail_result = ComponentResult("a", success=False, error="test failure")

        with patch("kstrl.factory._run_component", return_value=fail_result):
            result = run_factory(manifest, config, base, ui, root)

        assert "a" in result.failed
        assert "b" in result.skipped
        assert result.exit_code == 1

    def test_crash_recovery_resets_running(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        prd_rel = "scripts/kstrl/feature/a/prd.json"
        feature_dir = root / "scripts" / "kstrl" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )

        manifest = _make_manifest(
            [
                Component(
                    "a",
                    "A",
                    "",
                    [],
                    prd_rel,
                    "b/a",
                    status=ComponentStatus.RUNNING.value,
                ),
            ]
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        success_result = ComponentResult("a", success=True, iterations=1)

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success_result,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(manifest, config, base, ui, root)

        assert "a" in result.completed

    def test_crash_recovery_resets_verifying(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        prd_rel = "scripts/kstrl/feature/a/prd.json"
        feature_dir = root / "scripts" / "kstrl" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )

        manifest = _make_manifest(
            [
                Component(
                    "a",
                    "A",
                    "",
                    [],
                    prd_rel,
                    "b/a",
                    status=ComponentStatus.VERIFYING.value,
                ),
            ]
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        success_result = ComponentResult("a", success=True, iterations=1)

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success_result,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(manifest, config, base, ui, root)

        assert "a" in result.completed

    def test_manifest_saved_during_execution(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        prd_rel = "scripts/kstrl/feature/a/prd.json"
        feature_dir = root / "scripts" / "kstrl" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )

        manifest = _make_manifest(
            [
                Component("a", "A", "", [], prd_rel, "b/a"),
            ]
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )
        base = _make_base_config(root)
        ui = PlainUI(no_color=True)

        # This duplicate PRD creation already exists above, remove the second one
        success_result = ComponentResult("a", success=True, iterations=1)
        manifest_path = root / "scripts" / "kstrl" / "manifest.json"

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success_result,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            run_factory(manifest, config, base, ui, root)

        assert manifest_path.exists()
        saved = json.loads(manifest_path.read_text())
        assert saved["components"][0]["status"] == "completed"

    def test_verification_failure_triggers_retry(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)

        prd_rel = "scripts/kstrl/feature/a/prd.json"
        manifest = _make_manifest(
            [
                Component("a", "A", "", [], prd_rel, "b/a"),
            ]
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            max_retries=1,
            retry_delay=0,
            review_mode="skip",
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
        feature_dir = root / "scripts" / "kstrl" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )

        success_result = ComponentResult("a", success=True, iterations=1)

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success_result,
            ) as mock_run,
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
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


class TestEvolutionRecording:
    """R6.1 + R6.4 end to end: a real factory run journals structured
    failure signatures and a nonzero attempt duration."""

    def test_failure_signature_and_duration_reach_journal(
        self,
        tmp_path: Path,
    ) -> None:
        root = _setup_project(tmp_path)
        prd_rel = "scripts/kstrl/feature/a/prd.json"
        manifest = _make_manifest(
            [
                Component("a", "A", "", [], prd_rel, "b/a"),
            ]
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            max_retries=0,
            retry_delay=0,
            review_mode="skip",
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

        feature_dir = root / "scripts" / "kstrl" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )

        success_result = ComponentResult("a", success=True, iterations=1)
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=success_result,
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(manifest, config, base, ui, root)

        assert "a" in result.failed

        journal_path = root / ".kstrl" / "evolution.jsonl"
        entries = [json.loads(line) for line in journal_path.read_text().strip().splitlines()]
        comp_entries = [
            e
            for e in entries
            if e.get("event_type") == "component_result" and e.get("component_id") == "a"
        ]
        assert comp_entries, f"no component_result entry in {entries}"
        entry = comp_entries[-1]
        # R6.4: journal format is versioned.
        assert entry["schema_version"] == 2
        # R6.1: the structured signature from the failing check, not a
        # slug of "Mechanical verification failed".
        assert entry["failure_signatures"] == [
            "test_suite:tests-failed-exit-code",
        ]
        assert entry["check_name"] == "test_suite"
        assert entry["error_signature"] == "tests-failed-exit-code"
        # R6.4: duration is the attempt wall clock, not 0.0. The mocked
        # engineer returns instantly, so any nonzero value proves the
        # stamp comes from the factory's own attempt clock.
        assert entry["duration_seconds"] > 0
        comp = manifest.get_component("a")
        assert comp is not None
        assert comp.duration_seconds > 0

    def _knowledge_project(self, tmp_path: Path) -> tuple[Path, Manifest]:
        """A project whose knowledge store already holds one fact for
        component 'a', so the submit-time prefix is non-empty."""
        root = _setup_project(tmp_path)
        prd_rel = "scripts/kstrl/feature/a/prd.json"
        manifest = _make_manifest(
            [
                Component("a", "A", "", [], prd_rel, "b/a"),
            ]
        )
        feature_dir = root / "scripts" / "kstrl" / "feature" / "a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        # Written through the real writer so the fixture cannot drift
        # from the on-disk format the reader expects.
        write_facts(
            [
                Fact(
                    id="fact-001",
                    component_id="a",
                    created_iter=1,
                    created_run_id="seed-run",
                    scope="contract",
                    evidence=["widget.py:12"],
                    confidence="review_passed",
                    claim="The widget parser rejects trailing commas.",
                )
            ],
            root / ".kstrl" / "knowledge",
            "a",
            "seed-run",
        )
        return root, manifest

    def _passing_config(self, root: Path) -> FactoryConfig:
        return FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            max_retries=0,
            retry_delay=0,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )

    def _component_entry(self, root: Path) -> dict[str, Any]:
        entries = [
            json.loads(line)
            for line in (root / ".kstrl" / "evolution.jsonl").read_text().strip().splitlines()
        ]
        comp_entries = [
            e
            for e in entries
            if e.get("event_type") == "component_result" and e.get("component_id") == "a"
        ]
        assert comp_entries, f"no component_result entry in {entries}"
        return comp_entries[-1]

    def test_fact_utilization_reaches_the_journal(
        self,
        tmp_path: Path,
    ) -> None:
        """#191 end to end: the prefix captured at submit time survives
        through the pipeline into the journal, with no LLM spend."""
        root, manifest = self._knowledge_project(tmp_path)
        base = _make_base_config(root)
        seen_prefixes: list[str] = []

        def fake_measure(
            prefix: str,
            *artifacts: str,
            **kwargs: Any,
        ) -> dict[str, int]:
            seen_prefixes.append(prefix)
            return {"injected": 1, "referenced": 1}

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=ComponentResult("a", success=True, iterations=1),
            ),
            patch("kstrl.git.get_diff_content", return_value="some diff"),
            patch(
                "kstrl.factory.measure_fact_utilization",
                fake_measure,
            ),
            patch(
                "kstrl.factory.distill_facts",
                return_value=(0, "none"),
            ),
        ):
            run_factory(
                manifest,
                self._passing_config(root),
                base,
                PlainUI(no_color=True),
                root,
            )

        # The measured prefix is the real one built at submit time, so
        # it carries the seeded fact.
        assert seen_prefixes, "utilization was never measured"
        assert "trailing commas" in seen_prefixes[0]

        util = self._component_entry(root)["knowledge_utilization"]
        assert util["measured"] is True
        assert util["injected"] == 1
        assert util["referenced"] == 1

    def test_knowledge_retrieval_failure_warns_and_is_unmeasured(
        self,
        tmp_path: Path,
    ) -> None:
        """The retrieval failure used to be a bare `except: pass` - it
        strips the engineer's whole prefix, so it must be loud, and the
        run must not be scored as if facts had been injected."""
        root, manifest = self._knowledge_project(tmp_path)
        base = _make_base_config(root)
        buf = io.StringIO()
        ui = PlainUI(no_color=True, file=buf)

        def boom(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("knowledge store unreadable")

        with (
            patch(
                "kstrl.factory._run_component",
                return_value=ComponentResult("a", success=True, iterations=1),
            ),
            patch("kstrl.git.get_diff_content", return_value="some diff"),
            patch("kstrl.factory.build_knowledge_context", boom),
            patch(
                "kstrl.factory.distill_facts",
                return_value=(0, "none"),
            ),
        ):
            run_factory(
                manifest,
                self._passing_config(root),
                base,
                ui,
                root,
            )

        assert "Knowledge retrieval failed" in buf.getvalue()
        util = self._component_entry(root)["knowledge_utilization"]
        assert util["measured"] is False
        assert util["reason"] == "knowledge retrieval failed"


class TestResolveExitCode:
    """#263: the ladder branch for a run that scheduled nothing.

    Unit level, because the interesting cases differ only in manifest
    state and the counters an executed run would have left behind.
    """

    @staticmethod
    def _ui() -> tuple[PlainUI, io.StringIO]:
        buf = io.StringIO()
        return PlainUI(no_color=True, file=buf), buf

    def _resolve(
        self,
        manifest: Manifest,
        result: FactoryResult,
        stopped: bool = False,
    ) -> tuple[int, str]:
        ui, buf = self._ui()
        code = resolve_exit_code(result, manifest, ui, stopped=stopped)
        return code, buf.getvalue()

    def test_empty_manifest_stays_zero(self) -> None:
        code, out = self._resolve(_make_manifest([]), FactoryResult())
        assert code == 0
        assert out == ""

    def test_finished_manifest_rerun_stays_zero(self) -> None:
        # Idempotent re-run of a manifest whose work is done. Every
        # counter is empty here exactly as in the bug report, which is
        # why a counter-based predicate cannot separate the two.
        manifest = _make_manifest(
            [Component("a", "A", "", [], "a.json", "b/a", status="completed")]
        )
        code, out = self._resolve(manifest, FactoryResult())
        assert code == 0
        assert out == ""

    def test_off_enum_status_fails_loudly(self) -> None:
        manifest = _make_manifest([Component("a", "A", "", [], "a.json", "b/a", status="PENDING")])
        code, out = self._resolve(manifest, FactoryResult())
        assert code == 1
        assert "No component was scheduled from 1 in the manifest" in out
        assert "ComponentStatus" in out
        for legal in ComponentStatus:
            assert legal.value in out

    def test_already_failed_rerun_fails(self) -> None:
        # Re-running a manifest whose component failed, without `ks
        # retry`: nothing is schedulable, so the run built nothing.
        manifest = _make_manifest([Component("a", "A", "", [], "a.json", "b/a", status="failed")])
        code, out = self._resolve(manifest, FactoryResult())
        assert code == 1
        assert "ks retry" in out

    def test_partly_finished_rerun_fails(self) -> None:
        # The case "no component ever completed" would wave through: one
        # component IS completed, but the other never ran.
        manifest = _make_manifest(
            [
                Component("a", "A", "", [], "a.json", "b/a", status="completed"),
                Component("b", "B", "", [], "b.json", "b/b", status="failed"),
            ]
        )
        code, out = self._resolve(manifest, FactoryResult())
        assert code == 1
        assert "1 did not complete: b" in out

    def test_leftover_skipped_rerun_fails(self) -> None:
        manifest = _make_manifest([Component("a", "A", "", [], "a.json", "b/a", status="skipped")])
        code, _ = self._resolve(manifest, FactoryResult())
        assert code == 1

    def test_merge_pending_belongs_to_the_earlier_branch(self) -> None:
        # _run_factory_locked rebuilds factory_result.merge_pending from
        # the manifest before calling this, so an all-merge_pending
        # manifest can never reach the nothing-scheduled branch. Pinned
        # with merge_pending populated the way the caller populates it,
        # not with the empty FactoryResult that would fake a reachable
        # state: this already returned 1 before #263.
        manifest = _make_manifest(
            [Component("a", "A", "", [], "a.json", "b/a", status="merge_pending")]
        )
        code, out = self._resolve(manifest, FactoryResult(merge_pending=["a"]))
        assert code == 1
        assert "No component was scheduled" not in out

    @staticmethod
    def _cascade_manifest() -> Manifest:
        """`a` failed and cascade-skipped `b`: the common stuck re-run."""
        return _make_manifest(
            [
                Component("a", "A", "", [], "a.json", "b/a", status="failed"),
                Component("b", "B", "", ["a"], "b.json", "b/b", status="skipped"),
            ]
        )

    def test_cascade_points_at_the_root_failure_not_the_victim(self) -> None:
        # `ks retry b` on a cascade-skipped component exits 2, so the
        # remedy has to name `a`. The old wording said "a component left
        # in 'failed' or 'skipped' ... until `ks retry <component-id>`",
        # which sent the operator at `b`.
        manifest = self._cascade_manifest()
        code, out = self._resolve(manifest, FactoryResult())
        assert code == 1
        assert "Failed here: a." in out
        assert "Failed here: b" not in out

    def test_named_retry_target_is_actually_retryable(self) -> None:
        # The advice is only worth printing if it runs, so put the id the
        # message names through the command's own gate.
        manifest = self._cascade_manifest()
        _, out = self._resolve(manifest, FactoryResult())
        assert "Failed here: a." in out
        reset = manifest.reset_for_retry("a")
        # Retrying the root also frees the component it cascade-skipped.
        assert "b" in reset
        assert manifest.get_component("b").status == ComponentStatus.PENDING.value

    def test_message_names_exactly_the_retryable_ids(self) -> None:
        # The message and `reset_for_retry` must read one definition, so
        # a future change to what counts as retryable cannot leave the
        # advice naming ids the command has started refusing.
        manifest = self._cascade_manifest()
        _, out = self._resolve(manifest, FactoryResult())
        assert manifest.retryable_component_ids() == ["a"]
        assert f"Failed here: {', '.join(manifest.retryable_component_ids())}." in out

    def test_retrying_the_skipped_victim_is_refused(self) -> None:
        # The behaviour the old message walked the operator into.
        manifest = self._cascade_manifest()
        with pytest.raises(ValueError, match="only failed components can be retried"):
            manifest.reset_for_retry("b")

    @pytest.mark.parametrize("status", ["skipped", "PENDING"])
    def test_no_failed_component_says_retry_will_not_help(self, status: str) -> None:
        manifest = _make_manifest([Component("a", "A", "", [], "a.json", "b/a", status=status)])
        code, out = self._resolve(manifest, FactoryResult())
        assert code == 1
        assert "accepts only a component in 'failed', and none is" in out
        assert "Failed here" not in out

    def test_scheduled_run_that_completed_stays_zero(self) -> None:
        manifest = _make_manifest(
            [Component("a", "A", "", [], "a.json", "b/a", status="completed")]
        )
        code, out = self._resolve(manifest, FactoryResult(completed=["a"], scheduled=["a"]))
        assert code == 0
        assert out == ""

    def test_scheduled_run_left_unfinished_is_not_reported_here(self) -> None:
        # A component that ran and did not finish is already named by an
        # earlier branch; this branch must not double-report it, and must
        # not fire for any run that actually scheduled work.
        manifest = _make_manifest(
            [
                Component("a", "A", "", [], "a.json", "b/a", status="completed"),
                Component("b", "B", "", ["a"], "b.json", "b/b", status="skipped"),
            ]
        )
        code, out = self._resolve(
            manifest,
            FactoryResult(completed=["a"], scheduled=["a"]),
        )
        assert code == 0
        assert out == ""

    def test_stop_wins_over_nothing_scheduled(self) -> None:
        manifest = _make_manifest([Component("a", "A", "", [], "a.json", "b/a", status="failed")])
        code, out = self._resolve(manifest, FactoryResult(), stopped=True)
        assert code == 130
        assert out == ""

    def test_failure_branch_wins_over_nothing_scheduled(self) -> None:
        manifest = _make_manifest([Component("a", "A", "", [], "a.json", "b/a", status="failed")])
        code, out = self._resolve(manifest, FactoryResult(failed=["a"]))
        assert code == 1
        assert out == ""


class TestRunFactorySchedulesNothing:
    """#263 end to end: a run that launches nothing must not report success."""

    @staticmethod
    def _config() -> FactoryConfig:
        return FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            review_mode="skip",
        )

    def _run(self, root: Path, manifest: Manifest) -> tuple[Any, str]:
        buf = io.StringIO()
        ui = PlainUI(no_color=True, file=buf)
        result = run_factory(manifest, self._config(), _make_base_config(root), ui, root)
        return result, buf.getvalue()

    def test_off_enum_status_exits_nonzero(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest([Component("a", "A", "", [], "a.json", "b/a", status="PENDING")])

        result, out = self._run(root, manifest)

        assert result.scheduled == []
        assert result.completed == []
        assert result.exit_code == 1
        assert "No component was scheduled from 1 in the manifest" in out

    def test_already_failed_rerun_exits_nonzero(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest([Component("a", "A", "", [], "a.json", "b/a", status="failed")])

        result, out = self._run(root, manifest)

        assert result.scheduled == []
        assert result.exit_code == 1
        assert "ks retry" in out

    def test_finished_manifest_rerun_exits_zero(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest(
            [Component("a", "A", "", [], "a.json", "b/a", status="completed")]
        )

        result, out = self._run(root, manifest)

        assert result.scheduled == []
        assert result.exit_code == 0
        assert "No component was scheduled" not in out

    def test_a_scheduled_run_records_what_it_launched(self, tmp_path: Path) -> None:
        root = _setup_project(tmp_path)
        manifest = _make_manifest(
            [
                Component(
                    "comp-a",
                    "Component A",
                    "Desc",
                    [],
                    "scripts/kstrl/feature/comp-a/prd.json",
                    "kstrl/factory/comp-a",
                ),
            ]
        )
        feature_dir = root / "scripts" / "kstrl" / "feature" / "comp-a"
        feature_dir.mkdir(parents=True)
        (feature_dir / "prd.json").write_text(
            json.dumps(
                {
                    "branchName": "test",
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "Test",
                            "acceptanceCriteria": ["AC1"],
                            "priority": 1,
                            "passes": True,
                            "notes": "",
                        }
                    ],
                }
            )
        )
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            review_mode="skip",
            verify_config=VerifyConfig(
                test_command="true",
                typecheck_command="true",
                lint_command="true",
                check_diff_scope=False,
                check_bad_patterns=False,
                subprocess_timeout=5.0,
            ),
        )

        buf = io.StringIO()
        ui = PlainUI(no_color=True, file=buf)
        with (
            patch(
                "kstrl.factory._run_component",
                return_value=ComponentResult("comp-a", success=True, iterations=1),
            ),
            patch("kstrl.git.get_diff_content", return_value=""),
        ):
            result = run_factory(manifest, config, _make_base_config(root), ui, root)

        assert result.completed == ["comp-a"]
        assert result.scheduled == ["comp-a"]
        assert result.exit_code == 0
        assert "No component was scheduled" not in buf.getvalue()

    def test_scheduled_records_one_entry_per_attempt(self, tmp_path: Path) -> None:
        # `scheduled` is an attempt log, not a set: the branch that reads
        # it only asks whether it is empty, and a per-attempt record is
        # the more informative of the two.
        root = _setup_project(tmp_path)
        manifest = _make_manifest([Component("a", "A", "Desc", [], "a.json", "b/a")])
        config = FactoryConfig(
            use_worktrees=False,
            create_prs=False,
            max_parallel=1,
            max_retries=2,
            retry_delay=0,
            review_mode="skip",
        )

        buf = io.StringIO()
        ui = PlainUI(no_color=True, file=buf)
        with patch(
            "kstrl.factory._run_component",
            return_value=ComponentResult("a", success=False, error="boom"),
        ):
            result = run_factory(manifest, config, _make_base_config(root), ui, root)

        assert result.scheduled == ["a", "a", "a"]
        assert result.failed == ["a"]
        assert result.exit_code == 1
        assert "No component was scheduled" not in buf.getvalue()
