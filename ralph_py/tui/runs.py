"""Run discovery for `ralph dash` (stage 3 PR C).

A run is a directory under ``.ralph/runs/<run_id>/`` containing
events.jsonl. run_ids are lexicographically sortable by construction
(``factory-YYYYMMDD-HHMMSS.ffffff-<nonce>``), which discovery exploits.

Liveness is a judgment from two cheap signals: recent events-file
mtime, or the run-level factory.lock being held (a non-blocking flock
probe - safe because the factory holds the lock for the whole run and
we release our probe immediately).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

LIVE_MTIME_WINDOW_SECONDS = 60.0
_COMPLETED_TAIL_BYTES = 8192


@dataclass(frozen=True)
class RunRef:
    run_id: str
    run_dir: Path
    events_path: Path
    mtime: float
    completed: bool

    @property
    def live(self) -> bool:
        return not self.completed and (
            time.time() - self.mtime < LIVE_MTIME_WINDOW_SECONDS
        )


def _run_completed(events_path: Path) -> bool:
    """factory_completed in the last ~8KB of the stream (cheap check;
    a torn tail or missing marker just means "not completed")."""
    try:
        size = events_path.stat().st_size
        with open(events_path, "rb") as f:
            f.seek(max(0, size - _COMPLETED_TAIL_BYTES))
            tail = f.read()
    except OSError:
        return False
    return b'"factory_completed"' in tail


def factory_lock_held(root_dir: Path) -> bool:
    """Non-blocking probe of the run-level flock. True = a factory run
    holds it right now. POSIX-only; returns False where fcntl is
    unavailable (mirrors the factory's own degradation)."""
    lock_path = root_dir / ".ralph" / "factory.lock"
    if not lock_path.exists():
        return False
    try:
        import fcntl
    except ImportError:
        return False
    try:
        with open(lock_path, "a+") as fp:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True  # held by the factory
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def discover_runs(root_dir: Path) -> list[RunRef]:
    """All runs with an events.jsonl, newest first."""
    runs_root = root_dir / ".ralph" / "runs"
    refs: list[RunRef] = []
    try:
        candidates = sorted(runs_root.iterdir(), key=lambda d: d.name,
                            reverse=True)
    except OSError:
        return []
    for run_dir in candidates:
        events_path = run_dir / "events.jsonl"
        try:
            mtime = events_path.stat().st_mtime
        except OSError:
            continue
        refs.append(RunRef(
            run_id=run_dir.name,
            run_dir=run_dir,
            events_path=events_path,
            mtime=mtime,
            completed=_run_completed(events_path),
        ))
    return refs


def latest_run(root_dir: Path) -> RunRef | None:
    refs = discover_runs(root_dir)
    return refs[0] if refs else None


def find_run(root_dir: Path, run_id: str) -> RunRef | None:
    """Exact match, or unique-prefix match."""
    refs = discover_runs(root_dir)
    for ref in refs:
        if ref.run_id == run_id:
            return ref
    prefixed = [r for r in refs if r.run_id.startswith(run_id)]
    if len(prefixed) == 1:
        return prefixed[0]
    return None
