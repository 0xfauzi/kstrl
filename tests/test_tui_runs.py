"""Stage 3 PR C (TUI rewrite): run discovery for `ralph dash`."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ralph_py.tui.runs import (
    discover_runs,
    factory_lock_held,
    find_run,
    latest_run,
)
from tests.helpers.fake_run import FakeRunSpec, write_fake_run


def _three_runs(root: Path) -> list[str]:
    ids = [
        "factory-20260718-100000.000000-old",
        "factory-20260719-100000.000000-mid",
        "factory-20260720-100000.000000-new",
    ]
    for run_id in ids:
        write_fake_run(root, FakeRunSpec(components=1), run_id=run_id)
    return ids


class TestDiscovery:
    def test_newest_first_and_completed_flag(self, tmp_path: Path) -> None:
        ids = _three_runs(tmp_path)
        refs = discover_runs(tmp_path)
        assert [r.run_id for r in refs] == list(reversed(ids))
        assert all(r.completed for r in refs)

    def test_latest_run(self, tmp_path: Path) -> None:
        _three_runs(tmp_path)
        ref = latest_run(tmp_path)
        assert ref is not None
        assert ref.run_id.endswith("-new")

    def test_empty_root(self, tmp_path: Path) -> None:
        assert discover_runs(tmp_path) == []
        assert latest_run(tmp_path) is None

    def test_find_exact_and_unique_prefix(self, tmp_path: Path) -> None:
        ids = _three_runs(tmp_path)
        assert find_run(tmp_path, ids[0]) is not None
        ref = find_run(tmp_path, "factory-20260719")
        assert ref is not None and ref.run_id == ids[1]
        # Ambiguous prefix -> None
        assert find_run(tmp_path, "factory-2026") is None
        assert find_run(tmp_path, "nope") is None

    def test_incomplete_run_is_live_when_fresh(self, tmp_path: Path) -> None:
        run_id = "factory-20260720-150000.000000-live"
        write_fake_run(
            tmp_path, FakeRunSpec(components=1, complete=False),
            run_id=run_id,
        )
        ref = find_run(tmp_path, run_id)
        assert ref is not None
        assert ref.completed is False
        assert ref.live is True  # mtime is fresh
        # Age the file: no longer live.
        old = time.time() - 3600
        os.utime(ref.events_path, (old, old))
        aged = find_run(tmp_path, run_id)
        assert aged is not None
        assert aged.live is False

    def test_log_text_does_not_mark_run_complete(self, tmp_path: Path) -> None:
        run_id = "factory-20260720-150000.000000-log"
        run_dir = tmp_path / ".ralph" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text(json.dumps({
            "event": "log",
            "data": {"text": 'waiting for "factory_completed"'},
        }) + "\n")

        ref = find_run(tmp_path, run_id)

        assert ref is not None
        assert ref.completed is False

    def test_held_lock_keeps_newest_incomplete_run_live(
        self, tmp_path: Path,
    ) -> None:
        import fcntl

        run_id = "factory-20260720-150000.000000-locked"
        write_fake_run(
            tmp_path, FakeRunSpec(components=1, complete=False),
            run_id=run_id,
        )
        events = tmp_path / ".ralph" / "runs" / run_id / "events.jsonl"
        old = time.time() - 3600
        os.utime(events, (old, old))
        lock = tmp_path / ".ralph" / "factory.lock"
        with open(lock, "a+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            ref = find_run(tmp_path, run_id)

        assert ref is not None
        assert ref.live is True


class TestFactoryLockProbe:
    def test_no_lock_file(self, tmp_path: Path) -> None:
        assert factory_lock_held(tmp_path) is False

    def test_unheld_lock_file(self, tmp_path: Path) -> None:
        lock = tmp_path / ".ralph" / "factory.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("12345\n")
        assert factory_lock_held(tmp_path) is False

    def test_held_lock_detected(self, tmp_path: Path) -> None:
        import fcntl

        lock = tmp_path / ".ralph" / "factory.lock"
        lock.parent.mkdir(parents=True)
        with open(lock, "a+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert factory_lock_held(tmp_path) is True
        assert factory_lock_held(tmp_path) is False
