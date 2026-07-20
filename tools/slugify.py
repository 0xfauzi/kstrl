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
# - If the input text contains only whitespace, this function will return an empty string after lowercasing, which matches the intent to only keep alphanumeric content.
# - If the separator is a common regex metacharacter like '+' or '*', the strip() call will still work correctly because it treats the argument as a literal string, not a regex pattern.
# - If the input contains the separator character itself (e.g., text="hello-world" with separator="-"), the separator validation occurs before processing, so invalid separators are rejected upfront; however, if the input naturally contains the chosen separator, it will not be preserved (it will be treated as a word boundary).
