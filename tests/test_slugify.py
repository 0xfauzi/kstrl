"""Tests for the tools.slugify module."""

from __future__ import annotations

import pytest

from tools.slugify import slugify, slugify_max, unique_slug


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


class TestSlugifyMaxMultiCharSeparator:
    def test_cut_mid_separator_does_not_leave_partial_fragment(self) -> None:
        # slug is "foo__bar__baz"; max_length=4 lands one character past
        # the first underscore of the "foo" -> "bar" separator, i.e.
        # inside the two-character separator itself.
        assert slugify_max("foo bar baz", 4, separator="__") == "foo"

    def test_cut_exactly_at_separator_boundary(self) -> None:
        assert slugify_max("foo bar baz", 3, separator="__") == "foo"

    def test_cut_after_full_word_and_separator(self) -> None:
        assert slugify_max("foo bar baz", 8, separator="__") == "foo__bar"

    def test_hard_truncation_with_multi_char_separator(self) -> None:
        assert slugify_max("extraordinarily nice", 5, separator="__") == "extra"

    def test_no_trailing_partial_separator_across_all_cut_points(self) -> None:
        text = "foo bar baz qux"
        separator = "__"
        full = slugify(text, separator=separator)
        for max_length in range(1, len(full) + 5):
            result = slugify_max(text, max_length, separator=separator)
            assert len(result) <= max_length
            assert not result.endswith("_")
            assert not result.startswith("_")


class TestSlugifySeparatorValidation:
    def test_alphanumeric_letter_separator_raises(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            slugify("Hello World", separator="x")

    def test_alphanumeric_digit_separator_raises(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            slugify("Hello World", separator="1")

    def test_mixed_alphanumeric_separator_raises(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            slugify("Hello World", separator="a-")

    def test_alphanumeric_separator_raises_via_slugify_max(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            slugify_max("Hello World", 5, separator="ab")

    def test_alphanumeric_separator_cannot_eat_leading_text(self) -> None:
        # Regression guard: an alphanumeric separator must be rejected
        # rather than silently stripped from real word content that
        # happens to match it (e.g. text starting with "xy" and
        # separator="xy").
        with pytest.raises(ValueError):
            slugify("xylophone show", separator="xy")

    def test_non_alnum_multi_char_separator_still_allowed(self) -> None:
        assert slugify("Hello World", separator="::") == "hello::world"


class TestUniqueSlug:
    def test_returns_slug_unchanged_when_not_in_existing_set(self) -> None:
        assert unique_slug("Post", set()) == "post"

    def test_unchanged_slug_not_present_in_larger_existing_set(self) -> None:
        assert unique_slug("Post", {"other-post", "another"}) == "post"

    def test_appends_suffix_2_on_first_collision(self) -> None:
        assert unique_slug("Post", {"post"}) == "post-2"

    def test_appends_suffix_3_when_suffix_2_also_collides(self) -> None:
        assert unique_slug("Post", {"post", "post-2"}) == "post-3"

    def test_keeps_incrementing_past_multiple_collisions(self) -> None:
        existing = {"post", "post-2", "post-3", "post-4"}
        assert unique_slug("Post", existing) == "post-5"

    def test_empty_slug_maps_to_untitled(self) -> None:
        assert unique_slug("!!!", set()) == "untitled"

    def test_untitled_collision_resolution(self) -> None:
        assert unique_slug("!!!", {"untitled"}) == "untitled-2"

    def test_untitled_collision_resolution_multiple(self) -> None:
        assert unique_slug("!!!", {"untitled", "untitled-2"}) == "untitled-3"

    def test_does_not_mutate_existing_set(self) -> None:
        existing = {"post"}
        result = unique_slug("Post", existing)
        assert result == "post-2"
        assert existing == {"post"}

    def test_custom_separator_used_for_suffix(self) -> None:
        assert unique_slug("Post", {"post"}, separator="_") == "post_2"

    def test_sequential_calls_build_a_unique_set(self) -> None:
        existing: set[str] = set()
        slugs = []
        for _ in range(3):
            slug = unique_slug("Post", existing)
            existing.add(slug)
            slugs.append(slug)
        assert slugs == ["post", "post-2", "post-3"]
