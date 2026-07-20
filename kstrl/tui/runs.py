"""Run discovery for the dashboard and home shell.

A run is a directory under ``.kstrl/runs/<run_id>/`` containing
events.jsonl. Run ids are ``<kind>-<stamp>-<nonce>`` (kstrl.runid);
discovery orders by the stamp AFTER the kind prefix, so runs of
different kinds interleave chronologically instead of grouping by
kind name.

Liveness is a judgment from two cheap signals: recent events-file
mtime, or the run-level factory.lock being held (a non-blocking flock
probe - safe because the factory holds the lock for the whole run and
we release our probe immediately). The lock is a factory-only signal;
other kinds rely on the mtime window (heartbeats keep it fresh).
"""

from __future__ import annotations

import errno
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from kstrl.runid import run_kind, run_sort_key

LIVE_MTIME_WINDOW_SECONDS = 60.0
_COMPLETED_TAIL_BYTES = 8192


@dataclass(frozen=True)
class RunRef:
    run_id: str
    run_dir: Path
    events_path: Path
    mtime: float
    completed: bool
    lock_held: bool = False
    kind: str = "factory"

    @property
    def live(self) -> bool:
        return not self.completed and (
            self.lock_held or
            time.time() - self.mtime < LIVE_MTIME_WINDOW_SECONDS
        )


def _run_completed(events_path: Path) -> bool:
    """Find a factory_completed event near the end of the stream.

    Parse complete JSONL records rather than searching raw bytes, since a
    log payload can legitimately contain the event name as ordinary text.
    A torn tail or missing marker means "not completed".
    """
    try:
        with open(events_path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            start = max(0, size - _COMPLETED_TAIL_BYTES)
            f.seek(max(0, start - 1))
            tail = f.read()
    except OSError:
        return False
    if start:
        if tail.startswith(b"\n"):
            tail = tail[1:]
        else:
            _, separator, tail = tail.partition(b"\n")
            if not separator:
                return False
    lines = tail.split(b"\n")
    for raw in lines:
        try:
            obj = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("event") == "factory_completed":
            return True
    return False


def factory_lock_held(root_dir: Path) -> bool:
    """Non-blocking probe of the run-level flock. True = a factory run
    holds it right now. POSIX-only; returns False where fcntl is
    unavailable (mirrors the factory's own degradation)."""
    lock_path = root_dir / ".kstrl" / "factory.lock"
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
            except OSError as exc:
                return exc.errno in (errno.EACCES, errno.EAGAIN)
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def discover_runs(
    root_dir: Path, *, kinds: tuple[str, ...] | None = None,
) -> list[RunRef]:
    """All runs with an events.jsonl, newest first.

    ``kinds`` filters to the given run kinds; None means every kind.
    A held factory.lock is attributed to the newest factory-kind run
    only - it says nothing about the liveness of other kinds.
    """
    runs_root = root_dir / ".kstrl" / "runs"
    refs: list[RunRef] = []
    lock_held = factory_lock_held(root_dir)
    try:
        candidates = sorted(runs_root.iterdir(),
                            key=lambda d: run_sort_key(d.name),
                            reverse=True)
    except OSError:
        return []
    for run_dir in candidates:
        kind = run_kind(run_dir.name)
        if kinds is not None and kind not in kinds:
            continue
        events_path = run_dir / "events.jsonl"
        try:
            mtime = events_path.stat().st_mtime
        except OSError:
            continue
        holds_lock = lock_held and kind == "factory"
        if holds_lock:
            lock_held = False
        refs.append(RunRef(
            run_id=run_dir.name,
            run_dir=run_dir,
            events_path=events_path,
            mtime=mtime,
            completed=_run_completed(events_path),
            lock_held=holds_lock,
            kind=kind,
        ))
    return refs


def latest_run(
    root_dir: Path, *, kinds: tuple[str, ...] | None = None,
) -> RunRef | None:
    refs = discover_runs(root_dir, kinds=kinds)
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
