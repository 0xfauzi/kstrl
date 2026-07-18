"""R6.3: `ralph evolve --apply` - the minimal REAL apply path.

Convention-type proposals (computational, target claude_md) append to
the project CLAUDE.md Agent Learnings section after explicit
confirmation; everything else prints honest manual instructions.
auto_propose and auto_apply_computational are honored.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from ralph_py.cli import _append_to_agent_learnings, cli
from ralph_py.evolution import (
    EvolutionConfig,
    EvolutionJournal,
    FailurePattern,
)

CLAUDE_MD = """# Test project

## Coding standards

- type hints everywhere

## Agent Learnings

### Conventions

- existing convention
"""


def _linter_pattern(sig: str = "S608") -> FailurePattern:
    return FailurePattern(
        description=f"linter failure '{sig}' in 2/4 components",
        frequency=2,
        total_components=4,
        affected_components=["a", "b"],
        check_name="linter",
        error_signature=sig,
        category="verification",
    )


def _root_with_proposal(
    tmp_path: Path, pattern: FailurePattern | None = None,
) -> Path:
    """Project root with CLAUDE.md and one saved proposal."""
    (tmp_path / "CLAUDE.md").write_text(CLAUDE_MD)
    journal = EvolutionJournal(EvolutionConfig())
    proposals = journal.propose_improvements([pattern or _linter_pattern()])
    journal.save_proposals(proposals, tmp_path / ".ralph" / "proposals")
    return tmp_path


def _invoke(root: Path, *args: str, input: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(
        cli,
        ["evolve", *args, "--root", str(root), "--ui", "plain", "--no-color"],
        input=input,
    )
    return result.exit_code, result.output


class TestEvolveApply:
    def test_apply_appends_exactly_once_with_confirmation(
        self, tmp_path: Path,
    ) -> None:
        root = _root_with_proposal(tmp_path)

        exit_code, output = _invoke(
            root, "--apply", "PROP-001", input="y\n",
        )
        assert exit_code == 0, output
        content = (root / "CLAUDE.md").read_text()
        assert content.count("Avoid triggering linter rule S608") == 1
        assert "PROP-001" in content
        # Existing content untouched.
        assert "existing convention" in content
        # Proposal stamped as applied.
        proposal_text = (
            root / ".ralph" / "proposals" / "prop-001.md"
        ).read_text()
        assert "**Applied**:" in proposal_text

        # Second apply is a no-op: still exactly one entry.
        exit_code, output = _invoke(
            root, "--apply", "PROP-001", input="y\n",
        )
        assert exit_code == 0, output
        assert "already applied" in output
        content = (root / "CLAUDE.md").read_text()
        assert content.count("Avoid triggering linter rule S608") == 1

    def test_apply_declined_appends_nothing(self, tmp_path: Path) -> None:
        root = _root_with_proposal(tmp_path)
        exit_code, output = _invoke(
            root, "--apply", "PROP-001", input="n\n",
        )
        assert exit_code == 0, output
        assert "not applied" in output
        content = (root / "CLAUDE.md").read_text()
        assert "S608" not in content
        proposal_text = (
            root / ".ralph" / "proposals" / "prop-001.md"
        ).read_text()
        assert "**Applied**:" not in proposal_text

    def test_auto_apply_computational_skips_prompt(
        self, tmp_path: Path,
    ) -> None:
        root = _root_with_proposal(tmp_path)
        (root / "ralph.toml").write_text(
            "[evolution]\nauto_apply_computational = true\n"
        )
        # No input provided: a prompt would abort the runner.
        exit_code, output = _invoke(root, "--apply", "PROP-001")
        assert exit_code == 0, output
        content = (root / "CLAUDE.md").read_text()
        assert content.count("Avoid triggering linter rule S608") == 1

    def test_non_convention_proposal_prints_manual(
        self, tmp_path: Path,
    ) -> None:
        """typecheck proposals target pyproject: the CLI must be honest
        that only manual application exists, and touch nothing."""
        pattern = FailurePattern(
            description="typecheck failure 'arg-type' in 2/4 components",
            frequency=2, total_components=4,
            affected_components=["a", "b"],
            check_name="typecheck", error_signature="arg-type",
            category="verification",
        )
        root = _root_with_proposal(tmp_path, pattern)
        before = (root / "CLAUDE.md").read_text()
        exit_code, output = _invoke(root, "--apply", "PROP-001")
        assert exit_code == 0, output
        assert "manually" in output
        assert (root / "CLAUDE.md").read_text() == before

    def test_apply_unknown_id_errors(self, tmp_path: Path) -> None:
        root = _root_with_proposal(tmp_path)
        exit_code, output = _invoke(root, "--apply", "PROP-999")
        assert exit_code == 1
        assert "not found" in output

    def test_apply_without_agent_learnings_section_fails_honestly(
        self, tmp_path: Path,
    ) -> None:
        root = _root_with_proposal(tmp_path)
        (root / "CLAUDE.md").write_text("# No learnings section\n")
        exit_code, output = _invoke(
            root, "--apply", "PROP-001", input="y\n",
        )
        assert exit_code == 1
        assert "Agent Learnings" in output


class TestAppendToAgentLearnings:
    def test_appends_before_next_section(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "## Agent Learnings\n\n- old entry\n\n## Footer\n\ntext\n"
        )
        assert _append_to_agent_learnings(claude_md, "PROP-007", "new rule")
        content = claude_md.read_text()
        assert content.index("new rule") < content.index("## Footer")
        assert "- new rule (applied from PROP-007 by ralph evolve)" in content

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        assert not _append_to_agent_learnings(
            tmp_path / "absent.md", "PROP-001", "rule",
        )


class TestAutoPropose:
    def _journal_with_pattern(self, root: Path) -> None:
        entries = []
        for run_id in ("run-001", "run-002"):
            entries.append({
                "schema_version": 2,
                "run_id": run_id,
                "component_id": "comp-a",
                "event_type": "component_result",
                "status": "failed",
                "error": "Mechanical verification failed",
                "failure_signatures": ["linter:S608"],
                "findings_summary": {"total": 0, "by_category": {}},
            })
        journal_path = root / ".ralph" / "evolution.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n"
        )

    def test_auto_propose_false_reports_patterns_only(
        self, tmp_path: Path,
    ) -> None:
        self._journal_with_pattern(tmp_path)
        (tmp_path / "ralph.toml").write_text(
            "[evolution]\nauto_propose = false\n"
        )
        exit_code, output = _invoke(tmp_path)
        assert exit_code == 0, output
        assert "S608" in output
        assert "auto_propose is disabled" in output
        assert not (tmp_path / ".ralph" / "proposals").exists()

    def test_auto_propose_default_generates_monotonic_ids(
        self, tmp_path: Path,
    ) -> None:
        """Two `ralph evolve` invocations: the second run neither
        clobbers nor duplicates; a new pattern continues the numbering."""
        self._journal_with_pattern(tmp_path)
        exit_code, output = _invoke(tmp_path)
        assert exit_code == 0, output
        proposals_dir = tmp_path / ".ralph" / "proposals"
        assert (proposals_dir / "prop-001.md").exists()
        first_content = (proposals_dir / "prop-001.md").read_text()

        # Re-run with the same journal: same pattern, no duplicate file.
        exit_code, output = _invoke(tmp_path)
        assert exit_code == 0, output
        assert sorted(p.name for p in proposals_dir.glob("*.md")) == [
            "prop-001.md",
        ]
        assert (proposals_dir / "prop-001.md").read_text() == first_content
        assert "already exist" in output

        # A new signature appears: numbering continues at PROP-002.
        journal_path = tmp_path / ".ralph" / "evolution.jsonl"
        extra = []
        for run_id in ("run-003", "run-004"):
            extra.append({
                "schema_version": 2,
                "run_id": run_id,
                "component_id": "comp-b",
                "event_type": "component_result",
                "status": "failed",
                "error": "Mechanical verification failed",
                "failure_signatures": ["linter:E501"],
                "findings_summary": {"total": 0, "by_category": {}},
            })
        with open(journal_path, "a") as f:
            for e in extra:
                f.write(json.dumps(e) + "\n")
        exit_code, output = _invoke(tmp_path)
        assert exit_code == 0, output
        names = sorted(p.name for p in proposals_dir.glob("*.md"))
        assert names == ["prop-001.md", "prop-002.md"]
        assert "E501" in (proposals_dir / "prop-002.md").read_text()
