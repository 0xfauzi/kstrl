"""``run_factory`` driven on real project directories with a stubbed engineer.

Every test here builds a project under ``tmp_path``, hands ``run_factory``
a manifest and a base config, and reads what the run left behind: the
exit code, the manifest on disk, and the evolution journal. The engineer
(``_run_component``) is the one collaborator stubbed, so the run costs
nothing; the verification commands are real shell commands (``true`` and
``false``). Covered: DAG refusal before any component runs (#531), the
empty manifest, the single-component pass, the failure cascade, crash
recovery from ``running`` and ``verifying``, the manifest saved during
execution, a verification failure retried to exhaustion (R4.3), failure
signatures and durations reaching the journal (R6.1, R6.4), fact
utilisation measured at submit time (#191) with retrieval failure loud
and unmeasured (#599), and a run that schedules nothing not reporting
success (#263). The ``resolve_exit_code`` / ``run_is_clean`` tables are
carried by ``tests/test_run_honesty.py`` and
``tests/test_prelaunch_refusal_exit.py``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl.config import KstrlConfig
from kstrl.factory import (
    ComponentResult,
    FactoryConfig,
    run_factory,
)
from kstrl.knowledge import Fact, write_facts
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig
from tests.helpers.component_prd import write_component_prd

#: The component ids this module's manifests use. _setup_project puts
#: a PRD at each one's prdPath, which the plan-time scope snapshot
#: reads before the run is allowed to start.
_COMP_IDS = ("a", "b")


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
    """Create minimal project structure for factory tests.

    Including a PRD per component this module's manifests name.
    Without one the run is refused before it starts (#293 review): a
    component whose pre-run PRD will not read has no plan-time scope,
    and Phase 1 would fail it identically on every attempt.
    """
    kstrl_dir = tmp_path / "scripts" / "kstrl"
    kstrl_dir.mkdir(parents=True)
    (kstrl_dir / "prompt.md").write_text("test prompt")
    write_component_prd(tmp_path, "scripts/kstrl/prd.json")
    for comp_id in _COMP_IDS:
        write_component_prd(tmp_path, f"{comp_id}.json")
    return tmp_path


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
        assert result.exit_code == 2  # a refusal before any component runs (#531)

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
        assert entry["schema_version"] == 3
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
                return_value=(0, "none", False),
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
        run must not be scored as if facts had been injected.

        #599 A3 moved this call, and the try/except around it, off
        `factory._submit_args` and onto the shared
        `knowledge.retrieve_knowledge_context` (also called by
        `feature_cmd`); the patch target moves with it, onto the
        function that actually raises rather than the module that used
        to import it directly."""
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
            patch("kstrl.knowledge.build_knowledge_context", boom),
            patch(
                "kstrl.factory.distill_facts",
                return_value=(0, "none", False),
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
