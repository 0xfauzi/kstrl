"""SQLite storage for snippets (spec.md S3)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pastebin.snippets import Snippet, new_snippet

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snippets (
    snippet_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
"""


class SnippetStore:
    """Every snippet added, until the store is deleted."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def add(self, title: str, body: str, expires_in_days: int, now: datetime) -> Snippet:
        """Store a new snippet. Raises SnippetError when it breaks S1 or S2."""
        snippet = new_snippet(title, body, expires_in_days, now)
        self._db.execute(
            "INSERT INTO snippets VALUES (?, ?, ?, ?, ?)",
            (
                snippet.snippet_id,
                snippet.title,
                snippet.body,
                snippet.created_at.isoformat(),
                snippet.expires_at.isoformat(),
            ),
        )
        self._db.commit()
        return snippet

    def list_all(self) -> list[Snippet]:
        """Every stored snippet, oldest first."""
        rows = self._db.execute(
            "SELECT snippet_id, title, body, created_at, expires_at "
            "FROM snippets ORDER BY created_at"
        ).fetchall()
        return [
            Snippet(
                snippet_id,
                title,
                body,
                datetime.fromisoformat(created_at),
                datetime.fromisoformat(expires_at),
            )
            for snippet_id, title, body, created_at, expires_at in rows
        ]
