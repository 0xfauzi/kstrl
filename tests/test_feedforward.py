"""Tests for feedforward module."""

from __future__ import annotations

from pathlib import Path

from ralph_py.feedforward import (
    FeedforwardConfig,
    build_dependency_graph,
    build_feedforward_context,
    build_module_map,
    extract_conventions,
    extract_public_interfaces,
)

# ---------------------------------------------------------------------------
# build_module_map
# ---------------------------------------------------------------------------


class TestBuildModuleMap:
    def test_build_module_map(self, tmp_path: Path) -> None:
        # Create a small project structure with source files.
        pkg = tmp_path / "mypackage"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\ny = 2\n")
        (pkg / "utils.py").write_text("def helper():\n    pass\n")

        result = build_module_map(tmp_path)
        assert "mypackage/" in result
        # __init__.py, core.py, utils.py are all .py so count is 3
        assert "3 files" in result
        assert result != ""

    def test_build_module_map_empty(self, tmp_path: Path) -> None:
        result = build_module_map(tmp_path)
        assert result == ""

    def test_build_module_map_skips_hidden(self, tmp_path: Path) -> None:
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").write_text("x = 1\n")

        visible = tmp_path / "visible"
        visible.mkdir()
        (visible / "code.py").write_text("y = 2\n")

        result = build_module_map(tmp_path)
        assert ".hidden" not in result
        assert "visible/" in result


# ---------------------------------------------------------------------------
# extract_public_interfaces
# ---------------------------------------------------------------------------


class TestExtractPublicInterfaces:
    def test_extract_public_interfaces(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "models.py").write_text(
            "class User:\n"
            "    pass\n"
            "\n"
            "class _Internal:\n"
            "    pass\n"
            "\n"
            "def create_user(name: str) -> User:\n"
            "    pass\n"
            "\n"
            "def _private_helper():\n"
            "    pass\n"
        )

        result = extract_public_interfaces(tmp_path)
        assert "class User" in result
        assert "_Internal" not in result
        assert "create_user" in result
        assert "_private_helper" not in result

    def test_extract_public_interfaces_empty(self, tmp_path: Path) -> None:
        result = extract_public_interfaces(tmp_path)
        assert result == ""

    def test_extract_public_interfaces_skips_test_files(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "test_models.py").write_text("class TestUser:\n    pass\n")
        (pkg / "core.py").write_text("class Engine:\n    pass\n")

        result = extract_public_interfaces(tmp_path)
        assert "TestUser" not in result
        assert "Engine" in result


# ---------------------------------------------------------------------------
# build_dependency_graph
# ---------------------------------------------------------------------------


class TestBuildDependencyGraph:
    def test_build_dependency_graph(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "models.py").write_text("class Foo:\n    pass\n")
        (pkg / "service.py").write_text(
            "from mypkg.models import Foo\n"
            "\n"
            "def run(f: Foo) -> None:\n"
            "    pass\n"
        )

        result = build_dependency_graph(tmp_path)
        assert "service" in result
        assert "models" in result
        assert "Foo" in result

    def test_build_dependency_graph_no_packages(self, tmp_path: Path) -> None:
        # No __init__.py means no packages detected
        subdir = tmp_path / "plain"
        subdir.mkdir()
        (subdir / "code.py").write_text("import os\n")

        result = build_dependency_graph(tmp_path)
        assert result == ""


# ---------------------------------------------------------------------------
# extract_conventions
# ---------------------------------------------------------------------------


class TestExtractConventions:
    def test_extract_conventions_pyproject(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\n'
            'requires-python = ">=3.11"\n'
            '\n'
            '[tool.ruff]\n'
            'line-length = 100\n'
            'target-version = "py311"\n'
            '\n'
            '[tool.ruff.lint]\n'
            'select = ["E", "F", "W"]\n'
        )

        result = extract_conventions(tmp_path)
        assert "Python version: >=3.11" in result
        assert "Line length (ruff): 100" in result
        assert "Target version (ruff): py311" in result
        assert "Ruff rules: E, F, W" in result

    def test_extract_conventions_empty(self, tmp_path: Path) -> None:
        result = extract_conventions(tmp_path)
        assert result == ""


# ---------------------------------------------------------------------------
# build_feedforward_context
# ---------------------------------------------------------------------------


class TestBuildFeedforwardContext:
    def test_build_feedforward_context_disabled(self, tmp_path: Path) -> None:
        config = FeedforwardConfig(enabled=False)
        result = build_feedforward_context(tmp_path, config)
        assert result == ""

    def test_build_feedforward_context_full(self, tmp_path: Path) -> None:
        # Set up a minimal project so some sections produce output.
        pkg = tmp_path / "testpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text(
            "class Widget:\n"
            "    pass\n"
            "\n"
            "def build_widget(name: str) -> Widget:\n"
            "    pass\n"
        )

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\n'
            'requires-python = ">=3.11"\n'
        )

        config = FeedforwardConfig(enabled=True)
        result = build_feedforward_context(tmp_path, config)
        assert "CODEBASE CONTEXT" in result
        assert "END CODEBASE CONTEXT" in result
        # Should contain at least one section header
        assert "##" in result

    def test_build_feedforward_context_empty_project(self, tmp_path: Path) -> None:
        config = FeedforwardConfig(enabled=True)
        result = build_feedforward_context(tmp_path, config)
        # No source files, no config files - should return empty
        assert result == ""


# ---------------------------------------------------------------------------
# FeedforwardConfig defaults
# ---------------------------------------------------------------------------


class TestFeedforwardConfigDefaults:
    def test_feedforward_config_defaults(self) -> None:
        config = FeedforwardConfig()
        assert config.enabled is True
        assert config.module_map is True
        assert config.public_interfaces is True
        assert config.dependency_graph is True
        assert config.conventions is True
        assert config.max_context_tokens == 4000
