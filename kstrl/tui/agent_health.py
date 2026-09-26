"""Is a running component's agent still alive? (#433 M2, operator question Q6)

Two signals, kept apart because they fail differently:

- **Last output**: the newest modification time of the component's raw
  agent transcripts, ``components/<id>/{engineer,review,security,
  distill}.log``. The engineer's lines go to ``engineer.log`` as the
  agent prints them (``factory._transcript``); the review, security and
  distill phases write their own log (``pipeline._phase_transcript``).
  The files carry no per-line time, so the mtime is the only freshness
  signal there is. No file yet reads as "no output yet".
- **Process**: the pid in the component's latest ``worker_heartbeat``
  (``commandrun.start_heartbeat``, every 15 s) is the process that runs
  the agent: the pool worker, or the factory itself when it runs
  components inline. The agent CLI's own pid is kept in memory only
  (``agents/proc.py``) and is not on disk, so it is not what this
  reports, and the label says "worker". No heartbeat means "process
  unknown", never "alive".

  The pid is evidence only while its heartbeat is fresh: within
  ``HEARTBEAT_FRESH_SECONDS``, three heartbeat periods. The heartbeat
  stops when the engineer worker returns, and in pool mode the review
  and security phases then run in the parent while
  ``ProcessPoolExecutor`` keeps that worker for another component; a
  crashed run's pid can be reused. A stale heartbeat's pid is not
  probed and reads "process unknown".

Output older than ``STALE_OUTPUT_SECONDS`` is marked stale (#433 advice
2.4). kstrl configures no output-silence threshold, so this is the one
liveness window it already has, ``runs.LIVE_MTIME_WINDOW_SECONDS``: a
run whose event file is older is no longer called live. The component
detail names it beside the age.

Cost, measured on the harness laptop: four ``stat`` calls and one
``kill(pid, 0)`` per running component, no subprocess. It is called on
the 1 s age tick only for components that are running in a run that has
not finished, so a finished run's recycled pid is never probed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.commandrun import HEARTBEAT_INTERVAL_SECONDS
from kstrl.procgroup import pid_is_alive
from kstrl.tui.run_status import age_phrase
from kstrl.tui.runs import LIVE_MTIME_WINDOW_SECONDS

if TYPE_CHECKING:
    from kstrl.reducer import ComponentState

ALIVE = "alive"
EXITED = "exited"
UNKNOWN = "unknown"

#: How long a heartbeat's pid still says which process runs the agent:
#: two missed beats of ``commandrun.start_heartbeat``'s period.
HEARTBEAT_FRESH_SECONDS = 3 * HEARTBEAT_INTERVAL_SECONDS

#: Output older than this is marked stale; the TUI's run-liveness window.
STALE_OUTPUT_SECONDS = LIVE_MTIME_WINDOW_SECONDS

#: The transcripts an agent writes while it runs, per phase.
OUTPUT_LOGS = ("engineer.log", "review.log", "security.log", "distill.log")


@dataclass(frozen=True)
class AgentHealth:
    #: Seconds since the newest transcript was written; None when none was.
    output_age: float | None
    #: ALIVE | EXITED | UNKNOWN.
    process: str
    pid: int = 0

    @property
    def process_word(self) -> str:
        return self.process if self.process != UNKNOWN else "process unknown"

    @property
    def stale(self) -> bool:
        return self.output_age is not None and self.output_age > STALE_OUTPUT_SECONDS

    def output(self, *, threshold: bool = False) -> str:
        """``output 21s ago``, ``output 3m ago, stale`` or ``no output yet``;
        with ``threshold`` the stale-after window is named (#433 advice 2.4)."""
        if self.output_age is None:
            return "no output yet"
        text = f"output {age_phrase(self.output_age)} ago"
        if self.stale:
            text += ", stale"
        if threshold:
            text += f" (stale after {age_phrase(STALE_OUTPUT_SECONDS)})"
        return text

    def text(self, *, short: bool = False, pid: bool = True, threshold: bool = False) -> str:
        """``output 21s ago · worker 4242 alive``; without the pid
        ``output 21s ago · alive``; short ``21s · alive``. The output age and
        the process are two separate answers and stay two phrases."""
        state = self.process_word
        if short:
            out = age_phrase(self.output_age) if self.output_age is not None else "no output"
            return f"{out}{' stale' if self.stale else ''} · {state}"
        out = self.output(threshold=threshold)
        if not pid or self.process == UNKNOWN:
            return f"{out} · {state}"
        return f"{out} · worker {self.pid} {self.process}"


def newest_output_mtime(component_dir: Path) -> float | None:
    newest: float | None = None
    for name in OUTPUT_LOGS:
        try:
            mtime = os.stat(component_dir / name).st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return newest


def agent_health(
    run_dir: Path | None,
    comp: ComponentState,
    now: float | None = None,
    probe: Callable[[int], bool] = pid_is_alive,
) -> AgentHealth:
    clock = time.time() if now is None else now
    mtime = (
        newest_output_mtime(run_dir / "components" / comp.component_id)
        if run_dir is not None
        else None
    )
    age = max(0.0, clock - mtime) if mtime is not None else None
    pid = comp.heartbeat_pid
    if pid <= 0 or clock - comp.last_heartbeat_ts > HEARTBEAT_FRESH_SECONDS:
        return AgentHealth(output_age=age, process=UNKNOWN)
    return AgentHealth(output_age=age, process=ALIVE if probe(pid) else EXITED, pid=pid)
