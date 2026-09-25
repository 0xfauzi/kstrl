"""The request gate in front of the snippet handlers (spec.md H3, H9)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pastebin.snippets import SnippetError, check_body

_OWS = " \t"
MAX_REQUEST_BODY_BYTES = 65536


@dataclass(frozen=True)
class Refusal:
    status: int
    reason: str


def admit(headers: Mapping[str, str]) -> Refusal | None:
    """None when the request body may be read, else the refusal to send (H9).

    ``headers`` keys are lower case.
    """
    raw = headers.get("content-length", "").strip(_OWS)
    if not (raw.isascii() and raw.isdigit()):
        return Refusal(411, "a request body needs a numeric Content-Length")
    if int(raw) > MAX_REQUEST_BODY_BYTES:
        return Refusal(413, f"request body is larger than {MAX_REQUEST_BODY_BYTES} bytes")
    return None


def accept_snippet(headers: Mapping[str, str], snippet: str) -> Refusal | bytes:
    """The stored form of ``snippet``, or the refusal to send (H9, then S4)."""
    refusal = admit(headers)
    if refusal is not None:
        return refusal
    try:
        return check_body(snippet)
    except SnippetError as exc:
        return Refusal(422, str(exc))
