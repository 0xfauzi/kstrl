"""Graceful shutdown: StopController + signal handlers.

Stage 3 PR B of the TUI rewrite. Before this, Ctrl-C relied on Click's
default KeyboardInterrupt abort: worktree cleanup was skipped, the
executor's ``finally`` could block indefinitely on live workers, and
agent subprocesses were orphaned to their own sessions. Now SIGINT,
SIGTERM and SIGHUP request a stop that the scheduling loop honors
within its 0.5s wait slice: in-flight workers are group-terminated
(grace, then kill), aborted components are recorded as
failed_phase="aborted", the manifest is flushed, the normal cleanup
pass runs, and the run exits 130. A second signal escalates to force
(immediate kill).
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType

from kstrl.agents.proc import kill_active_process_groups


@dataclass
class StopController:
    """Cross-thread stop request. ``request`` is idempotent; the second
    call (or a second signal) sets ``force``."""

    reason: str = ""
    force: bool = False
    _event: threading.Event = field(default_factory=threading.Event)

    def request(self, reason: str, *, force: bool = False) -> None:
        """Set the stop, and end the agents running in THIS process.

        #461: with one worker the agent runs on the scheduler's thread,
        inside ``executor.submit``, so the loop that honours the stop does
        not run again until the agent's iteration ends (measured: 120 s
        for an agent sleeping 120 s). Ending the agents ends the
        iteration. #642: that kill lived only in the signal handler, so
        the TUI's confirmed stop set a flag and the agent ran on (measured:
        alive 15 s after ``y``, and the run never exited). Every request
        now starts the kill, on a daemon thread, because ``kill`` can wait
        out the whole grace and the caller may be the TUI's event loop.
        Pool workers hold no agent in this process, so this is a no-op
        for them; their agents end through ``_abort_inflight``.
        """
        if self._event.is_set():
            # Second request escalates.
            self.force = True
        else:
            self.reason = reason
            self.force = force
            self._event.set()
        threading.Thread(
            target=kill_active_process_groups, name="kstrl-stop-kill", daemon=True
        ).start()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


def install_signal_handlers(
    stop: StopController,
    *,
    on_second: Callable[[], None] | None = None,
) -> Callable[[], None]:
    """Route SIGINT, SIGTERM and SIGHUP into ``stop``; returns an uninstaller.

    Main-thread only (signal.signal requirement). The first signal
    requests a graceful stop; the second sets ``force`` and calls
    ``on_second`` (the immediate-kill path). Every signal also ends the
    agents running in THIS process (#461), through ``stop.request``.
    SIGHUP is left alone when it is already ignored. The uninstaller
    restores the previous handlers - call it in a ``finally``.
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

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if signum == signal.SIGHUP and signal.getsignal(signum) == signal.SIG_IGN:
            # Started under nohup: the operator asked for the run to
            # outlive the terminal, so a hangup stays ignored.
            continue
        previous[signum] = signal.signal(signum, _handler)

    def _uninstall() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)  # type: ignore[arg-type]
            except (ValueError, TypeError, OSError):
                pass

    return _uninstall
