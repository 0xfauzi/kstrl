"""Bearer tokens (spec.md H1)."""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")


def parse_bearer(value: str) -> str | None:
    """The token in an Authorization header value such as ``Bearer abc123``,
    or None when the value is not ``Bearer <token>``.

    ``value`` is the whole header value, scheme included: a bare token
    returns None.
    """
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not _TOKEN.fullmatch(token):
        return None
    return token
