"""Tests for evolution module."""

from __future__ import annotations

import json
from pathlib import Path

from ralph_py.evolution import (
    JOURNAL_SCHEMA_VERSION,
    EvolutionConfig,
    EvolutionJournal,
    FailurePattern,
    signature_for_error,
    signatures_from_findings,
    signatures_from_verification,
    split_signature,
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

    def test_record_run_includes_typed_findings(
        self, tmp_path: Path,
    ) -> None:
        """E3-consume: the evolution journal must serialize
        Component.findings alongside the existing scalars, and include
        a findings_summary for fast aggregation. This is what makes the
        typed Finding stream actually load-bearing."""
        from ralph_py.findings import Finding

        journal_path = tmp_path / "evolution.jsonl"
        experiments_path = tmp_path / "experiments.tsv"
        config = EvolutionConfig(
            journal_path=journal_path,
            experiments_path=experiments_path,
        )
        journal = EvolutionJournal(config)

        comp = _make_component(
            "a", status=ComponentStatus.COMPLETED.value, duration_seconds=10.0,
        )
        comp.findings = [
            Finding.from_review_concern(
                category="dead_code", severity="fail",
                location="src/a.py:1", explanation="unused",
            ),
            Finding.from_security_finding(
                category="injection", severity="critical",
                location="src/b.py:2", explanation="raw sql",
                suggestion="parametrize",
                owasp="A03:2021-Injection", cwe="CWE-89",
            ),
            Finding.infrastructure_error("security", "agent timeout"),
        ]
        manifest = _make_manifest([comp])
        factory_result = FactoryResult(completed=["a"], failed=[], skipped=[])

        journal.record_run("run-findings", manifest, factory_result)

        entry = json.loads(journal_path.read_text().strip())
        # All three findings serialized.
        assert len(entry["findings"]) == 3
        # Summary aggregates correctly.
        summary = entry["findings_summary"]
        assert summary["total"] == 3
        assert summary["by_phase"]["review"] == 1
        assert summary["by_phase"]["security"] == 2
        assert summary["by_severity"]["fail"] == 1
        assert summary["by_severity"]["critical"] == 2
        assert summary["by_category"]["dead_code"] == 1
        assert summary["by_category"]["injection"] == 1
        assert summary["by_owasp"]["A03:2021-Injection"] == 1
        # Infrastructure errors are counted separately from real findings.
        assert summary["infrastructure_errors"] == 1


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

    def test_counts_categories_from_findings_summary(
        self, tmp_path: Path,
    ) -> None:
        """R6.2: the hit rate consumes the typed findings_summary that
        record_run writes, not the error string (concern categories
        never appear there, so the old scan was structurally zero)."""
        journal_path = tmp_path / "evolution.jsonl"
        self._write_entries(journal_path, [
            {
                "run_id": "run-1", "component_id": "a",
                "event_type": "component_result",
                "findings_summary": {
                    "total": 2,
                    "by_category": {"scope_creep": 1, "test_quality": 1},
                },
            },
            {
                "run_id": "run-1", "component_id": "b",
                "event_type": "component_result",
                # Infrastructure-only summaries are non-execution, not
                # adversarial signal.
                "findings_summary": {
                    "total": 1,
                    "by_category": {"infrastructure_error": 1},
                },
            },
            {
                "run_id": "run-2", "component_id": "c",
                "event_type": "component_result",
                "findings_summary": {
                    "total": 1,
                    "by_category": {"security_concern": 1},
                },
            },
            {
                "run_id": "run-2", "component_id": "d",
                "event_type": "component_result",
                "findings_summary": {"total": 0, "by_category": {}},
            },
            # Non-component entries are excluded from the denominator.
            {
                "run_id": "run-2", "component_id": "",
                "event_type": "contract_result", "tier": 1, "passed": True,
            },
        ])
        config = EvolutionConfig(journal_path=journal_path)
        journal = EvolutionJournal(config)
        result = journal.get_concern_hit_rate()
        assert result["runs"] == 2
        assert result["components"] == 4
        assert result["with_concern"] == 2
        assert result["by_category"] == {
            "scope_creep": 1, "test_quality": 1, "security_concern": 1,
        }
        assert "infrastructure_error" not in result["by_category"]


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


# ---------------------------------------------------------------------------
# R6.1: structured failure signatures
# ---------------------------------------------------------------------------


class TestSignatureHelpers:
    def test_signatures_from_verification_uses_parser_codes(self) -> None:
        from ralph_py.parsers import ParsedFailure, ParsedOutput
        from ralph_py.verify import CheckResult

        ruff = ParsedOutput(tool="ruff", failures=[
            ParsedFailure(file="a.py", line=1, rule_or_test="E501", message="x"),
            ParsedFailure(file="b.py", line=2, rule_or_test="S608", message="y"),
            ParsedFailure(file="c.py", line=3, rule_or_test="E501", message="z"),
        ])
        mypy = ParsedOutput(tool="mypy", failures=[
            ParsedFailure(file="a.py", line=4, rule_or_test="arg-type", message="m"),
        ])
        pytest_out = ParsedOutput(tool="pytest", failures=[
            ParsedFailure(
                file="tests/test_a.py", rule_or_test="test_x",
                message="AssertionError: assert 1 == 2",
            ),
        ])
        checks = [
            CheckResult(name="linter", passed=False, message="Linter failed",
                        parsed=ruff),
            CheckResult(name="typecheck", passed=False,
                        message="Typecheck failed", parsed=mypy),
            CheckResult(name="test_suite", passed=False,
                        message="Tests failed", parsed=pytest_out),
            CheckResult(name="bad_patterns", passed=True, message="ok"),
        ]
        sigs = signatures_from_verification(checks)
        assert "linter:E501" in sigs
        assert "linter:S608" in sigs
        assert "typecheck:arg-type" in sigs
        assert "test_suite:assertion-error" in sigs
        # Passing checks contribute nothing; duplicates collapse.
        assert sigs.count("linter:E501") == 1
        assert not any(s.startswith("bad_patterns") for s in sigs)

    def test_signatures_from_verification_fallback_slug(self) -> None:
        from ralph_py.verify import CheckResult

        checks = [CheckResult(
            name="diff_scope", passed=False,
            message="3 files outside allowed scope (diff vs base branch 'main')",
        )]
        sigs = signatures_from_verification(checks)
        assert len(sigs) == 1
        check, code = split_signature(sigs[0])
        assert check == "diff_scope"
        # Counts and quoted names are stripped so the slug is stable
        # across runs with different violation counts.
        assert "3" not in code
        assert "main" not in code
        assert "outside-allowed-scope" in code

    def test_signatures_from_findings(self) -> None:
        from ralph_py.findings import Finding

        findings = [
            Finding.from_review_concern(
                category="scope_creep", severity="fail",
                location="a.py", explanation="x",
            ),
            Finding.from_review_concern(
                category="test_quality", severity="advisory",
                location="b.py", explanation="y",
            ),
        ]
        assert signatures_from_findings("review", findings) == [
            "review:scope_creep",
        ]

    def test_signatures_from_findings_infrastructure(self) -> None:
        from ralph_py.findings import Finding

        findings = [Finding.infrastructure_error("review", "crashed")]
        assert signatures_from_findings("review", findings) == [
            "review:infrastructure",
        ]

    def test_signature_for_error_stable(self) -> None:
        sig1 = signature_for_error(
            "engineer", "component timeout: exceeded 600s wall clock",
        )
        sig2 = signature_for_error(
            "engineer", "component timeout: exceeded 1200s wall clock",
        )
        assert sig1 == sig2
        assert sig1.startswith("engineer:")


class TestRecordRunSignatures:
    def test_journal_entry_carries_structured_signatures(
        self, tmp_path: Path,
    ) -> None:
        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
        )
        journal = EvolutionJournal(config)
        comp = _make_component(
            "b", status=ComponentStatus.FAILED.value,
            error="Mechanical verification failed", retries=1,
        )
        comp.failed_phase = "verify"
        comp.failed_check = "linter"
        manifest = _make_manifest([comp])
        factory_result = FactoryResult(completed=[], failed=["b"], skipped=[])

        journal.record_run(
            "run-001", manifest, factory_result,
            failure_signatures={"b": ["linter:S608", "linter:E501"]},
        )

        entry = json.loads(config.journal_path.read_text().strip())
        assert entry["schema_version"] == JOURNAL_SCHEMA_VERSION
        assert entry["failure_signatures"] == ["linter:S608", "linter:E501"]
        assert entry["check_name"] == "linter"
        assert entry["error_signature"] == "S608"
        assert entry["failed_phase"] == "verify"
        assert entry["failed_check"] == "linter"
        # TSV common_failure carries the full signature, not a slug of
        # the flattened string.
        tsv = config.experiments_path.read_text()
        assert "linter:S608" in tsv

    def test_legacy_fallback_without_signatures(self, tmp_path: Path) -> None:
        """A failed component with no recorded signatures still gets a
        classified signature from its error string, so entries never
        lose the fields entirely."""
        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
        )
        journal = EvolutionJournal(config)
        comp = _make_component(
            "b", status=ComponentStatus.FAILED.value,
            error="ruff: S608 violation",
        )
        manifest = _make_manifest([comp])
        journal.record_run(
            "run-001", manifest,
            FactoryResult(completed=[], failed=["b"], skipped=[]),
        )
        entry = json.loads(config.journal_path.read_text().strip())
        assert entry["failure_signatures"] == ["linter:S608"]


class TestEvolutionIntegration:
    """R6 'done when': a synthetic-but-realistic journal (real signature
    strings, typed findings) yields a proposal traceable to a recorded
    signature and a nonzero concern hit rate."""

    def _failed_component(self, comp_id: str) -> Component:
        from ralph_py.findings import Finding

        comp = _make_component(
            comp_id, status=ComponentStatus.FAILED.value,
            error="Review failed", retries=1, duration_seconds=42.0,
            iteration_count=3,
        )
        comp.failed_phase = "review"
        comp.failed_check = "criteria"
        comp.findings = [
            Finding.from_review_concern(
                category="scope_creep", severity="fail",
                location=f"{comp_id}.py", explanation="touched other files",
            ),
        ]
        return comp

    def test_journal_to_traceable_proposal(self, tmp_path: Path) -> None:
        config = EvolutionConfig(
            journal_path=tmp_path / "evolution.jsonl",
            experiments_path=tmp_path / "experiments.tsv",
            min_pattern_frequency=2,
        )
        journal = EvolutionJournal(config)

        # Two runs, each with one linter:S608 failure and one review
        # scope_creep failure - the shapes record_run actually writes.
        for run_id in ("run-001", "run-002"):
            lint_comp = _make_component(
                "comp-lint", status=ComponentStatus.FAILED.value,
                error="Mechanical verification failed", retries=2,
                duration_seconds=30.0, iteration_count=2,
            )
            lint_comp.failed_phase = "verify"
            lint_comp.failed_check = "linter"
            review_comp = self._failed_component("comp-review")
            manifest = _make_manifest([lint_comp, review_comp])
            journal.record_run(
                run_id, manifest,
                FactoryResult(
                    completed=[], failed=["comp-lint", "comp-review"],
                    skipped=[],
                ),
                failure_signatures={
                    "comp-lint": ["linter:S608"],
                    "comp-review": ["review:scope_creep"],
                },
            )

        patterns = journal.get_cross_run_patterns(lookback_runs=10)
        linter_patterns = [
            p for p in patterns
            if p.check_name == "linter" and p.error_signature == "S608"
        ]
        assert linter_patterns, (
            f"expected a linter:S608 pattern, got "
            f"{[(p.check_name, p.error_signature) for p in patterns]}"
        )
        assert linter_patterns[0].frequency == 2
        review_patterns = [
            p for p in patterns
            if p.check_name == "review" and p.error_signature == "scope_creep"
        ]
        assert review_patterns

        # Proposals trace back to the recorded signature: the S608
        # linter fast path fires, and the review proposal derives from
        # the finding taxonomy.
        proposals = journal.propose_improvements(patterns)
        s608 = [p for p in proposals if "S608" in p.title]
        assert s608 and s608[0].target == "claude_md"
        assert any("S608" in src for src in s608[0].source_patterns)
        assert any("scope_creep" in p.title for p in proposals)

        # Concern hit rate is nonzero because findings_summary carries
        # the scope_creep finding.
        hit_rate = journal.get_concern_hit_rate()
        assert hit_rate["with_concern"] > 0
        assert hit_rate["by_category"].get("scope_creep", 0) > 0


class TestProposalIdMonotonicity:
    def test_ids_continue_across_invocations(self, tmp_path: Path) -> None:
        """R6.2: a second `evolve` run continues numbering after the
        files already on disk and never clobbers them."""
        config = EvolutionConfig()
        journal = EvolutionJournal(config)
        output_dir = tmp_path / "proposals"

        def _pattern(sig: str) -> FailurePattern:
            return FailurePattern(
                description=f"linter failure '{sig}' in 2/4 components",
                frequency=2, total_components=4,
                affected_components=["a", "b"],
                check_name="linter", error_signature=sig,
                category="verification",
            )

        first = journal.propose_improvements(
            [_pattern("S608")],
            starting_number=journal.next_proposal_number(output_dir),
        )
        assert first[0].id == "PROP-001"
        journal.save_proposals(first, output_dir)
        first_content = (output_dir / "prop-001.md").read_text()

        second = journal.propose_improvements(
            [_pattern("E501")],
            starting_number=journal.next_proposal_number(output_dir),
        )
        assert second[0].id == "PROP-002"
        written = journal.save_proposals(second, output_dir)
        assert [p.name for p in written] == ["prop-002.md"]
        # Prior file untouched.
        assert (output_dir / "prop-001.md").read_text() == first_content

    def test_save_never_clobbers_existing_file(self, tmp_path: Path) -> None:
        config = EvolutionConfig()
        journal = EvolutionJournal(config)
        output_dir = tmp_path / "proposals"
        output_dir.mkdir()
        (output_dir / "prop-001.md").write_text("# PROP-001: original\n")

        clashing = journal.propose_improvements([
            FailurePattern(
                description="linter failure 'E501' in 2/4 components",
                frequency=2, total_components=4,
                affected_components=["a", "b"],
                check_name="linter", error_signature="E501",
                category="verification",
            ),
        ])
        written = journal.save_proposals(clashing, output_dir)
        assert written == []
        assert (output_dir / "prop-001.md").read_text() == "# PROP-001: original\n"
