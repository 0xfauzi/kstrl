"""Tests for evolution module."""

from __future__ import annotations

import json
from pathlib import Path

from ralph_py.evolution import (
    EvolutionConfig,
    EvolutionJournal,
    FailurePattern,
)
from ralph_py.factory import FactoryResult
from ralph_py.manifest import Component, ComponentStatus, Manifest


def _make_manifest(
    components: list[Component] | None = None,
) -> Manifest:
    """Build a minimal manifest for testing."""
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test-project",
        base_branch="main",
        single_pr=False,
        components=components or [],
    )


def _make_component(
    id: str,
    status: str = ComponentStatus.PENDING.value,
    error: str = "",
    retries: int = 0,
    duration_seconds: float = 0.0,
    iteration_count: int = 0,
) -> Component:
    return Component(
        id=id,
        title=f"Component {id}",
        description=f"Description of {id}",
        dependencies=[],
        prd_path=f"prd/{id}.json",
        branch_name=f"ralph/{id}",
        status=status,
        error=error,
        retries=retries,
        duration_seconds=duration_seconds,
        iteration_count=iteration_count,
    )


# ---------------------------------------------------------------------------
# EvolutionConfig defaults
# ---------------------------------------------------------------------------


class TestEvolutionConfigDefaults:
    def test_evolution_config_defaults(self) -> None:
        config = EvolutionConfig()
        assert config.enabled is True
        assert config.min_pattern_frequency == 2
        assert config.lookback_runs == 10
        assert config.auto_propose is True
        assert config.auto_apply_computational is False
        assert str(config.journal_path).endswith("evolution.jsonl")
        assert str(config.experiments_path).endswith("experiments.tsv")


# ---------------------------------------------------------------------------
# record_run
# ---------------------------------------------------------------------------


class TestRecordRun:
    def test_record_run_creates_files(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "evolution.jsonl"
        experiments_path = tmp_path / "experiments.tsv"
        config = EvolutionConfig(
            journal_path=journal_path,
            experiments_path=experiments_path,
        )
        journal = EvolutionJournal(config)

        manifest = _make_manifest([
            _make_component("a", status=ComponentStatus.COMPLETED.value,
                            duration_seconds=10.0, iteration_count=2),
            _make_component("b", status=ComponentStatus.FAILED.value,
                            error="pytest: assert 1 == 2", retries=3,
                            duration_seconds=5.0, iteration_count=1),
        ])
        factory_result = FactoryResult(completed=["a"], failed=["b"], skipped=[])

        journal.record_run("run-001", manifest, factory_result)

        # JSONL file should exist with 2 entries (one per component).
        assert journal_path.exists()
        lines = journal_path.read_text().strip().splitlines()
        assert len(lines) == 2
        entry_a = json.loads(lines[0])
        assert entry_a["component_id"] == "a"
        assert entry_a["run_id"] == "run-001"
        entry_b = json.loads(lines[1])
        assert entry_b["component_id"] == "b"
        assert entry_b["status"] == ComponentStatus.FAILED.value

        # TSV file should exist with a header and one data row.
        assert experiments_path.exists()
        tsv_lines = experiments_path.read_text().strip().splitlines()
        assert len(tsv_lines) == 2  # header + data row
        assert "run-001" in tsv_lines[1]


# ---------------------------------------------------------------------------
# extract_failure_patterns
# ---------------------------------------------------------------------------


class TestExtractFailurePatterns:
    def test_extract_failure_patterns(self) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)

        manifest = _make_manifest([
            _make_component("a", status=ComponentStatus.FAILED.value,
                            error="ruff: S608 violation", retries=1),
            _make_component("b", status=ComponentStatus.FAILED.value,
                            error="ruff: S608 violation", retries=2),
            _make_component("c", status=ComponentStatus.COMPLETED.value),
        ])

        patterns = journal.extract_failure_patterns(manifest, min_frequency=2)
        assert len(patterns) >= 1
        # Both a and b share the S608 signature
        assert any("S608" in p.error_signature for p in patterns)
        assert any(p.frequency >= 2 for p in patterns)

    def test_extract_failure_patterns_no_failures(self) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)
        manifest = _make_manifest([
            _make_component("a", status=ComponentStatus.COMPLETED.value),
        ])
        patterns = journal.extract_failure_patterns(manifest)
        assert patterns == []


# ---------------------------------------------------------------------------
# get_experiment_trends
# ---------------------------------------------------------------------------


class TestGetExperimentTrends:
    def test_get_experiment_trends(self, tmp_path: Path) -> None:
        tsv_path = tmp_path / "experiments.tsv"
        tsv_path.write_text(
            "run_id\ttimestamp\tproject\tcomponents_total\tcompleted\tfailed\t"
            "skipped\tavg_iterations\tavg_duration_s\tretry_rate\tcommon_failure\n"
            "run-001\t2025-01-01T00:00:00Z\ttest\t3\t2\t1\t0\t1.50\t10.0\t0.33\tS608\n"
            "run-002\t2025-01-02T00:00:00Z\ttest\t3\t3\t0\t0\t1.00\t8.0\t0.00\t\n"
        )

        config = EvolutionConfig(experiments_path=tsv_path)
        journal = EvolutionJournal(config)
        trends = journal.get_experiment_trends(last_n=10)
        assert len(trends) == 2
        assert trends[0]["run_id"] == "run-001"
        assert trends[1]["completed"] == "3"

    def test_get_experiment_trends_missing_file(self, tmp_path: Path) -> None:
        config = EvolutionConfig(experiments_path=tmp_path / "nonexistent.tsv")
        journal = EvolutionJournal(config)
        trends = journal.get_experiment_trends()
        assert trends == []


class TestConcernHitRate:
    """D8: aggregate reviewer-concern signal across recent runs."""

    def _write_entries(self, path: Path, entries: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def test_empty_journal(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "evolution.jsonl"
        journal_path.write_text("")
        config = EvolutionConfig(journal_path=journal_path)
        journal = EvolutionJournal(config)
        result = journal.get_concern_hit_rate()
        assert result == {
            "runs": 0, "components": 0, "with_concern": 0, "by_category": {},
        }

    def test_counts_categories(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "evolution.jsonl"
        self._write_entries(journal_path, [
            {"run_id": "run-1", "component_id": "a", "error": "FAIL scope_creep: x"},
            {"run_id": "run-1", "component_id": "b", "error": "ADVISORY test_quality: y"},
            {"run_id": "run-2", "component_id": "c", "error": "FAIL security_concern: hardcoded key"},
            {"run_id": "run-2", "component_id": "d", "error": ""},
        ])
        config = EvolutionConfig(journal_path=journal_path)
        journal = EvolutionJournal(config)
        result = journal.get_concern_hit_rate()
        assert result["runs"] == 2
        assert result["components"] == 4
        assert result["with_concern"] == 3
        assert result["by_category"]["scope_creep"] == 1
        assert result["by_category"]["test_quality"] == 1
        assert result["by_category"]["security_concern"] == 1


# ---------------------------------------------------------------------------
# propose_improvements
# ---------------------------------------------------------------------------


class TestProposeImprovements:
    def test_propose_improvements(self) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)

        patterns = [
            FailurePattern(
                description="linter failure 'S608' in 3/5 components",
                frequency=3,
                total_components=5,
                affected_components=["a", "b", "c"],
                check_name="linter",
                error_signature="S608",
                category="verification",
            ),
            FailurePattern(
                description="test_suite failure 'assert-mismatch' in 2/5 components",
                frequency=2,
                total_components=5,
                affected_components=["d", "e"],
                check_name="test_suite",
                error_signature="assert-mismatch",
                category="verification",
            ),
        ]

        proposals = journal.propose_improvements(patterns)
        assert len(proposals) == 2
        assert proposals[0].id == "PROP-001"
        assert "S608" in proposals[0].title
        assert proposals[0].target == "claude_md"
        assert proposals[1].target == "feedforward_config"

    def test_propose_improvements_empty(self) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)
        proposals = journal.propose_improvements([])
        assert proposals == []


# ---------------------------------------------------------------------------
# save_proposals
# ---------------------------------------------------------------------------


class TestSaveProposals:
    def test_save_proposals(self, tmp_path: Path) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)

        patterns = [
            FailurePattern(
                description="linter failure 'E501' in 4/6 components",
                frequency=4,
                total_components=6,
                affected_components=["a", "b", "c", "d"],
                check_name="linter",
                error_signature="E501",
                category="verification",
            ),
        ]
        proposals = journal.propose_improvements(patterns)

        output_dir = tmp_path / "proposals"
        written = journal.save_proposals(proposals, output_dir)
        assert len(written) == 1
        assert written[0].name == "prop-001.md"
        content = written[0].read_text()
        assert "PROP-001" in content
        assert "E501" in content
        assert "computational" in content
