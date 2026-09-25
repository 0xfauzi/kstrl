"""HTTP handlers for the snippet API (spec.md H1 to H4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pastebin.tokens import parse_bearer

OWS = " \t"


@dataclass(frozen=True)
class Response:
    status: int
    body: str


def get_snippet(
    headers: Mapping[str, str],
    known_tokens: frozenset[str],
    snippets: Mapping[str, str],
    snippet_id: str,
) -> Response:
    """Handle ``GET /snippets/<snippet_id>``. ``headers`` keys are lower case."""
    token = parse_bearer(headers.get("authorization", "").strip(OWS))
    if token is None or token not in known_tokens:
        return Response(401, "missing, malformed or unknown bearer token")
    body = snippets.get(snippet_id)
    if body is None:
        return Response(404, "no such snippet")
    return Response(200, body)
