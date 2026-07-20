"""Polling JSONL/text tailers for the TUI spike.

The production shape (graduates to kstrl/tui/tail.py in PR C):
byte-offset polling with a partial-line buffer, tolerant of torn tails
(a JSON line written in two flushes) and file truncation/replacement.
Stdlib only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TailChunk:
    records: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


class JsonlTailer:
    """Tail a JSONL file by byte offset.

    A trailing partial line (no newline yet) is buffered, never parsed,
    and completed on a later poll. A line that has a newline but is not
    valid JSON is dropped (matches read_progress_events semantics).
    """

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
            # Truncated/replaced under us: reset and re-read from zero.
            self._offset = 0
            self._partial = b""
            chunk.truncated = True
        if size == self._offset:
            return chunk
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            data = f.read()
            self._offset = f.tell()
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()  # b"" when data ended with newline
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                chunk.records.append(obj)
        return chunk


class TextTailer:
    """Tail a plain-text transcript; returns new complete lines."""

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
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            data = f.read()
            self._offset = f.tell()
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()
        out = [ln.decode("utf-8", errors="replace") for ln in lines]
        return out[-self.max_lines:]


class RunTailer:
    """Tail one run directory: events.jsonl + per-component engineer.jsonl."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self._events = JsonlTailer(run_dir / "events.jsonl")
        self._comp_tailers: dict[str, JsonlTailer] = {}

    def _discover_components(self) -> None:
        comp_root = self.run_dir / "components"
        if not comp_root.is_dir():
            return
        for d in comp_root.iterdir():
            if d.name not in self._comp_tailers and (d / "engineer.jsonl").exists():
                self._comp_tailers[d.name] = JsonlTailer(d / "engineer.jsonl")

    def poll_events(self) -> list[dict[str, Any]]:
        self._discover_components()
        records = self._events.poll().records
        for tailer in self._comp_tailers.values():
            records.extend(tailer.poll().records)
        records.sort(key=lambda r: (r.get("ts", 0.0), r.get("seq", 0)))
        return records
