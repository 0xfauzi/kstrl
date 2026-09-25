"""Request header handling (spec.md H3)."""

from __future__ import annotations

from collections.abc import Mapping

OWS = " \t"


def header_value(headers: Mapping[str, str], name: str) -> str:
    """Header ``name`` (lower case) with optional whitespace removed, or "" when absent."""
    return headers.get(name, "").strip(OWS)
