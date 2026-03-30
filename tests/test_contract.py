"""Tests for contract module."""

from __future__ import annotations

from ralph_py.contract import (
    ContractConfig,
    ContractMode,
    compute_tiers,
)
from ralph_py.manifest import Component, Manifest


def _make_manifest(components: list[Component]) -> Manifest:
    return Manifest("1", "spec.md", "test", "main", False, components)


class TestComputeTiers:
    def test_empty(self) -> None:
        m = _make_manifest([])
        assert compute_tiers(m) == []

    def test_single_no_deps(self) -> None:
        m = _make_manifest([
            Component("a", "A", "", [], "a.json", "b/a"),
        ])
        assert compute_tiers(m) == [["a"]]

    def test_linear_chain(self) -> None:
        m = _make_manifest([
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
            Component("c", "C", "", ["b"], "c.json", "b/c"),
        ])
        tiers = compute_tiers(m)
        assert tiers == [["a"], ["b"], ["c"]]

    def test_diamond(self) -> None:
        m = _make_manifest([
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", ["a"], "b.json", "b/b"),
            Component("c", "C", "", ["a"], "c.json", "b/c"),
            Component("d", "D", "", ["b", "c"], "d.json", "b/d"),
        ])
        tiers = compute_tiers(m)
        assert tiers[0] == ["a"]
        assert sorted(tiers[1]) == ["b", "c"]
        assert tiers[2] == ["d"]

    def test_independent_components(self) -> None:
        m = _make_manifest([
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", [], "b.json", "b/b"),
            Component("c", "C", "", [], "c.json", "b/c"),
        ])
        tiers = compute_tiers(m)
        assert len(tiers) == 1
        assert sorted(tiers[0]) == ["a", "b", "c"]

    def test_wide_then_narrow(self) -> None:
        m = _make_manifest([
            Component("a", "A", "", [], "a.json", "b/a"),
            Component("b", "B", "", [], "b.json", "b/b"),
            Component("c", "C", "", [], "c.json", "b/c"),
            Component("d", "D", "", ["a", "b", "c"], "d.json", "b/d"),
        ])
        tiers = compute_tiers(m)
        assert len(tiers) == 2
        assert sorted(tiers[0]) == ["a", "b", "c"]
        assert tiers[1] == ["d"]


class TestContractConfig:
    def test_defaults(self) -> None:
        config = ContractConfig()
        assert config.mode == ContractMode.TIER.value
        assert config.test_command == "uv run pytest"
        assert config.timeout == 600.0

    def test_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("RALPH_CONTRACT_MODE", "final")
        monkeypatch.setenv("RALPH_CONTRACT_TEST_CMD", "make test")
        config = ContractConfig.from_env()
        assert config.mode == "final"
        assert config.test_command == "make test"
