"""Tests for decompose module."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl.decompose import (
    SpecBlockerError,
    _extract_json,
    _parse_spec_issues,
    _validate_decompose_output,
    decompose_spec,
)
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


VALID_DECOMPOSE_OUTPUT = json.dumps({
    "components": [
        {
            "id": "database",
            "title": "Database Schema",
            "description": "Create the database tables",
            "dependencies": [],
            "allowedPaths": [
                "src/", "tests/", "scripts/kstrl/feature/database/",
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
                "src/", "tests/", "scripts/kstrl/feature/api/",
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
})


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
        assert any(
            "allowedPaths" in e and "required" in e for e in errors
        )

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
        assert any(
            "allowedPaths" in e and "non-empty" in e for e in errors
        )

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
        data = {"spec_issues": [{
            "severity": "critical",  # not valid
            "kind": "ambiguity",
            "summary": "x",
        }]}
        assert _parse_spec_issues(data) == []

    def test_invalid_kind_dropped(self) -> None:
        data = {"spec_issues": [{
            "severity": "major",
            "kind": "made_up_kind",
            "summary": "x",
        }]}
        assert _parse_spec_issues(data) == []

    def test_missing_summary_dropped(self) -> None:
        data = {"spec_issues": [{
            "severity": "minor",
            "kind": "ambiguity",
            "summary": "",
        }]}
        assert _parse_spec_issues(data) == []

    def test_empty_components_allowed_when_blocker_exists(self) -> None:
        data = {
            "components": [],
            "spec_issues": [{
                "severity": "blocker",
                "kind": "ambiguity",
                "summary": "spec is too vague",
            }],
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

        output = json.dumps({
            "spec_issues": [{
                "severity": "blocker",
                "kind": "ambiguity",
                "summary": "Spec is empty",
                "location": "everywhere",
                "suggestion": "Write actual requirements",
            }],
            "components": [],
        })
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

        output = json.dumps({
            "spec_issues": [{
                "severity": "minor",
                "kind": "missing_detail",
                "summary": "Edge case unspecified",
            }],
            "components": [
                {
                    "id": "comp-a",
                    "title": "A",
                    "description": "x",
                    "dependencies": [],
                    "allowedPaths": [
                        "src/", "tests/", "scripts/kstrl/feature/comp-a/",
                    ],
                    "userStories": [{
                        "id": "US-001",
                        "title": "S1",
                        "acceptanceCriteria": ["AC1", "AC2"],
                        "priority": 1,
                        "passes": False,
                        "notes": "",
                    }],
                },
            ],
        })
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
                    "src/", "tests/", "scripts/kstrl/feature/comp-a/",
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
        assert any(
            "userStories" in e and "must not be empty" in e for e in errors
        )

    def test_empty_acceptance_criteria_rejected(self) -> None:
        data = json.loads(
            _single_component_output([_story(acceptanceCriteria=[])])
        )
        errors = _validate_decompose_output(data)
        assert any(
            "acceptanceCriteria" in e and "must not be empty" in e
            for e in errors
        )

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

        agent = SequenceAgent([
            _single_component_output([_story(passes=True)]),
            _single_component_output([_story()]),
        ])
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


class TestSpecIssuesPersistence:
    """R1.7: red-team output becomes a durable artifact + journal event."""

    def _run(
        self,
        tmp_path: Path,
        output: str,
    ) -> Path:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nBuild it.")
        (tmp_path / "scripts" / "kstrl").mkdir(parents=True, exist_ok=True)
        decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=MockDecomposeAgent(output),
            ui=PlainUI(no_color=True),
            root_dir=tmp_path,
        )
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
            tmp_path, _single_component_output([_story()], spec_issues=[]),
        )
        assert artifact.exists()
        content = json.loads(artifact.read_text())
        assert content["halted"] is False
        assert content["issues"] == []

    def _read_journal_events(self, tmp_path: Path) -> list[dict[str, object]]:
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        assert journal.exists(), "journal event was not written"
        entries = [
            json.loads(line)
            for line in journal.read_text().splitlines()
            if line.strip()
        ]
        return [e for e in entries if e.get("event_type") == "spec_issues"]

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

        events = self._read_journal_events(tmp_path)
        assert len(events) == 1
        assert events[0]["halted"] is True
        assert events[0]["counts"] == {"blocker": 1, "major": 0, "minor": 0}
        assert events[0]["artifact"] == "scripts/kstrl/spec-issues.json"

    def test_journal_event_on_success(self, tmp_path: Path) -> None:
        self._run(
            tmp_path,
            _single_component_output([_story()], spec_issues=[MINOR_ISSUE]),
        )
        events = self._read_journal_events(tmp_path)
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
        agent = SequenceAgent([
            _single_component_output([malformed]),
            _single_component_output([_story()]),
        ])
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
        prd_path = (
            tmp_path / "scripts" / "kstrl" / "feature" / "comp-a" / "prd.json"
        )
        assert prd_path.exists()
        assert PRD.load(prd_path).user_stories[0].id == "US-001"

    def test_no_partial_files_after_terminal_failure(
        self, tmp_path: Path
    ) -> None:
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

        def flaky_generate(
            comp_data: dict[str, object], root_dir: Path, branch_name: str
        ) -> Path:
            calls.append(str(comp_data["id"]))
            if len(calls) == 2:
                raise OSError("disk full")
            return real_generate(comp_data, root_dir, branch_name)  # type: ignore[arg-type]

        monkeypatch.setattr(
            decompose_mod, "_generate_component_prd", flaky_generate
        )

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
