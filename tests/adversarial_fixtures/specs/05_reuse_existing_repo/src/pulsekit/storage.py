"""Where accepted events go, and how the read side gets them back."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from pulsekit.envelope import EventEnvelope


@dataclass
class EventStore:
    """An append-only log of accepted events, newest last."""

    events: list[EventEnvelope] = field(default_factory=list)

    def append(self, event: EventEnvelope) -> int:
        """Store ``event`` and return its offset in the log."""
        self.events.append(event)
        return len(self.events) - 1

    def since(self, offset: int) -> Iterator[EventEnvelope]:
        """Every event stored at or after ``offset``."""
        yield from self.events[max(offset, 0) :]

    def count_for_device(self, device_id: str) -> int:
        """How many stored events came from ``device_id``."""
        return sum(1 for event in self.events if event.device_id == device_id)
