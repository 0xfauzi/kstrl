"""The interaction seam: every blocking prompt as a typed request.

Stage 3 PR A of the TUI rewrite. The harness has exactly seven places
that block on a human (E6 checkpoint, guards, iteration pause, feature
gate, factory/retry confirms, evolve apply). Each becomes a
:class:`PromptRequest` sent through an :class:`InteractionChannel`:

- :class:`UiInteractionChannel` reproduces today's terminal behavior
  exactly (``ui.choose`` under a ``can_prompt`` guard) - the plain-mode
  degradation path.
- :class:`QueueInteractionChannel` is the thread-safe bridge for
  embedded mode (PR F): the orchestrator thread blocks on a
  ``threading.Event`` while the Textual UI answers via
  ``resolve``; ``detach``/``cancel_all`` degrade pending prompts to
  their non-interactive defaults so a dead TUI can never hang a run.

``PromptResponse.answered`` is the load-bearing bit: ``False`` means
"nobody was there to ask" and maps onto today's NOT_PROMPTED /
skip-the-gate semantics at every call site.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ralph_py.agents.base import UsageTotals
    from ralph_py.findings import Finding
    from ralph_py.ui.base import UI


class PromptKind(StrEnum):
    CHECKPOINT = "checkpoint"
    GUARD = "guard"
    ITERATION = "iteration"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class CheckpointContext:
    """Everything a human needs to judge an E6 checkpoint: the diff,
    both finding streams, and the spend so far - not just the review
    summary string the old prompt showed."""

    component_id: str
    diff_excerpt: str = ""
    review_findings: tuple[Finding, ...] = ()
    security_findings: tuple[Finding, ...] = ()
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


@dataclass(frozen=True)
class PromptResponse:
    request_id: str
    choice: int
    answered: bool  # False = non-interactive default (NOT_PROMPTED)


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
            return PromptResponse(
                request_id=req.request_id, choice=req.default, answered=False,
            )
        choice = self._ui.choose(req.header, list(req.options), req.default)
        return PromptResponse(
            request_id=req.request_id, choice=choice, answered=True,
        )


class _Pending:
    def __init__(self, request: PromptRequest) -> None:
        self.request = request
        self.event = threading.Event()
        self.choice: int | None = None


class QueueInteractionChannel:
    """Thread-safe request/response bridge (embedded mode, PR F).

    The requesting thread blocks in :meth:`request` until a resolver
    answers via :meth:`resolve` or the channel is detached/cancelled.
    ``attach`` registers the resolver notification callback (the TUI's
    ``call_from_thread`` entry point - the only orchestrator-to-UI
    crossing, per the spike's G4 validation).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._on_request: Callable[[PromptRequest], None] | None = None

    def attach(self, on_request: Callable[[PromptRequest], None]) -> None:
        with self._lock:
            self._on_request = on_request

    def detach(self) -> None:
        """Subsequent requests degrade to answered=False; pending ones
        are released with their defaults."""
        with self._lock:
            self._on_request = None
        self.cancel_all()

    def can_prompt(self) -> bool:
        with self._lock:
            return self._on_request is not None

    def request(self, req: PromptRequest) -> PromptResponse:
        with self._lock:
            notify = self._on_request
            if notify is None:
                return PromptResponse(
                    request_id=req.request_id, choice=req.default,
                    answered=False,
                )
            pending = _Pending(req)
            self._pending[req.request_id] = pending
        try:
            notify(req)
        except Exception:  # noqa: BLE001 - a dying UI must not hang the run
            with self._lock:
                self._pending.pop(req.request_id, None)
            return PromptResponse(
                request_id=req.request_id, choice=req.default, answered=False,
            )
        pending.event.wait()
        with self._lock:
            self._pending.pop(req.request_id, None)
        if pending.choice is None:
            return PromptResponse(
                request_id=req.request_id, choice=req.default, answered=False,
            )
        return PromptResponse(
            request_id=req.request_id, choice=pending.choice, answered=True,
        )

    def resolve(self, request_id: str, choice: int) -> bool:
        """Answer one pending request; False when it is unknown (already
        cancelled, double-answered, or never existed)."""
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.event.is_set():
                return False
            pending.choice = choice
            pending.event.set()
            return True

    def cancel_all(self) -> None:
        """Release every waiter with its default (answered=False)."""
        with self._lock:
            waiters = list(self._pending.values())
        for pending in waiters:
            pending.event.set()
