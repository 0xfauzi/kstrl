"""Tests for PR module."""

from __future__ import annotations

from unittest.mock import patch

from kstrl.manifest import Component, Manifest
from kstrl.pr import _generate_pr_body, is_gh_available


def _test_manifest() -> Manifest:
    """Build a test manifest with two components."""
    return Manifest(
        version="1",
        spec_file="spec.md",
        project_name="test-project",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id="database",
                title="Database Schema",
                description="Create the database tables",
                dependencies=[],
                prd_path="scripts/kstrl/feature/database/prd.json",
                branch_name="kstrl/factory/database",
                status="completed",
                pr_number=10,
                pr_url="https://github.com/test/repo/pull/10",
            ),
            Component(
                id="api",
                title="API Endpoints",
                description="Create REST API endpoints",
                dependencies=["database"],
                prd_path="scripts/kstrl/feature/api/prd.json",
                branch_name="kstrl/factory/api",
                status="completed",
            ),
        ],
    )


class TestIsGhAvailable:
    """Tests for is_gh_available."""

    def test_gh_not_in_path(self) -> None:
        with patch("shutil.which", return_value=None):
            assert is_gh_available() is False

    def test_gh_not_authenticated(self) -> None:
        mock_result = type("Result", (), {"returncode": 1})()
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", return_value=mock_result),
        ):
            assert is_gh_available() is False

    def test_gh_authenticated(self) -> None:
        mock_result = type("Result", (), {"returncode": 0})()
        with (
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch("subprocess.run", return_value=mock_result),
        ):
            assert is_gh_available() is True


class TestGeneratePrBody:
    """Tests for _generate_pr_body."""

    def test_body_contains_description(self) -> None:
        manifest = _test_manifest()
        body = _generate_pr_body(manifest.components[0], manifest)
        assert "Create the database tables" in body

    def test_body_contains_prd_path(self) -> None:
        manifest = _test_manifest()
        body = _generate_pr_body(manifest.components[0], manifest)
        assert "scripts/kstrl/feature/database/prd.json" in body

    def test_body_contains_dependencies_with_links(self) -> None:
        manifest = _test_manifest()
        body = _generate_pr_body(manifest.components[1], manifest)
        assert "Dependencies" in body
        assert "Database Schema" in body
        assert "https://github.com/test/repo/pull/10" in body

    def test_body_no_dependencies_section_when_none(self) -> None:
        manifest = _test_manifest()
        body = _generate_pr_body(manifest.components[0], manifest)
        assert "Dependencies" not in body

    def test_body_contains_attribution(self) -> None:
        manifest = _test_manifest()
        body = _generate_pr_body(manifest.components[0], manifest)
        assert "kstrl" in body
