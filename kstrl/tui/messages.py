"""Textual messages for the dashboard (stage 3 PR D)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.message import Message

if TYPE_CHECKING:
    from kstrl.reducer import RunState


class StateChanged(Message):
    """New events were folded into the RunState. Posted at most once
    per poll (coalescing is structural: polls are <=5Hz and only actual
    changes post - spike finding 3's render policy)."""

    def __init__(self, state: RunState) -> None:
        super().__init__()
        self.state = state
