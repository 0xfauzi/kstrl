"""Tests for the per-config TOML loaders added in Phase B.

Each section of ralph.toml ([factory], [verify], [contract], [feedforward],
[evolution], [security]) now has an observable runtime effect via the
corresponding ``Config.load(root_dir)`` classmethod.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph_py.contract import ContractConfig, ContractMode
from ralph_py.evolution import EvolutionConfig
from ralph_py.factory import FactoryConfig
from ralph_py.feedforward import FeedforwardConfig
from ralph_py.security import SecurityConfig, SecurityMode
from ralph_py.verify import VerifyConfig


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _clear_env(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for n in names:
        monkeypatch.delenv(n, raising=False)


# ---------------------------------------------------------------------------
# FactoryConfig
# ---------------------------------------------------------------------------


class TestFactoryConfigLoad:
    def test_reads_factory_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(monkeypatch, "FACTORY_MAX_PARALLEL", "FACTORY_MAX_RETRIES")
        _write(tmp_path / "ralph.toml", """
[factory]
max_parallel = 8
max_retries = 5
review_mode = "advisory"
""")
        config = FactoryConfig.load(tmp_path)
        assert config.max_parallel == 8
        assert config.max_retries == 5
        assert config.review_mode == "advisory"

    def test_env_overrides_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(tmp_path / "ralph.toml", "[factory]\nmax_parallel = 8\n")
        monkeypatch.setenv("FACTORY_MAX_PARALLEL", "99")
        config = FactoryConfig.load(tmp_path)
        assert config.max_parallel == 99


# ---------------------------------------------------------------------------
# VerifyConfig
# ---------------------------------------------------------------------------


class TestVerifyConfigLoad:
    def test_reads_verify_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(
            monkeypatch, "RALPH_VERIFY_TEST_CMD", "RALPH_VERIFY_TYPECHECK_CMD",
            "RALPH_VERIFY_REQUIRE_SELF_CRITIQUE",
        )
        _write(tmp_path / "ralph.toml", """
[verify]
test_command = "pytest -x"
typecheck_command = "mypy ."
require_self_critique = true
self_critique_min_bullets = 5
""")
        config = VerifyConfig.load(tmp_path)
        assert config.test_command == "pytest -x"
        assert config.typecheck_command == "mypy ."
        assert config.require_self_critique is True
        assert config.self_critique_min_bullets == 5


# ---------------------------------------------------------------------------
# ContractConfig
# ---------------------------------------------------------------------------


class TestContractConfigLoad:
    def test_reads_contract_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(monkeypatch, "RALPH_CONTRACT_MODE", "RALPH_CONTRACT_TEST_CMD")
        _write(tmp_path / "ralph.toml", """
[contract]
mode = "final"
test_command = "pytest tests/"
""")
        config = ContractConfig.load(tmp_path)
        assert config.mode == ContractMode.FINAL.value
        assert config.test_command == "pytest tests/"

    def test_invalid_mode_raises(self, tmp_path: Path) -> None:
        _write(tmp_path / "ralph.toml", '[contract]\nmode = "always"\n')
        with pytest.raises(ValueError, match="Invalid ContractConfig.mode"):
            ContractConfig.load(tmp_path)


# ---------------------------------------------------------------------------
# FeedforwardConfig
# ---------------------------------------------------------------------------


class TestFeedforwardConfigLoad:
    def test_reads_feedforward_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(
            monkeypatch, "RALPH_FEEDFORWARD_ENABLED",
            "RALPH_FEEDFORWARD_MAX_TOKENS",
        )
        _write(tmp_path / "ralph.toml", """
[feedforward]
enabled = false
module_map = false
max_context_tokens = 8000
""")
        config = FeedforwardConfig.load(tmp_path)
        assert config.enabled is False
        assert config.module_map is False
        assert config.max_context_tokens == 8000

    def test_env_overrides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(tmp_path / "ralph.toml", "[feedforward]\nenabled = false\n")
        monkeypatch.setenv("RALPH_FEEDFORWARD_ENABLED", "true")
        config = FeedforwardConfig.load(tmp_path)
        assert config.enabled is True


# ---------------------------------------------------------------------------
# EvolutionConfig
# ---------------------------------------------------------------------------


class TestEvolutionConfigLoad:
    def test_reads_evolution_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(
            monkeypatch, "RALPH_EVOLUTION_ENABLED",
            "RALPH_EVOLUTION_JOURNAL_PATH",
            "RALPH_EVOLUTION_LOOKBACK_RUNS",
        )
        _write(tmp_path / "ralph.toml", """
[evolution]
enabled = false
lookback_runs = 25
""")
        config = EvolutionConfig.load(tmp_path)
        assert config.enabled is False
        assert config.lookback_runs == 25

    def test_resolves_journal_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(monkeypatch, "RALPH_EVOLUTION_JOURNAL_PATH")
        _write(tmp_path / "ralph.toml", """
[evolution]
journal_path = "custom/evolution.jsonl"
""")
        config = EvolutionConfig.load(tmp_path)
        assert config.journal_path == tmp_path / "custom/evolution.jsonl"


# ---------------------------------------------------------------------------
# SecurityConfig
# ---------------------------------------------------------------------------


class TestSecurityConfigLoad:
    def test_reads_security_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(
            monkeypatch, "RALPH_SECURITY_MODE",
            "RALPH_SECURITY_FAIL_THRESHOLD",
        )
        _write(tmp_path / "ralph.toml", """
[security]
mode = "hard"
fail_threshold = "critical"
""")
        config = SecurityConfig.load(tmp_path)
        assert config.mode == SecurityMode.HARD.value
        assert config.fail_threshold == "critical"

    def test_invalid_mode_in_toml_raises(self, tmp_path: Path) -> None:
        _write(tmp_path / "ralph.toml", '[security]\nmode = "blocky"\n')
        with pytest.raises(ValueError, match="Invalid SecurityConfig.mode"):
            SecurityConfig.load(tmp_path)

    def test_invalid_threshold_in_toml_raises(self, tmp_path: Path) -> None:
        _write(tmp_path / "ralph.toml", '[security]\nfail_threshold = "scary"\n')
        with pytest.raises(ValueError, match="Invalid SecurityConfig.fail_threshold"):
            SecurityConfig.load(tmp_path)

    def test_invalid_threshold_in_env_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("RALPH_SECURITY_FAIL_THRESHOLD", "critcial")
        with pytest.raises(ValueError):
            SecurityConfig.load(tmp_path)


# ---------------------------------------------------------------------------
# Cross-cutting: malformed TOML surfaces through every loader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "loader",
    [
        FactoryConfig.load,
        VerifyConfig.load,
        ContractConfig.load,
        FeedforwardConfig.load,
        EvolutionConfig.load,
        SecurityConfig.load,
    ],
)
def test_malformed_toml_raises_value_error(
    loader, tmp_path: Path,
) -> None:
    _write(tmp_path / "ralph.toml", "this is = not = valid = [ toml\n")
    with pytest.raises(ValueError, match="Invalid TOML"):
        loader(tmp_path)
