from __future__ import annotations

import re

_NON_ALNUM_RUN_RE = re.compile(r"[^a-z0-9]+")


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
    """
    if not separator:
        raise ValueError("separator must be a non-empty string")
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
    If even the first word exceeds max_length (no separator fits), the
    result is hard-truncated to exactly max_length characters.
    """
    if max_length < 1:
        raise ValueError(f"max_length must be at least 1, got {max_length!r}")
    slug = slugify(text, separator=separator)
    if len(slug) <= max_length:
        return slug
    truncated = slug[:max_length]
    if slug.startswith(separator, max_length):
        return truncated
    boundary = truncated.rfind(separator)
    if boundary == -1:
        return truncated
    return truncated[:boundary]
