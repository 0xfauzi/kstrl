"""The wire shape of one telemetry event and its parser."""

from __future__ import annotations

import json
from dataclasses import dataclass


class EnvelopeError(ValueError):
    """The payload is not a telemetry envelope this service accepts."""


@dataclass(frozen=True)
class EventEnvelope:
    """One accepted event: who sent it, when, and the body."""

    device_id: str
    sent_at: float
    kind: str
    body: dict[str, object]


def parse_envelope(raw: bytes) -> EventEnvelope:
    """Parse one envelope from request bytes.

    Raises ``EnvelopeError`` for anything the service will not store:
    bad JSON, a missing field, or a field of the wrong type.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError(f"payload is not UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EnvelopeError("payload is not a JSON object")
    try:
        device_id = payload["deviceId"]
        sent_at = payload["sentAt"]
        kind = payload["kind"]
    except KeyError as exc:
        raise EnvelopeError(f"payload is missing {exc.args[0]!r}") from exc
    if not isinstance(device_id, str) or not device_id:
        raise EnvelopeError("deviceId must be a non-empty string")
    if not isinstance(sent_at, int | float):
        raise EnvelopeError("sentAt must be a number")
    if not isinstance(kind, str):
        raise EnvelopeError("kind must be a string")
    body = payload.get("body")
    return EventEnvelope(
        device_id=device_id,
        sent_at=float(sent_at),
        kind=kind,
        body=body if isinstance(body, dict) else {},
    )
