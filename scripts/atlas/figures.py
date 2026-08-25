"""Static figures from the atlas, for the documents that cannot run a script.

README.md, ARCHITECTURE.md and the wiki embed pictures of the system as
images. A hand-drawn picture drifts the moment a component moves, and it
summarises to five boxes because a hand cannot keep sixty edges honest. So
every figure here is drawn by the atlas's own generator from the atlas's own
data: the artifact on every edge, the build state in every card's fill.

What is written to docs/atlas/figures/:

    system.svg              the whole map, every component, every layer in
                            its colour, at the atlas's own scale
    layer-<id>.svg          one layer, compact: only the components its
                            edges touch, laid out as columns by region,
                            its question as the title
    journey-<id>.svg        one journey, compact: the components its steps
                            act with, measure or trace, its edges numbered
                            from the step's start, acting components in
                            amber, measuring in steel, the steps listed
                            under the map
    loops.svg               the seven loops of docs/control-loop-design.md
                            section 2 as nested bands, innermost fastest

A compact figure exists because the whole atlas fitted to a 900px column
puts an 11px card name at 4px. compact.py draws only what takes part, at
13px names, on a canvas that follows the number of columns (at most 1398
wide in the panel, so a name renders at 8px or more at 900px); more
columns than fit wrap to a second row rather than shrink the type. Every
compact figure is verified after it is drawn (compact.problems): each
participant present, no edge through a card it does not connect, no label
on a card, a strip, a badge or another label, nothing under 11px, and the
name size at 900px. `--no-compact` draws a layer or a journey the old way,
the whole atlas with the rest dimmed.

Each figure is one self-contained .svg: no script, no external reference,
system font stacks, and the atlas's dark panel drawn as the figure's own
background so it reads the same on a light or a dark page. The map inside
is schematic.render or compact.render; nothing here draws a card or an
edge. The loops figure is the one drawing whose data the atlas does not
carry: its table is small, below, and cites its source.

The caption baked into every figure names the atlas commit it was drawn
from. `--check` regenerates in memory, runs every figure's checks, and
fails when any committed figure differs, the same contract as
render_html.py; refresh.sh and the atlas workflow run it.

Usage:
  uv run python scripts/atlas/figures.py [--out-dir docs/atlas/figures] [--scale N]
  uv run python scripts/atlas/figures.py --check
  uv run python scripts/atlas/figures.py --no-compact
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import theme as theme_mod
from compact import problems as compact_problems
from compact import render as render_compact
from logical_model import COMPONENTS, build_state, layout_problems
from relations import JOURNEYS, LAYERS
from schematic import esc, layer_rows, legend_rows, mix, wrap_words
from schematic import render as render_schematic

GENERATOR = "scripts/atlas/figures.py"
ATLAS_PATH = "docs/atlas/atlas.json"
FIGURE_DIR = Path("docs/atlas/figures")

# Panel chrome, in drawing units. The map is drawn at the atlas's own scale
# (2278 x 978 plus its padding), so a README column of about 900px shows
# the map at roughly 0.37: its 11px card text is a preview there and reads
# at full size on click. The chrome is sized so the title, the legend, the
# steps and the caption read at that column width without a click.
MARGIN = 40.0
TITLE_PX = 40.0
SUB_PX = 28.0
LEGEND_PX = 26.0
STEP_PX = 26.0
CAPTION_PX = 22.0
# A compact panel is at most 1398 wide and shows at about 0.64 in a 900px
# column, so its legend, steps and caption are set smaller than the full
# map's to keep the same rendered size there.
COMPACT_LEGEND_PX = 22.0
COMPACT_STEP_PX = 22.0
COMPACT_CAPTION_PX = 20.0
# A compact map can be two columns wide; the panel round it is never
# narrower than this, so the title, the legend and the caption have room,
# and the map sits centred in what is left.
COMPACT_MIN_W = 900.0
# Width estimate per glyph, as a fraction of the font size, for wrapping
# and right-alignment without a renderer.
GLYPH = 0.52
GLYPH_MONO = 0.62

# ---------------------------------------------------------------------------
# The loops. docs/control-loop-design.md, section 2, "The loop nest", is the
# one table the atlas does not carry. Rate, what acts, what measures, the
# set point and the closed column are that table's words (the seventh loop
# is the paragraph under it). `layer` is where the loop lives on the map,
# empty for a loop the map has no edges for. `sensors` names the components
# that do the measuring, so whether the sensor exists is derived from the
# atlas by build_state, not typed here.
# ---------------------------------------------------------------------------

LOOPS: list[dict[str, Any]] = [
    {
        "id": "implement",
        "rate": "seconds to minutes",
        "acts": "the engineer agent",
        "measures": "none: the breaker and the scope fence watch it, neither reads the code",
        "toward": "this story's criteria",
        "closed": "open",
        "layer": "feedback",
        "sensors": [],
    },
    {
        "id": "accept",
        "rate": "minutes",
        "acts": "a retry with context",
        "measures": "verify, review, security",
        "toward": "the component PRD",
        "closed": "closed, but winds up",
        "layer": "measure",
        "sensors": ["MechanicalVerifier", "Reviewer", "SecurityReviewer"],
    },
    {
        "id": "integrate",
        "rate": "tens of minutes",
        "acts": "schedule, merge, reset the breaker",
        "measures": "contract tests",
        "toward": "the manifest DAG satisfied",
        "closed": "closed",
        "layer": "work",
        "sensors": ["ContractTester"],
    },
    {
        "id": "intake",
        "rate": "hours",
        "acts": "queue admission",
        "measures": "queue state, spend, inbox",
        "toward": "the queue drained",
        "closed": "closed",
        "layer": "work",
        "sensors": ["WorkQueue", "SpendLedger", "Inbox"],
    },
    {
        "id": "trust",
        "rate": "days",
        "acts": "an autonomy level change",
        "measures": "run outcomes",
        "toward": "the autonomy the evidence supports",
        "closed": "closed, two inputs unwired",
        "layer": "trust",
        "sensors": ["AutonomyLadder"],
    },
    {
        "id": "learn",
        "rate": "weeks",
        "acts": "playbook and prompt edits",
        "measures": "attribution, calibration",
        "toward": "the harness's own detection rates",
        "closed": "open",
        "layer": "learn",
        "sensors": ["Calibration", "Playbook"],
    },
    {
        "id": "operate",
        "rate": "not built (R8.7, R8.8)",
        "acts": "the release driver",
        "measures": "runtime error rates",
        "toward": "the service's SLO",
        "closed": "not built",
        "layer": "",
        "sensors": ["RuntimeSignals"],
    },
]


class Chrome:
    """Text around the map, styled by class so the figure stays small."""

    def __init__(self, t: dict[str, Any]) -> None:
        self.t = t
        self.styles: dict[tuple[float, str, str, bool, str], str] = {}

    def text(
        self,
        x: float,
        y: float,
        s: str,
        size: float,
        colour: str,
        weight: str = "400",
        mono: bool = False,
        anchor: str = "start",
    ) -> str:
        key = (size, colour, weight, mono, anchor)
        cls = self.styles.setdefault(key, f"c{len(self.styles)}")
        return f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}">{esc(s)}</text>'

    def span(self, s: str, colour: str, weight: str = "400") -> str:
        return f'<tspan fill="{colour}" font-weight="{weight}">{esc(s)}</tspan>'

    def spans(self, x: float, y: float, size: float, parts: list[tuple[str, str, str]]) -> str:
        """One text of several colours: [(text, colour, weight), ...]."""
        key = (size, self.t["ink_2"], "400", False, "start")
        cls = self.styles.setdefault(key, f"c{len(self.styles)}")
        inner = "".join(self.span(s, c, w) for s, c, w in parts)
        return f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}">{inner}</text>'

    def css(self) -> str:
        rules = []
        for (size, colour, weight, mono, anchor), cls in self.styles.items():
            family = self.t["font_mono"] if mono else self.t["font_ui"]
            rules.append(
                f".{cls}{{font-family:{family};font-size:{size}px;font-weight:{weight};"
                f"fill:{colour};text-anchor:{anchor}}}"
            )
        return "<style>" + "".join(rules) + "</style>"


def width_of(s: str, size: float, mono: bool = False) -> float:
    return len(s) * size * (GLYPH_MONO if mono else GLYPH)


def swatch_legend(
    c: Chrome, x: float, y: float, rows: list[tuple[str, str, str]], size: float
) -> tuple[str, float]:
    """Filled squares with a word each, left to right; returns (svg, next x)."""
    out: list[str] = []
    for fill, stroke, label in rows:
        out.append(
            f'<rect x="{x:.1f}" y="{y - size * 0.72:.1f}" width="{size * 0.8:.0f}" '
            f'height="{size * 0.8:.0f}" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
        )
        x += size * 1.1
        out.append(c.text(x, y, label, size, c.t["ink_2"]))
        x += width_of(label, size) + size * 1.6
    return "".join(out), x


def line_legend(
    c: Chrome, x: float, y: float, rows: list[tuple[str, str]], size: float
) -> tuple[str, float]:
    """A short coloured line with a word each; returns (svg, next x)."""
    out: list[str] = []
    for colour, label in rows:
        out.append(
            f'<rect x="{x:.1f}" y="{y - size * 0.42:.1f}" width="{size * 1.5:.0f}" height="4" '
            f'rx="2" fill="{colour}"/>'
        )
        x += size * 1.8
        out.append(c.text(x, y, label, size, c.t["ink_2"]))
        x += width_of(label, size) + size * 1.6
    return "".join(out), x


def caption_for(atlas: dict[str, Any]) -> str:
    commit = str(atlas.get("built_at_commit", ""))[:7] or "unknown"
    return f"Generated by {GENERATOR} from {ATLAS_PATH} at {commit}; do not edit by hand."


def map_size(map_svg: str) -> tuple[float, float]:
    """The map's own width and height, from its viewBox."""
    m = re.match(r'<svg (?P<attrs>[^>]*)>', map_svg)
    if not m:
        raise ValueError("schematic did not return an <svg> root")
    vb = re.search(r'viewBox="([^"]+)"', m.group("attrs"))
    if not vb:
        raise ValueError("schematic root lacks a viewBox")
    _vx, _vy, vw, vh = (float(v) for v in vb.group(1).split())
    return vw, vh


def nest(map_svg: str, x: float, y: float, clip: bool = True) -> tuple[str, float, float]:
    """The map as a nested <svg> at (x, y), at 1:1; returns (svg, w, h).

    The renderer's root carries the page's attributes (a tab stop, an
    application role). Inside a figure it is a picture, so the root is
    rebuilt with only its id and viewBox plus its place in the panel. A
    nested svg clips to its viewport; with `clip` off it does not, for a
    map whose label placer may seat a label a few units past the canvas
    edge (the panel's margin has the room).
    """
    m = re.match(r'<svg (?P<attrs>[^>]*)>', map_svg)
    if not m:
        raise ValueError("schematic did not return an <svg> root")
    attrs = m.group("attrs")
    vb = re.search(r'viewBox="([^"]+)"', attrs)
    sid = re.search(r'id="([^"]+)"', attrs)
    if not vb or not sid:
        raise ValueError("schematic root lacks viewBox or id")
    vw, vh = map_size(map_svg)
    overflow = "" if clip else ' overflow="visible"'
    head = (
        f'<svg id="{sid.group(1)}" x="{x:.1f}" y="{y:.1f}" width="{vw:.0f}" height="{vh:.0f}" '
        f'viewBox="{vb.group(1)}"{overflow}>'
    )
    return head + map_svg[m.end():], vw, vh


def panel(
    atlas: dict[str, Any],
    map_svg: str,
    title: str,
    sub: str,
    legend: Legend,
    steps: list[str],
    scale: float,
    label: str,
    legend_px: float = LEGEND_PX,
    step_px: float = STEP_PX,
    caption_px: float = CAPTION_PX,
    min_width: float = 0.0,
    wrap: bool = False,
) -> str:
    """One figure: the panel, the title, the map, a legend, steps, the caption.

    The panel is as wide as the map plus its margins, or `min_width` if
    that is wider, and then the map sits centred. With `wrap`, the title,
    the subtitle and the caption wrap to the panel instead of running
    past it.
    """
    t = theme_mod.get()
    c = Chrome(t)
    parts: list[str] = []
    mw, mh = map_size(map_svg)
    w = max(mw + 2 * MARGIN, min_width)

    def lines_of(text: str, size: float, max_lines: int) -> list[str]:
        if not wrap:
            return [text]
        return wrap_words(text, int((w - 2 * MARGIN) / (size * GLYPH)), max_lines)

    y = MARGIN + TITLE_PX
    for k, line in enumerate(lines_of(title, TITLE_PX, 2)):
        if k:
            y += TITLE_PX + 6
        parts.append(c.text(MARGIN, y, line, TITLE_PX, t["ink"], "600"))
    y += SUB_PX + 10
    for k, line in enumerate(lines_of(sub, SUB_PX, 3)):
        if k:
            y += SUB_PX + 6
        parts.append(c.text(MARGIN, y, line, SUB_PX, t["ink_3"]))
    y += 18
    body, _mw, _mh = nest(map_svg, MARGIN + (w - 2 * MARGIN - mw) / 2, y, clip=not wrap)
    parts.append(body)
    y += mh + legend_px + 16
    seg, y = legend(c, y, w)
    parts.append(seg)
    if steps:
        y += step_px + 22
        avail = int((w - 2 * MARGIN - 56) / (step_px * GLYPH))
        for n, say in enumerate(steps, start=1):
            lines = wrap_words(say, avail, 4)
            parts.append(c.text(MARGIN + 30, y, f"{n}", step_px, t["accent"], "700", True, "end"))
            for k, line in enumerate(lines):
                parts.append(c.text(MARGIN + 46, y, line, step_px, t["ink_2"]))
                if k < len(lines) - 1:
                    y += step_px + 6
            y += step_px + 12
        y -= step_px + 12
    y += caption_px + 34
    for k, line in enumerate(lines_of(caption_for(atlas), caption_px, 2)):
        if k:
            y += caption_px + 6
        parts.append(c.text(MARGIN, y, line, caption_px, t["ink_3"]))
    h = y + MARGIN - 6
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w * scale:.0f}" '
        f'height="{h * scale:.0f}" viewBox="0 0 {w:.0f} {h:.0f}" role="img" '
        f'aria-label="{esc(label)}">'
        f'<rect x="0" y="0" width="{w:.0f}" height="{h:.0f}" rx="14" fill="{t["bg"]}" '
        f'stroke="{t["line_2"]}" stroke-width="2"/>'
        + "".join(parts)
        + c.css()
        + "</svg>"
    )


# (chrome, y, panel width) -> (svg, the y of the legend's last line)
Legend = Callable[[Chrome, float, float], tuple[str, float]]


def _legend_line(
    rows: list[tuple[str, str, str]],
    lines: list[tuple[str, str]],
    note: str,
    size: float = LEGEND_PX,
    wrap: bool = False,
) -> Legend:
    """A legend row: swatches, then coloured lines, then a note, at the y given.

    With `wrap`, a line entry or the note that would run past the panel's
    right margin starts a new legend line instead.
    """

    def draw(c: Chrome, y: float, w: float) -> tuple[str, float]:
        limit = w - MARGIN if wrap else math.inf
        x = MARGIN
        out = ""
        for row in rows:
            need = size * 1.1 + width_of(row[2], size)
            if x > MARGIN and x + need > limit:
                y += size + 12
                x = MARGIN
            seg, x = swatch_legend(c, x, y, [row], size)
            out += seg
        x += 6
        for colour, label in lines:
            need = size * 1.8 + width_of(label, size)
            if x > MARGIN and x + need > limit:
                y += size + 12
                x = MARGIN
            more, x = line_legend(c, x, y, [(colour, label)], size)
            out += more
        if note and wrap and x + 6 + width_of(note, size) > limit:
            # The note takes lines of its own, wrapped to the panel.
            if x > MARGIN:
                y += size + 12
            for k, line in enumerate(wrap_words(note, int((w - 2 * MARGIN) / (size * GLYPH)), 3)):
                if k:
                    y += size + 6
                out += c.text(MARGIN, y, line, size, c.t["ink_3"])
        elif note:
            out += c.text(x + 6, y, note, size, c.t["ink_3"])
        return out, y

    return draw


def system_figure(atlas: dict[str, Any], scale: float) -> str:
    svg, _detail = render_schematic(atlas, svg_id="map", static=True)
    legend = _legend_line(
        legend_rows("system"),
        [(colour, f"{label} layer") for _lid, colour, label in layer_rows()],
        "",
    )
    return panel(
        atlas,
        svg,
        "The system: every component and every flow",
        "Fill is build state; dashed ghosts are planned. Every line is labelled with what "
        "it carries and coloured by the layer it belongs to.",
        legend,
        [],
        scale,
        "System map of kstrl",
    )


def layer_figure(atlas: dict[str, Any], layer: dict[str, str], scale: float) -> str:
    t = theme_mod.get()
    svg, _detail = render_schematic(atlas, svg_id="map", static=True, layer=layer["id"])
    legend = _legend_line(
        legend_rows("system"),
        [
            (
                t["layers"][layer["id"]],
                f"{layer['label']} layer edges, labelled with what they carry",
            )
        ],
        "dimmed: components this layer does not touch",
    )
    return panel(
        atlas,
        svg,
        layer["question"],
        f"The {layer['label']} layer: {layer['sub']}. Other layers' edges are not drawn.",
        legend,
        [],
        scale,
        f"{layer['label']} layer of the kstrl system map",
    )


def journey_figure(atlas: dict[str, Any], journey: dict[str, Any], scale: float) -> str:
    t = theme_mod.get()
    svg, _detail = render_schematic(atlas, svg_id="map", static=True, journey=journey)
    legend = _legend_line(
        [
            (t["raised"], t["accent"], "acts in this step"),
            (t["raised"], t["steel"], "measures this step"),
        ]
        + legend_rows("system"),
        [],
        "the number on an edge is the step that traces it; a step nothing measures says so",
    )
    steps = [str(s["say"]) for s in journey["steps"]]
    return panel(
        atlas,
        svg,
        str(journey["label"]),
        "The path in order, all steps at once. Only the edges the journey traces are drawn.",
        legend,
        steps,
        scale,
        f"Journey: {journey['label']}",
    )


@dataclass(frozen=True)
class Figure:
    """One figure's svg, what its own checks found, and its numbers."""

    svg: str
    problems: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _panel_width(svg: str) -> float:
    m = re.search(r'viewBox="0 0 ([0-9.]+) ([0-9.]+)"', svg)
    if not m:
        raise ValueError("figure root lacks a viewBox")
    return float(m.group(1))


def compact_layer_figure(atlas: dict[str, Any], layer: dict[str, str], scale: float) -> Figure:
    """One layer, only the components its edges touch."""
    t = theme_mod.get()
    svg, meta = render_compact(atlas, layer=layer["id"])
    legend = _legend_line(
        legend_rows("system"),
        [
            (
                t["layers"][layer["id"]],
                f"{layer['label']} layer edges, labelled with what they carry",
            )
        ],
        "only the components this layer touches are drawn",
        COMPACT_LEGEND_PX,
        wrap=True,
    )
    fig = panel(
        atlas,
        svg,
        layer["question"],
        f"The {layer['label']} layer: {layer['sub']}. Other layers' edges are not drawn.",
        legend,
        [],
        scale,
        f"{layer['label']} layer of the kstrl system map",
        COMPACT_LEGEND_PX,
        COMPACT_STEP_PX,
        COMPACT_CAPTION_PX,
        COMPACT_MIN_W,
        wrap=True,
    )
    found, stats = compact_problems(meta, fig, _panel_width(fig))
    return Figure(fig, found, stats)


def compact_journey_figure(atlas: dict[str, Any], journey: dict[str, Any], scale: float) -> Figure:
    """One journey, only the components its steps act with, measure or trace."""
    t = theme_mod.get()
    svg, meta = render_compact(atlas, journey=journey)
    legend = _legend_line(
        [
            (t["raised"], t["accent"], "acts in this step"),
            (t["raised"], t["steel"], "measures this step"),
        ]
        + legend_rows("system"),
        [],
        "the number at the start of an edge is the step that traces it; "
        "a step nothing measures says so",
        COMPACT_LEGEND_PX,
        wrap=True,
    )
    steps = [str(s["say"]) for s in journey["steps"]]
    fig = panel(
        atlas,
        svg,
        str(journey["label"]),
        "The path in order, all steps at once. Only the components and edges the journey "
        "touches are drawn.",
        legend,
        steps,
        scale,
        f"Journey: {journey['label']}",
        COMPACT_LEGEND_PX,
        COMPACT_STEP_PX,
        COMPACT_CAPTION_PX,
        COMPACT_MIN_W,
        wrap=True,
    )
    found, stats = compact_problems(meta, fig, _panel_width(fig))
    return Figure(fig, found, stats)


def loops_figure(atlas: dict[str, Any], scale: float) -> str:
    """Seven nested bands, innermost fastest, from the table above."""
    t = theme_mod.get()
    c = Chrome(t)
    states = {comp["id"]: build_state(comp, atlas) for comp in COMPONENTS}
    W = 1500.0
    M = MARGIN
    DX = 30.0  # side inset per band
    ST = 124.0  # the top strip of a band, where its text sits
    DB = 20.0  # the bottom strip of a band
    CORE_H = 140.0
    n = len(LOOPS)

    parts: list[str] = []
    y = M + TITLE_PX
    parts.append(c.text(M, y, "The loops kstrl is built as", TITLE_PX, t["ink"], "600"))
    y += SUB_PX + 10
    parts.append(
        c.text(
            M,
            y,
            "Innermost fastest. Each band: what acts, what measures it, and what it steers toward.",
            SUB_PX,
            t["ink_3"],
        )
    )
    y0 = y + 22

    ring_font = 19.0
    name_font = 26.0
    # Outermost first so each band paints over the one around it.
    for k, loop in enumerate(reversed(LOOPS)):
        number = n - k
        hue = t["layers"].get(loop["layer"], "")
        x = M + k * DX
        ry = y0 + k * ST
        w = W - 2 * M - 2 * k * DX
        h = (n - k) * ST + CORE_H + (n - k) * DB
        if hue:
            stroke, fill, dash = hue, mix(t["bg"], hue, 0.06), ""
        else:
            stroke, fill, dash = t["ink_3"], t["surface"], ' stroke-dasharray="8 6"'
        parts.append(
            f'<rect x="{x:.0f}" y="{ry:.0f}" width="{w:.0f}" height="{h:.0f}" rx="18" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2"{dash}/>'
        )
        # Number, name, rate on the first line; the status at the right.
        bx, by = x + 24 + 15, ry + 30
        parts.append(f'<circle cx="{bx:.0f}" cy="{by:.0f}" r="15" fill="{stroke}"/>')
        parts.append(c.text(bx, by + 6, str(number), 18, t["bg"], "700", True, "middle"))
        nx = bx + 28
        parts.append(c.text(nx, by + 9, loop["id"], name_font, t["ink"], "600"))
        nx += width_of(loop["id"], name_font) * 1.08 + 14
        parts.append(c.text(nx, by + 9, loop["rate"], 20, t["ink_3"]))

        sensor_states = [states[s] for s in loop["sensors"]]
        derived = ""
        if sensor_states and all(s == "planned" for s in sensor_states):
            derived = "sensor not built"
        elif any(s == "planned" for s in sensor_states):
            derived = "sensor part built"
        status = loop["closed"]
        if derived and status != "not built":
            status = f"{status}; {derived}"
        closed = loop["closed"]
        if closed == "closed":
            mfill, mstroke, mdash = t["good"], t["good"], ""
        elif closed.startswith("closed"):
            mfill, mstroke, mdash = t["warn"], t["warn"], ""
        elif closed == "open":
            mfill, mstroke, mdash = "none", t["bad"], ""
        else:
            mfill, mstroke, mdash = "none", t["ink_3"], ' stroke-dasharray="3 3"'
        sx = x + w - 24
        parts.append(c.text(sx, by + 7, status, 20, t["ink_2"], "500", False, "end"))
        mx = sx - width_of(status, 20) - 18
        parts.append(
            f'<circle cx="{mx:.0f}" cy="{by:.0f}" r="8" fill="{mfill}" stroke="{mstroke}" '
            f'stroke-width="2.2"{mdash}/>'
        )
        # Then acts, measures, toward: one line each.
        lx = x + 24
        rows = [
            (62, "acts  ", loop["acts"], t["accent"]),
            (88, "measures  ", loop["measures"], t["steel"]),
            (114, "toward  ", loop["toward"], t["ink_2"]),
        ]
        for dy, head, value, colour in rows:
            parts.append(
                c.spans(lx, ry + dy, ring_font, [(head, t["ink_3"], "400"), (value, colour, "500")])
            )

    # The plant at the centre: the thing every loop acts on.
    cx = M + n * DX
    cy = y0 + n * ST
    cw = W - 2 * M - 2 * n * DX
    stroke, fill = t["container"]["isolated"]
    parts.append(
        f'<rect x="{cx:.0f}" y="{cy:.0f}" width="{cw:.0f}" height="{CORE_H:.0f}" rx="10" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
    )
    parts.append(c.text(cx + 24, cy + 34, "THE PLANT", 16, t["ink_3"], "600", True))
    plant = [
        "The coding agent: a subprocess in its own process group, on a deadline,",
        "writing only its worktree. Every band around it acts on what it produced",
        "and measures it with something the agent did not write.",
    ]
    for k, line in enumerate(plant):
        parts.append(c.text(cx + 24, cy + 68 + 26 * k, line, ring_font, t["ink"], "500"))

    y = cy + CORE_H + n * DB + LEGEND_PX + 22
    x = M
    seg, x = swatch_legend(
        c,
        x,
        y,
        [(t["raised"], t["accent"], "what acts"), (t["raised"], t["steel"], "what measures")],
        LEGEND_PX,
    )
    parts.append(seg)
    markers = [
        (t["good"], t["good"], "", "closed"),
        (t["warn"], t["warn"], "", "closed, winds up or an input unwired"),
        ("none", t["bad"], "", "open"),
        ("none", t["ink_3"], ' stroke-dasharray="3 3"', "not built"),
    ]
    for mfill, mstroke, mdash, label in markers:
        parts.append(
            f'<circle cx="{x + 8:.0f}" cy="{y - 7:.0f}" r="8" fill="{mfill}" stroke="{mstroke}" '
            f'stroke-width="2.2"{mdash}/>'
        )
        x += 24
        parts.append(c.text(x, y, label, LEGEND_PX, t["ink_2"]))
        x += width_of(label, LEGEND_PX) + LEGEND_PX * 1.6
    y += LEGEND_PX + 12
    note = "The band's edge is the colour of the layer the loop lives in on the atlas."
    parts.append(c.text(M, y, note, LEGEND_PX, t["ink_3"]))
    y += CAPTION_PX + 30
    parts.append(c.text(M, y, caption_for(atlas), CAPTION_PX, t["ink_3"]))
    y += CAPTION_PX + 8
    source = "Loop table: docs/control-loop-design.md, section 2, The loop nest."
    parts.append(c.text(M, y, source, CAPTION_PX, t["ink_3"]))
    h = y + M - 6
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * scale:.0f}" '
        f'height="{h * scale:.0f}" viewBox="0 0 {W:.0f} {h:.0f}" role="img" '
        f'aria-label="The seven control loops of kstrl as nested bands">'
        f'<rect x="0" y="0" width="{W:.0f}" height="{h:.0f}" rx="14" fill="{t["bg"]}" '
        f'stroke="{t["line_2"]}" stroke-width="2"/>'
        + "".join(parts)
        + c.css()
        + "</svg>"
    )


def all_figures(
    atlas: dict[str, Any], scale: float = 1.0, compact: bool = True
) -> dict[str, Figure]:
    """Every figure by file name, in the order they are written."""
    out: dict[str, Figure] = {"system.svg": Figure(system_figure(atlas, scale))}
    for layer in LAYERS:
        name = f"layer-{layer['id']}.svg"
        if compact:
            out[name] = compact_layer_figure(atlas, layer, scale)
        else:
            out[name] = Figure(layer_figure(atlas, layer, scale))
    for journey in JOURNEYS:
        name = f"journey-{journey['id']}.svg"
        if compact:
            out[name] = compact_journey_figure(atlas, journey, scale)
        else:
            out[name] = Figure(journey_figure(atlas, journey, scale))
    out["loops.svg"] = Figure(loops_figure(atlas, scale))
    return out


def describe(name: str, fig: Figure) -> str:
    """One line of numbers for a compact figure; the name alone for the rest."""
    s = fig.stats
    if not s:
        return name
    w, h = s["canvas"]
    return (
        f"{name}: map {w:.0f} x {h:.0f} in {s['columns']} columns, {s['rows']} row"
        f"{'s' if s['rows'] != 1 else ''}, {s['components']} components, {s['edges']} edges, "
        f"{s['through_cards']} through a card, {s['across_regions']} across a region, "
        f"{s['label_collisions']} label collisions, nothing under {s['min_px']:g}px, "
        f"names {s['name_px_at_900']}px at 900px"
    )


def self_check(name: str, svg: str) -> list[str]:
    """What a figure must be to be embedded anywhere: well-formed, inert, local."""
    problems: list[str] = []
    try:
        # The parser reads a string this process just built, not a file
        # from outside, so the stdlib parser is the right tool here.
        ET.fromstring(svg)
    except ET.ParseError as e:
        problems.append(f"{name}: not well-formed XML: {e}")
    if "<script" in svg:
        problems.append(f"{name}: contains a script")
    for hit in re.findall(r"https?://[^\s\"'<>)]+", svg):
        if hit != "http://www.w3.org/2000/svg":
            problems.append(f"{name}: external reference {hit}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlas", default=ATLAS_PATH)
    parser.add_argument("--out-dir", default=str(FIGURE_DIR))
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="multiplies each figure's declared pixel size; the drawing is unchanged. "
        "A page that fits the image to its column ignores it; a page that shows the "
        "image at its own size shows it that much larger.",
    )
    parser.add_argument(
        "--compact",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="draw each layer and journey with only the components it touches (the "
        "default); --no-compact draws the whole atlas with the rest dimmed",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when any committed figure differs from what would be generated",
    )
    args = parser.parse_args()

    problems = layout_problems()
    if problems:
        for p in problems:
            print(f"layout: {p}", file=sys.stderr)
        return 1

    atlas = json.loads(Path(args.atlas).read_text(encoding="utf-8"))
    figures = all_figures(atlas, args.scale, args.compact)
    bad: list[str] = []
    for name, fig in figures.items():
        bad.extend(self_check(name, fig.svg))
        bad.extend(f"{name}: {p}" for p in fig.problems)
    if bad:
        for line in bad:
            print(line, file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    if args.check:
        stale: list[str] = []
        for name, fig in figures.items():
            path = out_dir / name
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            if current != fig.svg:
                stale.append(name)
        extra = sorted(p.name for p in out_dir.glob("*.svg") if p.name not in figures)
        if stale or extra:
            for name in stale:
                print(f"{out_dir / name} is stale", file=sys.stderr)
            for name in extra:
                print(f"{out_dir / name} is not a figure this script generates", file=sys.stderr)
            print(
                f"run: uv run python {GENERATOR}, then commit {out_dir}/",
                file=sys.stderr,
            )
            return 1
        for name, fig in figures.items():
            if fig.stats:
                print(f"  {describe(name, fig)}")
        print(f"{out_dir}: {len(figures)} figures current")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, fig in figures.items():
        path = out_dir / name
        path.write_text(fig.svg, encoding="utf-8")
        print(f"wrote {path} ({path.stat().st_size / 1024:.0f} KB)")
        if fig.stats:
            print(f"  {describe(name, fig)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
