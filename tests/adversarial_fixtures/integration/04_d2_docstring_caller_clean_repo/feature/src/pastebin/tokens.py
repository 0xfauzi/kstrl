"""Bearer tokens (spec.md H1, H2)."""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")


def parse_bearer(value: str) -> str | None:
    """The token in an Authorization header value, or None when it is malformed.

    Callers strip optional whitespace (spec.md H3) from the raw header value
    first. A value that still has leading or trailing whitespace returns None.
    """
    if value != value.strip():
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not _TOKEN.fullmatch(token):
        return None
    return token
