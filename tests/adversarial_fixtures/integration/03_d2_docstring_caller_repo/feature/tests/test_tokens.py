from __future__ import annotations

import pytest
from pastebin.tokens import parse_bearer


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),
        (" Bearer abc123", None),
        ("Bearer abc123 ", None),
        ("Basic abc123", None),
        ("Bearer ", None),
        ("Bearer a b", None),
    ],
)
def test_parse_bearer(value: str, expected: str | None) -> None:
    assert parse_bearer(value) == expected
