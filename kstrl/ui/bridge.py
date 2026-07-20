"""EventBridgeUI: the UI-protocol-to-event-stream migration shim.

Chunk 5 of the TUI rewrite. The old ``UI`` protocol is an imperative
line logger with ~390 call sites; rewriting them all at once is not a
reviewable change. This bridge implements the protocol's 14 methods by
emitting :class:`~kstrl.events.Log` events on an
:class:`~kstrl.events.EventBus` instead of printing - the call sites
keep compiling unchanged while every line they narrate becomes part of
the replayable event stream. A renderer (chunk 7) projects the events
back onto a concrete UI implementation, byte-identically.

Interactivity does NOT go through events: ``choose``/``can_prompt``
delegate to a real :class:`Prompter` (``PlainUI``/``RichUI`` satisfy it
structurally), so blocking prompts remain synchronous and testable.

Raw agent output is a transcript, not an event: ``stream_line`` calls
whose tag is in ``transcript_tags`` route to the ``transcript`` callable
(a file writer) instead of the bus - the spike measured that flooding a
UI surface with full agent output starves input, and the event stream
has the same interest in staying lean.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from kstrl.events import EventBus, Log

DEFAULT_TRANSCRIPT_TAGS = frozenset({"AI"})


class Prompter(Protocol):
    """The interactive sub-surface of the UI protocol."""

    def choose(self, header: str, options: list[str], default: int = 0) -> int: ...

    def can_prompt(self) -> bool: ...


class NullPrompter:
    """Non-interactive prompter: every choice resolves to the default."""

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        return default

    def can_prompt(self) -> bool:
        return False


class EventBridgeUI:
    """Implements the 14-method ``UI`` protocol as Log events on a bus."""

    def __init__(
        self,
        bus: EventBus,
        prompter: Prompter | None = None,
        transcript: Callable[[str], None] | None = None,
        transcript_tags: frozenset[str] = DEFAULT_TRANSCRIPT_TAGS,
    ) -> None:
        self.bus = bus
        self.prompter: Prompter = prompter if prompter is not None else NullPrompter()
        self._transcript = transcript
        self._transcript_tags = transcript_tags

    # -- display methods: one Log event each ---------------------------------

    def title(self, text: str) -> None:
        self.bus.emit(Log(kind="title", text=text))

    def section(self, text: str) -> None:
        self.bus.emit(Log(kind="section", text=text))

    def subsection(self, text: str) -> None:
        self.bus.emit(Log(kind="subsection", text=text))

    def hr(self) -> None:
        self.bus.emit(Log(kind="hr"))

    def kv(self, key: str, value: str) -> None:
        self.bus.emit(Log(kind="kv", key=key, text=value))

    def startup_art(self) -> None:
        self.bus.emit(Log(kind="startup_art"))

    def info(self, text: str) -> None:
        self.bus.emit(Log(severity="info", kind="line", text=text))

    def ok(self, text: str) -> None:
        self.bus.emit(Log(severity="ok", kind="line", text=text))

    def warn(self, text: str) -> None:
        self.bus.emit(Log(severity="warn", kind="line", text=text))

    def err(self, text: str) -> None:
        self.bus.emit(Log(severity="error", kind="line", text=text))

    def channel_header(self, channel: str, title: str = "") -> None:
        self.bus.emit(Log(kind="channel", key=channel, text=title))

    def stream_line(self, tag: str, line: str) -> None:
        if tag in self._transcript_tags and self._transcript is not None:
            try:
                self._transcript(line)
            except Exception:  # noqa: BLE001 - transcripts never gate
                self._transcript = None
            return
        self.bus.emit(Log(kind="stream", key=tag, text=line))

    # -- interactive methods: delegate to the real prompter ------------------

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        return self.prompter.choose(header, options, default)

    def can_prompt(self) -> bool:
        return self.prompter.can_prompt()
