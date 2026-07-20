"""Graceful shutdown: StopController + signal handlers.

Stage 3 PR B of the TUI rewrite. Before this, Ctrl-C relied on Click's
default KeyboardInterrupt abort: worktree cleanup was skipped, the
executor's ``finally`` could block indefinitely on live workers, and
agent subprocesses were orphaned to their own sessions. Now SIGINT and
SIGTERM request a stop that the scheduling loop honors within its
0.5s wait slice: in-flight workers are group-terminated (grace, then
kill), aborted components are recorded as failed_phase="aborted",
the manifest is flushed, the normal cleanup pass runs, and the run
exits 130. A second signal escalates to force (immediate kill).
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType


@dataclass
class StopController:
    """Cross-thread stop request. ``request`` is idempotent; the second
    call (or a second signal) sets ``force``."""

    reason: str = ""
    force: bool = False
    _event: threading.Event = field(default_factory=threading.Event)

    def request(self, reason: str, *, force: bool = False) -> None:
        if self._event.is_set():
            # Second request escalates.
            self.force = True
        else:
            self.reason = reason
            self.force = force
            self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


def install_signal_handlers(
    stop: StopController,
    *,
    on_second: Callable[[], None] | None = None,
) -> Callable[[], None]:
    """Route SIGINT/SIGTERM into ``stop``; returns an uninstaller.

    Main-thread only (signal.signal requirement). The first signal
    requests a graceful stop; the second sets ``force`` and calls
    ``on_second`` (the immediate-kill path). The uninstaller restores
    the previous handlers - call it in a ``finally``.
    """
    previous: dict[int, object] = {}

    def _handler(signum: int, frame: FrameType | None) -> None:
        del frame
        name = signal.Signals(signum).name
        if stop.is_set():
            stop.request(f"second {name}", force=True)
            if on_second is not None:
                on_second()
        else:
            stop.request(f"received {name}")

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, _handler)

    def _uninstall() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)  # type: ignore[arg-type]
            except (ValueError, TypeError, OSError):
                pass

    return _uninstall
