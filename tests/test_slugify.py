from __future__ import annotations

import pytest

from tools.slugify import slugify


class TestBasicAsciiUnicode:
    def test_simple_ascii(self) -> None:
        assert slugify("Hello, World!") == "hello-world"

    def test_unicode_accents(self) -> None:
        assert slugify("Crème Brûlée") == "creme-brulee"

    def test_only_non_alphanumeric(self) -> None:
        assert slugify("!!!") == ""

    def test_empty_string(self) -> None:
        assert slugify("") == ""


class TestWhitespaceCollapsing:
    def test_spaced_out(self) -> None:
        assert slugify("  --spaced--  out__ ") == "spaced-out"

    def test_consecutive_punctuation(self) -> None:
        assert slugify("hello...world") == "hello-world"

    def test_leading_trailing_separators(self) -> None:
        assert slugify("---hello---") == "hello"


class TestCustomSeparator:
    def test_underscore_separator(self) -> None:
        assert slugify("a b", separator="_") == "a_b"

    def test_empty_with_custom_separator(self) -> None:
        assert slugify("", separator="_") == ""

    def test_custom_separator_collapse(self) -> None:
        assert slugify("hello...world", separator="_") == "hello_world"


class TestSeparatorValidation:
    def test_multi_char_separator(self) -> None:
        with pytest.raises(ValueError, match="single character"):
            slugify("hello", separator="ab")

    def test_digit_separator(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            slugify("hello", separator="1")

    def test_letter_separator(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            slugify("hello", separator="a")


class TestEdgeCases:
    def test_only_digits(self) -> None:
        assert slugify("12345") == "12345"

    def test_leading_trailing_non_alphanumeric(self) -> None:
        assert slugify("!!!hello!!!") == "hello"

    def test_high_unicode_characters(self) -> None:
        assert slugify("Café naïve") == "cafe-naive"
