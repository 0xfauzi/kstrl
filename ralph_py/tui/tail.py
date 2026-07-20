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

    def poll(self) -> TailChunk:
        chunk = TailChunk()
        try:
            size = self.path.stat().st_size
        except OSError:
            return chunk
        if size < self._offset:
            self._offset = 0
            self._partial = b""
            chunk.truncated = True
        if size == self._offset:
            return chunk
        try:
            with open(self.path, "rb") as f:
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
        self.path = path
        self.max_lines = max_lines
        self._offset = 0
        self._partial = b""

    def poll(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0
            self._partial = b""
        if size == self._offset:
            return []
        try:
            with open(self.path, "rb") as f:
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

    def poll_events(self) -> list[Event]:
        self._discover_workers()
        events = self._events.poll().events
        for tailer in self._workers.values():
            events.extend(tailer.poll().events)
        events.sort(key=lambda e: (e.ts, e.source, e.seq))
        return events
