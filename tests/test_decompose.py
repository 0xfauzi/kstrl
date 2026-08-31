"""Tests for decompose module."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl.decompose import (
    SPEC_ISSUES_EVENT,
    ExcludedHistory,
    SpecBlockerError,
    SpecConvergence,
    SpecIssue,
    _build_convergence,
    _counted_audits,
    _excluded_history,
    _excluded_lines,
    _excluded_projects,
    _extract_json,
    _issue_dicts,
    _journal_snapshot,
    _parse_spec_issues,
    _spec_audits,
    _stored_issues,
    _validate_decompose_output,
    _windowed_audits,
    decompose_spec,
)
from kstrl.evolution import EvolutionConfig, EvolutionJournal
from kstrl.prd import PRD
from kstrl.ui.plain import PlainUI


class MockDecomposeAgent:
    """Mock agent that returns predetermined JSON output."""

    def __init__(self, output: str):
        self._output = output
        self._final_message: str | None = None

    @property
    def name(self) -> str:
        return "mock-decompose"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        yield from self._output.splitlines()
        if self._output.strip():
            self._final_message = self._output.splitlines()[-1]

    @property
    def final_message(self) -> str | None:
        return self._final_message


VALID_DECOMPOSE_OUTPUT = json.dumps(
    {
        "components": [
            {
                "id": "database",
                "title": "Database Schema",
                "description": "Create the database tables",
                "dependencies": [],
                "allowedPaths": [
                    "src/",
                    "tests/",
                    "scripts/kstrl/feature/database/",
                ],
                "userStories": [
                    {
                        "id": "US-001",
                        "title": "Create users table",
                        "acceptanceCriteria": ["Users table exists", "Tests pass"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            },
            {
                "id": "api",
                "title": "API Endpoints",
                "description": "Create REST API endpoints",
                "dependencies": ["database"],
                "allowedPaths": [
                    "src/",
                    "tests/",
                    "scripts/kstrl/feature/api/",
                ],
                "userStories": [
                    {
                        "id": "US-002",
                        "title": "GET /users endpoint",
                        "acceptanceCriteria": ["Returns user list", "Tests pass"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }
                ],
            },
        ]
    }
)


class TestExtractJson:
    """Tests for _extract_json."""

    def test_plain_json(self) -> None:
        result = _extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_with_whitespace(self) -> None:
        result = _extract_json('  \n  {"key": "value"}  \n  ')
        assert result == {"key": "value"}

    def test_json_in_code_fence(self) -> None:
        text = '```json\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_json_in_plain_code_fence(self) -> None:
        text = '```\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_json_with_surrounding_text(self) -> None:
        text = 'Here is the output:\n{"key": "value"}\nDone.'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_no_json_raises(self) -> None:
        with pytest.raises(ValueError, match="No valid JSON"):
            _extract_json("no json here")

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="No valid JSON"):
            _extract_json("{invalid json}")

    def test_nested_json(self) -> None:
        data = {"components": [{"id": "test", "nested": {"a": 1}}]}
        result = _extract_json(json.dumps(data))
        assert result == data


class TestValidateDecomposeOutput:
    """Tests for _validate_decompose_output."""

    def test_valid_output(self) -> None:
        data = json.loads(VALID_DECOMPOSE_OUTPUT)
        assert _validate_decompose_output(data) == []

    def test_not_a_dict(self) -> None:
        errors = _validate_decompose_output("not a dict")
        assert any("object" in e for e in errors)

    def test_missing_components(self) -> None:
        errors = _validate_decompose_output({})
        assert any("components" in e for e in errors)

    def test_components_not_array(self) -> None:
        errors = _validate_decompose_output({"components": "not array"})
        assert any("array" in e for e in errors)

    def test_empty_components(self) -> None:
        errors = _validate_decompose_output({"components": []})
        assert any("empty" in e for e in errors)

    def test_duplicate_component_id(self) -> None:
        data = {
            "components": [
                {
                    "id": "same",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "userStories": [],
                },
                {
                    "id": "same",
                    "title": "B",
                    "description": "B",
                    "dependencies": [],
                    "userStories": [],
                },
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("duplicate" in e.lower() for e in errors)

    def test_unknown_dependency(self) -> None:
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": ["nonexistent"],
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("nonexistent" in e for e in errors)

    def test_allowed_paths_required(self) -> None:
        """DECOMPOSE_PROMPT v1.2.0+ requires allowedPaths on every
        component. The architect output gate rejects emissions that
        omit it; the diff-scope check would otherwise be silently
        disabled at Phase 1."""
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("allowedPaths" in e and "required" in e for e in errors)

    def test_allowed_paths_must_be_array(self) -> None:
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "allowedPaths": "src/",
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("allowedPaths" in e and "array" in e for e in errors)

    def test_allowed_paths_empty_rejected(self) -> None:
        """An empty allowedPaths silently disables the diff-scope check
        which is worse than not setting it at all; reject explicitly."""
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "allowedPaths": [],
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("allowedPaths" in e and "non-empty" in e for e in errors)

    def test_allowed_paths_non_string_item_rejected(self) -> None:
        data = {
            "components": [
                {
                    "id": "a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "allowedPaths": ["src/", 42],
                    "userStories": [],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert any("allowedPaths" in e for e in errors)

    def test_allowed_paths_valid(self) -> None:
        # userStories must be non-empty since R1.8's vacuous-PRD gate,
        # so this fixture carries one real story.
        data = {
            "components": [
                {
                    "id": "comp-a",
                    "title": "A",
                    "description": "A",
                    "dependencies": [],
                    "allowedPaths": [
                        "src/",
                        "tests/",
                        "scripts/kstrl/feature/comp-a/",
                    ],
                    "userStories": [
                        {
                            "id": "US-001",
                            "title": "S1",
                            "acceptanceCriteria": ["AC1", "AC2"],
                            "priority": 1,
                            "passes": False,
                            "notes": "",
                        }
                    ],
                }
            ]
        }
        errors = _validate_decompose_output(data)
        assert errors == []


class TestSpecIssues:
    """Tests for the red-team / spec-audit surface."""

    def test_parse_typed_issues(self) -> None:
        data = {
            "spec_issues": [
                {
                    "severity": "blocker",
                    "kind": "ambiguity",
                    "summary": "What 'fast' means is not defined",
                    "location": "Performance section",
                    "suggestion": "Specify a P95 latency budget",
                },
                {
                    "severity": "major",
                    "kind": "undefined_failure_mode",
                    "summary": "No error path for db unavailable",
                },
            ],
        }
        issues = _parse_spec_issues(data)
        assert len(issues) == 2
        assert issues[0].severity == "blocker"
        assert issues[1].kind == "undefined_failure_mode"

    def test_invalid_severity_dropped(self) -> None:
        data = {
            "spec_issues": [
                {
                    "severity": "critical",  # not valid
                    "kind": "ambiguity",
                    "summary": "x",
                }
            ]
        }
        assert _parse_spec_issues(data) == []

    def test_invalid_kind_dropped(self) -> None:
        data = {
            "spec_issues": [
                {
                    "severity": "major",
                    "kind": "made_up_kind",
                    "summary": "x",
                }
            ]
        }
        assert _parse_spec_issues(data) == []

    def test_missing_summary_dropped(self) -> None:
        data = {
            "spec_issues": [
                {
                    "severity": "minor",
                    "kind": "ambiguity",
                    "summary": "",
                }
            ]
        }
        assert _parse_spec_issues(data) == []

    def test_empty_components_allowed_when_blocker_exists(self) -> None:
        data = {
            "components": [],
            "spec_issues": [
                {
                    "severity": "blocker",
                    "kind": "ambiguity",
                    "summary": "spec is too vague",
                }
            ],
        }
        assert _validate_decompose_output(data) == []

    def test_empty_components_rejected_without_blockers(self) -> None:
        data = {"components": []}
        errors = _validate_decompose_output(data)
        assert errors
        assert "components" in errors[0]

    def test_decompose_raises_on_blocker(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec\nDo something good.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "spec_issues": [
                    {
                        "severity": "blocker",
                        "kind": "ambiguity",
                        "summary": "Spec is empty",
                        "location": "everywhere",
                        "suggestion": "Write actual requirements",
                    }
                ],
                "components": [],
            }
        )
        agent = MockDecomposeAgent(output)
        ui = PlainUI(no_color=True)
        with pytest.raises(SpecBlockerError) as exc_info:
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=ui,
                root_dir=tmp_path,
            )
        assert len(exc_info.value.issues) == 1
        assert exc_info.value.issues[0].severity == "blocker"

    def test_decompose_continues_on_non_blockers(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps(
            {
                "spec_issues": [
                    {
                        "severity": "minor",
                        "kind": "missing_detail",
                        "summary": "Edge case unspecified",
                    }
                ],
                "components": [
                    {
                        "id": "comp-a",
                        "title": "A",
                        "description": "x",
                        "dependencies": [],
                        "allowedPaths": [
                            "src/",
                            "tests/",
                            "scripts/kstrl/feature/comp-a/",
                        ],
                        "userStories": [
                            {
                                "id": "US-001",
                                "title": "S1",
                                "acceptanceCriteria": ["AC1", "AC2"],
                                "priority": 1,
                                "passes": False,
                                "notes": "",
                            }
                        ],
                    },
                ],
            }
        )
        agent = MockDecomposeAgent(output)
        ui = PlainUI(no_color=True)
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
        )
        assert len(manifest.components) == 1
        assert manifest.components[0].id == "comp-a"


class TestDecomposeSpec:
    """Tests for decompose_spec end-to-end."""

    def test_successful_decomposition(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# My Feature\nBuild a user management system.")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        ui = PlainUI(no_color=True)

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test-project",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
        )

        assert len(manifest.components) == 2
        assert manifest.components[0].id == "database"
        assert manifest.components[1].id == "api"
        assert manifest.components[1].dependencies == ["database"]
        assert manifest.project_name == "test-project"

        # Verify PRD files were created
        db_prd = tmp_path / "scripts" / "kstrl" / "feature" / "database" / "prd.json"
        assert db_prd.exists()
        prd = PRD.load(db_prd)
        assert len(prd.user_stories) == 1
        assert prd.user_stories[0].id == "US-001"

        # Verify manifest was saved
        manifest_path = tmp_path / "scripts" / "kstrl" / "manifest.json"
        assert manifest_path.exists()

    def test_single_pr_mode_uses_shared_branch(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        ui = PlainUI(no_color=True)

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="my-project",
            base_branch="main",
            single_pr=True,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
        )

        # All components should share the same branch
        branches = {c.branch_name for c in manifest.components}
        assert len(branches) == 1
        assert "my-project" in branches.pop()

    def test_multi_pr_mode_uses_separate_branches(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        ui = PlainUI(no_color=True)

        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=ui,
            root_dir=tmp_path,
        )

        branches = {c.branch_name for c in manifest.components}
        assert len(branches) == 2
        assert any("database" in b for b in branches)
        assert any("api" in b for b in branches)

    def test_retries_on_invalid_json(self, tmp_path: Path) -> None:
        """Agent returns invalid output first, then valid."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        call_count = 0

        class RetryAgent:
            @property
            def name(self) -> str:
                return "retry-mock"

            def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    yield "not valid json"
                else:
                    yield VALID_DECOMPOSE_OUTPUT

            @property
            def final_message(self) -> str | None:
                return None

        ui = PlainUI(no_color=True)
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=RetryAgent(),
            ui=ui,
            root_dir=tmp_path,
        )

        assert call_count == 2
        assert len(manifest.components) == 2

    def test_fails_after_max_retries(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        kstrl_dir = tmp_path / "scripts" / "kstrl"
        kstrl_dir.mkdir(parents=True)

        agent = MockDecomposeAgent("always invalid")
        ui = PlainUI(no_color=True)

        with pytest.raises(ValueError, match="Failed to decompose"):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=ui,
                root_dir=tmp_path,
                max_retries=2,
            )


class SequenceAgent:
    """Agent returning one canned output per invocation, recording prompts."""

    def __init__(self, outputs: list[str]):
        self._outputs = outputs
        self._final_message: str | None = None
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "sequence-agent"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        self.prompts.append(prompt)
        output = self._outputs[min(len(self.prompts) - 1, len(self._outputs) - 1)]
        self._final_message = output
        yield from output.splitlines()

    @property
    def final_message(self) -> str | None:
        return self._final_message


def _story(**overrides: object) -> dict[str, object]:
    story: dict[str, object] = {
        "id": "US-001",
        "title": "S1",
        "acceptanceCriteria": ["AC1", "AC2"],
        "priority": 1,
        "passes": False,
        "notes": "",
    }
    story.update(overrides)
    return story


def _single_component_output(
    stories: list[dict[str, object]],
    spec_issues: list[dict[str, object]] | None = None,
) -> str:
    payload: dict[str, object] = {
        "components": [
            {
                "id": "comp-a",
                "title": "A",
                "description": "x",
                "dependencies": [],
                "allowedPaths": [
                    "src/",
                    "tests/",
                    "scripts/kstrl/feature/comp-a/",
                ],
                "userStories": stories,
            }
        ],
    }
    if spec_issues is not None:
        payload["spec_issues"] = spec_issues
    return json.dumps(payload)


class TestVacuousPrdRejection:
    """R1.8: vacuous shapes that previously sailed through validation."""

    def test_empty_user_stories_rejected(self) -> None:
        data = json.loads(_single_component_output([]))
        errors = _validate_decompose_output(data)
        assert any("userStories" in e and "must not be empty" in e for e in errors)

    def test_empty_acceptance_criteria_rejected(self) -> None:
        data = json.loads(_single_component_output([_story(acceptanceCriteria=[])]))
        errors = _validate_decompose_output(data)
        assert any("acceptanceCriteria" in e and "must not be empty" in e for e in errors)

    def test_passes_true_rejected(self) -> None:
        data = json.loads(_single_component_output([_story(passes=True)]))
        errors = _validate_decompose_output(data)
        assert any("passes" in e and "must be false" in e for e in errors)

    def test_vacuous_output_is_retryable(self, tmp_path: Path) -> None:
        """passes:true fails attempt 1; the retry prompt carries the
        error and attempt 2 succeeds."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        agent = SequenceAgent(
            [
                _single_component_output([_story(passes=True)]),
                _single_component_output([_story()]),
            ]
        )
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
        )

        assert len(agent.prompts) == 2
        assert "PREVIOUS ATTEMPT FAILED" in agent.prompts[1]
        assert "passes" in agent.prompts[1]
        assert len(manifest.components) == 1


BLOCKER_ISSUE: dict[str, object] = {
    "severity": "blocker",
    "kind": "ambiguity",
    "summary": "What 'fast' means is not defined",
    "location": "Performance section",
    "suggestion": "Specify a P95 latency budget",
}

MINOR_ISSUE: dict[str, object] = {
    "severity": "minor",
    "kind": "missing_detail",
    "summary": "Edge case unspecified",
    "location": "API section",
    "suggestion": "Document the empty-input path",
}


def _run_decompose(
    tmp_path: Path,
    output: str,
    *,
    spec_name: str = "spec.md",
    project_name: str = "test",
) -> str:
    """Decompose a spec against a mock agent; returns the UI output.

    A blocker halt is swallowed, because what decompose printed and
    wrote before raising is what these tests are about.
    """
    spec_file = tmp_path / spec_name
    spec_file.write_text("# Spec\nBuild it.")
    (tmp_path / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    try:
        decompose_spec(
            spec_path=spec_file,
            project_name=project_name,
            base_branch="main",
            single_pr=False,
            agent=MockDecomposeAgent(output),
            ui=PlainUI(no_color=True, file=buffer),
            root_dir=tmp_path,
        )
    except SpecBlockerError:
        pass
    return buffer.getvalue()


def _journal_with(tmp_path: Path, entries: list[dict[str, object]]) -> EvolutionJournal:
    """A real journal on disk holding ``entries``, written its own way."""
    journal = EvolutionJournal(EvolutionConfig.load(tmp_path))
    journal.append_entries(entries)
    return journal


def _read_spec_issue_events(tmp_path: Path) -> list[dict[str, object]]:
    journal = tmp_path / ".kstrl" / "evolution.jsonl"
    assert journal.exists(), "journal event was not written"
    entries = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    return [e for e in entries if e.get("event_type") == "spec_issues"]


class TestSpecIssuesPersistence:
    """R1.7: red-team output becomes a durable artifact + journal event."""

    def _run(self, tmp_path: Path, output: str) -> Path:
        _run_decompose(tmp_path, output)
        return tmp_path / "scripts" / "kstrl" / "spec-issues.json"

    def test_artifact_written_on_halt(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps({"components": [], "spec_issues": [BLOCKER_ISSUE]})
        with pytest.raises(SpecBlockerError) as exc_info:
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(output),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
            )

        artifact = tmp_path / "scripts" / "kstrl" / "spec-issues.json"
        assert artifact.exists()
        assert exc_info.value.artifact_path == artifact

        content = json.loads(artifact.read_text())
        assert content["project"] == "test"
        assert content["specFile"] == "spec.md"
        assert content["halted"] is True
        assert content["counts"] == {"blocker": 1, "major": 0, "minor": 0}
        assert content["issues"] == [
            {
                "severity": "blocker",
                "kind": "ambiguity",
                "summary": "What 'fast' means is not defined",
                "location": "Performance section",
                "suggestion": "Specify a P95 latency budget",
            }
        ]

    def test_artifact_written_on_success(self, tmp_path: Path) -> None:
        artifact = self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[MINOR_ISSUE]),
        )
        assert artifact.exists()
        content = json.loads(artifact.read_text())
        assert content["halted"] is False
        assert content["counts"] == {"blocker": 0, "major": 0, "minor": 1}
        assert content["issues"][0]["summary"] == "Edge case unspecified"
        assert content["issues"][0]["location"] == "API section"

    def test_artifact_written_on_clean_audit(self, tmp_path: Path) -> None:
        """An empty issues array is the record that the audit ran and
        found nothing - distinct from no record at all."""
        artifact = self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[]),
        )
        assert artifact.exists()
        content = json.loads(artifact.read_text())
        assert content["halted"] is False
        assert content["issues"] == []

    def test_journal_event_on_halt(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Vague spec")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        output = json.dumps({"components": [], "spec_issues": [BLOCKER_ISSUE]})
        with pytest.raises(SpecBlockerError):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(output),
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
            )

        events = _read_spec_issue_events(tmp_path)
        assert len(events) == 1
        assert events[0]["halted"] is True
        assert events[0]["counts"] == {"blocker": 1, "major": 0, "minor": 0}
        assert events[0]["artifact"] == "scripts/kstrl/spec-issues.json"

    def test_journal_event_on_success(self, tmp_path: Path) -> None:
        self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[MINOR_ISSUE]),
        )
        events = _read_spec_issue_events(tmp_path)
        assert len(events) == 1
        assert events[0]["halted"] is False
        assert events[0]["counts"] == {"blocker": 0, "major": 0, "minor": 1}


class TestPrdValidationInsideRetryLoop:
    """R1.8: PRD schema errors are retryable and never leave partial files."""

    def test_malformed_story_triggers_retry(self, tmp_path: Path) -> None:
        """A story missing the 'notes' key passes decompose-output
        validation but fails PRD schema validation; the error must feed
        back through the retry loop instead of crashing after it."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        malformed = _story()
        del malformed["notes"]
        agent = SequenceAgent(
            [
                _single_component_output([malformed]),
                _single_component_output([_story()]),
            ]
        )
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
        )

        assert len(agent.prompts) == 2
        assert "PREVIOUS ATTEMPT FAILED" in agent.prompts[1]
        assert "notes" in agent.prompts[1]
        assert len(manifest.components) == 1
        prd_path = tmp_path / "scripts" / "kstrl" / "feature" / "comp-a" / "prd.json"
        assert prd_path.exists()
        assert PRD.load(prd_path).user_stories[0].id == "US-001"

    def test_no_partial_files_after_terminal_failure(self, tmp_path: Path) -> None:
        """Terminal validation failure must not leave prd.json, feature
        dirs, or a manifest behind."""
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        malformed = _story()
        del malformed["notes"]
        agent = MockDecomposeAgent(_single_component_output([malformed]))
        with pytest.raises(ValueError, match="Failed to decompose"):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
                max_retries=2,
            )

        assert not (tmp_path / "scripts" / "kstrl" / "feature").exists()
        assert not (tmp_path / "scripts" / "kstrl" / "manifest.json").exists()
        assert list(tmp_path.rglob("prd.json")) == []

    def test_write_failure_cleans_up_partial_prds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If writing component 2's PRD fails, component 1's already
        written PRD and the directories created for it are removed; the
        spec-issues audit artifact survives."""
        import kstrl.decompose as decompose_mod

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True)

        real_generate = decompose_mod._generate_component_prd
        calls: list[str] = []

        def flaky_generate(comp_data: dict[str, object], root_dir: Path, branch_name: str) -> Path:
            calls.append(str(comp_data["id"]))
            if len(calls) == 2:
                raise OSError("disk full")
            return real_generate(comp_data, root_dir, branch_name)  # type: ignore[arg-type]

        monkeypatch.setattr(decompose_mod, "_generate_component_prd", flaky_generate)

        agent = MockDecomposeAgent(VALID_DECOMPOSE_OUTPUT)
        with pytest.raises(OSError, match="disk full"):
            decompose_spec(
                spec_path=spec_file,
                project_name="test",
                base_branch="main",
                single_pr=False,
                agent=agent,
                ui=PlainUI(no_color=True),
                root_dir=tmp_path,
            )

        assert calls == ["database", "api"]
        assert list(tmp_path.rglob("prd.json")) == []
        assert not (tmp_path / "scripts" / "kstrl" / "feature").exists()
        assert not (tmp_path / "scripts" / "kstrl" / "manifest.json").exists()
        # The audit artifact is deliberately kept.
        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()


class TestSpecConvergenceReport:
    """#260: what this audit says about the previous one."""

    def _issue(self, severity: str, kind: str, summary: str) -> SpecIssue:
        return SpecIssue(severity=severity, kind=kind, summary=summary)

    def _entry(
        self,
        issues: list[SpecIssue] | None,
        spec_file: str = "spec.md",
    ) -> dict[str, object]:
        """One prior journal entry, in the shape decompose writes."""
        entry: dict[str, object] = {"event_type": "spec_issues", "spec_file": spec_file}
        if issues is not None:
            entry["issues"] = _issue_dicts(issues)
        return entry

    def test_first_run_has_nothing_to_compare(self) -> None:
        assert _build_convergence([self._issue("blocker", "ambiguity", "a")], "spec.md", []) is None

    def test_entry_without_an_issue_list_is_not_a_comparison(self) -> None:
        """An entry that cannot be counted is not evidence, and a
        journal holding only such entries reads as no history."""
        assert _build_convergence([], "spec.md", [self._entry(None)]) is None

    def test_counts_and_deltas_against_the_previous_run(self) -> None:
        report = _build_convergence(
            [
                self._issue("blocker", "ambiguity", "new one"),
                self._issue("blocker", "contradiction", "another"),
                self._issue("minor", "other", "small"),
            ],
            "spec.md",
            [self._entry([self._issue("blocker", "ambiguity", "old one")])],
        )

        assert report is not None
        assert report.current_counts == {"blocker": 2, "major": 0, "minor": 1}
        assert report.previous_counts == {"blocker": 1, "major": 0, "minor": 0}
        assert report.previous_total == 1

    def test_repeats_match_on_normalized_text(self) -> None:
        """Collapsed whitespace and folded case still match, and so does
        a changed severity; different wording does not."""
        report = _build_convergence(
            [
                self._issue("major", "ambiguity", "What   'FAST' means\nis not defined"),
                self._issue("blocker", "ambiguity", "What fast means is undefined"),
            ],
            "spec.md",
            [
                self._entry(
                    [
                        self._issue("blocker", "ambiguity", "What 'fast' means is not defined"),
                        self._issue("minor", "other", "gone"),
                    ]
                )
            ],
        )

        assert report is not None
        assert report.repeated == 1
        assert report.previous_total == 2

    def test_same_summary_under_a_different_kind_is_not_a_repeat(self) -> None:
        report = _build_convergence(
            [self._issue("blocker", "contradiction", "same words")],
            "spec.md",
            [self._entry([self._issue("blocker", "ambiguity", "same words")])],
        )

        assert report is not None
        assert report.repeated == 0

    def test_trend_spans_every_recorded_run_and_ends_with_this_one(self) -> None:
        history = [
            self._entry([self._issue("blocker", "ambiguity", f"r1-{n}") for n in range(7)]),
            self._entry([self._issue("blocker", "ambiguity", f"r2-{n}") for n in range(11)]),
            self._entry([self._issue("blocker", "ambiguity", "r3-0")]),
            self._entry([self._issue("blocker", "ambiguity", f"r4-{n}") for n in range(3)]),
        ]

        report = _build_convergence(
            [self._issue("blocker", "ambiguity", f"r5-{n}") for n in range(4)],
            "spec-slice-1.md",
            history,
        )

        assert report is not None
        assert report.blocker_trend == (7, 11, 1, 3, 4)

    def test_previous_spec_file_is_carried_for_the_rename_case(self) -> None:
        report = _build_convergence(
            [],
            "spec-slice-1.md",
            [self._entry([], spec_file="spec.md")],
        )

        assert report is not None
        assert report.previous_spec_file == "spec.md"
        assert report.current_spec_file == "spec-slice-1.md"

    def test_malformed_stored_issues_do_not_crash_the_reader(self) -> None:
        """Journals written by older versions, and any entry an
        operator hand-edited, must read rather than raise."""
        history: list[dict[str, object]] = [
            {
                "event_type": "spec_issues",
                "issues": [
                    "not a dict",
                    {"severity": "blocker"},
                    {"summary": None, "kind": 7, "severity": "major"},
                ],
            }
        ]

        report = _build_convergence([], "spec.md", history)

        assert report is not None
        assert report.previous_counts == {"blocker": 1, "major": 1, "minor": 0}
        assert report.previous_spec_file == ""

    def test_two_current_issues_matching_one_previous_cannot_overcount(self) -> None:
        """`repeated` is rendered as a statement about the previous
        run, so it must be counted over that side. Counting the current
        side let two current issues match one previous issue and made
        "did not come back" negative: `_issue_identity` drops severity
        and normalizes text, and `_parse_spec_issues` de-duplicates
        nothing, so this shape is reachable from real architect output.
        """
        report = _build_convergence(
            [
                self._issue("blocker", "ambiguity", "What fast means is undefined"),
                self._issue("major", "ambiguity", "What  FAST  means is undefined"),
            ],
            "spec.md",
            [self._entry([self._issue("blocker", "ambiguity", "What fast means is undefined")])],
        )

        assert report is not None
        assert report.previous_total == 1
        assert report.repeated == 1
        assert report.previous_total - report.repeated == 0

    def test_a_previous_issue_raised_twice_counts_twice_when_it_returns(self) -> None:
        """The mirror case, and why this counts the previous list
        rather than intersecting two identity sets: both of the
        previous run's issues did come back, so 0 of 2 did not."""
        duplicated = self._issue("blocker", "ambiguity", "same finding, said twice")
        report = _build_convergence(
            [self._issue("blocker", "ambiguity", "same finding, said twice")],
            "spec.md",
            [self._entry([duplicated, duplicated])],
        )

        assert report is not None
        assert report.previous_total == 2
        assert report.repeated == 2


class TestExcludedHistory:
    """#280: the audit history the report does not count, named."""

    def _entry(
        self,
        project: str,
        spec_file: str = "spec.md",
        event_type: str = SPEC_ISSUES_EVENT,
        timestamp: str = "2026-08-20T00:00:00Z",
    ) -> dict[str, object]:
        return {
            "event_type": event_type,
            "project": project,
            "spec_file": spec_file,
            "timestamp": timestamp,
        }

    def _history(
        self,
        journal: EvolutionJournal,
        project: str,
        spec_file: str = "mine.md",
        lookback: int = 10,
    ) -> ExcludedHistory:
        return _excluded_history(_journal_snapshot(journal)[0], project, spec_file, lookback)

    def _lines(
        self,
        journal: EvolutionJournal,
        project: str,
        spec_file: str = "mine.md",
        counted: int = 0,
    ) -> str:
        """The rendered note lines for ``project``, joined."""
        return "\n".join(
            _excluded_lines(self._history(journal, project, spec_file), project, counted)
        )

    def test_a_journal_of_one_project_excludes_no_other_project(self) -> None:
        entries = [self._entry("writers-room"), self._entry("writers-room")]

        assert _excluded_projects(entries, "writers-room", "spec.md") == ()

    def test_another_project_is_counted_with_the_files_it_read(self) -> None:
        entries = [
            self._entry("writers-room", "spec.md"),
            self._entry("writers-room", "spec.md"),
            self._entry("writers-room-slice1", "spec-slice-1.md"),
        ]

        excluded = _excluded_projects(entries, "writers-room-slice1", "spec-slice-1.md")

        assert len(excluded) == 1
        assert excluded[0].project == "writers-room"
        assert excluded[0].audits == 2
        assert excluded[0].spec_files == ("spec.md",)
        assert excluded[0].read_this_spec is False
        assert excluded[0].last_recorded == "2026-08-20T00:00:00Z"

    def test_only_spec_audits_count(self) -> None:
        """The journal carries component results and experiments too.
        Counting those would inflate the number the operator reads."""
        entries = [
            self._entry("other", event_type="component_result"),
            self._entry("other", event_type=SPEC_ISSUES_EVENT),
        ]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert [(e.project, e.audits) for e in excluded] == [("other", 1)]

    def test_an_entry_without_a_project_is_not_evidence(self) -> None:
        """An unnamed project cannot be somewhere the operator can go
        and look, so it is not history worth pointing at."""
        entries: list[dict[str, object]] = [{"event_type": SPEC_ISSUES_EVENT}]

        assert _excluded_projects(entries, "mine", "mine.md") == ()

    def test_a_json_null_project_is_not_a_project_named_none(self) -> None:
        """Round 1 of review: ``str(entry.get("project", ""))`` renders
        a JSON null as the literal "None", which then passes the
        emptiness guard and prints a phantom project. A null field is
        an absent field, and ``get_spec_issue_runs`` promises nothing
        is assumed about an entry beyond it being a JSON object."""
        entries: list[dict[str, object]] = [
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": None,
                "spec_file": None,
                "timestamp": None,
            }
        ]

        assert _excluded_projects(entries, "mine", "mine.md") == ()

    def test_a_non_string_spec_file_and_timestamp_are_dropped_not_stringified(
        self,
    ) -> None:
        """The same rule on the other two fields: a hand-edited journal
        must not put a file literally named ``None`` or ``7`` in the
        list, nor a date the operator cannot act on."""
        entries: list[dict[str, object]] = [
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": "other",
                "spec_file": 7,
                "timestamp": None,
            }
        ]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert excluded[0].spec_files == ()
        assert excluded[0].last_recorded == ""

    def test_projects_are_ordered_by_how_much_history_they_hold(self) -> None:
        entries = [
            self._entry("a"),
            self._entry("b"),
            self._entry("b"),
            self._entry("c"),
        ]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert [(e.project, e.audits) for e in excluded] == [("b", 2), ("a", 1), ("c", 1)]

    def test_a_project_that_read_this_spec_file_sorts_first(self) -> None:
        """#280's first arm: the project that audited the file this run
        audited is the strongest evidence of a plain rename, so it
        leads even though it holds the least history here."""
        entries = [self._entry("busy") for _ in range(9)] + [self._entry("renamed", "mine.md")]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert [e.project for e in excluded] == ["renamed", "busy"]
        assert excluded[0].read_this_spec is True
        assert excluded[1].read_this_spec is False

    def test_the_last_recorded_timestamp_is_the_newest_entry_in_file_order(
        self,
    ) -> None:
        """The journal is append-only, so the last entry for a project
        is its most recent audit."""
        entries = [
            self._entry("other", timestamp="2026-01-01T00:00:00Z"),
            self._entry("other", timestamp="2026-06-30T12:00:00Z"),
        ]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert excluded[0].last_recorded == "2026-06-30T12:00:00Z"

    def test_distinct_spec_files_are_deduplicated_and_sorted(self) -> None:
        entries = [
            self._entry("other", "b.md"),
            self._entry("other", "a.md"),
            self._entry("other", "b.md"),
            self._entry("other", ""),
        ]

        excluded = _excluded_projects(entries, "mine", "mine.md")

        assert excluded[0].audits == 4
        assert excluded[0].spec_files == ("a.md", "b.md")

    def test_no_journal_excludes_nothing(self) -> None:
        assert _excluded_history([], "writers-room", "spec.md", 10).is_empty

    def test_the_read_covers_the_whole_journal(self, tmp_path: Path) -> None:
        journal = _journal_with(tmp_path, [self._entry("writers-room", "spec.md")] * 2)

        assert self._lines(journal, "writers-room-slice1", "spec-slice-1.md") == (
            "Note: audits are matched by project name, and this report covers "
            "'writers-room-slice1'. This journal also records 2 spec audit(s) under "
            "'writers-room' (2 audit(s), spec.md, last 2026-08-20)."
        )

    def test_a_journal_holding_only_this_project_names_no_other(
        self,
        tmp_path: Path,
    ) -> None:
        journal = _journal_with(tmp_path, [self._entry("writers-room")] * 3)

        assert self._history(journal, "writers-room", "spec.md").projects == ()

    def test_this_projects_own_audits_are_counted_unwindowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The count the accounting line rests on. Not windowed by
        ``lookback_runs``, because a count of what the trend does not
        cover that was itself windowed would omit history silently."""
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        journal = _journal_with(tmp_path, [self._entry("writers-room")] * 6)

        assert self._history(journal, "writers-room", "spec.md", lookback=2).own_recorded == 6

    def test_a_missing_journal_file_excludes_nothing(self, tmp_path: Path) -> None:
        journal = EvolutionJournal(EvolutionConfig.load(tmp_path))

        assert self._history(journal, "writers-room", "spec.md").is_empty

    def test_a_torn_line_does_not_cost_the_note(self, tmp_path: Path) -> None:
        """The journal is append-only and a crash mid-write leaves a
        torn tail; the rest of the history still has to be readable."""
        journal = _journal_with(tmp_path, [self._entry("writers-room", "spec.md")])
        with open(journal.config.journal_path, "a", encoding="utf-8") as handle:
            handle.write('{"event_type": "spec_iss')

        assert "records 1 spec audit(s)" in self._lines(journal, "writers-room-slice1")

    def test_many_projects_are_summarised_rather_than_all_named(
        self,
        tmp_path: Path,
    ) -> None:
        """A display cap on the names, never on the count: the total
        still covers every audit the report leaves out."""
        journal = _journal_with(tmp_path, [self._entry(f"p{n}", f"s{n}.md") for n in range(6)])

        line = self._lines(journal, "mine")

        assert "records 6 spec audit(s)" in line
        assert (
            "'p0' (1 audit(s), s0.md, last 2026-08-20), "
            "'p1' (1 audit(s), s1.md, last 2026-08-20), "
            "'p2' (1 audit(s), s2.md, last 2026-08-20) and 3 more project(s)" in line
        )

    def test_every_project_that_read_this_spec_file_survives_the_cap(
        self,
        tmp_path: Path,
    ) -> None:
        """Round 1 of review: the sort put spec-file matches first but
        the cap then truncated them, so four projects that had all read
        the current spec file - a repo that split one spec across
        several names, which is #280's own shape - printed three and
        "and 1 more project(s)". The cap now applies only to projects
        that did NOT read it."""
        journal = _journal_with(
            tmp_path,
            [self._entry(f"p{n}", "mine.md") for n in range(4)]
            + [self._entry(f"q{n}", "other.md") for n in range(4)],
        )

        line = self._lines(journal, "mine")

        for name in ("p0", "p1", "p2"):
            assert f"'{name}' (1 audit(s), mine.md" in line
        assert "and 1 more project(s) that read this spec file" in line
        assert "and 1 more project(s)." in line
        assert "'q3'" not in line

    def test_many_spec_files_under_one_project_are_summarised_too(
        self,
        tmp_path: Path,
    ) -> None:
        journal = _journal_with(tmp_path, [self._entry("other", f"s{n}.md") for n in range(5)])

        assert "'other' (5 audit(s), s0.md, s1.md, s2.md and 2 more file(s), last " in (
            self._lines(journal, "mine")
        )

    def test_an_entry_with_no_timestamp_names_the_project_without_a_date(
        self,
        tmp_path: Path,
    ) -> None:
        journal = _journal_with(tmp_path, [self._entry("other", "o.md", timestamp="")])

        assert "'other' (1 audit(s), o.md)." in self._lines(journal, "mine")


class TestJournalFieldsAreReadNotStringified:
    """#280 round 2, finding 1: a null field is an absent field, on
    every site that reads one, not just the site that was patched."""

    def _run_with_history(self, tmp_path: Path, entry: dict[str, object]) -> str:
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        return _run_decompose(
            tmp_path,
            _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE]),
            spec_name="spec.md",
            project_name="mine",
        )

    def test_a_null_spec_file_is_not_a_phantom_rename(self, tmp_path: Path) -> None:
        """``str(entry.get("spec_file", ""))`` yields 'None' when the
        key is PRESENT and null, so the rename line fired comparing
        'None' with the real file: "the previous audit read None"."""
        output = self._run_with_history(
            tmp_path,
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": "mine",
                "spec_file": None,
                "timestamp": "2026-08-20T00:00:00Z",
                "issues": [{"severity": "blocker", "kind": "ambiguity", "summary": "old"}],
            },
        )

        assert "previous audit read None" not in output
        assert "Runs are matched by project name" not in output

    def test_an_unscoreable_severity_does_not_put_a_false_zero_in_the_trend(
        self,
        tmp_path: Path,
    ) -> None:
        """The worse half of the same class. ``_issue_counts`` buckets
        by severity, so seven issues stored with a null severity were
        counted as nothing: "Previous run raised 0 issue(s)" and a 0 in
        the blocker trend for a run that raised seven. The audit is now
        refused and reported instead of part-scored."""
        output = self._run_with_history(
            tmp_path,
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": "mine",
                "spec_file": "spec.md",
                "timestamp": "2026-08-20T00:00:00Z",
                "issues": [
                    {"severity": None, "kind": "ambiguity", "summary": f"old-{n}"} for n in range(7)
                ],
            },
        )

        assert "Previous run raised 0 issue(s)" not in output
        assert "0, 1 (blockers" not in output
        assert "could not be scored" in output

    def test_a_severity_outside_the_three_is_refused_by_the_rehydrator(self) -> None:
        entry: dict[str, object] = {
            "issues": [{"severity": "critical", "kind": "ambiguity", "summary": "x"}]
        }

        assert _stored_issues(entry) is None

    def test_a_well_formed_issue_list_still_rehydrates(self) -> None:
        entry: dict[str, object] = {
            "issues": [{"severity": "minor", "kind": "ambiguity", "summary": "x"}]
        }
        stored = _stored_issues(entry)

        assert stored is not None
        assert [i.severity for i in stored] == ["minor"]


class TestUnattributedAudits:
    """#280 round 2, finding 2: audits belonging to no project name."""

    def test_they_are_counted_by_a_third_bucket(self) -> None:
        """They satisfy neither ``own_recorded`` nor ``_excluded_projects``,
        so three audits on disk were reported as one."""
        entries: list[dict[str, object]] = [
            {"event_type": SPEC_ISSUES_EVENT, "project": None, "spec_file": "a.md"},
            {"event_type": SPEC_ISSUES_EVENT, "spec_file": "b.md"},
            {"event_type": SPEC_ISSUES_EVENT, "project": "other", "spec_file": "c.md"},
        ]

        history = _excluded_history(entries, "mine", "mine.md", 10)

        assert history.own_recorded == 0
        assert history.other_audits == 1
        assert history.unattributed == 2

    def test_every_spec_audit_lands_in_exactly_one_bucket(self) -> None:
        """The property the accounting docstring claims, checked rather
        than asserted in prose."""
        entries: list[dict[str, object]] = [
            {"event_type": SPEC_ISSUES_EVENT, "project": "mine"},
            {"event_type": SPEC_ISSUES_EVENT, "project": "mine"},
            {"event_type": SPEC_ISSUES_EVENT, "project": "other"},
            {"event_type": SPEC_ISSUES_EVENT, "project": None},
            {"event_type": "component_result", "project": "mine"},
        ]

        history = _excluded_history(entries, "mine", "mine.md", 10)
        audits = sum(1 for e in entries if e.get("event_type") == SPEC_ISSUES_EVENT)

        assert history.own_recorded + history.other_audits + history.unattributed == audits


class TestOneJournalRead:
    """#280 round 2, findings 6 and 7: one read, and a pin on the
    layering shortcut that read takes."""

    def _journal(self, tmp_path: Path, entries: list[dict[str, object]]) -> EvolutionJournal:
        journal = EvolutionJournal(EvolutionConfig.load(tmp_path))
        journal.append_entries(entries)
        return journal

    def test_the_journal_is_parsed_once_per_report(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The trend and the accounting used to read the same file
        twice. The call site has both in scope, so one read feeds both
        and they cannot disagree because the file moved between them."""
        import kstrl.decompose

        reads: list[Path] = []
        real = kstrl.decompose.read_progress_events

        def counting(path: Path) -> list[dict[str, object]]:
            reads.append(path)
            return real(path)

        monkeypatch.setattr(kstrl.decompose, "read_progress_events", counting)
        _run_decompose(
            tmp_path,
            _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE]),
            project_name="mine",
        )

        assert len(reads) == 1

    def test_the_windowing_rule_matches_the_journals_own(self, tmp_path: Path) -> None:
        """``_journal_snapshot`` reaches past ``EvolutionJournal`` to its
        storage path and ``_windowed_audits`` restates the window rule
        that ``get_spec_issue_runs`` owns. Both are deferrals to
        #314, and a deferral needs a mechanism: if the journal ever
        compacts, rotates or gains a second segment, or if the window
        rule changes, the two stop agreeing and this fails. Silent loss
        of the accounting is the defect #280 exists to fix.
        """
        entries: list[dict[str, object]] = [
            {
                "event_type": SPEC_ISSUES_EVENT,
                "project": "mine" if n % 2 else "other",
                "spec_file": "spec.md",
                "timestamp": f"2026-08-{n + 1:02d}T00:00:00Z",
            }
            for n in range(9)
        ]
        journal = self._journal(tmp_path, entries)

        for last_n in (0, 1, 3, 10):
            assert _windowed_audits(
                _spec_audits(_journal_snapshot(journal)[0]), "mine", last_n
            ) == journal.get_spec_issue_runs("mine", last_n=last_n)

    def test_no_journal_reads_nothing_and_windows_nothing(self) -> None:
        assert _journal_snapshot(None) == ([], 0)


class TestExcludedAccountingLine:
    """#280 round 1, finding 2: the same-project half of the accounting."""

    def _history(self, own: int, lookback: int = 10) -> ExcludedHistory:
        return ExcludedHistory(own_recorded=own, projects=(), lookback=lookback)

    def test_nothing_is_said_when_the_trend_counted_everything(self) -> None:
        assert _excluded_lines(self._history(3), "mine", 3) == []

    def test_a_windowed_out_audit_is_a_trend_footnote_not_a_warning(self) -> None:
        """Round 2 of review: once a project has more audits than
        ``lookback_runs`` this holds on every run forever, so a Note
        would be permanent noise. It is a footnote on the trend line
        instead; see ``_surface_trend``."""
        history = self._history(40, lookback=10)

        assert _excluded_lines(history, "mine", 10) == []
        assert history.windowed_out(10) == 30
        assert history.unreadable(10) == 0

    def test_an_audit_the_window_offered_but_could_not_be_scored_is_named(self) -> None:
        """The anomaly half of the same gap, which does deserve a line."""
        history = self._history(3, lookback=10)

        assert history.unreadable(0) == 3
        assert _excluded_lines(history, "mine", 0) == [
            "Note: 3 earlier audit(s) of 'mine' fall inside the lookback window but "
            "could not be scored, so the trend does not count them. An audit is "
            "skipped when it records no issue list, or an issue whose severity is "
            "not blocker, major or minor."
        ]

    def test_the_two_causes_are_separated_when_both_apply(self) -> None:
        history = self._history(40, lookback=10)

        assert history.unreadable(7) == 3
        assert history.windowed_out(7) == 30

    def test_audits_with_no_project_name_are_their_own_line(self) -> None:
        """Round 2 of review: an entry whose ``project`` is null or
        absent was counted by neither axis, so three audits on disk
        were reported as one."""
        history = ExcludedHistory(own_recorded=0, projects=(), unattributed=2, lookback=10)

        assert _excluded_lines(history, "mine", 0) == [
            "Note: 2 spec audit(s) in this journal record no project name, so neither "
            "the trend nor the line above counts them."
        ]
        assert not history.is_empty

    def test_counted_audits_is_read_off_the_rendered_trend(self) -> None:
        """So the accounting line can never disagree with the trend
        printed directly above it."""
        report = SpecConvergence(
            current_counts={"blocker": 0, "major": 0, "minor": 0},
            previous_counts={"blocker": 0, "major": 0, "minor": 0},
            current_spec_file="spec.md",
            previous_spec_file="spec.md",
            repeated=0,
            blocker_trend=(1, 1, 0),
        )

        assert _counted_audits(report) == 2
        assert _counted_audits(None) == 0


class TestSpecConvergenceThroughDecompose:
    """The report as the operator meets it, on the real code path."""

    def _run(
        self,
        tmp_path: Path,
        issues: list[dict[str, object]],
        spec_name: str = "spec.md",
        project_name: str = "writers-room",
    ) -> str:
        return _run_decompose(
            tmp_path,
            _single_component_output([_story()], spec_issues=issues),
            spec_name=spec_name,
            project_name=project_name,
        )

    def test_first_run_prints_no_report(self, tmp_path: Path) -> None:
        output = self._run(tmp_path, [BLOCKER_ISSUE])
        assert "Spec Convergence" not in output

    def test_second_run_compares_against_the_first(self, tmp_path: Path) -> None:
        """Also pins the ordering: the history is read before this run
        is appended, so "previous run" is never this run."""
        self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(
            tmp_path,
            [
                BLOCKER_ISSUE,
                {
                    "severity": "blocker",
                    "kind": "contradiction",
                    "summary": "Stage is recorded twice",
                    "location": "",
                    "suggestion": "",
                },
                MINOR_ISSUE,
            ],
        )

        assert "Spec Convergence" in output
        assert "Blocker:" in output
        assert "2 (previous run: 1, +1)" in output
        assert "Minor:" in output
        assert "1 (previous run: 0, +1)" in output
        assert "1, 2 (blockers, oldest run first)" in output
        assert "Previous run raised 1 issue(s): 1 reappear verbatim, 0 do not." in output

    def test_journal_entries_still_carry_no_run_id(self, tmp_path: Path) -> None:
        """The report reads entries the run-windowed reader drops; if a
        run_id ever appears here, that reader would start windowing
        spec audits by factory run and this feature would go quiet."""
        self._run(tmp_path, [BLOCKER_ISSUE])
        events = _read_spec_issue_events(tmp_path)

        assert events and all("run_id" not in e for e in events)

    def test_a_legacy_entry_without_run_id_does_not_break_the_read(
        self,
        tmp_path: Path,
    ) -> None:
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps({"event_type": "component_result", "component": "legacy"}) + "\n"
        )

        self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert "1 (previous run: 1, no change)" in output
        assert "1, 1 (blockers, oldest run first)" in output

    def test_a_rename_is_reported_rather_than_hidden(self, tmp_path: Path) -> None:
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec.md")
        output = self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec-slice-1.md")

        assert "the previous audit read spec.md, this one read spec-slice-1.md" in output

    def test_a_different_project_has_its_own_history(self, tmp_path: Path) -> None:
        """Still its own trend, but no longer its own silence (#280).

        This test previously asserted the whole section was absent,
        which is exactly the loss #280 reports: the operator was told
        nothing at all about the audits the trend had just dropped.
        """
        self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="deckgen")

        assert "Trend:" not in output
        assert "No earlier audit of this project is recorded." in output
        assert "also records 1 spec audit(s)" in output
        assert "'writers-room' (1 audit(s), spec.md, last " in output

    def test_the_rename_that_lost_two_runs_now_says_so(self, tmp_path: Path) -> None:
        """#280's own shape, end to end: five audits, a project AND
        spec rename between runs 2 and 3, and a trend that covers only
        the last three. The trend is unchanged; what is new is the line
        that says the other two exist."""
        for spec, project in [
            ("spec.md", "writers-room"),
            ("spec.md", "writers-room"),
            ("spec-slice-1.md", "writers-room-slice1"),
            ("spec-slice-1.md", "writers-room-slice1"),
        ]:
            self._run(tmp_path, [BLOCKER_ISSUE], spec_name=spec, project_name=project)
        output = self._run(
            tmp_path,
            [BLOCKER_ISSUE],
            spec_name="spec-slice-1.md",
            project_name="writers-room-slice1",
        )

        assert "1, 1, 1 (blockers, oldest run first)" in output
        assert (
            "Note: audits are matched by project name, and this report covers "
            "'writers-room-slice1'. This journal also records 2 spec audit(s) under "
            "'writers-room' (2 audit(s), spec.md, last " in output
        )
        # The trend counted every audit of this project, so neither the
        # anomaly line nor the trend footnote has anything to say.
        assert "earlier audit(s) of 'writers-room-slice1'" not in output
        assert "outside the lookback window" not in output

    def test_an_ordinary_single_project_history_prints_no_note(
        self,
        tmp_path: Path,
    ) -> None:
        """The false-positive check. A warning that always fires is
        noise, and noise is how a report stops being read."""
        for _ in range(4):
            self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert "1, 1, 1, 1, 1 (blockers, oldest run first)" in output
        assert "Note:" not in output

    def test_a_genuine_two_project_repo_is_told_the_truth(self, tmp_path: Path) -> None:
        """The measured cost of keying the note on "any other project"
        rather than on a matching spec file: a repo holding two real
        projects sees the line on every decompose of either.

        Pinned rather than hidden, because it is the price of covering
        #280's own session, where the spec file was renamed at the same
        moment as the project and a spec-file match would have found
        nothing. The line names the other project and the file it read,
        so an operator on 'billing' dismisses 'auth' (auth.md) at a
        glance instead of investigating.
        """
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="auth.md", project_name="auth")
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="billing.md", project_name="billing")
        output = self._run(
            tmp_path,
            [BLOCKER_ISSUE],
            spec_name="billing.md",
            project_name="billing",
        )

        assert "1, 1 (blockers, oldest run first)" in output
        assert "this report covers 'billing'" in output
        assert "also records 1 spec audit(s) under 'auth' (1 audit(s), auth.md, last " in output

    def test_a_rename_within_one_project_prints_no_note(self, tmp_path: Path) -> None:
        """The spec file moving is already reported by the rename line;
        the note is about the OTHER half of the key and must stay out
        of it."""
        self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec.md")
        output = self._run(tmp_path, [BLOCKER_ISSUE], spec_name="spec-slice-1.md")

        assert "the previous audit read spec.md" in output
        assert "Note:" not in output

    def test_no_note_when_the_journal_is_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A disabled journal reads nothing, so it can claim nothing."""
        self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        monkeypatch.setenv("KSTRL_EVOLUTION_ENABLED", "0")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="deckgen")

        assert "Spec Convergence" not in output

    def test_the_note_is_not_windowed_by_the_lookback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``lookback_runs`` bounds how far back the trend reaches. A
        note about history the trend excludes that were itself windowed
        would omit history silently, which is the bug it fixes."""
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for _ in range(4):
            self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="deckgen")

        assert "also records 4 spec audit(s)" in output

    def test_a_windowed_out_run_is_counted_not_swallowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 1 of review, finding 2: the note counted only
        cross-project audits while its wording claimed everything the
        journal holds, so same-project audits dropped by
        ``lookback_runs`` went silently missing. That is #280's own
        defect on the other axis. Reachable on the DEFAULT lookback of
        10 after 11 audits, so not an exotic config.
        """
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for _ in range(6):
            self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")

        assert (
            "1, 1, 1 (blockers, oldest run first; 4 older audit(s) outside the "
            "lookback window)" in output
        )
        # Round 2 of review: this is the configured steady state, so it
        # qualifies the trend in place rather than firing a warning that
        # would print on every run forever.
        assert "could not be scored" not in output

    def test_no_earlier_audit_is_claimed_only_when_none_is_recorded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 1 of review, finding 1: with ``lookback_runs=0`` the
        trend reads nothing, and the report announced that no earlier
        audit of this project was recorded while three of them sat on
        disk. A confident statement over less data than the journal
        holds is the defect #280 is about.
        """
        for _ in range(3):
            self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "0")
        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")

        assert "No earlier audit of this project is recorded." not in output
        assert (
            "No earlier audit of 'mine' could be compared, though this journal records 3." in output
        )

    def test_a_legacy_entry_with_no_issue_list_is_counted_not_denied(
        self,
        tmp_path: Path,
    ) -> None:
        """The second route to the same false line: entries the trend
        cannot compare because they carry no issue list, which is the
        legacy journal shape ``_stored_issues`` exists to tolerate."""
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            "".join(
                json.dumps(
                    {
                        "event_type": SPEC_ISSUES_EVENT,
                        "project": "mine",
                        "spec_file": "spec.md",
                        "timestamp": "2026-08-20T00:00:00Z",
                        "counts": {"blocker": 1, "major": 0, "minor": 0},
                    }
                )
                + "\n"
                for _ in range(3)
            ),
            encoding="utf-8",
        )

        output = self._run(tmp_path, [BLOCKER_ISSUE], project_name="mine")

        assert "No earlier audit of this project is recorded." not in output
        assert (
            "No earlier audit of 'mine' could be compared, though this journal records 3." in output
        )
        assert (
            "Note: 3 earlier audit(s) of 'mine' fall inside the lookback window but "
            "could not be scored" in output
        )

    def test_the_journal_reader_agrees_with_the_event_name_written(
        self,
        tmp_path: Path,
    ) -> None:
        """Round 1 of review, finding 5. ``SPEC_ISSUES_EVENT`` names the
        discriminator this module writes and reads, but
        ``EvolutionJournal.get_spec_issue_runs`` hardcodes the same
        literal independently and that file is under concurrent edit on
        another branch. This is the mechanism that makes the two
        diverging impossible to ship quietly: change the constant alone
        and the trend the journal reader feeds goes empty here.
        """
        self._run(tmp_path, [BLOCKER_ISSUE], project_name="writers-room")
        runs = EvolutionJournal(EvolutionConfig.load(tmp_path)).get_spec_issue_runs("writers-room")

        assert [r["event_type"] for r in runs] == [SPEC_ISSUES_EVENT]

    def test_a_clean_audit_still_reports_the_drop(self, tmp_path: Path) -> None:
        self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [])

        assert "0 (previous run: 1, -1)" in output
        assert "1, 0 (blockers, oldest run first)" in output
        assert "Previous run raised 1 issue(s): 0 reappear verbatim, 1 do not." in output

    def test_the_window_is_the_journal_lookback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "2")
        for _ in range(3):
            self._run(tmp_path, [BLOCKER_ISSUE])
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert (
            "1, 1, 1 (blockers, oldest run first; 1 older audit(s) outside the "
            "lookback window)" in output
        )

    def test_no_report_when_the_journal_is_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._run(tmp_path, [BLOCKER_ISSUE])
        monkeypatch.setenv("KSTRL_EVOLUTION_ENABLED", "0")
        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert "Spec Convergence" not in output

    def test_the_rendered_overlap_never_goes_negative(self, tmp_path: Path) -> None:
        """The end-to-end guard on the count: two current issues that
        normalize to the previous run's single issue must not print
        "2 reappear verbatim, -1 do not"."""
        self._run(tmp_path, [BLOCKER_ISSUE])
        restated = dict(BLOCKER_ISSUE)
        restated["severity"] = "major"
        restated["summary"] = "  What   'FAST'  MEANS is not   defined  "
        output = self._run(tmp_path, [BLOCKER_ISSUE, restated])

        assert "Previous run raised 1 issue(s): 1 reappear verbatim, 0 do not." in output
        assert "-1 do not" not in output

    def test_a_bad_evolution_config_does_not_cost_the_audit_artifact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R1.7 says the artifact is written for halt, success and
        clean-audit alike. Loading the journal config happens before
        that write, so a config that will not parse must degrade to
        "no journal", never abort the audit."""
        monkeypatch.setenv("KSTRL_EVOLUTION_LOOKBACK_RUNS", "many")

        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()
        assert "Evolution config unreadable" in output
        assert "Spec Convergence" not in output

    def test_malformed_toml_does_not_cost_the_audit_artifact_either(
        self,
        tmp_path: Path,
    ) -> None:
        """The other ValueError path into EvolutionConfig.load.

        Scoped to the halt path on purpose, and the reason narrowed when
        #272 landed. It used to be that a malformed kstrl.toml ALSO
        failed LinearConfig.load further down decompose, after the
        architect had been paid for; ``ks decompose`` now rejects the
        file at command entry and never reaches this function, which
        ``tests/test_config_preflight.py`` pins. What this still covers
        is the direct call: ``decompose_spec`` invoked in-process, where
        the halt raises before the Linear load and the artifact is the
        only record the operator gets.
        """
        (tmp_path / "kstrl.toml").write_text("[evolution\nenabled = true\n")

        output = self._run(tmp_path, [BLOCKER_ISSUE])

        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()
        assert "Evolution config unreadable" in output

    def test_the_artifact_is_written_before_any_journal_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The structural version of the two tests above, independent of
        which exceptions the config guard happens to catch.

        R1.7's artifact is the only durable record on the halt path, so
        nothing that can fail belongs upstream of it. An error the guard
        does not catch still leaves the artifact on disk.
        """
        import kstrl.evolution

        def _explode(root_dir: Path | None = None) -> None:
            raise RuntimeError("journal config exploded")

        monkeypatch.setattr(kstrl.evolution.EvolutionConfig, "load", _explode)

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
        with pytest.raises(RuntimeError, match="journal config exploded"):
            decompose_spec(
                spec_path=spec_file,
                project_name="writers-room",
                base_branch="main",
                single_pr=False,
                agent=MockDecomposeAgent(
                    _single_component_output([_story()], spec_issues=[BLOCKER_ISSUE])
                ),
                ui=PlainUI(no_color=True, file=io.StringIO()),
                root_dir=tmp_path,
            )

        assert (tmp_path / "scripts" / "kstrl" / "spec-issues.json").exists()
