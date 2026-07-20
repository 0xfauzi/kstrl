"""Byte-offset file tailers for the dashboard (stage 3 PR C).

Promoted from the spike (spike/tui0/tailer.py) after its measurements:
polling at 0.2-0.25s gives p95 tail latency ~240ms at BOTH realistic
and 10x event rates with <1.3% CPU, and zero record loss under
torn-tail injection. Polling (not watchdog/FSEvents) is deliberate:
macOS FSEvents coalesces beyond our latency gate, the offset/torn-tail
logic is needed regardless, and we poll at most a handful of files.

Torn-tail contract (mirrors observability.read_progress_events): a
trailing partial line is buffered, never parsed, and completed by a
later poll; a complete line that is not valid JSON is skipped; a file
that shrank (truncated/replaced) resets to offset zero and reports
``truncated`` so consumers can rebuild state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ralph_py.events import Event, event_from_dict


@dataclass
class TailChunk:
    """One poll's yield: newly complete, parsed events."""

    events: list[Event] = field(default_factory=list)
    truncated: bool = False  # file shrank; offsets were reset


class JsonlTailer:
    """Tail one JSONL event file by byte offset."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._partial = b""
        self._identity: tuple[int, int] | None = None

    def poll(self) -> TailChunk:
        chunk = TailChunk()
        try:
            with open(self.path, "rb") as f:
                stat = os.fstat(f.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if (
                    self._identity is not None
                    and identity != self._identity
                ) or stat.st_size < self._offset:
                    self._offset = 0
                    self._partial = b""
                    chunk.truncated = True
                self._identity = identity
                if stat.st_size == self._offset:
                    return chunk
                f.seek(self._offset)
                data = f.read()
                self._offset = f.tell()
        except OSError:
            return chunk
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()  # b"" when data ended in a newline
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                chunk.events.append(event_from_dict(obj))
        return chunk


class TextTailer:
    """Tail a plain-text transcript; yields new complete lines."""

    def __init__(self, path: Path, max_lines: int = 2000) -> None:
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")
        self.path = path
        self.max_lines = max_lines
        self._offset = 0
        self._partial = b""
        self._identity: tuple[int, int] | None = None

    def poll(self) -> list[str]:
        try:
            with open(self.path, "rb") as f:
                stat = os.fstat(f.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if (
                    self._identity is not None
                    and identity != self._identity
                ) or stat.st_size < self._offset:
                    self._offset = 0
                    self._partial = b""
                self._identity = identity
                if stat.st_size == self._offset:
                    return []
                f.seek(self._offset)
                data = f.read()
                self._offset = f.tell()
        except OSError:
            return []
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()
        decoded = [line.decode("utf-8", errors="replace") for line in lines]
        # Bound a catch-up burst: the consumer is a display pane, and
        # the spike measured that unbounded transcript floods starve
        # input (finding 3) - the cap belongs at the source.
        return decoded[-self.max_lines:]


class RunTailer:
    """Tail one run directory: events.jsonl + workers' engineer.jsonl.

    Components appear on disk as workers start; each poll re-discovers
    the components/ dir so late starters are picked up without restart.
    Events across files are merged in (ts, source, seq) order.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self._events = JsonlTailer(run_dir / "events.jsonl")
        self._workers: dict[str, JsonlTailer] = {}

    def known_components(self) -> list[str]:
        comp_root = self.run_dir / "components"
        if not comp_root.is_dir():
            return []
        try:
            return sorted(d.name for d in comp_root.iterdir() if d.is_dir())
        except OSError:
            return []

    def _discover_workers(self) -> None:
        comp_root = self.run_dir / "components"
        if not comp_root.is_dir():
            return
        try:
            entries = list(comp_root.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name not in self._workers and (
                entry / "engineer.jsonl"
            ).exists():
                self._workers[entry.name] = JsonlTailer(
                    entry / "engineer.jsonl",
                )

    def _poll_once(self) -> TailChunk:
        self._discover_workers()
        run_chunk = self._events.poll()
        events = run_chunk.events
        truncated = run_chunk.truncated
        for tailer in self._workers.values():
            worker_chunk = tailer.poll()
            events.extend(worker_chunk.events)
            truncated = truncated or worker_chunk.truncated
        events.sort(key=lambda e: (e.ts, e.source, e.seq))
        return TailChunk(events=events, truncated=truncated)

    def poll_events(self) -> TailChunk:
        """Return new events, rebuilding a full snapshot after rewrites.

        A reducer cannot safely apply a replaced worker stream on top of
        its old state. If any constituent file reports truncation, reset
        every offset and return one complete, consistently ordered snapshot
        with ``truncated=True`` so the consumer can reset before folding it.
        """
        chunk = self._poll_once()
        if not chunk.truncated:
            return chunk
        self._events = JsonlTailer(self.run_dir / "events.jsonl")
        self._workers = {}
        rebuilt = self._poll_once()
        rebuilt.truncated = True
        return rebuilt
