"""A run's state as one word, and the reason behind it (#433 F4).

The home run table used to mark a run with a glyph alone: ``✓``, ``✗``,
``●``, or a dim ``·`` that meant both "not folded yet" and "stopped
without a finish record". An interrupted run looked exactly like one that
had not been read. Every surface now says one of four words, and every
state that is not ``completed`` carries a reason built from the folded
event stream:

- ``running``: the run's writer is alive (a held factory.lock, or events
  written in the last minute). Reason: what each running component is
  doing.
- ``completed``: a finish record and no failed component.
- ``failed``: a finish record and at least one failed component. Reason:
  which components, and the phase that failed.
- ``unknown``: no finish record and no live writer. The run may have been
  killed, or refused to start; the log cannot say which, so the word does
  not guess. Reason: the run's last error narration when it wrote one.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from kstrl.tui import theme

if TYPE_CHECKING:
    from kstrl.reducer import ComponentState, RunState

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
UNKNOWN = "unknown"
#: What a run in the unknown state says when its events name no cause.
NO_REASON = "no reason recorded"

#: outcome (home_data.RunSummary.outcome) -> state word.
_WORD_BY_OUTCOME = {"live": RUNNING, "done": COMPLETED, "failed": FAILED, "stale": UNKNOWN}

#: state word -> (glyph, colour). Colour is always paired with the word.
RUN_STATE_STYLE: dict[str, tuple[str, str]] = {
    RUNNING: ("●", theme.ACCENT),
    COMPLETED: ("✓", theme.SUCCESS),
    FAILED: ("✗", theme.ERROR),
    UNKNOWN: ("?", theme.WARNING),
}


def state_word(outcome: str) -> str:
    return _WORD_BY_OUTCOME.get(outcome, UNKNOWN)


def finished_word(state: RunState) -> str:
    """``failed`` when a component this run ran failed, else ``completed``.

    The run header said "finished" in green over a board with a failed
    row (#433 F4); a finished run is one of the two terminal words.
    """
    ran = (comp for comp in state.components.values() if not comp.carried)
    return FAILED if any(comp.status == "failed" for comp in ran) else COMPLETED


def age_phrase(seconds: float) -> str:
    """``42s``, ``7m``, ``3h05m``, ``2d``: a whole number, never cut."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d"


def component_took(comp: ComponentState) -> float:
    """How long a component took in this run, in seconds.

    The span between its first and last event, or the sum of the phase
    durations it recorded when that is larger. The phase durations are
    measured by the phase itself; the span is only as good as the event
    timestamps, and a stream written after the fact (or in one burst)
    makes it 0 while the engineer phase alone says 312 s (#433: the
    detail header read "took 0s" over a strip reading "engineer 312s").
    """
    span = comp.last_event_ts - comp.started_ts if comp.started_ts else 0.0
    phases = sum(float(entry.get("duration_seconds") or 0.0) for entry in comp.phase_history)
    return max(span, phases, 0.0)


def _running_detail(comp: ComponentState) -> str:
    parts = [comp.component_id]
    if comp.phase:
        parts.append(comp.phase)
    if comp.iteration:
        limit = f"/{comp.max_iterations}" if comp.max_iterations else ""
        parts.append(f"iteration {comp.iteration}{limit}")
    if comp.attempt > 1:
        parts.append(f"attempt {comp.attempt}")
    return " ".join(parts)


def running_brief(comp: ComponentState) -> str:
    """``http-app engineer 3/10``: component, phase and progress in few
    cells, for a row too narrow for ``_running_detail`` (#433 advice 2.6)."""
    parts = [comp.component_id]
    if comp.phase:
        parts.append(comp.phase)
    if comp.iteration:
        parts.append(
            f"{comp.iteration}/{comp.max_iterations}"
            if comp.max_iterations
            else f"iteration {comp.iteration}"
        )
    if comp.attempt > 1:
        parts.append(f"attempt {comp.attempt}")
    return " ".join(parts)


def failed_cause(comp: ComponentState) -> str:
    """``<phase>: <cause>`` for the phase that failed last, else the error.

    The cause is the gate's first failure when verification recorded one
    (#433 F7), then the phase's detail, then the component's error.
    """
    for entry in reversed(comp.phase_history):
        if entry.get("passed"):
            continue
        failures = entry.get("failures") or []
        cause = failures[0] if failures else entry.get("detail") or comp.error
        return f"{entry.get('phase', '?')}: {cause}" if cause else str(entry.get("phase", ""))
    return comp.error


def error_summary(state: RunState) -> str:
    """The run's last error headline, with its first detail line."""
    block = state.error_block
    if not block:
        return ""
    text = block[0]
    if len(block) > 1:
        text += f": {block[1]}"
    if len(block) > 2:
        text += f" (+{len(block) - 2} more)"
    return text


def _running_reason(ran: list[ComponentState]) -> str:
    active = [c for c in ran if c.status in ("running", "verifying")]
    if not active:
        return "no component running yet"
    return "; ".join(_running_detail(comp) for comp in active)


def _named_failure(comp: ComponentState) -> str:
    cause = failed_cause(comp)
    return f"{comp.component_id} ({cause})" if cause else comp.component_id


def _failed_reason(ran: list[ComponentState], state: RunState) -> str:
    named = "; ".join(_named_failure(c) for c in ran if c.status == "failed")
    return named or error_summary(state)


def _unknown_reason(state: RunState, now: float) -> str:
    last = state.last_event_ts
    stopped = f"no finish record; last event {age_phrase(now - last)} ago" if last else ""
    said = error_summary(state)
    if said and stopped:
        return f"{said} ({stopped})"
    return said or stopped or "no events"


def run_reason(word: str, state: RunState, now: float | None = None) -> str:
    """Why a run is in ``word``; "" for ``completed``."""
    ran = [comp for comp in state.components.values() if not comp.carried]
    if word == RUNNING:
        return _running_reason(ran)
    if word == FAILED:
        return _failed_reason(ran, state)
    if word == UNKNOWN:
        return _unknown_reason(state, now if now is not None else time.time())
    return ""
