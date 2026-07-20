"""Static brand mark for Ralph (PR G).

The 24fps Lissajous loop animation retired with the TUI rewrite: the
spike set 24fps as the ceiling, not the norm, and a blocking
1-second-plus Live animation on every `ks run` start earns its
keep worse than a one-line mark. `brand_mark()` is the identity that
remains; the dashboard's identity is the dashboard itself.
"""

from __future__ import annotations

BRAND_MARK = "\u25cd kstrl"


def brand_mark() -> str:
    """One-line static mark, printed once at startup on rich TTYs."""
    return BRAND_MARK
