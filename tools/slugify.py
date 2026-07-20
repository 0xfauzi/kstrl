from __future__ import annotations

import re

_NON_ALNUM_RUN_RE = re.compile(r"[^a-z0-9]+")


def _validate_separator(separator: str) -> None:
    if not separator:
        raise ValueError("separator must be a non-empty string")
    if any(char.isalnum() for char in separator):
        raise ValueError(
            f"separator must not contain alphanumeric characters, got {separator!r}"
        )


def _strip_separator(value: str, separator: str) -> str:
    """Strip leading/trailing occurrences of the literal separator string."""
    while value.startswith(separator):
        value = value[len(separator) :]
    while value.endswith(separator):
        value = value[: -len(separator)]
    return value


def slugify(text: str, separator: str = "-") -> str:
    """Convert text to a lowercase slug with runs of non-alphanumeric
    characters collapsed into a single separator occurrence.

    separator must consist entirely of non-alphanumeric characters. This
    guarantees the separator can never collide with slug word content
    (which is always [a-z0-9] after normalization), so leading/trailing
    stripping and word-boundary truncation can never eat or split real
    text.
    """
    _validate_separator(separator)
    normalized = text.strip().lower()
    # Escape backslashes so the separator is treated as a literal
    # replacement rather than a regex backreference (e.g. "\1").
    escaped_separator = separator.replace("\\", "\\\\")
    collapsed = _NON_ALNUM_RUN_RE.sub(escaped_separator, normalized)
    return _strip_separator(collapsed, separator)


def slugify_max(text: str, max_length: int, separator: str = "-") -> str:
    """Slugify text and truncate the result to at most max_length characters.

    When the full slug is too long, truncation prefers the last word
    boundary that fits within max_length so a word is never cut in half.
    Truncation operates on whole words split on the (validated, purely
    non-alphanumeric) separator, so a cut can never land inside - or leave
    a partial fragment of - a multi-character separator. If even the
    first word exceeds max_length (no separator fits), the result is
    hard-truncated to exactly max_length characters.
    """
    if max_length < 1:
        raise ValueError(f"max_length must be at least 1, got {max_length!r}")
    slug = slugify(text, separator=separator)
    if len(slug) <= max_length:
        return slug

    words = slug.split(separator)
    fitted: list[str] = []
    length = 0
    for word in words:
        addition = len(word) + (len(separator) if fitted else 0)
        if length + addition > max_length:
            break
        fitted.append(word)
        length += addition

    if not fitted:
        return words[0][:max_length]
    return separator.join(fitted)


def unique_slug(text: str, existing: set[str], separator: str = "-") -> str:
    """Slugify text and disambiguate it against a set of existing slugs.

    If slugify(text, separator) is empty (e.g. text is all punctuation),
    it is treated as "untitled" before uniqueness is checked. If the
    resulting slug is already present in existing, a numeric suffix
    (separator + N, starting at N=2) is appended and incremented until a
    slug not in existing is found. The existing set is read-only and is
    never mutated by this function.
    """
    slug = slugify(text, separator=separator)
    if not slug:
        slug = "untitled"
    if slug not in existing:
        return slug

    suffix = 2
    candidate = f"{slug}{separator}{suffix}"
    while candidate in existing:
        suffix += 1
        candidate = f"{slug}{separator}{suffix}"
    return candidate
