"""Textual messages for the dashboard (stage 3 PR D)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.message import Message

if TYPE_CHECKING:
    from kstrl.reducer import RunState
    from kstrl.safemode import SafeModeReason
    from kstrl.tui.home_data import HomeStats, RunSummary


class StateChanged(Message):
    """New events were folded into the RunState. Posted at most once
    per poll (coalescing is structural: polls are <=5Hz and only actual
    changes post - spike finding 3's render policy)."""

    def __init__(self, state: RunState) -> None:
        super().__init__()
        self.state = state


class SafeModeChecked(Message):
    """The background safe-mode check reported (R10.4 follow-up).

    Carries the list rather than a count so the panel and the chip
    render from ONE evaluation: two readers of the same predicate could
    disagree about what the factory is doing right now.
    """

    def __init__(self, reasons: list[SafeModeReason], *, seq: int = 0) -> None:
        super().__init__()
        self.reasons = reasons
        #: The check's start order. A superseded check that finishes late
        #: still posts (a thread cannot be cancelled), so the handler
        #: needs to tell a late answer from a current one.
        self.seq = seq


class SummariesReady(Message):
    """The home worker finished folding run summaries (D2)."""

    def __init__(
        self, summaries: dict[str, RunSummary], stats: HomeStats,
    ) -> None:
        super().__init__()
        self.summaries = summaries
        self.stats = stats
