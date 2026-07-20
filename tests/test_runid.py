"""TUI surface A1: run-id kinds, parsing, and chronological ordering."""

from __future__ import annotations

from pathlib import Path

from kstrl.reducer import load_run_state
from kstrl.runid import KNOWN_KINDS, mint_run_id, run_kind, run_sort_key
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


class TestMint:
    def test_default_kind_is_factory(self) -> None:
        assert run_kind(mint_run_id()) == "factory"

    def test_every_known_kind_round_trips(self) -> None:
        for kind in KNOWN_KINDS:
            rid = mint_run_id(kind)
            assert run_kind(rid) == kind
            assert rid.startswith(f"{kind}-")

    def test_same_microsecond_ids_are_distinct(self) -> None:
        assert mint_run_id() != mint_run_id()

    def test_sort_key_orders_by_stamp_across_kinds(self) -> None:
        ordered = sorted(MIXED_IDS, key=run_sort_key)
        assert ordered == MIXED_IDS

    def test_knowledge_delegate_keeps_the_format(self) -> None:
        from kstrl.knowledge import current_run_id

        rid = current_run_id()
        assert run_kind(rid) == "factory"
        # factory-YYYYMMDD-HHMMSS.ffffff-<hex nonce>
        _, date, clock, nonce = rid.split("-")
        assert len(date) == 8 and date.isdigit()
        seconds, _, micros = clock.partition(".")
        assert len(seconds) == 6 and len(micros) == 6
        assert len(nonce) == 6


class TestParseTotality:
    def test_no_separator_yields_empty_kind(self) -> None:
        assert run_kind("weird") == ""
        assert run_sort_key("weird") == "weird"

    def test_empty_string(self) -> None:
        assert run_kind("") == ""
        assert run_sort_key("") == ""

    def test_leading_separator(self) -> None:
        assert run_kind("-20260720") == ""
        assert run_sort_key("-20260720") == "20260720"


class TestMixedKindDiscovery:
    def _write_mixed(self, root: Path) -> None:
        for rid in MIXED_IDS:
            write_fake_run(root, FakeRunSpec(components=1), run_id=rid)

    def test_newest_first_across_kinds(self, tmp_path: Path) -> None:
        self._write_mixed(tmp_path)
        refs = discover_runs(tmp_path)
        assert [r.run_id for r in refs] == list(reversed(MIXED_IDS))
        assert [r.kind for r in refs] == [
            "feature", "understand", "factory", "decompose", "factory",
        ]

    def test_kinds_filter(self, tmp_path: Path) -> None:
        self._write_mixed(tmp_path)
        factory_only = discover_runs(tmp_path, kinds=("factory",))
        assert [r.run_id for r in factory_only] == [
            MIXED_IDS[2], MIXED_IDS[0],
        ]
        ref = latest_run(tmp_path, kinds=("decompose",))
        assert ref is not None and ref.run_id == MIXED_IDS[1]
        assert latest_run(tmp_path, kinds=("nope",)) is None

    def test_held_lock_attributed_to_newest_factory_run(
        self, tmp_path: Path,
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
        self, tmp_path: Path,
    ) -> None:
        self._write_mixed(tmp_path)
        state, source = load_run_state(tmp_path)
        assert state.run_id == MIXED_IDS[-1]
        assert state.kind == "feature"
        assert source == (
            tmp_path / ".kstrl" / "runs" / MIXED_IDS[-1] / "events.jsonl"
        )


class TestRunStateKind:
    def test_kind_property_defaults_to_factory(self) -> None:
        from kstrl.reducer import RunState

        assert RunState(run_id="").kind == "factory"
        assert RunState(run_id="weird").kind == "factory"
        assert RunState(run_id="decompose-2026").kind == "decompose"
