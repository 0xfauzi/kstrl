"""The HTTP surface: one ingest route and one admin replay route.

The admin route is throttled with ``pulsekit.throttle.token_bucket``.
The ingest route is not throttled at all, which is the gap the current
spec is about.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulsekit.envelope import EnvelopeError, parse_envelope
from pulsekit.storage import EventStore
from pulsekit.throttle.token_bucket import BucketRegistry


@dataclass(frozen=True)
class Response:
    """What a route hands back to the server adapter."""

    status: int
    body: dict[str, object]
    headers: dict[str, str]


@dataclass
class IngestApi:
    """Routes over one store. ``admin_limits`` throttles replay only."""

    store: EventStore
    admin_limits: BucketRegistry

    def post_events(self, raw: bytes) -> Response:
        """Accept one event. No per-caller limit is applied here."""
        try:
            envelope = parse_envelope(raw)
        except EnvelopeError as exc:
            return Response(status=400, body={"error": str(exc)}, headers={})
        offset = self.store.append(envelope)
        return Response(status=202, body={"offset": offset}, headers={})

    def get_replay(self, operator: str, offset: int, now: float) -> Response:
        """Replay stored events for an operator, throttled per operator."""
        bucket = self.admin_limits.bucket_for(operator)
        if not bucket.take(now):
            wait = bucket.retry_after(now)
            return Response(
                status=429,
                body={"error": "replay rate exceeded"},
                headers={"Retry-After": str(int(wait) + 1)},
            )
        events = [
            {"deviceId": event.device_id, "kind": event.kind} for event in self.store.since(offset)
        ]
        return Response(status=200, body={"events": events}, headers={})
