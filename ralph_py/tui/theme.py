"""The Ralph theme: one committed palette, one accent (design pass).

Direction (user-confirmed): dense pro-tool register, warm amber
identity - a factory control room, not a stock Textual demo. Rules:

- ONE accent (amber) carries selection, focus, primary actions, and
  the "actively running" state - the live thing on screen is the
  accented thing.
- Semantic state colors are reserved for component status only, never
  chrome: green=completed, red=failed, steel=verifying, violet=merge
  parked, dim=pending/skipped.
- Neutrals are a warm near-black ramp (background -> surface -> panel)
  so the amber sits in its own temperature; body text is warm off-white
  at >=4.5:1 on every surface it appears on.
- Empty data renders as a dim midpoint dot, never a bare "-" (a dash
  column reads as broken data).

Composed in OKLCH, expressed as hex (what Textual consumes). All
status glyph/color pairs live HERE so the board, the activity feed,
and any future surface agree.
"""

from __future__ import annotations

from textual.theme import Theme

# Warm neutral ramp: oklch(0.16->0.27, ~0.01, h=80).
BACKGROUND = "#161310"
SURFACE = "#1e1a15"
PANEL = "#27221a"

# Ink: oklch(0.92 0.015 85) - warm off-white, ~13:1 on BACKGROUND.
FOREGROUND = "#ece5d8"

# The accent: oklch(0.78 0.13 75) amber. Selection, focus, primary
# action, running state, brand mark.
ACCENT = "#e5a84f"

# State vocabulary (status only - never chrome):
SUCCESS = "#8fc470"   # completed
ERROR = "#e26d5a"     # failed
STEEL = "#82a7ba"     # verifying (adversarial phases in flight)
VIOLET = "#b48ec9"    # merge parked (waiting on the outside world)
WARNING = "#d9b036"   # banners/caution copy only

MUTED = "#a2967f"     # dim ink for secondary text (>=4.5:1 on BACKGROUND)

EMPTY_CELL = "·"      # dim placeholder; a "-" column reads as broken

RALPH_THEME = Theme(
    name="ralph",
    primary=ACCENT,
    secondary=STEEL,
    accent=ACCENT,
    warning=WARNING,
    error=ERROR,
    success=SUCCESS,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    dark=True,
    variables={
        # Footer keys pick up the identity instead of stock blue.
        "footer-key-foreground": ACCENT,
        "block-cursor-background": ACCENT,
        "block-cursor-foreground": BACKGROUND,
        "input-selection-background": ACCENT + " 35%",
    },
)

# status -> (glyph, theme color) - the single source of truth.
# Unicode by user decision: every terminal ralph targets is UTF-8;
# the plain-mode/ascii fallback paths never render these.
STATUS_GLYPHS: dict[str, tuple[str, str]] = {
    "pending": ("○", MUTED),
    "running": ("●", ACCENT),
    "verifying": ("◐", STEEL),
    "completed": ("✓", SUCCESS),
    "merge_pending": ("⏸", VIOLET),
    "failed": ("✗", ERROR),
    "skipped": ("◌", MUTED),
}


def status_glyph(status: str) -> tuple[str, str]:
    return STATUS_GLYPHS.get(status, ("?", MUTED))


def short_run_id(run_id: str) -> str:
    """`factory-20260720-171928.879456-f35e4e` -> `f35e4e` - the nonce
    is the only part a human compares; the timestamp is the header's
    elapsed clock's job."""
    tail = run_id.rsplit("-", 1)[-1]
    return tail if tail else run_id
