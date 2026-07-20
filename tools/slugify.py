from __future__ import annotations

import re
import string
import unicodedata


def slugify(text: str, separator: str = "-") -> str:
    if len(separator) != 1:
        raise ValueError(f"Separator must be a single character, got {len(separator)}")
    if separator not in string.punctuation:
        raise ValueError(f"Separator must be a punctuation character, got '{separator}'")

    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", lambda m: separator, text)
    text = text.strip(separator)
    return text


# ## Self-Critique
# - If separator is a regex metacharacter like backslash, the lambda callback
#   ensures it is treated as a literal replacement string and never interpreted
#   as regex escape syntax, preventing re.error from leaking to the caller.
# - If input contains only non-alphanumeric characters, the regex pattern
#   [^a-z0-9]+ matches all of them, re.sub replaces them with separators, and
#   strip() removes all leading/trailing separators, correctly yielding an empty
#   string.
# - If separator validation rejects whitespace or control characters before
#   re.sub is invoked, the function fails fast with a clear error message rather
#   than silently accepting invalid input or producing unexpected slugs.
