"""TUI surface A1: kind-prefixed run ids discovered on disk.

``discover_runs`` and ``latest_run`` over a run directory holding
factory, decompose, understand and feature runs: newest first by stamp
across kinds (whole-name lexicographic order would put every
decompose-* before every factory-*), the kinds filter, a held factory
lock attributed to the newest FACTORY run rather than the newest run,
and ``load_run_state`` resolving the newest run of any kind.
"""

from __future__ import annotations

from pathlib import Path

from kstrl.reducer import load_run_state
from kstrl.tui.runs import discover_runs, latest_run
from tests.helpers.fake_run import FakeRunSpec, write_fake_run

# Chronological order oldest->newest; whole-name lexicographic order
# would put every decompose-* before every factory-* instead.
MIXED_IDS = [
    "factory-20260718-100000.000000-aaa",
    "decompose-20260719-090000.000000-bbb",
    "factory-20260719-100000.000000-ccc",
    "understand-20260720-080000.000000-ddd",
    "feature-20260720-110000.000000-eee",
]


class TestMixedKindDiscovery:
    def _write_mixed(self, root: Path) -> None:
        for rid in MIXED_IDS:
            write_fake_run(root, FakeRunSpec(components=1), run_id=rid)

    def test_newest_first_across_kinds(self, tmp_path: Path) -> None:
        self._write_mixed(tmp_path)
        refs = discover_runs(tmp_path)
        assert [r.run_id for r in refs] == list(reversed(MIXED_IDS))
        assert [r.kind for r in refs] == [
            "feature",
            "understand",
            "factory",
            "decompose",
            "factory",
        ]

    def test_kinds_filter(self, tmp_path: Path) -> None:
        self._write_mixed(tmp_path)
        factory_only = discover_runs(tmp_path, kinds=("factory",))
        assert [r.run_id for r in factory_only] == [
            MIXED_IDS[2],
            MIXED_IDS[0],
        ]
        ref = latest_run(tmp_path, kinds=("decompose",))
        assert ref is not None and ref.run_id == MIXED_IDS[1]
        assert latest_run(tmp_path, kinds=("nope",)) is None

    def test_held_lock_attributed_to_newest_factory_run(
        self,
        tmp_path: Path,
    ) -> None:
        import fcntl

        self._write_mixed(tmp_path)
        lock = tmp_path / ".kstrl" / "factory.lock"
        with open(lock, "a+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            refs = discover_runs(tmp_path)
        held = [r.run_id for r in refs if r.lock_held]
        # Newest run overall is feature-*; the lock belongs to the
        # newest FACTORY run instead.
        assert held == [MIXED_IDS[2]]

    def test_load_run_state_resolves_newest_of_any_kind(
        self,
        tmp_path: Path,
    ) -> None:
        self._write_mixed(tmp_path)
        state, source = load_run_state(tmp_path)
        assert state.run_id == MIXED_IDS[-1]
        assert state.kind == "feature"
        assert source == (tmp_path / ".kstrl" / "runs" / MIXED_IDS[-1] / "events.jsonl")
