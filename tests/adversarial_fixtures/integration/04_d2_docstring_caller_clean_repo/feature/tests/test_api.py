from __future__ import annotations

import pytest
from pastebin.api import get_snippet

TOKENS = frozenset({"abc123"})
SNIPPETS = {"s1": "hello"}


@pytest.mark.parametrize("value", ["Bearer abc123", " Bearer abc123", "Bearer abc123\t "])
def test_a_known_token_reads_the_snippet(value: str) -> None:
    response = get_snippet({"authorization": value}, TOKENS, SNIPPETS, "s1")
    assert (response.status, response.body) == (200, "hello")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer nope"},
        {"authorization": "Basic abc123"},
        {"authorization": "Bearer"},
    ],
)
def test_a_missing_malformed_or_unknown_token_is_401(headers: dict[str, str]) -> None:
    assert get_snippet(headers, TOKENS, SNIPPETS, "s1").status == 401


def test_an_unknown_snippet_is_404() -> None:
    headers = {"authorization": "Bearer abc123"}
    assert get_snippet(headers, TOKENS, SNIPPETS, "nope").status == 404
