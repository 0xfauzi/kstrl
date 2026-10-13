"""The interaction seam: every blocking prompt as a typed request.

The harness has exactly seven places that block on a human (E6 checkpoint,
guards, iteration pause, feature gate, factory/retry confirms, evolve apply).
Each becomes a :class:`PromptRequest` sent through an :class:`InteractionChannel`.

:class:`UiInteractionChannel` asks on the terminal (``ui.choose`` under a
``can_prompt`` guard). It is the only channel. A run with no terminal
gets no answer, and the E6 merge gate then parks the component and files
a ``merge_gate`` inbox item that ``ks inbox approve`` resolves.

``PromptResponse.choice`` is ``None`` exactly when nobody answered: no
one was there to ask, the prompt was interrupted, or the answer was out
of range. It never carries the prompt's default,
which is Start or Approve at most prompts, so a reader that tests the
choice alone cannot read "nobody answered" as consent (#647).
``PromptResponse.answered`` is derived from it, never stored beside it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from kstrl.findings import Finding
    from kstrl.ui.base import UI
    from kstrl.usage import UsageTotals


class PromptKind(StrEnum):
    CHECKPOINT = "checkpoint"
    GUARD = "guard"
    ITERATION = "iteration"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class CheckpointContext:
    """What a human judges an E6 checkpoint on: the review findings,
    the spend so far and the branch - not just the review summary
    string the old prompt showed."""

    component_id: str
    review_findings: tuple[Finding, ...] = ()
    usage: UsageTotals | None = None
    branch: str = ""


@dataclass(frozen=True)
class PromptRequest:
    kind: PromptKind
    header: str
    options: tuple[str, ...]
    default: int = 0
    component_id: str = ""
    checkpoint: CheckpointContext | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError("prompt options must not be empty")
        if self.default < 0 or self.default >= len(self.options):
            raise ValueError("prompt default must index an option")


@dataclass(frozen=True)
class PromptResponse:
    request_id: str
    #: The option index the operator picked, or None when nobody answered.
    #: Never the prompt's default (#647).
    choice: int | None

    @property
    def answered(self) -> bool:
        """Whether anyone answered: derived from ``choice``, so the two
        cannot disagree (#647). False is never consent (#594)."""
        return self.choice is not None


class InteractionChannel(Protocol):
    def can_prompt(self) -> bool: ...

    def request(self, req: PromptRequest) -> PromptResponse: ...


class UiInteractionChannel:
    """Terminal-mode channel: exactly today's ui.choose behavior."""

    def __init__(self, ui: UI) -> None:
        self._ui = ui

    def can_prompt(self) -> bool:
        return self._ui.can_prompt()

    def request(self, req: PromptRequest) -> PromptResponse:
        if not self._ui.can_prompt():
            return PromptResponse(request_id=req.request_id, choice=None)
        try:
            choice = self._ui.choose(req.header, list(req.options), req.default)
        except (EOFError, KeyboardInterrupt):
            # #594 A2: an interrupted prompt is unanswered, not a choice.
            # Caught HERE rather than inside each ``UI.choose`` so a
            # prompter that lets the interrupt through - ``RichUI`` did,
            # raising it past this channel, past the pipeline, filing no
            # inbox item - degrades the same way ``PlainUI`` does.
            return PromptResponse(request_id=req.request_id, choice=None)
        if isinstance(choice, bool) or not 0 <= choice < len(req.options):
            return PromptResponse(request_id=req.request_id, choice=None)
        return PromptResponse(request_id=req.request_id, choice=choice)
