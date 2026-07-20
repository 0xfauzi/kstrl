"""Tests for the tools.slugify module."""

from __future__ import annotations

import pytest

from tools.slugify import slugify, slugify_max


class TestSlugify:
    def test_basic_words(self) -> None:
        assert slugify("Hello Brave World") == "hello-brave-world"

    def test_single_word(self) -> None:
        assert slugify("Extraordinary") == "extraordinary"

    def test_collapses_punctuation_runs(self) -> None:
        assert slugify("Hello,   Brave---World!!") == "hello-brave-world"

    def test_all_punctuation_yields_empty_string(self) -> None:
        assert slugify("!!!") == ""

    def test_custom_separator(self) -> None:
        assert slugify("Hello Brave World", separator="_") == "hello_brave_world"

    def test_empty_separator_raises(self) -> None:
        with pytest.raises(ValueError):
            slugify("Hello World", separator="")


class TestSlugifyMax:
    def test_truncates_at_word_boundary_exact_fit(self) -> None:
        assert slugify_max("Hello Brave World", 11) == "hello-brave"

    def test_truncates_at_earlier_word_boundary_on_mid_word_cut(self) -> None:
        assert slugify_max("Hello Brave World", 10) == "hello"

    def test_max_length_covers_full_slug_returns_unchanged(self) -> None:
        assert slugify_max("Hello Brave World", 100) == "hello-brave-world"

    def test_max_length_equal_to_full_slug_length_returns_unchanged(self) -> None:
        full = slugify("Hello Brave World")
        assert slugify_max("Hello Brave World", len(full)) == full

    def test_hard_truncation_when_first_word_exceeds_max_length(self) -> None:
        assert slugify_max("Extraordinary", 5) == "extra"

    def test_hard_truncation_no_separator_present(self) -> None:
        assert slugify_max("Supercalifragilisticexpialidocious", 4) == "supe"

    def test_zero_max_length_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            slugify_max("x", 0)

    def test_negative_max_length_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            slugify_max("x", -5)

    def test_max_length_one_or_greater_never_raises(self) -> None:
        # Sweep a handful of inputs, including ones that slugify to empty,
        # to confirm no ValueError is raised once max_length >= 1.
        for text in ("Hello Brave World", "Extraordinary", "!!!", "", "a"):
            for max_length in (1, 2, 5, 50):
                slugify_max(text, max_length)  # must not raise

    def test_empty_slug_returns_empty_string_regardless_of_max_length(self) -> None:
        assert slugify_max("!!!", 1) == ""
        assert slugify_max("!!!", 50) == ""

    def test_custom_separator_forwarded_to_slugify(self) -> None:
        assert slugify_max("Hello Brave World", 11, separator="_") == "hello_brave"

    def test_result_length_never_exceeds_max_length(self) -> None:
        for max_length in range(1, 20):
            result = slugify_max("Hello Brave World", max_length)
            assert len(result) <= max_length

    def test_result_never_ends_with_separator(self) -> None:
        for max_length in range(1, 18):
            result = slugify_max("Hello Brave World", max_length)
            assert not result.endswith("-")
