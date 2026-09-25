"""Snippet bodies (spec.md S4)."""

from __future__ import annotations

MAX_SNIPPET_BYTES = 65536


class SnippetError(ValueError):
    """A snippet body breaks rule S4."""


def check_body(body: str) -> bytes:
    """The body encoded as UTF-8. Raises SnippetError when it is larger than S4 allows."""
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_SNIPPET_BYTES:
        raise SnippetError(f"snippet body is larger than {MAX_SNIPPET_BYTES} bytes")
    return encoded
