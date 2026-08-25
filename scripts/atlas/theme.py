"""The two looks the atlas ships in, as one table of tokens.

Ported from the deckgen repository's atlas tooling.

Everything visual lives here so the scene and the page cannot disagree about a
colour, and so a second look costs a table rather than a fork of the drawing
code.

Two rules shaped both palettes.

ONE MEANING PER HUE. The first version had six hues competing at rest: green,
amber, grey and blue for build state, violet for model calls, red for the
loop. Nothing owned a colour, so nothing read as significant. Build state is
now ONE hue at three strengths, because it is an ordinal scale and not three
unrelated facts. Everything else is reserved: the accent means "you are
interacting with this" and nothing else, and the semantic hues stay behind
layer toggles that are off by default. At rest the scene is neutrals plus one
hue.

TYPE IS A SCALE, NOT A PILE OF SIZES. Both themes carry the same six steps at
a 1.15 ratio, which is the tight ratio product UI wants. `studio` sets all of
it in one sans, because a tool does not need display pairing. `paper` pairs a
serif for headings against the sans, which is a real contrast axis rather
than two similar sans-serifs pretending to be one.
"""

from __future__ import annotations

from typing import Any

SANS = (
    'ui-sans-serif,system-ui,-apple-system,"SF Pro Text","Segoe UI",Roboto,'
    '"Helvetica Neue",Arial,sans-serif'
)
SERIF = 'ui-serif,"New York",Iowan Old Style,Palatino,Georgia,serif'
MONO = 'ui-monospace,"SF Mono",SFMono-Regular,"JetBrains Mono",Menlo,Consolas,monospace'

# Studio: a dark instrument. Read beside an editor, at length, so the ground is
# a cool near-black rather than a flat grey and every surface above it is a
# real step in elevation.
STUDIO: dict[str, Any] = {
    "name": "studio",
    "scheme": "dark",
    "bg": "#0C0E13",
    "surface": "#11141A",
    "raised": "#181C24",
    "line": "#232833",
    "line_2": "#313846",
    # ink_3 carries 11px notes, so it clears 4.5:1 against the surface it
    # sits on. Measured: the first pick was 3.70:1, the muted-grey-on-near
    # -white failure, and it is the most common one in generated palettes.
    "ink": "#E9ECF2",
    "ink_2": "#99A2B2",
    "ink_3": "#747E92",
    "accent": "#6E9BFF",
    "accent_soft": "#6E9BFF22",
    # Build state: one hue, three strengths, because it is an ordinal scale.
    "state": {
        "built": ("#123024", "#46D6A0", "built"),
        "partial": ("#1B2A24", "#8CA79A", "part built"),
        "planned": ("#12151C", "#414A5A", "planned"),
    },
    "ghost": ("#12151B", "#232833"),
    "container": {
        "host": ("#333A46", "#12151C"),
        "client": ("#39445C", "#131822"),
        "server": ("#2E3542", "#11141A"),
        "isolated": ("#4A342F", "#1A1416"),
    },
    "region": "#6E9BFF",
    "guards": "#E0A94A",
    "calls": "#A98BF5",
    "change": "#FF7B6B",
    "flow": "#5A6577",
    "shadow": "#00000059",
    "grid": "#FFFFFF08",
    "delta": {
        "operations": "#6E9BFF",
        "types": "#46D6A0",
        "refusals": "#FF7B6B",
        "tests": "#E0A94A",
    },
    "font_display": SANS,
    "font_ui": SANS,
    "font_mono": MONO,
    "display_weight": "600",
    "display_track": "-.015em",
    "radius": "7px",
}

# Paper: the same information as a printed figure. Near-white at chroma zero,
# deliberately NOT the warm cream band that every generated page reaches for.
PAPER: dict[str, Any] = {
    "name": "paper",
    "scheme": "light",
    "bg": "#FCFCFD",
    "surface": "#FFFFFF",
    "raised": "#F3F4F7",
    "line": "#E4E6EB",
    "line_2": "#C8CDD7",
    "ink": "#14171D",
    "ink_2": "#4C5464",
    "ink_3": "#6E7688",
    "accent": "#2A5BD7",
    "accent_soft": "#2A5BD714",
    "state": {
        "built": ("#DCEFE6", "#16704F", "built"),
        "partial": ("#EDEFEE", "#7A8B83", "part built"),
        "planned": ("#F5F6F8", "#AAB1BD", "planned"),
    },
    "ghost": ("#F7F8FA", "#E4E6EB"),
    "container": {
        "host": ("#B9C0CC", "#FAFAFC"),
        "client": ("#A9B6CE", "#F7F9FC"),
        "server": ("#C2C7D1", "#FBFBFC"),
        "isolated": ("#CDB3AC", "#FDFAF9"),
    },
    "region": "#2A5BD7",
    "guards": "#9A6B12",
    "calls": "#6B3FC4",
    "change": "#BE3A2B",
    "flow": "#9AA2AF",
    "shadow": "#1417280F",
    "grid": "#14171D08",
    "delta": {
        "operations": "#2A5BD7",
        "types": "#16704F",
        "refusals": "#BE3A2B",
        "tests": "#9A6B12",
    },
    "font_display": SERIF,
    "font_ui": SANS,
    "font_mono": MONO,
    "display_weight": "500",
    "display_track": "0",
    "radius": "5px",
}

THEMES: dict[str, dict[str, Any]] = {"studio": STUDIO, "paper": PAPER}
DEFAULT = "paper"


def get(name: str | None) -> dict[str, Any]:
    return THEMES.get(name or DEFAULT, THEMES[DEFAULT])


def css_vars(t: dict[str, Any]) -> str:
    """The :root block. One source for both halves of the page."""
    return (
        f"color-scheme:{t['scheme']};"
        f"--bg:{t['bg']};--surface:{t['surface']};--raised:{t['raised']};"
        f"--line:{t['line']};--line-2:{t['line_2']};"
        f"--ink:{t['ink']};--ink-2:{t['ink_2']};--ink-3:{t['ink_3']};"
        f"--accent:{t['accent']};--accent-dim:{t['accent_soft']};"
        f"--good:{t['state']['built'][1]};--warn:{t['change']};"
        f"--amber:{t['guards']};--violet:{t['calls']};"
        f"--shadow:{t['shadow']};--grid:{t['grid']};"
        f"--fd:{t['font_display']};--fs:{t['font_ui']};--fm:{t['font_mono']};"
        f"--dw:{t['display_weight']};--dt:{t['display_track']};"
        f"--r:{t['radius']};"
        # One scale, 1.15 between steps, named so nothing spells a size inline.
        "--t0:10px;--t1:11px;--t2:12px;--t3:13px;--t4:15px;--t5:17px;"
        # Layout and stacking are theme-independent, but they belong to the
        # same block: splitting them once cost the page its grid, because
        # grid-template-columns silently drops when a var is missing.
        "--bar:56px;--panel-w:27rem;--rail-w:17.5rem;"
        "--z-canvas:1;--z-bar:20;--z-panel:30;--z-overlay:40;"
        "--ease:cubic-bezier(.2,.8,.2,1);"
    )
