"""#265: the divergence gate's configuration, read from disk.

``DivergenceConfig.load`` reads the ``[divergence]`` section of
``kstrl.toml``, lets the environment override it, and falls back to the
defaults when no toml exists. The predicate itself, ``detect_divergence``
over attempt readings, is exercised through ``run_factory`` with a
scripted reviewer whose change grows every attempt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.divergence import DivergenceConfig


class TestConfig:
    def test_load_reads_the_toml_section(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[divergence]\nmode = "block"\ngrowth_steps = 5\n',
            encoding="utf-8",
        )
        config = DivergenceConfig.load(tmp_path)
        assert config.mode == "block"
        assert config.growth_steps == 5

    def test_env_beats_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "kstrl.toml").write_text(
            '[divergence]\nmode = "block"\ngrowth_steps = 5\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("KSTRL_DIVERGENCE_MODE", "skip")
        monkeypatch.setenv("KSTRL_DIVERGENCE_GROWTH_STEPS", "3")
        config = DivergenceConfig.load(tmp_path)
        assert config.mode == "skip"
        assert config.growth_steps == 3

    def test_load_falls_back_to_defaults_without_a_toml(self, tmp_path: Path) -> None:
        assert DivergenceConfig.load(tmp_path) == DivergenceConfig()
