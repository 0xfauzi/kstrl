"""Draw the logical view as a flat figure. One generator for every picture.

A stranger needs four things from the map, and each one owns exactly one
visual channel so that no channel says two things at once:

    what exists ....... a card, inside the boundary it belongs to, carrying
                        its code name and its plain word
    how finished ...... the card's FILL, derived from entry points found
    how work travels .. lines between cards, each labelled with what it
                        carries and coloured by the LAYER it belongs to
    what it means ..... the focus interaction: click a card and its
                        neighbours stay lit, each edge thickens in its
                        layer's colour, each neighbour is tagged with the
                        verb that relates it, and the panel draws the
                        relationship wheel

Planned components are drawn as ghosts (dashed, muted) in their own region, so
"today" and "end state" are the same drawing with one CSS class flipped on the
root (`endstate`) rather than two drawings that could disagree. Every node
carries `data-id` and a class for its build state; every edge carries its
artifact as visible text and its layer as `data-layer`.

Edges are Manhattan paths routed by route.py through the gutters between
cards: never through a card they do not connect, never through a region they
neither start nor end in. Each label sits on the longest segment of its own
path, or in the gutter beside a run too short to hold it. What could not be
placed cleanly is reported in the detail JSON under `_meta.collisions`, and
any route that had to break a rule under `_meta.notes`, so a crowded layout
fails loudly instead of quietly drawing text over text.

Positions come from logical_model, so the same system always draws the same
figure and a moved card means the architecture moved. Meaning (layers, the
sentence per edge, the verb per spoke, the plain words) comes from relations.

No text in the figure is set below 11px.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

import theme as theme_mod
from logical_model import (
    CANVAS,
    COMPONENTS,
    CONTAINERS,
    FLOW_KINDS,
    FLOWS,
    GOVERNED_BY,
    INVARIANTS,
    REGIONS,
    build_state,
)
from relations import JOURNEYS, LAYERS, PLAIN, RELATIONS, layer_for, verb_for
from route import path_d, place_labels, route_all

CARD_W = 150.0
CARD_H = 56.0
STORE_H = 52.0
ACTOR_H = 44.0
RADIUS = 4.0
# The smallest type on the figure. Edge labels, plain words and every note
# sit at this size; names sit half a point above it.
TEXT_PX = 11.0
NAME_PX = 11.5
LABEL_PX = TEXT_PX
# Width is estimated from the glyph count, so the collision pass can run
# without a renderer.
LABEL_CHAR_W = 6.1
LABEL_H = 13.0
LABEL_GAP = 2.0
# A plain word wraps to the card: 140 units of inner width at 11px sans.
PLAIN_CHARS = 26

ISSUE_URL = "https://github.com/0xfauzi/kstrl/issues/{n}"

Box = tuple[float, float, float, float]


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def wrap_words(text: str, width: int, max_lines: int) -> list[str]:
    """Greedy word wrap, truncated at a word boundary rather than overflowing."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and len(" ".join(lines)) < len(text.rstrip(".")):
        tail = lines[-1]
        if len(tail) >= width:
            cut = tail[: width - 1]
            spaced, _, _ = cut.rpartition(" ")
            tail = spaced or cut
        lines[-1] = tail + "..."
    return lines


def _rgb(colour: str) -> tuple[int, int, int]:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(a: str, b: str, t: float) -> str:
    """a towards b by t."""
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    r = round(ra + (rb - ra) * t)
    g = round(ga + (gb - ga) * t)
    bl = round(ba + (bb - ba) * t)
    return f"#{r:02X}{g:02X}{bl:02X}"








def lives_in(modules: list[str]) -> str:
    """One muted line for a contributor: the file, or the package when many.

    Three or fewer modules are named as files. More than that is a package,
    named by its common directory with a count, so a component spread over a
    subpackage does not turn the panel into a listing.
    """
    if not modules:
        return ""
    if len(modules) <= 3:
        return ", ".join(m.replace(".", "/") + ".py" for m in modules)
    parts = [m.split(".") for m in modules]
    common: list[str] = []
    for column in zip(*parts, strict=False):
        if len(set(column)) == 1:
            common.append(column[0])
        else:
            break
    if len(common) == len(min(parts, key=len)):
        common = common[:-1]
    return "/".join(common) + f"/ ({len(modules)} modules)"


def tracker_issues(tracker: str) -> tuple[str, list[str]]:
    """('R10.7', ['228']) from 'R10.7 #228'; the label loses its numbers."""
    numbers = re.findall(r"#(\d+)", tracker)
    label = re.sub(r"\s*#\d+", "", tracker).strip(" ,")
    return label, numbers


def legend_rows(mode: str) -> list[tuple[str, str, str]]:
    """(fill, stroke, label) for the legend the given mode needs."""
    t = theme_mod.get()
    if mode == "change":
        d = t["delta"]
        return [
            (mix(t["bg"], t["change"], 0.14), t["change"], "changed here"),
            (mix(t["bg"], t["reach"], 0.12), t["reach"], "reached by it"),
            (t["ghost"][0], t["ghost"][1], "untouched"),
            (d["operations"], d["operations"], "new operations"),
            (d["types"], d["types"], "new types"),
            (d["refusals"], d["refusals"], "new refusals"),
            (d["tests"], d["tests"], "new tests"),
        ]
    return [(f, s, label) for f, s, label in t["state"].values()]


def layer_rows() -> list[tuple[str, str, str]]:
    """(id, colour, label) for every layer, in LAYERS order."""
    t = theme_mod.get()
    return [(layer["id"], t["layers"][layer["id"]], layer["label"]) for layer in LAYERS]


def _svg_style(svg_id: str, t: dict[str, Any]) -> str:
    """Interaction states, scoped to one figure so two on a page cannot leak."""
    s = f"#{svg_id}"
    return (
        "<style>"
        f"{s} .node{{transition:opacity .18s ease;cursor:pointer}}"
        f"{s} .node.dim{{opacity:.16}}"
        f"{s} .node.quiet{{opacity:.42}}"
        f"{s} .node.planned{{opacity:.5}}"
        f"{s}.endstate .node.planned{{opacity:1}}"
        f"{s}.endstate .node.planned.quiet{{opacity:.42}}"
        f"{s}.endstate .node.planned.dim{{opacity:.16}}"
        f"{s} .node.sel .node__box{{stroke:{t['accent']};stroke-width:2.6}}"
        f"{s} .node.meas .node__box{{stroke:{t['steel']};stroke-width:2.2}}"
        f"{s} .node.acts .node__box{{stroke:{t['accent']};stroke-width:2.4}}"
        f"{s} .node__ring{{display:none;fill:none;stroke:{t['steel']};stroke-width:1.6}}"
        f"{s} .node.meas .node__ring{{display:inline}}"
        f"{s} .node.tagged [data-layer=job]{{opacity:0}}"
        f"{s} .node:focus-visible{{outline:none}}"
        f"{s} .node:focus-visible .node__box{{stroke:{t['accent']};stroke-width:2.6}}"
        f"{s} .flow{{transition:opacity .18s ease}}"
        f"{s} .flow.planned{{opacity:.35}}"
        f"{s}.endstate .flow.planned{{opacity:1}}"
        f"{s} .flow.off,{s} .flowlbl.off{{display:none}}"
        f"{s} .flow.dim{{opacity:.07}}"
        f"{s} .flow.hot{{opacity:1;stroke-opacity:1;stroke-width:2.6;"
        "stroke-dasharray:8 6;animation:atlasflow 1.1s linear infinite}"
        f"{s} .flow.peek{{stroke-width:3.4}}"
        f"{s} .flowlbl{{transition:opacity .15s ease;pointer-events:none}}"
        f"{s} .flowlbl.planned{{opacity:.5}}"
        f"{s}.endstate .flowlbl.planned{{opacity:1}}"
        f"{s} .flowlbl.dim{{opacity:.08}}"
        f"{s} .flowlbl.hot{{opacity:1}}"
        f"{s} .vtag rect{{stroke-width:1}}"
        f"{s} .vtag text{{font-family:{t['font_ui']};font-size:{TEXT_PX}px;"
        "font-weight:500;text-anchor:middle}"
        # The figure fills its column; the drawing pans and zooms inside it
        # on the .view group, so the element itself never scrolls. touch-action
        # none hands one-finger drags and pinches to the script.
        f"{s}{{width:100%;height:auto;display:block;overflow:hidden;touch-action:none;"
        "cursor:grab}"
        f"{s}.panning{{cursor:grabbing}}"
        f"{s} .zone__h{{cursor:zoom-in;-webkit-user-select:none;user-select:none}}"
        "@keyframes atlasflow{to{stroke-dashoffset:-14}}"
        f"@media print{{{s} .view{{transform:none!important}}}}"
        "@media (prefers-reduced-motion:reduce){"
        f"{s} .flow.hot{{animation:none;stroke-dasharray:none}}"
        f"{s} .node,{s} .flow,{s} .flowlbl{{transition:none}}}}"
        "</style>"
    )


def _static_style(svg_id: str, t: dict[str, Any]) -> str:
    """The same class vocabulary as the interactive figure, frozen.

    A static figure (an .svg a document embeds as an image) cannot run the
    script, so what the script would set on a layer or a journey is set at
    render time, and this stylesheet gives those classes the same look
    without transitions, cursors or animation. Order matters: `hot` comes
    after `planned` so a planned edge on a traced path is drawn at full
    strength, dashed.
    """
    s = f"#{svg_id}"
    return (
        "<style>"
        f"{s} .node.quiet{{opacity:.42}}"
        f"{s} .node.planned{{opacity:.5}}"
        f"{s} .node.planned.quiet{{opacity:.3}}"
        f"{s} .node.meas .node__box{{stroke:{t['steel']};stroke-width:2.2}}"
        f"{s} .node.acts .node__box{{stroke:{t['accent']};stroke-width:2.4}}"
        f"{s} .node__ring{{display:none;fill:none;stroke:{t['steel']};stroke-width:1.6}}"
        f"{s} .node.meas .node__ring{{display:inline}}"
        f"{s} .flow.planned{{opacity:.35}}"
        f"{s} .flowlbl.planned{{opacity:.5}}"
        f"{s} .flow.off,{s} .flowlbl.off{{display:none}}"
        f"{s} .flow.hot{{opacity:1;stroke-opacity:1;stroke-width:2.6}}"
        f"{s} .flowlbl.hot{{opacity:1}}"
        "</style>"
    )


def _defs(svg_id: str, t: dict[str, Any]) -> str:
    """Arrowheads: one per layer colour, plus the change-map colours.

    Sized in user units so a thick focused edge and a hairline ghost edge
    carry the same head; the thickness is the emphasis, the head is the
    direction.
    """
    heads = dict(t["layers"])
    heads["change"] = t["change"]
    heads["reach"] = t["reach"]
    out = ["<defs>"]
    for name, colour in heads.items():
        out.append(
            f'<marker id="{svg_id}-m-{name}" viewBox="0 0 8 8" refX="7" refY="4" '
            f'markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" '
            f'orient="auto-start-reverse">'
            f'<path d="M0,0 L8,4 L0,8 z" fill="{colour}"/></marker>'
        )
    out.append("</defs>")
    return "".join(out)


class TextStyles:
    """Text styling deduplicated into classes.

    240 text elements each carrying a full font stack tripled the size of
    the figure for no information, and a lesson embeds the figure whole.
    Every distinct (size, colour, weight, face, anchor, spacing, halo) gets
    one class; `css()` emits them, scoped to the figure's id. Nothing may be
    set below `floor` (11px on the atlas), and the assertion is the check.
    """

    def __init__(self, svg_id: str, t: dict[str, Any], floor: float = TEXT_PX) -> None:
        self.svg_id = svg_id
        self.t = t
        self.floor = floor
        self.styles: dict[tuple[float, str, str, bool, str, str, bool], str] = {}

    def __call__(
        self,
        px: float,
        py: float,
        text: str,
        size: float,
        colour: str,
        weight: str = "500",
        mono: bool = False,
        anchor: str = "middle",
        spacing: str = "",
        halo: bool = False,
    ) -> str:
        assert size >= self.floor, f"text below {self.floor}px: {text!r} at {size}"
        key = (size, colour, weight, mono, anchor, spacing, halo)
        cls = self.styles.setdefault(key, f"t{len(self.styles)}")
        return f'<text x="{px:.1f}" y="{py:.1f}" class="{cls}">{esc(text)}</text>'

    def css(self) -> str:
        rules: list[str] = []
        for (size, colour, weight, mono, anchor, spacing, halo), cls in self.styles.items():
            family = self.t["font_mono"] if mono else self.t["font_ui"]
            rule = (
                f"font-family:{family};font-size:{size}px;font-weight:{weight};"
                f"fill:{colour};text-anchor:{anchor}"
            )
            if spacing:
                rule += f";letter-spacing:{spacing}"
            if halo:
                rule += (
                    f";paint-order:stroke;stroke:{self.t['bg']};stroke-width:4;"
                    "stroke-linejoin:round"
                )
            rules.append(f"#{self.svg_id} .{cls}{{{rule}}}")
        return "<style>" + "".join(rules) + "</style>"


def card_svg(
    L: TextStyles,
    T: dict[str, Any],
    cid: str,
    kind: str,
    state: str,
    box: Box,
    fill: str,
    stroke: str,
    classes: str,
    region: str,
    state_label: str,
    plain: str,
    stroke_width: float = 1.1,
    name_px: float = NAME_PX,
    text_px: float = TEXT_PX,
) -> str:
    """One card, open: the group, its box, its name and its plain word.

    The caller closes the group, so a change map can add its delta bar
    first. `classes` follows `node ` on the group (the build state, then
    any of quiet, acts, meas, node--moved). A store is the same card with a
    rule under its head: flat convention for a thing that holds rows rather
    than does work. How many plain-word lines fit is derived from the card,
    never assumed.
    """
    x, y, w, h = box
    dashes = ' stroke-dasharray="4 3"' if state == "planned" or kind == "actor" else ""
    g = [
        f'<g class="node {classes}" '
        f'data-id="{esc(cid)}" data-kind="{kind}" data-state="{state}" '
        f'data-region="{esc(region)}" '
        f'role="button" '
        f'tabindex="0" aria-label="{esc(cid)}, {esc(plain)}, {esc(state_label)}">'
        f'<rect class="node__box" x="{x}" y="{y}" width="{w}" height="{h}" '
        f'rx="{RADIUS}" fill="{fill}" stroke="{stroke}" '
        f'stroke-width="{stroke_width}"'
        f"{dashes}/>"
        f'<rect class="node__ring" x="{x + 3}" y="{y + 3}" width="{w - 6}" '
        f'height="{h - 6}" rx="{RADIUS - 1}"/>'
    ]
    name_ink = T["ink"] if state != "planned" else T["ink_2"]
    g.append(L(x + w / 2, y + 17, cid, name_px, name_ink, "600", True))
    if kind == "store":
        g.append(
            f'<line x1="{x + 1}" y1="{y + 23}" x2="{x + w - 1}" y2="{y + 23}" '
            f'stroke="{stroke}" stroke-opacity=".45" stroke-width="1"/>'
        )
    first = y + (36 if kind == "store" else 32)
    room = int((y + h - 4 - first) // 12) + 1
    plain_lines = wrap_words(plain, PLAIN_CHARS, max(1, room))
    g.append(
        '<g data-layer="job">'
        + "".join(
            L(x + w / 2, first + k * 12, line, text_px, T["ink_3"], "400")
            for k, line in enumerate(plain_lines)
        )
        + "</g>"
    )
    return "".join(g)


def flow_svg(
    svg_id: str,
    i: int,
    src: str,
    dst: str,
    artifact: str,
    kind: str,
    layer_: str,
    points: list[tuple[float, float]],
    colour: str,
    marker: str,
    ghost: bool,
    off: bool,
    hot: bool,
    art_hot: bool = False,
) -> str:
    """One routed flow as a path: dashed when planned, hidden when off."""
    classes = f"flow {kind}" + (" planned" if ghost else "")
    classes += (" off" if off else "") + (" hot" if hot else "")
    dash = ' stroke-dasharray="4 3"' if ghost else ""
    return (
        f'<path id="{svg_id}-f{i}" class="{classes}" data-edge="{i}" data-from="{esc(src)}" '
        f'data-to="{esc(dst)}" data-art="{esc(artifact)}" '
        f'data-kind="{esc(kind)}" data-layer="{layer_}" d="{path_d(points)}" '
        f'fill="none" stroke="{colour}" stroke-opacity="{0.95 if art_hot else 0.82}" '
        f'stroke-width="{1.8 if art_hot else 1.2}"{dash} stroke-linecap="round" '
        f'marker-end="url(#{svg_id}-m-{marker})"/>'
    )


def label_svg(
    L: TextStyles,
    i: int,
    src: str,
    dst: str,
    kind: str,
    layer_: str,
    artifact: str,
    box: Box,
    colour: str,
    ghost: bool,
    off: bool,
    hot: bool,
    label_px: float = LABEL_PX,
) -> str:
    """The artifact on a flow, haloed, centred in the box the placer chose."""
    lx, ly = box[0] + box[2] / 2, box[1] + box[3] - 3
    return (
        f'<g class="flowlbl {kind}{" planned" if ghost else ""}'
        f'{" off" if off else ""}{" hot" if hot else ""}" data-edge="{i}" '
        f'data-from="{esc(src)}" data-to="{esc(dst)}" data-layer="{layer_}">'
        + L(lx, ly, artifact, label_px, colour, "500", False, "middle", "", True)
        + "</g>"
    )


def badge_svg(
    L: TextStyles, T: dict[str, Any], i: int, n: str, bx: float, by: float
) -> str:
    """A journey step number in a filled accent circle, centred at (bx, by)."""
    r = 8.5 + 3.2 * (len(n) - 1)
    return (
        f'<g class="step" data-edge="{i}">'
        f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="{r:.1f}" fill="{T["accent"]}"/>'
        + L(bx, by + 4, n, TEXT_PX, T["bg"], "700", True)
        + "</g>"
    )


def render(
    atlas: dict[str, Any],
    changed: set[str] | None = None,
    changed_modules: set[str] | None = None,
    adjacent: set[str] | None = None,
    mode: str = "system",
    svg_id: str = "schematic",
    gained: dict[str, dict[str, int]] | None = None,
    hot_artifacts: set[str] | None = None,
    layer: str = "",
    journey: dict[str, Any] | None = None,
    static: bool = False,
) -> tuple[str, str]:
    """(svg, json detail).

    `changed` marks logical components a change moved (or a plan reaches).
    `changed_modules` marks physical modules it touched. `hot_artifacts` names
    the flow labels whose owning module redefined part of its surface; the
    change detector is the one place that computes it, so this only draws
    what it is told.

    `layer` draws one layer the way the page's layer switch does: the other
    layers' edges are off and the components they alone touch are quiet.
    `journey` (one entry of relations.JOURNEYS) draws one journey the way
    stepping through it does, all steps at once: only its edges are drawn,
    each numbered with the steps that trace it, acting components in amber,
    measuring components in steel, everything else quiet. Both set the same
    classes the interactive script sets, so a static figure and the page
    agree. `static` swaps the interactive stylesheet for one without
    transitions, cursors or animation, for a figure embedded as an image.

    The detail JSON carries one record per component (what the focus panel
    shows) plus a `_meta` key: the layers, every edge with its verbs and its
    sentence, the rules, the regions, and the edge labels the collision pass
    could not place cleanly.
    """
    changed = changed or set()
    changed_modules = changed_modules or set()
    adjacent = adjacent or set()
    gained = gained or {}
    hot_artifacts = hot_artifacts or set()
    change_mode = mode == "change"

    T = theme_mod.get()
    INK_3 = T["ink_3"]
    HALO = T["bg"]
    GHOST_FILL, GHOST_STROKE = T["ghost"]
    LAYER_COLOUR: dict[str, str] = T["layers"]

    L = TextStyles(svg_id, T)
    text_css = L.css

    states = {c["id"]: build_state(c, atlas) for c in COMPONENTS}

    def geom(c: dict[str, Any]) -> Box:
        h = {"store": STORE_H, "actor": ACTOR_H}.get(c["kind"], CARD_H)
        return float(c["x"]), float(c["y"]), CARD_W, h

    boxes = {c["id"]: geom(c) for c in COMPONENTS}

    # ---- ground: boundaries, then the bands -------------------------------
    floor: list[str] = []
    obstacles: list[tuple[str, Box]] = []
    # What a route may not cross besides a card: every header, and any
    # container that holds no card (the worktree, the control directory).
    blocks: list[Box] = []
    occupied = {c.get("container") for c in COMPONENTS if c.get("container")}
    for box in CONTAINERS:
        x, y, w, h = box["box"]
        stroke, fill = T["container"][box["tone"]]
        if change_mode:
            stroke, fill = T["line"], T["bg"]
        sub_lines = wrap_words(box["sub"], max(12, int((w - 26) / 5.6)), 2)
        floor.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>'
            + L(x + 13, y + 19, box["label"], TEXT_PX, INK_3, "600", True, "start", ".13em")
            + "".join(
                L(x + 13, y + 33 + 12 * k, line, TEXT_PX, INK_3, "400", False, "start")
                for k, line in enumerate(sub_lines)
            )
        )
        # The header obstacle is the text, not the whole top edge of the box:
        # the factory's spans the canvas, and a wall that wide would close
        # the corridor every long edge runs along.
        text_w = max([len(box["label"]) * 7.6] + [len(line) * 5.6 for line in sub_lines]) + 8
        header: Box = (x + 8, y + 6, min(w - 16, text_w), 30 + 12 * len(sub_lines))
        obstacles.append((f"{box['id']} header", header))
        blocks.append(header)
        if box["id"] not in occupied and box["id"] != "factory":
            blocks.append((float(x), float(y), float(w), float(h)))
            obstacles.append((box["id"], (float(x), float(y), float(w), float(h))))

    zones: list[str] = []
    for i, region in enumerate(REGIONS, start=1):
        x, y, w, h = region["box"]
        zones.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="none" '
            f'stroke="{T["region"]}" stroke-opacity=".28" stroke-width="1" '
            f'stroke-dasharray="3 4"/>'
            f'<g class="zone__h" data-zone="{esc(region["id"])}">'
            f'<circle cx="{x + 17:.0f}" cy="{y + 16:.0f}" r="8.5" fill="{HALO}" '
            f'stroke="{T["region"]}" stroke-opacity=".5"/>'
            + L(x + 17, y + 20, str(i), TEXT_PX, T["region"], "600", True)
            + L(
                x + 31, y + 20, region["label"], TEXT_PX, T["region"], "600", True, "start", ".13em"
            )
            + "</g>"
        )
        obstacles.append((f"{region['id']} header", (x + 6, y + 5, 150, 24)))
        blocks.append((x + 6, y + 5, 150, 24))
    for cid, (x, y, w, h) in boxes.items():
        obstacles.append((cid, (x - 3, y - 3, w + 6, h + 6)))

    # ---- flows ------------------------------------------------------------
    # Every flow is a Manhattan path through the gutters, routed by route.py
    # against the card boxes, the headers and the region boxes; the drawing
    # here only paints what the router returns and reports what it could
    # not do.
    actor_ids = {c["id"] for c in COMPONENTS if c["kind"] == "actor"}
    region_of = {c["id"]: c.get("region") or "" for c in COMPONENTS}
    region_boxes = {r["id"]: tuple(float(v) for v in r["box"]) for r in REGIONS}
    edges = [(src, dst) for src, dst, _art, _k in FLOWS]
    edge_index = {(a, b): i for i, (a, b, _art, _k) in enumerate(FLOWS)}
    # What a layer or a journey lights. `traced` maps an edge to the step
    # numbers that trace it; `lit` is every component a journey names or
    # its traced edges touch; `in_layer` every component the layer's edges
    # touch. Mirrors paint() in the interactive script.
    traced: dict[int, list[int]] = {}
    acts_ids: set[str] = set()
    meas_ids: set[str] = set()
    lit: set[str] = set()
    if journey:
        for n, step in enumerate(journey["steps"], start=1):
            e = edge_index.get(tuple(step["edge"]), -1)
            if e >= 0:
                traced.setdefault(e, []).append(n)
                lit.update(FLOWS[e][:2])
            acts_ids.update(step["acts"])
            meas_ids.update(step["measures"])
        lit |= acts_ids | meas_ids
    in_layer: set[str] = set()
    if layer:
        for src, dst, _art, kind in FLOWS:
            if layer_for((src, dst), kind) == layer:
                in_layer.update((src, dst))
    routes = route_all(edges, boxes, actor_ids, blocks, region_boxes, region_of, CANVAS)
    widths = {i: len(FLOWS[i][2]) * LABEL_CHAR_W + 6 for i in routes}
    seats = place_labels(routes, widths, LABEL_H, obstacles, CANVAS)

    flow_parts: list[str] = []
    label_parts: dict[int, str] = {}
    collisions: list[str] = []
    notes: list[str] = []
    label_boxes: list[dict[str, Any]] = []
    layers_of: dict[int, str] = {}
    paths: dict[int, list[list[float]]] = {}
    badges: list[str] = []
    for i, (src, dst, artifact, kind) in enumerate(FLOWS):
        layer_ = layer_for((src, dst), kind)
        layers_of[i] = layer_
        route = routes[i]
        paths[i] = [[round(x, 1), round(y, 1)] for x, y in route.points]
        if route.fallback:
            notes.append(f"{src} -> {dst}: {route.fallback}")
        art_hot = artifact in hot_artifacts
        colour, marker = LAYER_COLOUR[layer_], layer_
        if art_hot:
            colour, marker = T["change"], "change"
        ghost = states[src] == "planned" or states[dst] == "planned"
        off = (layer and layer_ != layer) or (journey is not None and i not in traced)
        hot = i in traced
        flow_parts.append(
            flow_svg(
                svg_id, i, src, dst, artifact, kind, layer_, route.points, colour, marker,
                ghost=ghost, off=bool(off), hot=hot, art_hot=art_hot,
            )
        )
        seat = seats[i]
        if seat.cost > 0:
            collisions.append(
                f"'{artifact}' ({src} -> {dst}) overlaps {', '.join(seat.hits[:3])}"
            )
        if not seat.on_longest:
            notes.append(f"'{artifact}' ({src} -> {dst}) sits on a shorter segment")
        lbox = seat.box
        label_boxes.append(
            {
                "from": src,
                "to": dst,
                "artifact": artifact,
                "box": [round(v, 1) for v in lbox],
                "segment": seat.segment,
            }
        )
        label_parts[i] = label_svg(
            L, i, src, dst, kind, layer_, artifact, lbox, colour,
            ghost=ghost, off=bool(off), hot=hot,
        )
        if hot:
            # The step number sits just left of the edge's label, on the
            # accent, so the path can be read in order without the page.
            n = ",".join(str(k) for k in traced[i])
            bx, by = lbox[0] - 11, lbox[1] + LABEL_H / 2
            badges.append(badge_svg(L, T, i, n, bx, by))

    # ---- cards ------------------------------------------------------------
    detail: dict[str, Any] = {}
    cards: list[str] = []
    for c in COMPONENTS:
        cid = c["id"]
        kind = c["kind"]
        state = states[cid]
        fill, stroke, state_label = T["state"][state]
        if kind == "actor":
            fill, stroke = mix(T["bg"], T["line_2"], 0.35), INK_3
        moved, near = cid in changed, cid in adjacent
        tier = "moved" if moved else ("near" if near else "far")
        if change_mode and tier == "far":
            fill, stroke = GHOST_FILL, GHOST_STROKE
        if change_mode and moved:
            stroke = T["change"]
        elif change_mode and near:
            stroke = T["reach"]
        x, y, w, h = boxes[cid]
        rules = GOVERNED_BY.get(cid, [])
        state_class = state if kind != "actor" else "actor"
        plain = PLAIN.get(cid, "")
        marks = ""
        if journey is not None:
            marks += (" acts" if cid in acts_ids else "") + (" meas" if cid in meas_ids else "")
            marks += "" if cid in lit else " quiet"
        elif layer and cid not in in_layer:
            marks = " quiet"

        g: list[str] = [
            card_svg(
                L, T, cid, kind, state, boxes[cid], fill, stroke,
                classes=f"{state_class}{marks}" + (f" node--{tier}" if change_mode else ""),
                region=c.get("region") or c.get("container") or "",
                state_label=state_label,
                plain=plain,
                stroke_width=1.6 if change_mode and (moved or near) else 1.1,
            )
        ]

        delta = gained.get(cid, {}) if change_mode and moved else {}
        if delta:
            total = sum(delta.values()) or 1
            pos = 0.0
            for key in ("operations", "types", "refusals", "tests"):
                n = delta.get(key, 0)
                if not n:
                    continue
                seg = (w - 2) * n / total
                g.append(
                    f'<rect x="{x + 1 + pos:.1f}" y="{y + 1}" '
                    f'width="{seg:.1f}" height="2.5" fill="{T["delta"][key]}"/>'
                )
                pos += seg
            g.append(
                f'<circle cx="{x + 12}" cy="{y - 1}" r="10" fill="{T["change"]}"/>'
                + L(x + 12, y + 3, f"+{sum(delta.values())}", TEXT_PX, HALO, "600", True)
            )
        elif change_mode and moved:
            g.append(
                L(
                    x + w / 2,
                    y - 5,
                    "IN REACH" if not changed_modules else "CHANGED INSIDE",
                    TEXT_PX,
                    T["change"],
                    "600",
                    True,
                    "middle",
                    ".1em",
                )
            )
        g.append("</g>")
        cards.append("".join(g))

        tracker_label, issues = tracker_issues(c.get("tracker", ""))
        detail[cid] = {
            "id": cid,
            "kind": kind,
            "region": c.get("region") or c.get("container") or "",
            "plain": plain,
            "does": c["does"],
            "state": state if kind != "actor" else "actor",
            "state_label": state_label if kind != "actor" else "outside",
            "tracker": tracker_label,
            "issues": [{"n": n, "url": ISSUE_URL.format(n=n)} for n in issues],
            "lives": lives_in(c.get("implemented_by", [])),
            "moved": moved,
            "rules": [n for n in rules if n in INVARIANTS],
            "edges": [i for i, (a, b, _art, _k) in enumerate(FLOWS) if cid in (a, b)],
        }

    edges_meta = []
    for i, (src, dst, artifact, kind) in enumerate(FLOWS):
        layer = layers_of[i]
        edges_meta.append(
            {
                "from": src,
                "to": dst,
                "art": artifact,
                "kind": kind,
                "layer": layer,
                "out": verb_for((src, dst), layer, True),
                "in": verb_for((src, dst), layer, False),
                "say": RELATIONS.get((src, dst), ""),
            }
        )
    journeys_meta = [
        {
            "id": j["id"],
            "label": j["label"],
            "steps": [
                {
                    "acts": s["acts"],
                    "measures": s["measures"],
                    "edge": edge_index.get(tuple(s["edge"]), -1),
                    "say": s["say"],
                }
                for s in j["steps"]
            ],
        }
        for j in JOURNEYS
    ]

    meta = {
        "_meta": {
            "layers": [
                {**layer, "colour": LAYER_COLOUR[layer["id"]]} for layer in LAYERS
            ],
            "edges": edges_meta,
            "journeys": journeys_meta,
            "regions": [
                {
                    "id": r["id"],
                    "label": r["label"],
                    "box": [float(v) for v in r["box"]],
                    "ids": [c["id"] for c in COMPONENTS if c.get("region") == r["id"]],
                }
                for r in REGIONS
            ],
            "rules": [
                {
                    "n": n,
                    "text": INVARIANTS[n],
                    "ids": sorted(k for k, v in GOVERNED_BY.items() if n in v),
                }
                for n in sorted(INVARIANTS)
            ],
            "kinds": FLOW_KINDS,
            "states": {s: sum(1 for v in states.values() if v == s) for s in states.values()},
            "collisions": collisions,
            "notes": notes,
            "labels": label_boxes,
            "cards": {cid: list(box) for cid, box in boxes.items()},
            "paths": paths,
        }
    }

    pad = 26
    width, height = CANVAS
    p: list[str] = [
        f'<svg id="{svg_id}" class="scene" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{-pad} {-pad} {width + pad * 2} {height + pad * 2}" '
        f'preserveAspectRatio="xMidYMid meet" tabindex="0" role="application" '
        f'aria-label="System map. Fill is build state; every line is labelled with '
        f'what it carries and coloured by its layer.">',
        _defs(svg_id, T),
        _static_style(svg_id, T) if static else _svg_style(svg_id, T),
    ]
    # Everything drawn sits in one group the script pans and zooms with a
    # transform; the viewBox never changes, so every coordinate read back from
    # the figure (card boxes, edge paths) stays in drawing units.
    p.append('<g class="view">')
    p.extend(floor)
    p.append(f'<g data-layer="zones">{"".join(zones)}</g>')
    p.append(f'<g data-layer="flow">{"".join(flow_parts)}</g>')
    p.extend(cards)
    p.append(
        '<g data-layer="flow">' + "".join(label_parts[i] for i in sorted(label_parts)) + "</g>"
    )
    if badges:
        p.append(f'<g data-layer="steps">{"".join(badges)}</g>')
    p.append('<g data-layer="tags"></g>')
    p.append("</g>")
    # Text classes are known only once everything is drawn, so their style
    # block goes in last; the renderer does not care where a style sits.
    p.append(text_css())
    p.append("</svg>")
    return "".join(p), json.dumps({**detail, **meta}, ensure_ascii=False)


def panel_css() -> str:
    """Styles for the focus panel the interactive script writes into.

    Shared by the atlas page and a lesson figure, so a component reads the
    same in both. Class names are prefixed so a host page's own styles are
    never caught by accident.
    """
    t = theme_mod.get()
    return (
        f".atlas-panel{{font-family:{t['font_ui']};font-size:13px;line-height:1.45;"
        f"color:{t['ink_2']};background:{t['surface']};border:1px solid {t['line']};"
        "border-radius:8px;padding:.9rem 1rem 1rem;min-height:3rem}"
        f".atlas-panel:empty::before{{content:'Click a component to read it.';"
        f"color:{t['ink_3']}}}"
        f".atlas-f__plain{{font-size:19px;font-weight:600;color:{t['ink']};"
        "letter-spacing:-.01em;line-height:1.2;margin:0}"
        f".atlas-f__code{{font-family:{t['font_mono']};font-size:12px;color:{t['accent']};"
        "margin:.3rem 0 0;display:flex;flex-wrap:wrap;gap:.2rem .7rem;align-items:baseline}"
        f".atlas-f__kind{{color:{t['ink_3']};font-size:11px;letter-spacing:.06em;"
        "text-transform:uppercase}"
        f".atlas-f__does{{margin:.6rem 0 .2rem;font-size:13px;color:{t['ink_2']}}}"
        ".atlas-f__wheel{margin:.4rem 0 0}"
        ".atlas-f__wheel svg{width:100%;height:auto;display:block;margin:0 auto}"
        f".atlas-f__say{{margin:.2rem 0 .6rem;padding:.55rem .7rem;font-size:13px;"
        f"line-height:1.45;color:{t['ink']};border-left:3px solid {t['accent']};"
        f"background:{t['raised']};border-radius:0 6px 6px 0;min-height:2.6rem}}"
        f".atlas-f__say.muted{{color:{t['ink_3']};border-left-color:{t['line_2']}}}"
        ".atlas-f__chips{display:flex;flex-wrap:wrap;gap:.35rem;margin:.5rem 0 0}"
        f".atlas-chip{{display:inline-flex;align-items:center;gap:.35em;min-height:24px;"
        f"padding:0 .55em;border-radius:4px;font-family:{t['font_mono']};font-size:11px;"
        f"letter-spacing:.04em;background:{t['raised']};color:{t['ink_3']};"
        f"border:1px solid {t['line']}}}"
        f".atlas-chip--built{{color:{t['good']};border-color:transparent}}"
        f".atlas-chip--partial{{color:{t['warn']};border-color:transparent}}"
        f".atlas-chip--planned{{color:{t['ink_3']};background:none;"
        f"border:1px dashed {t['line_2']}}}"
        f".atlas-chip--actor{{color:{t['ink_3']};background:none;"
        f"border:1px dashed {t['line_2']}}}"
        f".atlas-chip a{{color:{t['accent']};text-decoration:none}}"
        ".atlas-chip a:hover{text-decoration:underline}"
        f".atlas-chip--rule{{color:{t['violet']};cursor:help;min-width:24px;"
        "justify-content:center}"
        f".atlas-f__lives{{margin:.6rem 0 0;font-family:{t['font_mono']};font-size:11px;"
        f"color:{t['ink_3']}}}"
        f".atlas-f__lives b{{font-weight:400;color:{t['ink_3']}}}"
        # The wheel
        f".atlas-w__spoke{{cursor:pointer;outline:none}}"
        ".atlas-w__hit{stroke:transparent;stroke-width:26;fill:none;pointer-events:stroke}"
        ".atlas-w__line{fill:none;stroke-width:1.5;transition:stroke-width .12s ease}"
        f".atlas-w__verb{{font-family:{t['font_ui']};font-size:11px;font-weight:500;"
        f"text-anchor:middle;paint-order:stroke;stroke:{t['surface']};stroke-width:5;"
        "stroke-linejoin:round;pointer-events:none}"
        f".atlas-w__name{{font-family:{t['font_mono']};font-size:11px;font-weight:600;"
        f"fill:{t['ink']};pointer-events:none}}"
        f".atlas-w__centre rect{{fill:{t['raised']};stroke:{t['accent']};stroke-width:1.6}}"
        f".atlas-w__centre text{{font-family:{t['font_mono']};font-size:12px;font-weight:600;"
        f"fill:{t['ink']};text-anchor:middle}}"
        ".atlas-w__spoke.peek .atlas-w__line,.atlas-w__spoke:hover .atlas-w__line,"
        ".atlas-w__spoke:focus-visible .atlas-w__line{stroke-width:3.2}"
        f".atlas-w__spoke.peek .atlas-w__name,.atlas-w__spoke:hover .atlas-w__name,"
        f".atlas-w__spoke:focus-visible .atlas-w__name{{fill:{t['accent']}}}"
        f".atlas-w__empty{{font-family:{t['font_ui']};font-size:12px;fill:{t['ink_3']};"
        "text-anchor:middle}"
    )


def interactive_script(svg_id: str, panel_id: str, detail_json: str) -> str:
    """The one script that makes a figure operable. Plain DOM, no libraries.

    Clicking a component (or pressing Enter on it) dims everything but the
    component and its neighbours, thickens each connected edge in its
    layer's colour, tags each neighbour with the verb that relates it, and
    draws the relationship wheel in the panel. It also owns the viewport:
    wheel and pinch zoom about the pointer, drag pans, a selection or a
    journey step frames its neighbourhood, a double-click on a region label
    frames the region, and `svg.atlas.view` exposes fit, 100%, step and
    back. The same script serves the atlas page and a lesson figure, so the
    two cannot behave differently. The page adds layer and journey controls
    on top through `svg.atlas`.

    The detail JSON is inlined; `</` is broken up so no artifact label can
    close the script early.
    """
    # The layout audit (label boxes, card boxes) is for checkers, not the
    # page; it is dropped from the inlined copy to keep a figure small.
    parsed = json.loads(detail_json)
    meta = dict(parsed.get("_meta") or {})
    meta.pop("labels", None)
    meta.pop("cards", None)
    meta.pop("paths", None)
    parsed["_meta"] = meta
    data = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    t = theme_mod.get()
    palette = json.dumps(
        {
            "accent": t["accent"],
            "steel": t["steel"],
            "surface": t["surface"],
            "raised": t["raised"],
            "bg": t["bg"],
            "ink": t["ink"],
        }
    )
    return (
        "<script>(function(){\n"
        f"var DETAIL = {data};\n"
        f"var PAL = {palette};\n"
        f"var svg = document.getElementById({json.dumps(svg_id)});\n"
        f"var panel = document.getElementById({json.dumps(panel_id)});\n"
        + _INTERACTIVE_JS
        + "})();</script>"
    )


_INTERACTIVE_JS = r"""
if(!svg){ return; }
var META = DETAIL._meta || {};
var LAYERS = META.layers || [];
var EDGES = META.edges || [];
var RULES = {};
(META.rules || []).forEach(function(r){ RULES[r.n] = r.text; });
var LCOL = {}, LORD = {};
LAYERS.forEach(function(l, i){ LCOL[l.id] = l.colour; LORD[l.id] = i; });
var EDGE_AT = {};
EDGES.forEach(function(e, i){ EDGE_AT[e.from + '>' + e.to] = i; });
var NS = 'http://www.w3.org/2000/svg';
function esc(s){ return String(s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function mix(a, b, t){
  function rgb(h){
    h = h.replace('#', '');
    return [0, 2, 4].map(function(i){ return parseInt(h.substr(i, 2), 16); });
  }
  var x = rgb(a), y = rgb(b);
  return '#' + x.map(function(v, i){
    var m = Math.round(v + (y[i] - v) * t);
    return ('0' + m.toString(16)).slice(-2);
  }).join('');
}
var nodes = Array.prototype.slice.call(svg.querySelectorAll('.node'));
var flows = Array.prototype.slice.call(svg.querySelectorAll('.flow'));
var labels = Array.prototype.slice.call(svg.querySelectorAll('.flowlbl'));
var nodeOf = {}, labelOf = {}, flowOf = {};
nodes.forEach(function(n){ nodeOf[n.dataset.id] = n; });
labels.forEach(function(l){ labelOf[l.dataset.edge] = l; });
flows.forEach(function(p){ flowOf[p.dataset.edge] = p; });
var tags = svg.querySelector('[data-layer="tags"]');
var state = {focus:'', layer:'all', journey:null, endstate:false, peek:-1};

function setCls(el, map){
  for(var k in map){ if(map.hasOwnProperty(k)){ el.classList.toggle(k, !!map[k]); } }
}
function boxOf(n){
  var r = n.querySelector('.node__box');
  return {x:+r.getAttribute('x'), y:+r.getAttribute('y'),
    w:+r.getAttribute('width'), h:+r.getAttribute('height')};
}
function el(name, attrs, text){
  var e = document.createElementNS(NS, name);
  for(var k in attrs){ if(attrs.hasOwnProperty(k)){ e.setAttribute(k, attrs[k]); } }
  if(text !== undefined){ e.textContent = text; }
  return e;
}

// ---- the viewport ---------------------------------------------------------
// The drawing sits in <g class="view" transform="translate(tx ty) scale(k)">.
// (tx, ty, k) are in viewBox units, so a drawing point p lands at k*p + t in
// the viewBox and every box or path read from the figure stays in drawing
// units. `base` is the CSS pixels the browser gives one viewBox unit; the
// zoom the reader sees is base*k, and Fit (the whole map across the column)
// is k = 1, t = 0. Wheel and pinch zoom about the pointer, a drag pans, a
// selection or a journey step frames its neighbourhood, and Escape (in the
// page around this script) returns to the view before the framing began.
var view = svg.querySelector('.view');
var VB = svg.viewBox.baseVal;
var ZMIN = 0.4, ZMAX = 2.5, ZCAP = 1.4, FRAME_PAD = 28, ANIM_MS = 320;
var cur = {k:1, tx:0, ty:0};   // what is drawn now, mid-animation included
var goal = {k:1, tx:0, ty:0};  // where the view is heading
var saved = null;              // the view before the current framing chain
var framed = false;            // the view on screen is one a frame() set
var anim = 0, booted = false, dragged = false;
function base(){ var m = svg.getScreenCTM(); return m && m.a ? m.a : 1; }
function reduced(){
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}
function isFit(v){
  v = v || goal;
  return Math.abs(v.k - 1) < 1e-3 && Math.abs(v.tx) < 0.5 && Math.abs(v.ty) < 0.5;
}
function centre(){ return {x:VB.x + VB.width / 2, y:VB.y + VB.height / 2}; }
function toVb(clientX, clientY){
  // A client point in viewBox units.
  var m = svg.getScreenCTM();
  if(!m){ return centre(); }
  var q = new DOMPoint(clientX, clientY).matrixTransform(m.inverse());
  return {x:q.x, y:q.y};
}
function clampView(v){
  // The zoom stays between ZMIN and ZMAX (Fit is always allowed, even on a
  // column so narrow that Fit is under ZMIN), and a fifth of the viewport
  // always holds drawing, so the map cannot be dragged out of sight.
  var b = base(), lo = Math.min(ZMIN, b) / b, hi = ZMAX / b;
  var k = Math.min(hi, Math.max(lo, v.k));
  var mx = VB.width * 0.2, my = VB.height * 0.2;
  var tx = Math.min(VB.x + VB.width - mx - k * VB.x,
    Math.max(VB.x + mx - k * (VB.x + VB.width), v.tx));
  var ty = Math.min(VB.y + VB.height - my - k * VB.y,
    Math.max(VB.y + my - k * (VB.y + VB.height), v.ty));
  return {k:k, tx:tx, ty:ty};
}
function apply(v){
  cur = v;
  if(view){
    view.setAttribute('transform', 'translate(' + v.tx.toFixed(2) + ' ' + v.ty.toFixed(2)
      + ') scale(' + v.k.toFixed(4) + ')');
  }
  svg.dispatchEvent(new CustomEvent('atlas:view',
    {detail:{zoom:base() * v.k, fit:isFit(v)}, bubbles:true}));
}
function setView(v, instant){
  goal = clampView(v);
  if(anim){ cancelAnimationFrame(anim); anim = 0; }
  if(instant || !booted || reduced()){ apply(goal); return; }
  var from = cur, to = goal, t0 = 0;
  function tick(now){
    if(!t0){ t0 = now; }
    var u = Math.min(1, (now - t0) / ANIM_MS);
    var e = u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2;
    apply({k:from.k + (to.k - from.k) * e, tx:from.tx + (to.tx - from.tx) * e,
      ty:from.ty + (to.ty - from.ty) * e});
    anim = u < 1 ? requestAnimationFrame(tick) : 0;
  }
  anim = requestAnimationFrame(tick);
}
function userView(v, instant){
  // The reader moved the view: it is theirs now, and there is nothing to
  // go back to until the next framing.
  framed = false; saved = null;
  setView(v, instant);
}
function zoomAt(f, cx, cy, instant){
  // Zoom by f about the viewBox point (cx, cy): the drawing point under it
  // stays under it, even when the zoom is clamped.
  var k = clampView({k:goal.k * f, tx:goal.tx, ty:goal.ty}).k, g = k / goal.k;
  userView({k:k, tx:cx - g * (cx - goal.tx), ty:cy - g * (cy - goal.ty)}, instant);
}
function frameRect(r, inset, instant){
  // Fit the drawing rect r into the viewport minus inset {left, right, top,
  // bottom} CSS pixels, at the largest zoom that shows all of it, capped at
  // ZCAP. The first framing in a chain remembers the view it left.
  inset = inset || {};
  var b = base();
  var il = (inset.left || 0) / b, ir = (inset.right || 0) / b;
  var it = (inset.top || 0) / b, ib = (inset.bottom || 0) / b;
  var aw = Math.max(40, VB.width - il - ir), ah = Math.max(40, VB.height - it - ib);
  var k = Math.min(ZCAP / b, aw / r.w, ah / r.h);
  var cx = VB.x + il + aw / 2, cy = VB.y + it + ah / 2;
  if(!framed){ saved = goal; framed = true; }
  setView({k:k, tx:cx - k * (r.x + r.w / 2), ty:cy - k * (r.y + r.h / 2)}, instant);
}
function unionBox(ids, edgeIdx){
  // The rect around some cards and the edges between them, in drawing units.
  var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  function add(x, y, w, h){
    x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h);
  }
  ids.forEach(function(id){
    if(nodeOf[id]){ var b = boxOf(nodeOf[id]); add(b.x, b.y, b.w, b.h); } });
  (edgeIdx || []).forEach(function(i){
    var p = flowOf[i];
    if(!p || !p.getBBox){ return; }
    var bb = p.getBBox();
    if(bb.width || bb.height){ add(bb.x, bb.y, bb.width, bb.height); }
  });
  if(x0 === Infinity){ return null; }
  return {x:x0 - FRAME_PAD, y:y0 - FRAME_PAD, w:x1 - x0 + 2 * FRAME_PAD, h:y1 - y0 + 2 * FRAME_PAD};
}
function frameFocus(inset, instant){
  // The selected component, every neighbour, and the edges between them.
  var f = state.focus;
  if(!f || !DETAIL[f]){ return; }
  var ids = [f], edges = DETAIL[f].edges || [];
  edges.forEach(function(i){ ids.push(EDGES[i].from); ids.push(EDGES[i].to); });
  var r = unionBox(ids, edges);
  if(r){ frameRect(r, inset, instant); }
}
function frameJourney(step, instant){
  // The step's acting and measuring components and the edge it traces.
  var ids = (step.acts || []).concat(step.measures || []), edges = [];
  if(step.edge >= 0 && EDGES[step.edge]){
    ids.push(EDGES[step.edge].from); ids.push(EDGES[step.edge].to); edges.push(step.edge);
  }
  var r = unionBox(ids, edges);
  if(r){ frameRect(r, null, instant); }
}
function frameRegion(id){
  var box = null;
  (META.regions || []).forEach(function(z){ if(z.id === id && z.box){ box = z.box; } });
  if(!box){ return; }
  frameRect({x:box[0] - 12, y:box[1] - 12, w:box[2] + 24, h:box[3] + 24}, null);
}
function back(){
  // The view before the framing chain began; nothing if the reader has
  // moved the view since.
  if(!saved){ return; }
  var v = saved;
  saved = null; framed = false;
  setView(v);
}
function fracOf(id){
  // Where the card's centre sits across the viewport, 0 to 1, once the view
  // arrives. The page docks its drawer on the other side.
  var n = nodeOf[id];
  if(!n){ return 0.5; }
  var b = boxOf(n);
  return (goal.k * (b.x + b.w / 2) + goal.tx - VB.x) / VB.width;
}

// Wheel (and trackpad pinch, which arrives as ctrl+wheel) zooms about the
// pointer. A drag pans; under 4px of movement it is a click and the cards
// keep it. Two touches pinch.
svg.addEventListener('wheel', function(e){
  e.preventDefault();
  var d = e.deltaY;
  if(e.deltaMode === 1){ d *= 16; } else if(e.deltaMode === 2){ d *= 400; }
  var f = Math.max(0.5, Math.min(2, Math.exp(-d * (e.ctrlKey ? 0.01 : 0.0022))));
  var c = toVb(e.clientX, e.clientY);
  zoomAt(f, c.x, c.y, true);
}, {passive:false});
var ptrs = {}, drag = null, pinch = null;
function ptrList(){ return Object.keys(ptrs).map(function(k){ return ptrs[k]; }); }
function dist(a, b){ return Math.hypot(a.x - b.x, a.y - b.y); }
function startDrag(p){ drag = {x:p.x, y:p.y, tx:goal.tx, ty:goal.ty, moved:false}; }
svg.addEventListener('pointerdown', function(e){
  if(e.pointerType === 'mouse' && e.button !== 0){ return; }
  dragged = false;
  ptrs[e.pointerId] = {x:e.clientX, y:e.clientY};
  var list = ptrList();
  if(list.length === 1){ pinch = null; startDrag(list[0]); }
  else if(list.length === 2){
    drag = null;
    var m = toVb((list[0].x + list[1].x) / 2, (list[0].y + list[1].y) / 2);
    pinch = {d:dist(list[0], list[1]), k:goal.k,
      px:(m.x - goal.tx) / goal.k, py:(m.y - goal.ty) / goal.k};
  }
});
svg.addEventListener('pointermove', function(e){
  if(!ptrs[e.pointerId]){ return; }
  ptrs[e.pointerId] = {x:e.clientX, y:e.clientY};
  var list = ptrList();
  if(pinch && list.length >= 2){
    var m = toVb((list[0].x + list[1].x) / 2, (list[0].y + list[1].y) / 2);
    var k = clampView({k:pinch.k * dist(list[0], list[1]) / pinch.d, tx:0, ty:0}).k;
    dragged = true;
    userView({k:k, tx:m.x - k * pinch.px, ty:m.y - k * pinch.py}, true);
  } else if(drag){
    var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if(!drag.moved){
      if(Math.hypot(dx, dy) < 4){ return; }
      drag.moved = true;
      svg.classList.add('panning');
      if(svg.setPointerCapture){ try { svg.setPointerCapture(e.pointerId); } catch(_x){} }
    }
    var b = base();
    userView({k:goal.k, tx:drag.tx + dx / b, ty:drag.ty + dy / b}, true);
  }
});
function endPointer(e){
  if(!ptrs[e.pointerId]){ return; }
  delete ptrs[e.pointerId];
  if(drag && drag.moved){ dragged = true; }
  drag = null; pinch = null;
  svg.classList.remove('panning');
  var list = ptrList();
  if(list.length === 1){ startDrag(list[0]); drag.moved = true; }
}
svg.addEventListener('pointerup', endPointer);
svg.addEventListener('pointercancel', endPointer);
// A click that ends a drag is not a click: nothing under the pointer hears it.
svg.addEventListener('click', function(e){
  if(dragged){ dragged = false; e.preventDefault(); e.stopImmediatePropagation(); }
}, true);
Array.prototype.slice.call(svg.querySelectorAll('[data-zone]')).forEach(function(z){
  z.addEventListener('dblclick', function(e){ e.preventDefault(); frameRegion(z.dataset.zone); });
});

function paint(){
  var f = state.focus, j = state.journey, L = state.layer;
  var near = {}, hot = {};
  if(f && DETAIL[f]){
    near[f] = true;
    (DETAIL[f].edges || []).forEach(function(i){
      var e = EDGES[i]; hot[i] = true; near[e.from] = true; near[e.to] = true; });
  }
  var jset = {}, traced = -1;
  if(j){
    (j.acts || []).concat(j.measures || []).forEach(function(id){ jset[id] = true; });
    traced = j.edge;
    if(traced >= 0){ jset[EDGES[traced].from] = true; jset[EDGES[traced].to] = true; }
  }
  var inLayer = {};
  EDGES.forEach(function(e){
    if(L === 'all' || e.layer === L){ inLayer[e.from] = true; inLayer[e.to] = true; } });
  flows.forEach(function(p){
    var i = +p.dataset.edge, e = EDGES[i];
    var on = L === 'all' || e.layer === L;
    var lit = !!hot[i] || i === traced;
    var vis = on || lit;
    var dim = vis && !lit && (!!f || !!j);
    var m = {off:!vis, hot:lit, dim:dim, peek:(i === state.peek)};
    setCls(p, m);
    if(labelOf[i]){ setCls(labelOf[i], m); }
  });
  nodes.forEach(function(n){
    var id = n.dataset.id;
    var dim = f ? !near[id] : (j ? !jset[id] : false);
    var quiet = !f && !j && L !== 'all' && !inLayer[id];
    setCls(n, {sel:(id === f), dim:dim, quiet:quiet,
      acts:!!(j && (j.acts || []).indexOf(id) >= 0),
      meas:!!(j && (j.measures || []).indexOf(id) >= 0), tagged:false});
  });
  svg.classList.toggle('endstate', state.endstate);
  svg.classList.toggle('focused', !!f);
  drawTags();
}

function verbsFrom(cid, other){
  // The verbs on the edges between cid and other, read from cid, with the
  // layer colour of the first.
  var out = [], colour = '';
  (DETAIL[cid].edges || []).forEach(function(i){
    var e = EDGES[i];
    if(e.from === cid && e.to === other){ out.push(e.out); }
    else if(e.to === cid && e.from === other){ out.push(e['in']); }
    else { return; }
    colour = colour || LCOL[e.layer];
  });
  return {verbs:out, colour:colour || PAL.ink};
}

function drawTags(){
  if(!tags){ return; }
  while(tags.firstChild){ tags.removeChild(tags.firstChild); }
  var f = state.focus;
  if(!f || !DETAIL[f]){ return; }
  var seen = {};
  (DETAIL[f].edges || []).forEach(function(i){
    var e = EDGES[i];
    var other = e.from === f ? e.to : e.from;
    if(other === f || seen[other] || !nodeOf[other]){ return; }
    seen[other] = true;
    var v = verbsFrom(f, other);
    var text = v.verbs.join(' / ');
    var lines = text.length > 25 ? v.verbs : [text];
    var b = boxOf(nodeOf[other]);
    var th = lines.length > 1 ? 29 : 17;
    var g = el('g', {'class':'vtag', 'data-id':other});
    g.appendChild(el('rect', {x:b.x + 5, y:b.y + b.h - th - 3, width:b.w - 10, height:th, rx:3,
      fill:mix(PAL.raised, v.colour, 0.16), stroke:v.colour}));
    lines.forEach(function(line, k){
      var y = b.y + b.h - 8 - (lines.length - 1 - k) * 12;
      g.appendChild(el('text', {x:b.x + b.w / 2, y:y, fill:v.colour}, line));
    });
    tags.appendChild(g);
    nodeOf[other].classList.add('tagged');
  });
}

// ---- the relationship wheel --------------------------------------------
function wrapName(id){
  var parts = id.match(/[A-Z]+[a-z0-9]*|[a-z0-9]+/g) || [id];
  var lines = [], cur = '';
  parts.forEach(function(p){
    if(cur && (cur + p).length > 10){ lines.push(cur); cur = p; } else { cur += p; }
  });
  if(cur){ lines.push(cur); }
  return lines.slice(0, 3);
}
function wheelLayout(cid){
  // Spokes grouped by layer in LAYERS order, clockwise from the top, with a
  // half-slot of daylight between groups. Mirrored in check_layout.py.
  var d = DETAIL[cid];
  var idx = (d.edges || []).slice();
  idx.sort(function(a, b){ return (LORD[EDGES[a].layer] - LORD[EDGES[b].layer]) || (a - b); });
  var groups = 0, prev = null;
  idx.forEach(function(i){ if(EDGES[i].layer !== prev){ groups++; prev = EDGES[i].layer; } });
  var gap = groups > 1 ? 0.5 : 0;
  var step = 360 / (idx.length + gap * groups);
  var W = 400, H = 400, cx = 200, cy = 200, R = 118;
  var hw = Math.max(34, cid.length * 3.7 + 12), hh = 15;
  var spokes = [], a = -90; prev = null;
  idx.forEach(function(i){
    var e = EDGES[i];
    if(prev !== null && e.layer !== prev){ a += gap * step; }
    prev = e.layer;
    var th = a * Math.PI / 180; a += step;
    var ux = Math.cos(th), uy = Math.sin(th);
    var r0 = Math.min(hw / Math.max(Math.abs(ux), 1e-6), hh / Math.max(Math.abs(uy), 1e-6)) + 8;
    var other = e.from === cid ? e.to : e.from;
    var deg = th * 180 / Math.PI;
    spokes.push({i:i, e:e, other:other, out:(e.from === cid), ux:ux, uy:uy, r0:r0,
      deg:deg, verb:(e.from === cid ? e.out : e['in']), colour:LCOL[e.layer],
      lines:wrapName(other)});
  });
  return {W:W, H:H, cx:cx, cy:cy, R:R, hw:hw, hh:hh, spokes:spokes};
}
function wheelExtent(w){
  // The box the wheel actually occupies: the centre, plus every name label,
  // estimated at 6.6px per glyph. The viewBox is fitted to it so a component
  // with one spoke does not sit in a square of dead space.
  var x0 = w.cx - w.hw, y0 = w.cy - w.hh, x1 = w.cx + w.hw, y1 = w.cy + w.hh;
  w.spokes.forEach(function(s){
    var ex = w.cx + (w.R + 9) * s.ux, ey = w.cy + (w.R + 9) * s.uy;
    var n = s.lines.length, lw = 0;
    s.lines.forEach(function(l){ lw = Math.max(lw, l.length * 6.6); });
    var left, top;
    if(Math.abs(s.ux) < 0.35){
      left = ex - lw / 2; top = (s.uy < 0 ? ey - 4 - (n - 1) * 13 : ey + 12) - 10;
    } else {
      left = s.ux > 0 ? ex + 2 : ex - 2 - lw; top = ey + 4 - (n - 1) * 6.5 - 10;
    }
    x0 = Math.min(x0, left); y0 = Math.min(y0, top);
    x1 = Math.max(x1, left + lw); y1 = Math.max(y1, top + n * 13);
  });
  var pad = 8;
  if(!w.spokes.length){ x0 = w.cx - 100; x1 = w.cx + 100; y0 = w.cy - 44; }
  return {x:x0 - pad, y:y0 - pad, w:x1 - x0 + 2 * pad, h:y1 - y0 + 2 * pad};
}
function wheelSvg(cid){
  var w = wheelLayout(cid);
  var box = wheelExtent(w);
  var h = '<svg viewBox="' + box.x.toFixed(1) + ' ' + box.y.toFixed(1) + ' ' + box.w.toFixed(1)
        + ' ' + box.h.toFixed(1) + '" style="max-width:' + box.w.toFixed(0) + 'px" role="img" '
        + 'aria-label="relationship wheel of ' + esc(cid) + '">';
  h += '<defs>';
  LAYERS.forEach(function(l){
    h += '<marker id="wm-' + esc(l.id) + '" viewBox="0 0 8 8" refX="7" refY="4" '
       + 'markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" '
       + 'orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" fill="' + l.colour
       + '"/></marker>';
  });
  h += '</defs>';
  if(!w.spokes.length){
    h += '<text class="atlas-w__empty" x="' + w.cx + '" y="' + (w.cy - 30) + '">'
       + 'no flow touches this yet</text>';
  }
  w.spokes.forEach(function(s){
    var x0 = w.cx + s.r0 * s.ux, y0 = w.cy + s.r0 * s.uy;
    var x1 = w.cx + w.R * s.ux, y1 = w.cy + w.R * s.uy;
    var rm = (s.r0 + w.R) / 2, mx = w.cx + rm * s.ux, my = w.cy + rm * s.uy;
    var rot = s.ux < 0 ? s.deg + 180 : s.deg;
    var marker = (s.out ? ' marker-end' : ' marker-start') + '="url(#wm-' + esc(s.e.layer) + ')"';
    var ends = ' x1="' + x0.toFixed(1) + '" y1="' + y0.toFixed(1) + '" x2="' + x1.toFixed(1)
             + '" y2="' + y1.toFixed(1) + '"';
    h += '<g class="atlas-w__spoke" data-edge="' + s.i + '" data-go="' + esc(s.other)
       + '" tabindex="0" role="button" aria-label="'
       + esc(cid + ' ' + s.verb + ' ' + s.other) + '">';
    h += '<line class="atlas-w__hit"' + ends + '/>';
    h += '<line class="atlas-w__line"' + ends + ' stroke="' + s.colour + '"' + marker + '/>';
    h += '<text class="atlas-w__verb" x="' + mx.toFixed(1) + '" y="' + (my + 4).toFixed(1)
       + '" fill="' + s.colour + '" transform="rotate(' + rot.toFixed(1) + ' ' + mx.toFixed(1)
       + ' ' + my.toFixed(1) + ')">' + esc(s.verb) + '</text>';
    var ex = w.cx + (w.R + 9) * s.ux, ey = w.cy + (w.R + 9) * s.uy;
    var n = s.lines.length, anchor, lx, first;
    if(Math.abs(s.ux) < 0.35){
      anchor = 'middle'; lx = ex;
      first = s.uy < 0 ? ey - 4 - (n - 1) * 13 : ey + 12;
    } else {
      anchor = s.ux > 0 ? 'start' : 'end'; lx = ex + (s.ux > 0 ? 2 : -2);
      first = ey + 4 - (n - 1) * 6.5;
    }
    h += '<text class="atlas-w__name" text-anchor="' + anchor + '">';
    s.lines.forEach(function(line, k){
      h += '<tspan x="' + lx.toFixed(1) + '" y="' + (first + k * 13).toFixed(1) + '">'
         + esc(line) + '</tspan>';
    });
    h += '</text></g>';
  });
  h += '<g class="atlas-w__centre"><rect x="' + (w.cx - w.hw) + '" y="' + (w.cy - w.hh)
     + '" width="' + (2 * w.hw) + '" height="' + (2 * w.hh) + '" rx="5"/>';
  h += '<text x="' + w.cx + '" y="' + (w.cy + 4) + '">' + esc(cid) + '</text></g>';
  h += '</svg>';
  return h;
}

// ---- the panel -----------------------------------------------------------
var SAY_HINT = 'Hover or tap a spoke, or a neighbour on the map, to read what the relationship is.';
function describe(d){
  var h = '<div class="atlas-f">';
  h += '<h3 class="atlas-f__plain">' + esc(d.plain || d.id) + '</h3>';
  h += '<div class="atlas-f__code">' + esc(d.id) + '<span class="atlas-f__kind">' + esc(d.kind)
     + (d.region ? ' in ' + esc(d.region) : '') + '</span></div>';
  h += '<p class="atlas-f__does">' + esc(d.does) + '</p>';
  h += '<div class="atlas-f__wheel">' + wheelSvg(d.id) + '</div>';
  var say = (d.edges && d.edges.length) ? SAY_HINT : 'Nothing flows to or from this yet.';
  h += '<p class="atlas-f__say muted" data-say>' + esc(say) + '</p>';
  h += '<div class="atlas-f__chips">';
  h += '<span class="atlas-chip atlas-chip--' + esc(d.state) + '">' + esc(d.state_label);
  if(d.tracker){ h += ' <span>' + esc(d.tracker) + '</span>'; }
  (d.issues || []).forEach(function(x){
    h += ' <a href="' + esc(x.url) + '" target="_blank" rel="noopener">#' + esc(x.n) + '</a>'; });
  h += '</span>';
  (d.rules || []).forEach(function(n){
    h += '<span class="atlas-chip atlas-chip--rule" title="' + esc(RULES[n] || '')
       + '" tabindex="0">' + n + '</span>';
  });
  h += '</div>';
  if(d.lives){ h += '<p class="atlas-f__lives">lives in <b>' + esc(d.lives) + '</b></p>'; }
  h += '</div>';
  return h;
}
function peek(i, sticky){
  state.peek = i;
  var e = EDGES[i];
  if(panel){
    var say = panel.querySelector('[data-say]');
    if(say && e){
      say.textContent = e.say || (e.from + ' -> ' + e.to + ': ' + e.art);
      say.classList.remove('muted');
    }
    Array.prototype.slice.call(panel.querySelectorAll('.atlas-w__spoke')).forEach(function(s){
      s.classList.toggle('peek', +s.dataset.edge === i); });
  }
  flows.forEach(function(p){ p.classList.toggle('peek', +p.dataset.edge === i); });
  if(labelOf[i]){ labelOf[i].classList.add('peek'); }
}
function unpeek(){
  if(state.peek < 0){ return; }
  state.peek = -1;
  flows.forEach(function(p){ p.classList.remove('peek'); });
  labels.forEach(function(l){ l.classList.remove('peek'); });
  if(panel){
    Array.prototype.slice.call(panel.querySelectorAll('.atlas-w__spoke.peek')).forEach(
      function(s){ s.classList.remove('peek'); });
  }
}
function edgeBetween(a, b){
  var i = EDGE_AT[a + '>' + b];
  if(i === undefined){ i = EDGE_AT[b + '>' + a]; }
  return i === undefined ? -1 : i;
}
function select(cid){
  var d = DETAIL[cid];
  if(!d){ return; }
  state.focus = cid; state.journey = null; state.peek = -1;
  paint();
  frameFocus(null, !booted);
  if(panel){
    panel.innerHTML = describe(d);
    panel.classList.add('on');
    Array.prototype.slice.call(panel.querySelectorAll('.atlas-w__spoke')).forEach(function(s){
      var i = +s.dataset.edge;
      s.addEventListener('mouseenter', function(){ peek(i); });
      s.addEventListener('focus', function(){ peek(i); });
      // A hover peeks first, so a mouse click navigates at once; on touch
      // the first tap peeks (the sentence appears) and the second navigates.
      s.addEventListener('click', function(ev){
        ev.preventDefault();
        if(state.peek === i){ select(s.dataset.go); } else { peek(i); }
      });
      s.addEventListener('keydown', function(ev){
        if(ev.key === 'Enter' || ev.key === ' '){ ev.preventDefault(); select(s.dataset.go); }
      });
    });
  }
  svg.dispatchEvent(new CustomEvent('atlas:select', {detail:{id:cid}, bubbles:true}));
}
function clearAll(){
  state.focus = ''; state.journey = null; state.peek = -1;
  paint();
  if(panel){ panel.innerHTML = ''; panel.classList.remove('on'); }
  svg.dispatchEvent(new CustomEvent('atlas:clear', {bubbles:true}));
}
nodes.forEach(function(n){
  n.addEventListener('click', function(e){ e.stopPropagation(); select(n.dataset.id); });
  n.addEventListener('keydown', function(e){
    if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); select(n.dataset.id); }
  });
  n.addEventListener('mouseenter', function(){
    if(!state.focus || n.dataset.id === state.focus){ return; }
    var i = edgeBetween(state.focus, n.dataset.id);
    if(i >= 0){ peek(i); }
  });
  n.addEventListener('mouseleave', function(){ if(state.focus){ unpeek(); } });
});
svg.addEventListener('click', function(e){ if(!e.target.closest('.node')){ clearAll(); } });
svg.atlas = {
  select: select,
  clear: clearAll,
  peek: peek,
  state: state,
  setLayer: function(id){ state.layer = id; paint(); },
  setEndstate: function(on){ state.endstate = !!on; paint(); },
  setJourney: function(step){
    // step: {acts:[], measures:[], edge:index, say:''} or null.
    state.focus = '';
    state.journey = step;
    paint();
    if(step){ frameJourney(step, !booted); }
    if(panel && step){ panel.innerHTML = ''; panel.classList.remove('on'); }
  },
  view: {
    fit: function(){ userView({k:1, tx:0, ty:0}); },
    actual: function(){ var c = centre(); zoomAt(1 / (base() * goal.k), c.x, c.y); },
    zoomBy: function(f){ var c = centre(); zoomAt(f, c.x, c.y); },
    zoom: function(){ return base() * goal.k; },
    isFit: function(){ return isFit(); },
    frameFocus: frameFocus,
    frameRegion: frameRegion,
    fracOf: fracOf,
    back: back
  },
  edges: EDGES,
  layers: LAYERS,
  journeys: META.journeys || [],
  detail: DETAIL
};
svg.atlasSelect = select;
svg.atlasClear = clearAll;
function openHash(){
  var id = decodeURIComponent((location.hash || '').slice(1));
  if(id && DETAIL[id] && id !== state.focus){ select(id); }
}
window.addEventListener('hashchange', openHash);
openHash();
booted = true;
"""
