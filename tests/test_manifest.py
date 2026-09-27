"""Manifest persistence tests: ``Manifest.save`` and ``Manifest.load`` over
real files. The round trip of every component field, invalid JSON and an
invalid schema refused on load, parent directories created on save,
optional component fields defaulted on load, and the component status
enum enforced on load (an off-enum status is refused by name, a legal one
is kept).
"""

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
        "prdPath": f"scripts/kstrl/feature/{id}/prd.json",
        "branchName": f"kstrl/factory/{id}",
    }
    data.update(overrides)
    return data


class TestManifestLoadStatus:
    """The component status enum is enforced when a manifest is loaded."""

    def test_load_rejects_off_enum_status(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(_minimal_manifest_data([_component_data(status="PENDING")])))
        with pytest.raises(ValueError, match="'PENDING' is not a valid status"):
            Manifest.load(path)

    def test_load_accepts_a_legal_status(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(_minimal_manifest_data([_component_data(status="failed")])))
        assert Manifest.load(path).components[0].status == ComponentStatus.FAILED.value


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
                    prd_path="scripts/kstrl/feature/comp-a/prd.json",
                    branch_name="kstrl/factory/comp-a",
                    status="completed",
                    error="",
                    retries=1,
                    pr_number=42,
                    pr_url="https://github.com/test/pr/42",
                    merge_sha="a" * 40,
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
        assert loaded.components[0].merge_sha == "a" * 40

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
