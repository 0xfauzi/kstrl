"""Snippet records and the rules a new snippet must meet (spec.md S1, S2)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_TITLE_LENGTH = 80
MIN_EXPIRY_DAYS = 1
MAX_EXPIRY_DAYS = 30


class SnippetError(ValueError):
    """A new snippet breaks rule S1 or S2."""


@dataclass(frozen=True)
class Snippet:
    """One snippet. The rules for a new snippet are checked by new_snippet."""

    snippet_id: str
    title: str
    body: str
    created_at: datetime
    expires_at: datetime


def new_snippet(title: str, body: str, expires_in_days: int, now: datetime) -> Snippet:
    """A new snippet created at ``now``. Raises SnippetError when it breaks S1 or S2."""
    if len(title) > MAX_TITLE_LENGTH:
        raise SnippetError(f"title is longer than {MAX_TITLE_LENGTH} characters")
    if not MIN_EXPIRY_DAYS <= expires_in_days <= MAX_EXPIRY_DAYS:
        raise SnippetError(
            f"expiry must be {MIN_EXPIRY_DAYS} to {MAX_EXPIRY_DAYS} days after creation"
        )
    return Snippet(uuid.uuid4().hex, title, body, now, now + timedelta(days=expires_in_days))
