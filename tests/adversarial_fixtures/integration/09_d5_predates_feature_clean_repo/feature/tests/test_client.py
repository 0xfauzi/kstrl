from __future__ import annotations

import pytest
from pastebin.client import ClientError, build_request, load_token, main


def test_a_missing_token_is_refused() -> None:
    with pytest.raises(ClientError):
        load_token({})


def test_the_request_carries_the_bearer_token() -> None:
    request = build_request("http://h", "abc123", "a/b")
    assert request.full_url == "http://h/snippets/a%2Fb"
    assert request.get_header("Authorization") == "Bearer abc123"


def test_an_unknown_command_is_a_usage_error() -> None:
    assert main(["put", "x"], {}) == 2


def test_a_valid_token_is_loaded() -> None:
    assert load_token({"PASTEBIN_TOKEN": "abc123"}) == "abc123"


@pytest.mark.parametrize("token", ["abc 123", "abc\n"])
def test_a_malformed_token_is_refused(token: str) -> None:
    with pytest.raises(ClientError):
        load_token({"PASTEBIN_TOKEN": token})
