"""The atlas's look, as one table of tokens: kstrl's own, from DESIGN.md.

Everything visual lives here so the scene, the panel and the page cannot
disagree about a colour. The palette is the dashboard's (warm near-black
ground, one amber accent) so the atlas reads as the same instrument as the
terminal UI it describes.

Colour carries meaning or is absent:

    amber ....... you are interacting with this (selection, focus, the node
                  that ACTS in a journey step)
    steel ....... measurement (the node that MEASURES a step; the
                  measurement layer's edges)
    seven hues .. one per layer of the map; printed in the page legend
    success ..... built; warning: part built; muted, dashed: planned
    error ....... only for "nothing measures this step" and a change map

Nothing else on the page is coloured.
"""

from __future__ import annotations

from typing import Any

SANS = (
    'ui-sans-serif,system-ui,-apple-system,"SF Pro Text","Segoe UI",Roboto,'
    '"Helvetica Neue",Arial,sans-serif'
)
MONO = 'ui-monospace,"SF Mono",SFMono-Regular,"JetBrains Mono",Menlo,Consolas,monospace'

# The seven layer hues. Chosen to read apart from each other on #161310 and to
# stay quieter than amber: every one sits at a lower chroma than the accent.
# Measurement is steel because steel means measurement everywhere else on the
# page (the measuring node in a journey step); trust is the dashboard's
# violet; work, the forward path, is a warm ivory so the default view reads
# as ink on the ground and the coloured layers read as readings over it.
LAYER_COLOURS: dict[str, str] = {
    "work": "#D9CDB2",
    "measure": "#82A7BA",
    "feedback": "#E39A86",
    "operator": "#DD9BBD",
    "trust": "#B48EC9",
    "learn": "#86C9A9",
    "record": "#B7C27C",
}

KSTRL: dict[str, Any] = {
    "name": "kstrl",
    "scheme": "dark",
    "bg": "#161310",
    "surface": "#1E1A15",
    "raised": "#27221A",
    "line": "#2E2820",
    "line_2": "#4A4237",
    "ink": "#ECE5D8",
    "ink_2": "#C4B9A4",
    "ink_3": "#A2967F",
    "accent": "#E5A84F",
    "accent_soft": "#E5A84F2E",
    "steel": "#82A7BA",
    "good": "#8FC470",
    "warn": "#D9B036",
    "bad": "#E26D5A",
    "violet": "#B48EC9",
    # Build state is one ordinal scale: fill and stroke per step, then the
    # word the legend prints.
    "state": {
        "built": ("#27221A", "#8A7D63", "built"),
        "partial": ("#2A2519", "#D9B036", "part built"),
        "planned": ("#1A1713", "#4A4237", "planned"),
    },
    "ghost": ("#1A1713", "#2E2820"),
    # Hard boundaries: (stroke, fill). Isolated things (the agent's process,
    # its worktree, the control directory) carry a warm red-brown edge.
    "container": {
        "host": ("#4A4237", "#1A1713"),
        "client": ("#4A4237", "#1A1713"),
        "server": ("#3B3428", "#1B1814"),
        "isolated": ("#6B4A3D", "#1D1613"),
    },
    "region": "#A2967F",
    "change": "#E26D5A",
    "reach": "#E5A84F",
    "flow": "#5E5548",
    "layers": LAYER_COLOURS,
    "delta": {
        "operations": "#82A7BA",
        "types": "#8FC470",
        "refusals": "#E26D5A",
        "tests": "#E5A84F",
    },
    "font_ui": SANS,
    "font_mono": MONO,
}


def get() -> dict[str, Any]:
    return KSTRL


def css_vars(t: dict[str, Any] | None = None) -> str:
    """The :root block. One source for both halves of the page."""
    t = t or KSTRL
    layers = "".join(f"--l-{k}:{v};" for k, v in t["layers"].items())
    return (
        f"color-scheme:{t['scheme']};"
        f"--bg:{t['bg']};--surface:{t['surface']};--raised:{t['raised']};"
        f"--line:{t['line']};--line-2:{t['line_2']};"
        f"--ink:{t['ink']};--ink-2:{t['ink_2']};--ink-3:{t['ink_3']};"
        f"--accent:{t['accent']};--accent-soft:{t['accent_soft']};"
        f"--steel:{t['steel']};--good:{t['good']};--warn:{t['warn']};"
        f"--bad:{t['bad']};--violet:{t['violet']};"
        f"--fs:{t['font_ui']};--fm:{t['font_mono']};"
        f"{layers}"
    )
