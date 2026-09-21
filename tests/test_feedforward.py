"""Tests for feedforward module."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from kstrl import feedforward
from kstrl.feedforward import (
    FeedforwardConfig,
    build_dependency_graph,
    build_feedforward_context,
    build_module_map,
    extract_conventions,
    extract_public_interfaces,
)
from tests.test_context import section

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


# ---------------------------------------------------------------------------
# #403: the budget is consulted before the work is done
# ---------------------------------------------------------------------------


def _tiny_repo(root: Path) -> None:
    """One package, two modules, one internal import, one pyproject."""
    pkg = root / "demo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    (pkg / "api.py").write_text(
        "from demo_pkg.models import Thing\n\n\ndef get(t: Thing) -> Thing:\n    return t\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )


def _wide_repo(root: Path) -> None:
    """Twelve packages, one module each: a module map that alone is over budget."""
    for i in range(12):
        pkg = root / f"package_number_{i:02d}"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "core.py").write_text(f"class Core{i:02d}:\n    pass\n", encoding="utf-8")


def _deep_repo(root: Path) -> None:
    """One package, forty modules, each importing the one before it."""
    pkg = root / "demo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod00.py").write_text("class Base00:\n    pass\n", encoding="utf-8")
    for i in range(1, 40):
        (pkg / f"mod{i:02d}.py").write_text(
            f"from demo_pkg.mod{i - 1:02d} import Base{i - 1:02d}\n\n\n"
            f"class Base{i:02d}:\n    pass\n",
            encoding="utf-8",
        )


def _no_import_repo(root: Path) -> None:
    """A package whose modules import nothing internal."""
    pkg = root / "demo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "alpha.py").write_text("import os\n\n\nclass Alpha:\n    pass\n", encoding="utf-8")
    (pkg / "beta.py").write_text("class Beta:\n    pass\n", encoding="utf-8")


_TINY_MODULE_MAP_ONLY = (
    "=== CODEBASE CONTEXT (auto-generated) ===\n"
    "\n"
    "## Module map\n"
    "  demo_pkg/            # 3 files, 7 lines\n"
    "\n"
    "=== END CODEBASE CONTEXT ==="
)

_TINY_TWO_SECTIONS = (
    "=== CODEBASE CONTEXT (auto-generated) ===\n"
    "\n"
    "## Module map\n"
    "  demo_pkg/            # 3 files, 7 lines\n"
    "\n"
    "## Dependency graph\n"
    "api -> models (imports: Thing)\n"
    "\n"
    "=== END CODEBASE CONTEXT ==="
)

_TINY_EVERYTHING = (
    "=== CODEBASE CONTEXT (auto-generated) ===\n"
    "\n"
    "## Module map\n"
    "  demo_pkg/            # 3 files, 7 lines\n"
    "\n"
    "## Dependency graph\n"
    "api -> models (imports: Thing)\n"
    "\n"
    "## Public interfaces\n"
    "demo_pkg/api.py: def get(t: Thing) -> Thing\n"
    "demo_pkg/models.py: class Thing\n"
    "\n"
    "## Conventions\n"
    "- Python version: >=3.11\n"
    "\n"
    "=== END CODEBASE CONTEXT ==="
)


def test_a_spent_budget_stops_before_the_dependency_graph_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wide_repo(tmp_path)
    calls: list[str] = []

    def _spy(name: str) -> None:
        real = getattr(feedforward, name)

        def spy(root: Path, *args: Any, **kwargs: Any) -> str:
            calls.append(name)
            result: str = real(root, *args, **kwargs)
            return result

        monkeypatch.setattr(feedforward, name, spy)

    # Every builder after the module map, not just the expensive one: a
    # stop that special-cases the dependency graph still does the other
    # two pieces of work the budget has already spent.
    _spy("build_dependency_graph")
    _spy("extract_public_interfaces")
    _spy("extract_conventions")

    context = build_feedforward_context(tmp_path, FeedforwardConfig(max_context_tokens=100))

    assert calls == [], calls
    assert "## Module map" in context


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (24, ""),
        (35, _TINY_MODULE_MAP_ONLY),
        (50, _TINY_TWO_SECTIONS),
        (100, _TINY_EVERYTHING),
    ],
)
def test_the_delivered_context_is_byte_for_byte_what_it_was(
    tmp_path: Path, tokens: int, expected: str
) -> None:
    _tiny_repo(tmp_path)
    assert build_feedforward_context(tmp_path, FeedforwardConfig(max_context_tokens=tokens)) == (
        expected
    )


def test_a_dependency_graph_that_did_not_fit_says_so(tmp_path: Path) -> None:
    _deep_repo(tmp_path)

    context = build_feedforward_context(tmp_path, FeedforwardConfig(max_context_tokens=100))
    body = section(context, "## Dependency graph")

    assert "## Dependency graph" in context
    assert "did not fit" in body
    assert "->" not in body

    fit = re.search(
        r"passed the (\d+) characters left in the context budget after (\d+) of (\d+) files",
        body,
    )
    assert fit is not None, body
    room, parsed, total = (int(group) for group in fit.groups())
    whole_budget_chars = 100 * 4  # max_context_tokens=100 above, 4 chars/token
    files_in_deep_repo = 1 + 40  # __init__.py plus mod00.py .. mod39.py (40 files)
    # The graph is told the room left for ITS OWN section, not the whole
    # budget the module map has already eaten into.
    assert 0 < room < whole_budget_chars, body
    # And it stopped when what it had built outgrew that room, rather
    # than parsing every file and reporting the overflow afterwards.
    assert total == files_in_deep_repo, body
    assert parsed < total, body


def test_a_graph_with_nothing_to_say_is_not_a_graph_that_did_not_fit(tmp_path: Path) -> None:
    _no_import_repo(tmp_path)

    context = build_feedforward_context(tmp_path, FeedforwardConfig(max_context_tokens=1000))

    assert "## Public interfaces" in context
    assert "## Dependency graph" not in context
    assert "did not fit" not in context


def test_a_component_filtered_graph_is_built_even_when_the_whole_graph_is_over_budget(
    tmp_path: Path,
) -> None:
    _deep_repo(tmp_path)

    context = build_feedforward_context(
        tmp_path,
        FeedforwardConfig(max_context_tokens=100),
        component_id="mod05",
        component_deps=["mod05"],
    )
    body = section(context, "## Dependency graph")

    assert body == "mod05 -> mod04 (imports: Base04)\nmod06 -> mod05 (imports: Base05)"


def test_the_graph_as_the_only_section_is_truncated_not_refused(tmp_path: Path) -> None:
    # module_map is a documented config key (kstrl/init_cmd.py:631,
    # docs/spec-harness-engineering.md:148). With it off the graph is the only
    # section, and _truncate_to_budget CUTS the last section to fit rather than
    # dropping it, so a bail that cannot see that shrink refuses a graph that
    # would in fact have been delivered.
    _deep_repo(tmp_path)

    context = build_feedforward_context(
        tmp_path, FeedforwardConfig(module_map=False, max_context_tokens=100)
    )

    assert "## Dependency graph" in context
    assert "did not fit" not in context
    assert "->" in context
    assert "... (truncated)" in context


# ---------------------------------------------------------------------------
# Simplify-pass addendum, B3: _record_edge's running count must be exact
# ---------------------------------------------------------------------------


def test_the_edge_size_estimate_is_an_exact_sum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bail this feeds must never stop on a graph that would in fact
    have fit, so `_record_edge`'s running total is not an estimate: the
    sum of everything it returns must equal len() of the rendered graph
    body exactly, neither more (an over-count refuses a graph that would
    have fit) nor less (an under-count tells the graph it has room it
    does not)."""
    pkg = tmp_path / "edge_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "base.py").write_text(
        "class Base:\n    pass\n\n\nclass Extra:\n    pass\n", encoding="utf-8"
    )
    (pkg / "mid.py").write_text(
        "from edge_pkg.base import Base, Extra\n\n\nclass Mid:\n    pass\n", encoding="utf-8"
    )
    (pkg / "top.py").write_text(
        "from edge_pkg.mid import Mid\nfrom edge_pkg.base import Base\n\n\nclass Top:\n    pass\n",
        encoding="utf-8",
    )

    returned: list[int] = []
    real = feedforward._record_edge

    def spy(*args: Any, **kwargs: Any) -> int:
        added: int = real(*args, **kwargs)
        returned.append(added)
        return added

    monkeypatch.setattr(feedforward, "_record_edge", spy)

    body = build_dependency_graph(tmp_path)

    # mid -> base (2 names), top -> mid (1 name), top -> base (1 name).
    assert len(returned) == 3, returned
    assert sum(returned) == len(body)
