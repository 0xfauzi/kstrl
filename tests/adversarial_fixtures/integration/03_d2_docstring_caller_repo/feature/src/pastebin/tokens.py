"""Bearer tokens (spec.md H1, H2)."""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")


def parse_bearer(value: str) -> str | None:
    """The token in an Authorization header value, or None when it is malformed.

    Callers pass the raw header value exactly as received. A value with
    leading or trailing whitespace returns None, so a request that sends
    one is refused as unauthenticated.
    """
    if value != value.strip():
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not _TOKEN.fullmatch(token):
        return None
    return token
