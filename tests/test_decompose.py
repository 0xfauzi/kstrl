"""Tests for decompose module."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from ralph_py.decompose import (
    SpecBlockerError,
    _extract_json,
    _parse_spec_issues,
    _validate_decompose_output,
    decompose_spec,
)
from ralph_py.prd import PRD
from ralph_py.ui.plain import PlainUI


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
        (tmp_path / "scripts" / "ralph").mkdir(parents=True)

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
        (tmp_path / "scripts" / "ralph").mkdir(parents=True)

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

        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)

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
        db_prd = tmp_path / "scripts" / "ralph" / "feature" / "database" / "prd.json"
        assert db_prd.exists()
        prd = PRD.load(db_prd)
        assert len(prd.user_stories) == 1
        assert prd.user_stories[0].id == "US-001"

        # Verify manifest was saved
        manifest_path = tmp_path / "scripts" / "ralph" / "manifest.json"
        assert manifest_path.exists()

    def test_single_pr_mode_uses_shared_branch(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Feature")

        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)

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

        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)

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

        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)

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

        ralph_dir = tmp_path / "scripts" / "ralph"
        ralph_dir.mkdir(parents=True)

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
