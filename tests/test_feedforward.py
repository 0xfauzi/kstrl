"""Tests for feedforward module."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from kstrl.feedforward import (
    _MAX_PUBLIC_INTERFACE_FILES,
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
        # #378: an empty body and an absent section read the same to the
        # engineer, so the body now says why instead of being "".
        result = extract_public_interfaces(tmp_path)
        assert "no Python source root found" in result
        assert ": class " not in result and ": def " not in result

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
            "from mypkg.models import Foo\n\ndef run(f: Foo) -> None:\n    pass\n"
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
            "[project]\n"
            'requires-python = ">=3.11"\n'
            "\n"
            "[tool.ruff]\n"
            "line-length = 100\n"
            'target-version = "py311"\n'
            "\n"
            "[tool.ruff.lint]\n"
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
            "class Widget:\n    pass\n\ndef build_widget(name: str) -> Widget:\n    pass\n"
        )

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nrequires-python = ">=3.11"\n')

        config = FeedforwardConfig(enabled=True)
        result = build_feedforward_context(tmp_path, config)
        assert "CODEBASE CONTEXT" in result
        assert "END CODEBASE CONTEXT" in result
        # Should contain at least one section header
        assert "##" in result

    def test_build_feedforward_context_empty_project(self, tmp_path: Path) -> None:
        # #378: no source files and no config files is now a context block
        # carrying one section that says the stage looked and found nothing.
        # An absent block was indistinguishable from the stage never running.
        config = FeedforwardConfig(enabled=True)
        result = build_feedforward_context(tmp_path, config)
        assert "## Public interfaces" in result
        assert "no Python source root found" in result
        assert "## Module map" not in result
        assert "## Conventions" not in result


# ---------------------------------------------------------------------------
# The budget names what it drops (#199)
# ---------------------------------------------------------------------------


def _headings(text: str) -> set[str]:
    return {ln[3:] for ln in text.splitlines() if ln.startswith("## ")}


def test_a_dropped_feedforward_section_is_named_in_the_rendered_text(tmp_path: Path) -> None:
    pkg = tmp_path / "testpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(
        "class Widget:\n    pass\n\ndef build_widget(name: str) -> Widget:\n    pass\n"
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11"\n')

    cfg = FeedforwardConfig(enabled=True)
    full = build_feedforward_context(tmp_path, replace(cfg, max_context_tokens=100_000))
    headings_full = _headings(full)
    assert len(headings_full) >= 2, "the fixture needs at least two sections to mean anything"

    # N is DERIVED by searching for a budget that drops something but not
    # everything, never typed as a literal: the search stops at the first
    # token count (ascending) whose render lost at least one heading and
    # kept at least one.
    small = None
    chosen_tokens = None
    for tokens in range(10, len(full)):
        candidate = build_feedforward_context(tmp_path, replace(cfg, max_context_tokens=tokens))
        headings_candidate = _headings(candidate)
        if headings_candidate and headings_candidate != headings_full:
            small = candidate
            chosen_tokens = tokens
            break
    assert small is not None, "no budget in range dropped a section without dropping all of them"

    dropped = headings_full - _headings(small)
    assert dropped, "the budget did not drop anything, so this test proves nothing"
    assert _headings(small), "everything was dropped; the search range needs to widen"
    for name in dropped:
        assert name in small
    assert str(chosen_tokens * 4) in small


def test_the_public_interfaces_section_carries_its_denominator(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("_private = True\n")
    # Eligible candidates that get READ and contribute no public symbol,
    # named to sort ahead of the public ones so they are read first.
    # Without them the count of files read and the count of files listed
    # are the same number, and the assertion below cannot tell `examined`
    # from `len(file_symbols)`, which is the distinction this line of
    # output exists to state.
    blanks = 5
    for i in range(blanks):
        (pkg / f"ablank{i:02d}.py").write_text("_hidden = 1\n\n\ndef _nope():\n    pass\n")
    eligible = 35
    for i in range(eligible):
        (pkg / f"mod{i:02d}.py").write_text(f"def public_{i:02d}():\n    pass\n")

    body = extract_public_interfaces(tmp_path)

    # Read count and listed count asserted as DIFFERENT numbers, both
    # derived from the fixture rather than written in by hand.
    assert f"(sample: {blanks + _MAX_PUBLIC_INTERFACE_FILES} of {blanks + eligible} " in body
    assert f"the {_MAX_PUBLIC_INTERFACE_FILES} listed below" in body
    assert "no Python source root found" not in body
    assert "no public classes or functions" not in body


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
