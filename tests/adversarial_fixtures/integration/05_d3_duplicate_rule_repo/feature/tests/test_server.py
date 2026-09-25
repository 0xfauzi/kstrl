from __future__ import annotations

import pytest
from pastebin.server import MAX_REQUEST_BODY_BYTES, Refusal, accept_snippet, admit
from pastebin.snippets import MAX_SNIPPET_BYTES


@pytest.mark.parametrize("length", ["0", "12", f" {MAX_REQUEST_BODY_BYTES}\t"])
def test_a_body_within_the_limit_is_admitted(length: str) -> None:
    assert admit({"content-length": length}) is None


def test_a_body_over_the_limit_is_413() -> None:
    refusal = admit({"content-length": str(MAX_REQUEST_BODY_BYTES + 1)})
    assert refusal is not None and refusal.status == 413


@pytest.mark.parametrize("headers", [{}, {"content-length": "-1"}, {"content-length": "1e3"}])
def test_a_missing_or_malformed_length_is_411(headers: dict[str, str]) -> None:
    refusal = admit(headers)
    assert refusal is not None and refusal.status == 411


def test_an_admitted_snippet_is_stored_as_utf8() -> None:
    assert accept_snippet({"content-length": "20"}, "h\u00e9llo") == "h\u00e9llo".encode()


def test_a_snippet_over_s4_is_422() -> None:
    result = accept_snippet({"content-length": "20"}, "x" * (MAX_SNIPPET_BYTES + 1))
    assert isinstance(result, Refusal) and result.status == 422
