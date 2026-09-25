from __future__ import annotations

from pastebin.api import header_value


def test_optional_whitespace_is_removed() -> None:
    assert header_value({"content-length": " \t12\t "}, "content-length") == "12"


def test_an_absent_header_is_empty() -> None:
    assert header_value({}, "content-length") == ""
