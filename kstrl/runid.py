"""Run-id minting and parsing, shared by every command kind.

A run id is ``<kind>-YYYYMMDD-HHMMSS.ffffff-<nonce>``. The kind prefix
names the command that produced the run (``factory``, ``decompose``,
``feature``, ``understand``); the microsecond stamp makes same-second
ids order deterministically by creation time; the nonce guards against
collisions inside the same microsecond.

Whole-id lexicographic ordering breaks the moment two kinds coexist
(``decompose-*`` < ``factory-*`` regardless of date), so discovery and
the reducer sort by ``run_sort_key`` - the stamp after the kind prefix,
which stays lexicographically chronological.
"""

from __future__ import annotations

from typing import Final

KNOWN_KINDS: Final = ("factory", "decompose", "feature", "understand")


def mint_run_id(kind: str = "factory") -> str:
    import secrets
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S.%f")
    return f"{kind}-{stamp}-{secrets.token_hex(3)}"


def run_kind(run_id: str) -> str:
    """The kind prefix; "" for ids with no ``-`` at all."""
    head, sep, _ = run_id.partition("-")
    return head if sep else ""


def run_sort_key(run_id: str) -> str:
    """Chronologically sortable key: the id minus its kind prefix.

    Falls back to the whole id when there is no prefix, so sorting a
    mixed list of well-formed and foreign names stays total.
    """
    _, sep, rest = run_id.partition("-")
    return rest if sep else run_id
