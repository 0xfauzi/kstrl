"""Universal run substrate: any command as an event-stream run.

``open_command_run`` gives decompose/feature/understand the same disk
contract the factory has - ``.kstrl/runs/<run_id>/events.jsonl`` plus
per-component transcripts - so the dashboard, home shell, and replay
work identically for every kind. It mirrors the factory's sink-attach
block with two deliberate differences:

- No V1CompatSink: ``.kstrl/progress.jsonl`` is a factory-only
  contract with its own consumers (v1 status, the Linear ProgressSink).
  Non-factory commands never wrote it; starting now would surprise
  both.
- A stream filter keeps full agent output OUT of events.jsonl: those
  bytes are teed to the per-component transcript file instead (the
  spike's lean-stream rule - flooding the event stream starves the
  TUI input loop).

Gating matches the factory exactly: ``[factory] progress_log_enabled``
(env or kstrl.toml) turns recording off, in which case the bus still
renders to the terminal but nothing lands on disk.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from kstrl.events import (
    Event,
    EventBus,
    EventSink,
    JsonlSink,
    Log,
    RunPaths,
    WorkerHeartbeat,
)
from kstrl.runid import mint_run_id
from kstrl.ui.bridge import EventBridgeUI

if TYPE_CHECKING:
    from kstrl.ui.base import UI

HEARTBEAT_INTERVAL_SECONDS = 15.0


def start_heartbeat(
    bus: EventBus, interval: float | None = None,
) -> Callable[[], None]:
    """Emit WorkerHeartbeat every ``interval`` seconds on a daemon
    thread until the returned stop callable runs. JsonlSink's lock
    makes the cross-thread emit safe. (Moved from factory.py so every
    run kind keeps its liveness window fresh during long agent calls.)
    """
    stop_event = threading.Event()
    period = HEARTBEAT_INTERVAL_SECONDS if interval is None else interval
    started = time.monotonic()
    pid = os.getpid()

    def _beat() -> None:
        while not stop_event.wait(period):
            bus.emit(WorkerHeartbeat(
                pid=pid,
                elapsed_seconds=round(time.monotonic() - started, 1),
            ))

    thread = threading.Thread(target=_beat, daemon=True, name="ralph-heartbeat")
    thread.start()

    def _stop() -> None:
        stop_event.set()
        thread.join(timeout=1.0)

    return _stop


class _StreamFilterSink:
    """Drops ``Log(kind="stream")`` lines for the given keys before the
    inner sink sees them. Only keys that are teed to a transcript file
    belong here - filtering anything else would silently lose record.
    """

    def __init__(
        self, inner: EventSink,
        drop_stream_keys: frozenset[str] = frozenset({"AI"}),
    ) -> None:
        self._inner = inner
        self._drop = drop_stream_keys

    def emit(self, event: Event) -> None:
        if (
            isinstance(event, Log)
            and event.kind == "stream"
            and event.key in self._drop
        ):
            return
        self._inner.emit(event)

    def close(self) -> None:
        self._inner.close()


@dataclass
class CommandRun:
    """One command execution's recording session. ``paths`` is None
    when recording is disabled - every accessor degrades to None and
    ``close()`` stays safe to call."""

    run_id: str
    kind: str
    bus: EventBus
    paths: RunPaths | None
    _sinks: list[EventSink] = field(default_factory=list)
    _stop_heartbeat: Callable[[], None] | None = None
    _transcripts: dict[str, TextIO] = field(default_factory=dict)
    _restore_run_id: str | None = None
    _restore_component: str | None = None

    @property
    def recording(self) -> bool:
        return self.paths is not None

    def transcript_path(self, component_id: str) -> Path | None:
        if self.paths is None:
            return None
        return self.paths.engineer_log(component_id)

    def transcript_writer(
        self, component_id: str,
    ) -> Callable[[str], None] | None:
        """Line-buffered appender onto the component's transcript file
        (the file the dashboard's transcript pane tails); None when
        recording is disabled. The handle closes with the run."""
        path = self.transcript_path(component_id)
        if path is None:
            return None
        handle = self._transcripts.get(component_id)
        if handle is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a", buffering=1, encoding="utf-8")
            self._transcripts[component_id] = handle

        def _write(line: str) -> None:
            handle.write(line if line.endswith("\n") else line + "\n")

        return _write

    def close(self) -> None:
        """Stop the heartbeat, detach and close the run's file sinks,
        close transcripts, and un-stamp the shared console bus so
        post-run narration reads run-level again."""
        if self._stop_heartbeat is not None:
            self._stop_heartbeat()
            self._stop_heartbeat = None
        for sink in self._sinks:
            self.bus.remove_sink(sink)
            sink.close()
        self._sinks.clear()
        for handle in self._transcripts.values():
            try:
                handle.close()
            except OSError:
                pass
        self._transcripts.clear()
        if self._restore_run_id is not None:
            self.bus.run_id = self._restore_run_id
            self._restore_run_id = None
        if self._restore_component is not None:
            self.bus.component = self._restore_component
            self._restore_component = None


def open_command_run(
    ui: UI,
    root_dir: Path,
    kind: str,
    *,
    component: str = "",
    run_id: str | None = None,
    enabled: bool | None = None,
    heartbeat: bool = True,
) -> CommandRun:
    """Open a recording session for one command execution.

    Reuses the console's bus when ``ui`` is the event bridge (so every
    imperative narration line lands in the run's stream, exactly like
    the factory's chunk-7 wiring); a bare UI gets a private bus.
    ``component`` becomes the bus's default stamp - the pseudo-component
    the reducer projects this command's work onto. ``enabled=None``
    resolves the factory's ``progress_log_enabled`` (env + kstrl.toml).
    """
    if enabled is None:
        from kstrl.factory import FactoryConfig

        enabled = FactoryConfig.load(root_dir).progress_log_enabled
    rid = run_id or mint_run_id(kind)

    restore_run_id: str | None = None
    restore_component: str | None = None
    if isinstance(ui, EventBridgeUI):
        bus = ui.bus
        restore_run_id = bus.run_id
        restore_component = bus.component
        bus.run_id = rid
        bus.component = component
    else:
        bus = EventBus(run_id=rid, component=component)

    run = CommandRun(
        run_id=rid, kind=kind, bus=bus, paths=None,
        _restore_run_id=restore_run_id,
        _restore_component=restore_component,
    )
    if not enabled:
        return run

    paths = RunPaths.for_run(root_dir, rid)
    paths.root.mkdir(parents=True, exist_ok=True)
    sink: EventSink = _StreamFilterSink(JsonlSink(paths.events_file))
    bus.add_sink(sink)
    run.paths = paths
    run._sinks.append(sink)
    if heartbeat:
        run._stop_heartbeat = start_heartbeat(bus)
    return run
