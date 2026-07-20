"""Tests for manifest module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kstrl.manifest import Component, ComponentStatus, Manifest


def _minimal_manifest_data(
    components: list[dict] | None = None,
) -> dict:
    """Build valid minimal manifest data."""
    return {
        "version": "1",
        "specFile": "spec.md",
        "projectName": "test-project",
        "baseBranch": "main",
        "singlePr": False,
        "components": components or [],
    }


def _component_data(
    id: str = "comp-a",
    title: str = "Component A",
    dependencies: list[str] | None = None,
    **overrides: object,
) -> dict:
    """Build valid component data."""
    data: dict = {
        "id": id,
        "title": title,
        "description": f"Description of {id}",
        "dependencies": dependencies or [],
        "prdPath": f"scripts/ralph/feature/{id}/prd.json",
        "branchName": f"ralph/factory/{id}",
    }
    data.update(overrides)
    return data


class TestManifestValidateSchema:
    """Tests for Manifest.validate_schema."""

    def test_valid_empty_components(self) -> None:
        data = _minimal_manifest_data()
        assert Manifest.validate_schema(data) == []

    def test_valid_with_components(self) -> None:
        data = _minimal_manifest_data([_component_data()])
        assert Manifest.validate_schema(data) == []

    def test_not_a_dict(self) -> None:
        errors = Manifest.validate_schema("not a dict")
        assert any("JSON object" in e for e in errors)

    def test_missing_required_key(self) -> None:
        data = _minimal_manifest_data()
        del data["projectName"]
        errors = Manifest.validate_schema(data)
        assert any("projectName" in e for e in errors)

    def test_empty_project_name(self) -> None:
        data = _minimal_manifest_data()
        data["projectName"] = ""
        errors = Manifest.validate_schema(data)
        assert any("non-empty" in e for e in errors)

    def test_empty_base_branch(self) -> None:
        data = _minimal_manifest_data()
        data["baseBranch"] = ""
        errors = Manifest.validate_schema(data)
        assert any("non-empty" in e for e in errors)

    def test_wrong_type_single_pr(self) -> None:
        data = _minimal_manifest_data()
        data["singlePr"] = "false"
        errors = Manifest.validate_schema(data)
        assert any("boolean" in e for e in errors)

    def test_components_not_array(self) -> None:
        data = _minimal_manifest_data()
        data["components"] = "not an array"
        errors = Manifest.validate_schema(data)
        assert any("array" in e for e in errors)

    def test_component_missing_key(self) -> None:
        comp = _component_data()
        del comp["title"]
        data = _minimal_manifest_data([comp])
        errors = Manifest.validate_schema(data)
        assert any("missing keys" in e for e in errors)

    def test_component_extra_key(self) -> None:
        comp = _component_data()
        comp["unexpected"] = "value"
        data = _minimal_manifest_data([comp])
        errors = Manifest.validate_schema(data)
        assert any("unexpected" in e for e in errors)

    def test_component_empty_id(self) -> None:
        comp = _component_data(id="")
        data = _minimal_manifest_data([comp])
        errors = Manifest.validate_schema(data)
        assert any("non-empty" in e for e in errors)

    def test_component_dependencies_not_array(self) -> None:
        comp = _component_data()
        comp["dependencies"] = "not-an-array"
        data = _minimal_manifest_data([comp])
        errors = Manifest.validate_schema(data)
        assert any("array" in e for e in errors)

    def test_component_with_optional_fields(self) -> None:
        comp = _component_data()
        comp["status"] = "completed"
        comp["error"] = ""
        comp["retries"] = 2
        comp["prNumber"] = 42
        comp["prUrl"] = "https://github.com/test/pr/42"
        data = _minimal_manifest_data([comp])
        assert Manifest.validate_schema(data) == []


class TestManifestValidateDAG:
    """Tests for Manifest.validate_dag."""

    def test_no_components(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [])
        assert m.validate_dag() == []

    def test_no_dependencies(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", [], "b.json", "b/b"),
        ])
        assert m.validate_dag() == []

    def test_valid_linear_chain(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
            Component("c", "C", "", ["b"], "c.json", "b/c"),
        ])
        assert m.validate_dag() == []

    def test_valid_diamond(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
            Component("c", "C", "", ["a"], "c.json", "b/c"),
            Component("d", "D", "", ["b", "c"], "d.json", "b/d"),
        ])
        assert m.validate_dag() == []

    def test_missing_dependency(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", ["nonexistent"], "a.json", "b/a"),
        ])
        errors = m.validate_dag()
        assert any("nonexistent" in e for e in errors)

    def test_self_dependency_cycle(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", ["a"], "a.json", "b/a"),
        ])
        errors = m.validate_dag()
        assert any("cycle" in e.lower() for e in errors)

    def test_two_node_cycle(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", ["b"], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
        ])
        errors = m.validate_dag()
        assert any("cycle" in e.lower() for e in errors)

    def test_three_node_cycle(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", ["c"], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
            Component("c", "C", "", ["b"], "c.json", "b/c"),
        ])
        errors = m.validate_dag()
        assert any("cycle" in e.lower() for e in errors)

    def test_duplicate_ids(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("a", "A copy", "", [], "a2.json", "b/a2"),
        ])
        errors = m.validate_dag()
        assert any("Duplicate" in e for e in errors)


class TestTopologicalOrder:
    """Tests for Manifest.topological_order."""

    def test_empty(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [])
        assert m.topological_order() == []

    def test_single(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
        ])
        assert m.topological_order() == ["a"]

    def test_linear_chain(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("c", "C", "", ["b"], "c.json", "b/c"),
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
        ])
        order = m.topological_order()
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")

    def test_diamond(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
            Component("c", "C", "", ["a"], "c.json", "b/c"),
            Component("d", "D", "", ["b", "c"], "d.json", "b/d"),
        ])
        order = m.topological_order()
        assert order[0] == "a"
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_independent_components(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", [], "b.json", "b/b"),
            Component("c", "C", "", [], "c.json", "b/c"),
        ])
        order = m.topological_order()
        assert sorted(order) == ["a", "b", "c"]

    def test_cycle_raises(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", ["b"], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
        ])
        with pytest.raises(ValueError, match="cycle"):
            m.topological_order()


class TestGetReadyComponents:
    """Tests for Manifest.get_ready_components."""

    def test_no_deps_all_ready(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", [], "b.json", "b/b"),
        ])
        ready = m.get_ready_components()
        assert {c.id for c in ready} == {"a", "b"}

    def test_deps_not_met(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
        ])
        ready = m.get_ready_components()
        assert [c.id for c in ready] == ["a"]

    def test_deps_satisfied(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="completed"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
        ])
        ready = m.get_ready_components()
        assert [c.id for c in ready] == ["b"]

    def test_running_not_ready(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="running"),
        ])
        ready = m.get_ready_components()
        assert ready == []

    def test_completed_not_ready(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="completed"),
        ])
        ready = m.get_ready_components()
        assert ready == []

    def test_failed_not_ready(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="failed"),
        ])
        ready = m.get_ready_components()
        assert ready == []

    def test_mixed_states(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="completed"),
            Component("b", "B", "", ["a"], "b.json", "b/b", status="running"),
            Component("c", "C", "", ["a"], "c.json", "b/c"),
            Component("d", "D", "", ["b", "c"], "d.json", "b/d"),
        ])
        ready = m.get_ready_components()
        assert [c.id for c in ready] == ["c"]


class TestCascadeSkip:
    """Tests for Manifest.cascade_skip."""

    def test_no_dependents(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", [], "b.json", "b/b"),
        ])
        skipped = m.cascade_skip("a")
        assert skipped == []

    def test_single_dependent(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="failed"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
        ])
        skipped = m.cascade_skip("a")
        assert skipped == ["b"]
        assert m.get_component("b").status == ComponentStatus.SKIPPED.value

    def test_chain_of_dependents(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="failed"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
            Component("c", "C", "", ["b"], "c.json", "b/c"),
        ])
        skipped = m.cascade_skip("a")
        assert sorted(skipped) == ["b", "c"]

    def test_diamond_cascade(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="failed"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
            Component("c", "C", "", ["a"], "c.json", "b/c"),
            Component("d", "D", "", ["b", "c"], "d.json", "b/d"),
        ])
        skipped = m.cascade_skip("a")
        assert sorted(skipped) == ["b", "c", "d"]

    def test_does_not_skip_completed(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="failed"),
            Component("b", "B", "", ["a"], "b.json", "b/b", status="completed"),
        ])
        skipped = m.cascade_skip("a")
        assert skipped == []

    def test_does_not_skip_already_skipped(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a", status="failed"),
            Component("b", "B", "", ["a"], "b.json", "b/b", status="skipped"),
        ])
        skipped = m.cascade_skip("a")
        assert skipped == []


class TestManifestLoadSave:
    """Tests for Manifest.load and save."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        manifest = Manifest(
            version="1",
            spec_file="spec.md",
            project_name="test",
            base_branch="main",
            single_pr=False,
            components=[
                Component(
                    id="comp-a",
                    title="Component A",
                    description="Description A",
                    dependencies=[],
                    prd_path="scripts/ralph/feature/comp-a/prd.json",
                    branch_name="ralph/factory/comp-a",
                    status="completed",
                    error="",
                    retries=1,
                    pr_number=42,
                    pr_url="https://github.com/test/pr/42",
                ),
            ],
        )

        path = tmp_path / "manifest.json"
        manifest.save(path)

        loaded = Manifest.load(path)
        assert loaded.version == "1"
        assert loaded.project_name == "test"
        assert len(loaded.components) == 1
        assert loaded.components[0].id == "comp-a"
        assert loaded.components[0].status == "completed"
        assert loaded.components[0].retries == 1
        assert loaded.components[0].pr_number == 42

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text("not json")
        with pytest.raises(json.JSONDecodeError):
            Manifest.load(path)

    def test_load_invalid_schema(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text('{"invalid": true}')
        with pytest.raises(ValueError, match="Invalid manifest schema"):
            Manifest.load(path)

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "manifest.json"
        m = Manifest("1", "spec.md", "test", "main", False, [])
        m.save(path)
        assert path.exists()

    def test_load_with_optional_fields_defaulted(self, tmp_path: Path) -> None:
        """Components without optional fields get defaults."""
        data = _minimal_manifest_data([_component_data()])
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(data))

        loaded = Manifest.load(path)
        comp = loaded.components[0]
        assert comp.status == ComponentStatus.PENDING.value
        assert comp.error == ""
        assert comp.retries == 0
        assert comp.pr_number is None
        assert comp.pr_url == ""


class TestGetComponent:
    """Tests for Manifest.get_component."""

    def test_found(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [
            Component("a", "A", "", [], "a.json", "b/a"),
        ])
        assert m.get_component("a") is not None
        assert m.get_component("a").title == "A"

    def test_not_found(self) -> None:
        m = Manifest("1", "spec.md", "test", "main", False, [])
        assert m.get_component("nonexistent") is None
