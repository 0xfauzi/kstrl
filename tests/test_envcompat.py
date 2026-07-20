"""PR 2 of the rename: the KSTRL_/RALPH_ env compatibility layer.

Existing suite tests keep setting RALPH_* names, so the legacy fallback
is exercised hundreds of times for free. What is NOT covered for free is
the contract itself: new-name precedence, the once-per-variable
deprecation warning, and the toml filename fallback. That lives here.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from kstrl import envcompat
from kstrl.config import KstrlConfig, resolve_config_file


@pytest.fixture(autouse=True)
def _fresh_warn_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envcompat, "_warned", set())


class TestEnvPrecedence:
    def test_new_name_wins_over_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KSTRL_VERIFY_TEST_CMD", "new")
        monkeypatch.setenv("RALPH_VERIFY_TEST_CMD", "old")
        assert envcompat.get("KSTRL_VERIFY_TEST_CMD") == "new"

    def test_legacy_read_when_new_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KSTRL_VERIFY_TEST_CMD", raising=False)
        monkeypatch.setenv("RALPH_VERIFY_TEST_CMD", "old")
        with pytest.warns(DeprecationWarning, match="RALPH_VERIFY_TEST_CMD"):
            assert envcompat.get("KSTRL_VERIFY_TEST_CMD") == "old"

    def test_default_when_both_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KSTRL_VERIFY_TEST_CMD", raising=False)
        monkeypatch.delenv("RALPH_VERIFY_TEST_CMD", raising=False)
        assert envcompat.get("KSTRL_VERIFY_TEST_CMD", "dflt") == "dflt"

    def test_contains_covers_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KSTRL_UI", raising=False)
        monkeypatch.setenv("RALPH_UI", "plain")
        with pytest.warns(DeprecationWarning):
            assert envcompat.contains("KSTRL_UI")

    def test_require_raises_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KSTRL_UI", raising=False)
        monkeypatch.delenv("RALPH_UI", raising=False)
        with pytest.raises(KeyError):
            envcompat.require("KSTRL_UI")

    def test_warning_fires_once_per_variable(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("KSTRL_UI", raising=False)
        monkeypatch.setenv("RALPH_UI", "plain")
        with pytest.warns(DeprecationWarning):
            envcompat.get("KSTRL_UI")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert envcompat.get("KSTRL_UI") == "plain"

    def test_non_kstrl_name_has_no_fallback(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert envcompat.get("NO_COLOR") is None

    @pytest.mark.parametrize(
        ("new", "legacy"),
        [
            ("KSTRL_BRANCH", "RALPH_BRANCH"),
            ("KSTRL_AGENT_TYPE", "RALPH_AGENT_TYPE"),
            ("KSTRL_FACTORY_MAX_PARALLEL", "RALPH_FACTORY_MAX_PARALLEL"),
            ("KSTRL_TIMEOUT_AGENT_ITERATION", "RALPH_TIMEOUT_AGENT_ITERATION"),
        ],
    )
    def test_family_fallback_shape(
        self, monkeypatch: pytest.MonkeyPatch, new: str, legacy: str,
    ) -> None:
        monkeypatch.delenv(new, raising=False)
        monkeypatch.setenv(legacy, "x")
        with pytest.warns(DeprecationWarning, match=legacy):
            assert envcompat.get(new) == "x"


class TestConfigThroughCompat:
    def test_legacy_branch_env_still_lands_in_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("RALPH_BRANCH", "legacy/branch")
        config = KstrlConfig.from_env(tmp_path)
        assert config.kstrl_branch == "legacy/branch"
        assert config.kstrl_branch_explicit is True

    def test_new_branch_env_beats_legacy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("KSTRL_BRANCH", "new/branch")
        monkeypatch.setenv("RALPH_BRANCH", "legacy/branch")
        config = KstrlConfig.from_env(tmp_path)
        assert config.kstrl_branch == "new/branch"


class TestTomlFilenameFallback:
    def test_kstrl_toml_preferred(self, tmp_path: Path) -> None:
        (tmp_path / "kstrl.toml").write_text("")
        (tmp_path / "ralph.toml").write_text("")
        assert resolve_config_file(tmp_path).name == "kstrl.toml"

    def test_legacy_toml_used_with_warning(self, tmp_path: Path) -> None:
        (tmp_path / "ralph.toml").write_text('[run]\nmax_iterations = 7\n')
        with pytest.warns(DeprecationWarning, match="mv ralph.toml kstrl.toml"):
            resolved = resolve_config_file(tmp_path)
        assert resolved.name == "ralph.toml"
        config = KstrlConfig.load(tmp_path)
        assert config.max_iterations == 7

    def test_neither_present_returns_primary(self, tmp_path: Path) -> None:
        assert resolve_config_file(tmp_path).name == "kstrl.toml"
