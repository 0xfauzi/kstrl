"""Activity feed: the run narrated, one line per meaningful event.

The critique's biggest structural finding: the overview was ~85% dead
space. The factory produces a rich semantic stream; the feed turns the
empty region into the live pulse of the run - what k9s' event pane is
to pods. Deliberately curated: heartbeats, raw Log narration, and
usage rollups stay out (the meter and board carry those); lifecycle,
verdicts, findings, PRs, and checkpoints go in.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import RichLog

from ralph_py import events as ev
from ralph_py.tui import theme

MAX_FEED_LINES = 500


def _stamp(ts: float) -> str:
    if ts <= 0:
        return "--:--:--"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def humanize(event: ev.Event) -> Text | None:  # noqa: C901 - flat dispatch
    """One feed line for a semantic event; None = not feed-worthy."""
    line = Text()
    line.append(_stamp(event.ts) + "  ", style=theme.MUTED)
    comp = event.component

    if isinstance(event, ev.ComponentStarted):
        line.append("● ", style=theme.ACCENT)
        line.append(comp, style="bold")
        line.append(" started", style=theme.MUTED)
    elif isinstance(event, ev.IterationCompleted):
        line.append("· ", style=theme.MUTED)
        line.append(comp, style=theme.MUTED)
        line.append(
            f" iteration {event.iteration} "
            f"({event.duration_seconds:.0f}s)"
            + (" · complete" if event.completed else ""),
            style=theme.MUTED,
        )
    elif isinstance(event, ev.PhaseCompleted):
        if event.passed:
            line.append("✓ ", style=theme.SUCCESS)
        else:
            line.append("✗ ", style=f"bold {theme.ERROR}")
        line.append(comp, style="bold")
        line.append(
            f" {event.phase} {'passed' if event.passed else 'failed'}",
            style="" if event.passed else theme.ERROR,
        )
        if event.duration_seconds:
            line.append(f" in {event.duration_seconds:.0f}s", style=theme.MUTED)
        if event.detail and not event.passed:
            line.append(f" · {event.detail[:80]}", style=theme.MUTED)
    elif isinstance(event, ev.FindingRecorded):
        if event.category == "phase_skipped":
            return None  # bookkeeping, not news
        line.append("▲ ", style=theme.WARNING)
        line.append(comp, style="bold")
        line.append(f" {event.phase} finding ", style=theme.MUTED)
        line.append(f"[{event.severity}] ", style=theme.WARNING)
        line.append(event.category)
        if event.location:
            line.append(f" at {event.location}", style=theme.MUTED)
    elif isinstance(event, ev.ComponentCompleted):
        line.append("✓ ", style=f"bold {theme.SUCCESS}")
        line.append(comp, style=f"bold {theme.SUCCESS}")
        line.append(
            f" completed · {event.iterations} iteration(s)",
            style=theme.MUTED,
        )
    elif isinstance(event, ev.ComponentFailed):
        line.append("✗ ", style=f"bold {theme.ERROR}")
        line.append(comp, style=f"bold {theme.ERROR}")
        line.append(" failed", style=theme.ERROR)
        if event.error:
            line.append(f" · {event.error[:100]}", style=theme.MUTED)
    elif isinstance(event, ev.CircuitBreakerTripped):
        line.append("⊘ ", style=f"bold {theme.ERROR}")
        line.append(comp, style="bold")
        line.append(" no-progress breaker tripped", style=theme.ERROR)
    elif isinstance(event, ev.ComponentRetrying):
        line.append("↻ ", style=theme.WARNING)
        line.append(comp, style="bold")
        line.append(f" retrying (attempt {event.attempt})", style=theme.MUTED)
        if event.reason:
            line.append(f" · {event.reason[:80]}", style=theme.MUTED)
    elif isinstance(event, ev.PrCreated):
        line.append("⇡ ", style=theme.STEEL)
        line.append(comp, style="bold")
        line.append(f" PR #{event.pr_number} opened", style=theme.MUTED)
    elif isinstance(event, ev.PrMerged):
        line.append("⇣ ", style=theme.SUCCESS)
        line.append(comp, style="bold")
        line.append(f" PR #{event.pr_number} merged", style=theme.MUTED)
    elif isinstance(event, ev.PrMergePending):
        line.append("⏸ ", style=theme.VIOLET)
        line.append(comp, style="bold")
        line.append(" merge parked (unconfirmed)", style=theme.MUTED)
    elif isinstance(event, ev.CheckpointRequested):
        line.append("◆ ", style=f"bold {theme.ACCENT}")
        line.append(comp, style="bold")
        line.append(" checkpoint: ", style=theme.ACCENT)
        line.append(event.question, style=theme.MUTED)
    elif isinstance(event, ev.CheckpointResolved):
        line.append("◆ ", style=theme.ACCENT)
        line.append(comp, style="bold")
        line.append(
            f" checkpoint {event.decision} ({event.decided_by})",
            style=theme.MUTED,
        )
    elif isinstance(event, ev.ContractResult):
        glyph = "✓" if event.passed else "✗"
        style = theme.SUCCESS if event.passed else theme.ERROR
        line.append(f"{glyph} ", style=f"bold {style}")
        line.append(f"contract tier {event.tier} ", style="bold")
        line.append("passed" if event.passed else "failed", style=style)
        if event.breaker:
            line.append(f" · breaker {event.breaker}", style=theme.MUTED)
    elif isinstance(event, ev.RunCompleted):
        line.append("■ ", style="bold")
        line.append(
            f"run finished · {event.completed} completed, "
            f"{event.failed} failed, {event.skipped} skipped",
            style="bold",
        )
    else:
        return None
    return line


class ActivityFeed(RichLog):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            max_lines=MAX_FEED_LINES, wrap=False, highlight=False,
            auto_scroll=True,
            **kwargs,  # type: ignore[arg-type]
        )

    def feed_events(self, batch: list[ev.Event]) -> None:
        for event in batch:
            line = humanize(event)
            if line is not None:
                self.write(line)
