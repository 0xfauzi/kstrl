from __future__ import annotations

import re
import unicodedata


def slugify(text: str, separator: str = "-") -> str:
    if len(separator) != 1:
        raise ValueError(f"Separator must be a single character, got {len(separator)}")
    if separator.isalnum():
        raise ValueError(f"Separator must not be alphanumeric, got '{separator}'")

    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", separator, text)
    text = text.strip(separator)
    return text


# ## Self-Critique
# - If input is whitespace-only, strip() returns empty string, which matches the
#   intent to keep only alphanumeric content.
# - If separator is a regex metacharacter like '+', strip() treats it as a literal
#   string, not a regex pattern, so the call still works correctly.
# - If input contains the chosen separator (e.g., "hello-world" with sep="-"), it
#   will not be preserved; the separator validation fails on invalid separators
#   upfront, and valid separators within input text are treated as word boundaries.
