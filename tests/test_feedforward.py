"""Tests for the codebase scan module."""

from __future__ import annotations

from pathlib import Path

from kstrl.feedforward import CodebaseScanConfig, build_codebase_scan_context, build_module_map

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


class TestBuildCodebaseScanContext:
    def test_build_codebase_scan_context_disabled(self, tmp_path: Path) -> None:
        config = CodebaseScanConfig(enabled=False)
        result = build_codebase_scan_context(tmp_path, config)
        assert result == ""


class TestCodebaseScanConfigDefaults:
    def test_codebase_scan_config_defaults(self) -> None:
        config = CodebaseScanConfig()
        assert config.enabled is True
        assert config.module_map is True
        assert config.max_context_tokens == 4000
