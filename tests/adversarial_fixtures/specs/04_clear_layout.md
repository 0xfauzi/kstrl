# Spec: url-slugifier

A tiny pure-function library that converts arbitrary text into a URL-safe
slug. Single module, single component, unambiguous layout.

## Functional requirements

Add one function `slugify(text: str) -> str` to `src/slugify/__init__.py`:

- Lowercase the input.
- Replace any run of whitespace or non-alphanumeric characters with a
  single hyphen.
- Strip leading and trailing hyphens.

## Acceptance criteria

Tests in `tests/test_slugify.py` covering:

- `slugify("Hello World")` returns `"hello-world"`
- `slugify("  Multiple   spaces  ")` returns `"multiple-spaces"`
- `slugify("Already-Hyphenated")` returns `"already-hyphenated"`
- `slugify("Symbols!@#$%")` returns `"symbols"`
- `slugify("")` returns `""`
- `slugify("a")` returns `"a"`

## Out of scope

- Unicode normalization (NFKD, accents).
- Length truncation / max-slug-length.
- Async variant.
- CLI wrapper.
- Any error handling beyond the empty-string case above.
