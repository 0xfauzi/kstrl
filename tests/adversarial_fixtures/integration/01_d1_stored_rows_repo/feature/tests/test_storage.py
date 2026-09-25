from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pastebin.snippets import MAX_EXPIRY_DAYS, MAX_TITLE_LENGTH, SnippetError
from pastebin.storage import SnippetStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[SnippetStore]:
    opened = SnippetStore(":memory:")
    yield opened
    opened.close()


def test_added_snippets_are_listed_oldest_first(store: SnippetStore) -> None:
    first = store.add("first", "a", 1, NOW)
    second = store.add("second", "b", MAX_EXPIRY_DAYS, NOW.replace(day=2))
    assert store.list_all() == [first, second]


def test_a_title_over_the_limit_is_refused(store: SnippetStore) -> None:
    with pytest.raises(SnippetError):
        store.add("x" * (MAX_TITLE_LENGTH + 1), "a", 1, NOW)
    assert store.list_all() == []


@pytest.mark.parametrize("days", [0, MAX_EXPIRY_DAYS + 1])
def test_an_expiry_outside_the_range_is_refused(store: SnippetStore, days: int) -> None:
    with pytest.raises(SnippetError):
        store.add("t", "a", days, NOW)
    assert store.list_all() == []
