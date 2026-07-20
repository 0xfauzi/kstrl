"""TUI surface B1: the click-free config report engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.config import KstrlConfig
from kstrl.config_report import (
    ConfigRow,
    build_config_report,
    normalize_ui_mode,
)


def _row(report_rows: tuple[ConfigRow, ...], section: str, key: str) -> ConfigRow:
    for row in report_rows:
        if row.section == section and row.key == key:
            return row
    raise AssertionError(f"row [{section}] {key} missing")


class TestBuildConfigReport:
    def test_sources_default_toml_env_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "kstrl.toml").write_text(
            "[run]\nmax_iterations = 42\n"
            "[factory]\nmax_parallel = 3\n",
        )
        monkeypatch.setenv("SLEEP_SECONDS", "9")

        def overlay(config: KstrlConfig) -> set[str]:
            config.model = "test-model"
            return {"model"}

        report = build_config_report(tmp_path, overlay=overlay)

        assert report.toml_exists
        assert report.toml_path == tmp_path / "kstrl.toml"
        assert _row(report.rows, "run", "max_iterations") == ConfigRow(
            "run", "max_iterations", "42", "toml",
        )
        assert _row(report.rows, "run", "sleep_seconds").source == "env"
        assert _row(report.rows, "agent", "model") == ConfigRow(
            "agent", "model", "'test-model'", "flag",
        )
        assert _row(report.rows, "run", "interactive").source == "default"
        assert _row(report.rows, "factory", "max_parallel") == ConfigRow(
            "factory", "max_parallel", "3", "toml",
        )
        assert _row(report.rows, "factory", "max_retries").source == "default"

    def test_phase_env_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FACTORY_MAX_PARALLEL", "5")
        report = build_config_report(tmp_path)
        assert _row(report.rows, "factory", "max_parallel") == ConfigRow(
            "factory", "max_parallel", "5", "env",
        )

    def test_absent_toml(self, tmp_path: Path) -> None:
        report = build_config_report(tmp_path)
        assert not report.toml_exists
        # Every documented section is present in order.
        sections = list(dict.fromkeys(row.section for row in report.rows))
        assert sections[:5] == ["agent", "run", "paths", "git", "ui"]
        assert sections[5] == "factory"
        assert sections[-1] == "linear"

    def test_loader_valueerror_propagates(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("not [valid toml\n")
        with pytest.raises(ValueError):
            build_config_report(tmp_path)

    def test_environ_restored_after_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SLEEP_SECONDS", "9")
        import os

        before = dict(os.environ)
        build_config_report(tmp_path)
        assert dict(os.environ) == before


class TestNormalizeUiMode:
    def test_cases(self) -> None:
        assert normalize_ui_mode("gum") == "rich"
        assert normalize_ui_mode("off") == "plain"
        assert normalize_ui_mode("0") == "plain"
        assert normalize_ui_mode("") == "auto"
        assert normalize_ui_mode("RICH ") == "rich"
        assert normalize_ui_mode("garbage") == "auto"
