"""Kind-prefixed run ids discovered on disk.

``load_run_state`` resolves the newest run of any kind (factory, decompose,
understand and feature), newest first by stamp across kinds (whole-name
lexicographic order would put every decompose-* before every factory-*).
"""

from __future__ import annotations

from pathlib import Path

from kstrl.events.reducer import load_run_state
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

    def test_load_run_state_resolves_newest_of_any_kind(
        self,
        tmp_path: Path,
    ) -> None:
        self._write_mixed(tmp_path)
        state, source = load_run_state(tmp_path)
        assert state.run_id == MIXED_IDS[-1]
        assert state.kind == "feature"
        assert source == (tmp_path / ".kstrl" / "runs" / MIXED_IDS[-1] / "events.jsonl")
