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

Edge labels are placed by a collision pass, not by hand: each label tries a
run of positions along its own curve and takes the first that touches no card
and no other label. What could not be placed cleanly is reported in the detail
JSON under `_meta.collisions`, so a crowded layout fails loudly instead of
quietly drawing text over text.

Positions come from logical_model, so the same system always draws the same
figure and a moved card means the architecture moved. Meaning (layers, the
sentence per edge, the verb per spoke, the plain words) comes from relations.

No text in the figure is set below 11px.
"""

from __future__ import annotations

import html
import json
import math
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
# Where along its curve a label may sit, in order of preference, and how far
# it may step off the line. Mid-curve first, then outwards.
LABEL_T = (0.5, *(v for k in range(1, 25) for v in (0.5 - k * 0.02, 0.5 + k * 0.02)))
# 38 clears a component card beside a short edge (28 half-height, 3 margin,
# half a label); 51 is the second lane in a 36-unit row gutter.
LABEL_OFFSETS = (0.0, 12.0, -12.0, 24.0, -24.0, 38.0, -38.0, 51.0, -51.0)

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


def _edges(a: Box, b: Box) -> tuple[float, float, float, float]:
    """Where a line leaves a and enters b: the nearer sides of each."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    acx, acy = ax + aw / 2, ay + ah / 2
    bcx, bcy = bx + bw / 2, by + bh / 2
    if abs(bcx - acx) > abs(bcy - acy) * 1.15:
        if bcx > acx:
            return ax + aw, acy, bx, bcy
        return ax, acy, bx + bw, bcy
    if bcy > acy:
        return acx, ay + ah, bcx, by
    return acx, ay, bcx, by + bh


def _bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    u = 1 - t
    x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
    return x, y


def _overlap_area(a: Box, b: Box) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return w * h if w > 0 and h > 0 else 0.0


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
        "@keyframes atlasflow{to{stroke-dashoffset:-14}}"
        "@media (prefers-reduced-motion:reduce){"
        f"{s} .flow.hot{{animation:none;stroke-dasharray:none}}"
        f"{s} .node,{s} .flow,{s} .flowlbl{{transition:none}}}}"
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


def render(
    atlas: dict[str, Any],
    changed: set[str] | None = None,
    changed_modules: set[str] | None = None,
    adjacent: set[str] | None = None,
    mode: str = "system",
    svg_id: str = "schematic",
    gained: dict[str, dict[str, int]] | None = None,
    hot_artifacts: set[str] | None = None,
) -> tuple[str, str]:
    """(svg, json detail).

    `changed` marks logical components a change moved (or a plan reaches).
    `changed_modules` marks physical modules it touched. `hot_artifacts` names
    the flow labels whose owning module redefined part of its surface; the
    change detector is the one place that computes it, so this only draws
    what it is told.

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
    INK, INK_2, INK_3 = T["ink"], T["ink_2"], T["ink_3"]
    HALO = T["bg"]
    GHOST_FILL, GHOST_STROKE = T["ghost"]
    LAYER_COLOUR: dict[str, str] = T["layers"]

    # Text styling is deduplicated into classes: 240 text elements each
    # carrying a full font stack tripled the size of the figure for no
    # information. A lesson embeds this whole.
    text_styles: dict[tuple[float, str, str, bool, str, str, bool], str] = {}

    def L(
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
        assert size >= TEXT_PX, f"text below {TEXT_PX}px: {text!r} at {size}"
        key = (size, colour, weight, mono, anchor, spacing, halo)
        cls = text_styles.setdefault(key, f"t{len(text_styles)}")
        return f'<text x="{px:.1f}" y="{py:.1f}" class="{cls}">{esc(text)}</text>'

    def text_css() -> str:
        rules: list[str] = []
        for (size, colour, weight, mono, anchor, spacing, halo), cls in text_styles.items():
            family = T["font_mono"] if mono else T["font_ui"]
            rule = (
                f"font-family:{family};font-size:{size}px;font-weight:{weight};"
                f"fill:{colour};text-anchor:{anchor}"
            )
            if spacing:
                rule += f";letter-spacing:{spacing}"
            if halo:
                rule += f";paint-order:stroke;stroke:{HALO};stroke-width:4;stroke-linejoin:round"
            rules.append(f"#{svg_id} .{cls}{{{rule}}}")
        return "<style>" + "".join(rules) + "</style>"

    states = {c["id"]: build_state(c, atlas) for c in COMPONENTS}

    def geom(c: dict[str, Any]) -> Box:
        h = {"store": STORE_H, "actor": ACTOR_H}.get(c["kind"], CARD_H)
        return float(c["x"]), float(c["y"]), CARD_W, h

    boxes = {c["id"]: geom(c) for c in COMPONENTS}

    # ---- ground: boundaries, then the bands -------------------------------
    floor: list[str] = []
    obstacles: list[tuple[str, Box]] = []
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
        obstacles.append((f"{box['id']} header", (x + 8, y + 6, w - 16, 30 + 12 * len(sub_lines))))

    zones: list[str] = []
    for i, region in enumerate(REGIONS, start=1):
        x, y, w, h = region["box"]
        zones.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="none" '
            f'stroke="{T["region"]}" stroke-opacity=".28" stroke-width="1" '
            f'stroke-dasharray="3 4"/>'
            f'<circle cx="{x + 17:.0f}" cy="{y + 16:.0f}" r="8.5" fill="{HALO}" '
            f'stroke="{T["region"]}" stroke-opacity=".5"/>'
            + L(x + 17, y + 20, str(i), TEXT_PX, T["region"], "600", True)
            + L(
                x + 31, y + 20, region["label"], TEXT_PX, T["region"], "600", True, "start", ".13em"
            )
        )
        obstacles.append((f"{region['id']} header", (x + 6, y + 5, 150, 24)))
    for cid, (x, y, w, h) in boxes.items():
        obstacles.append((cid, (x - 3, y - 3, w + 6, h + 6)))

    # ---- flows ------------------------------------------------------------
    reverse = {(a, b) for a, b, _art, _k in FLOWS} & {(b, a) for a, b, _art, _k in FLOWS}
    flow_parts: list[str] = []
    label_parts: dict[int, str] = {}
    collisions: list[str] = []
    label_boxes: list[dict[str, Any]] = []
    width_px, height_px = CANVAS
    layers_of: dict[int, str] = {}
    geometry: dict[int, tuple[tuple[float, float], ...]] = {}
    for i, (src, dst, artifact, kind) in enumerate(FLOWS):
        if src not in boxes or dst not in boxes:
            continue
        layer = layer_for((src, dst), kind)
        layers_of[i] = layer
        sx, sy, ex, ey = _edges(boxes[src], boxes[dst])
        if (src, dst) in reverse:
            # Two flows between the same pair, one each way: shift both a few
            # units off the shared centre line so neither hides the other.
            dxn, dyn = ex - sx, ey - sy
            norm = math.hypot(dxn, dyn) or 1.0
            side = 6.0 if src < dst else -6.0
            px_, py_ = -dyn / norm * side, dxn / norm * side
            sx, sy, ex, ey = sx + px_, sy + py_, ex + px_, ey + py_
        dx = max(26.0, abs(ex - sx) * 0.42)
        dy = max(20.0, abs(ey - sy) * 0.38)
        if abs(ex - sx) > abs(ey - sy):
            c1 = (sx + dx if ex >= sx else sx - dx, sy)
            c2 = (ex - dx if ex >= sx else ex + dx, ey)
        else:
            c1 = (sx, sy + dy if ey >= sy else sy - dy)
            c2 = (ex, ey - dy if ey >= sy else ey + dy)
        path = (
            f"M {sx:.1f} {sy:.1f} C {c1[0]:.1f} {c1[1]:.1f}, "
            f"{c2[0]:.1f} {c2[1]:.1f}, {ex:.1f} {ey:.1f}"
        )
        geometry[i] = ((sx, sy), c1, c2, (ex, ey))
        art_hot = artifact in hot_artifacts
        colour, marker = LAYER_COLOUR[layer], layer
        if art_hot:
            colour, marker = T["change"], "change"
        ghost = states[src] == "planned" or states[dst] == "planned"
        classes = f"flow {kind}" + (" planned" if ghost else "")
        dash = ' stroke-dasharray="4 3"' if ghost else ""
        fid = f"{svg_id}-f{i}"
        flow_parts.append(
            f'<path id="{fid}" class="{classes}" data-edge="{i}" data-from="{esc(src)}" '
            f'data-to="{esc(dst)}" data-art="{esc(artifact)}" '
            f'data-kind="{esc(kind)}" data-layer="{layer}" d="{path}" fill="none" '
            f'stroke="{colour}" stroke-opacity="{0.95 if art_hot else 0.82}" '
            f'stroke-width="{1.8 if art_hot else 1.2}"{dash} '
            f'marker-end="url(#{svg_id}-m-{marker})"/>'
        )

    # Labels second, shortest edge first: a short edge has the fewest places
    # its label can go, so it chooses before a long edge takes them.
    def _length(i: int) -> float:
        (sx, sy), _c1, _c2, (ex, ey) = geometry[i]
        return math.hypot(ex - sx, ey - sy)

    def _candidates(i: int) -> list[tuple[float, Box]]:
        """Every place this label may sit, in preference order, each with its
        overlap against the fixed obstacles (cards and headers)."""
        (sx, sy), c1, c2, (ex, ey) = geometry[i]
        lw = len(FLOWS[i][2]) * LABEL_CHAR_W + 6
        out: list[tuple[float, Box]] = []
        for t in LABEL_T:
            bx, by = _bezier((sx, sy), c1, c2, (ex, ey), t)
            ax_, ay_ = _bezier((sx, sy), c1, c2, (ex, ey), min(1.0, t + 0.02))
            tx, ty = ax_ - bx, ay_ - by
            tn = math.hypot(tx, ty) or 1.0
            nx, ny = -ty / tn, tx / tn
            for off in LABEL_OFFSETS:
                cx, cy = bx + nx * off, by + ny * off
                lbox: Box = (cx - lw / 2, cy - LABEL_H / 2, lw, LABEL_H)
                if lbox[0] < 0 or lbox[1] < 0:
                    continue
                if lbox[0] + lw > width_px or lbox[1] + LABEL_H > height_px:
                    continue
                out.append((sum(_overlap_area(lbox, ob) for _n, ob in obstacles), lbox))
        return out

    placed: dict[int, Box] = {}

    def _pad(b: Box) -> Box:
        g = LABEL_GAP
        return (b[0] - g, b[1] - g, b[2] + 2 * g, b[3] + 2 * g)

    def _best(i: int, cands: list[tuple[float, Box]]) -> tuple[float, Box]:
        best: tuple[float, Box] | None = None
        for fixed, lbox in cands:
            cost = fixed + sum(
                _overlap_area(_pad(lbox), other) for j, other in placed.items() if j != i
            )
            if best is None or cost < best[0]:
                best = (cost, lbox)
            if cost == 0:
                break
        assert best is not None
        return best

    all_cands = {i: _candidates(i) for i in geometry}
    for i in sorted(geometry, key=_length):
        cost, lbox = _best(i, all_cands[i])
        if cost > 0:
            blockers = [j for j, other in placed.items() if _overlap_area(_pad(lbox), other)]
            for j in blockers:
                original = placed.pop(j)
                found = False
                for fixed_j, alt in all_cands[j][:400]:
                    if fixed_j > 0:
                        continue
                    if any(_overlap_area(_pad(alt), o) for k, o in placed.items() if k != i):
                        continue
                    placed[j] = alt
                    cost_i, lbox_i = _best(i, all_cands[i])
                    if cost_i == 0:
                        cost, lbox, found = cost_i, lbox_i, True
                        break
                    placed.pop(j)
                if found:
                    break
                placed[j] = original
        placed[i] = lbox
        if cost > 0:
            named = [(n, ob) for n, ob in obstacles] + [
                (f"label '{FLOWS[j][2]}'", ob) for j, ob in placed.items() if j != i
            ]
            hits = [n for n, ob in named if _overlap_area(lbox, ob)]
            collisions.append(
                f"'{FLOWS[i][2]}' ({FLOWS[i][0]} -> {FLOWS[i][1]}) overlaps {', '.join(hits[:3])}"
            )

    for i, lbox in placed.items():
        src, dst, artifact, kind = FLOWS[i]
        art_hot = artifact in hot_artifacts
        colour = T["change"] if art_hot else LAYER_COLOUR[layers_of[i]]
        ghost = states[src] == "planned" or states[dst] == "planned"
        lw = lbox[2]
        label_boxes.append(
            {
                "from": src,
                "to": dst,
                "artifact": artifact,
                "box": [round(v, 1) for v in lbox],
            }
        )
        lx, ly = lbox[0] + lw / 2, lbox[1] + LABEL_H - 3
        label_parts[i] = (
            f'<g class="flowlbl {kind}{" planned" if ghost else ""}" data-edge="{i}" '
            f'data-from="{esc(src)}" data-to="{esc(dst)}" data-layer="{layers_of[i]}">'
            + L(lx, ly, artifact, LABEL_PX, colour, "500", False, "middle", "", True)
            + "</g>"
        )

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

        g: list[str] = [
            f'<g class="node {state_class}'
            f'{f" node--{tier}" if change_mode else ""}" '
            f'data-id="{esc(cid)}" data-kind="{kind}" data-state="{state}" '
            f'data-region="{esc(c.get("region") or c.get("container") or "")}" '
            f'role="button" '
            f'tabindex="0" aria-label="{esc(cid)}, {esc(plain)}, {esc(state_label)}">'
        ]
        dashes = ' stroke-dasharray="4 3"' if state == "planned" or kind == "actor" else ""
        g.append(
            f'<rect class="node__box" x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="{RADIUS}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{1.6 if change_mode and (moved or near) else 1.1}"'
            f"{dashes}/>"
            f'<rect class="node__ring" x="{x + 3}" y="{y + 3}" width="{w - 6}" '
            f'height="{h - 6}" rx="{RADIUS - 1}"/>'
        )
        name_ink = INK if state != "planned" else INK_2
        g.append(L(x + w / 2, y + 17, cid, NAME_PX, name_ink, "600", True))
        # A store is the same card with a rule under its head: flat convention
        # for a thing that holds rows rather than does work.
        if kind == "store":
            g.append(
                f'<line x1="{x + 1}" y1="{y + 23}" x2="{x + w - 1}" y2="{y + 23}" '
                f'stroke="{stroke}" stroke-opacity=".45" stroke-width="1"/>'
            )
        # The plain word under the code name. How many lines fit is derived
        # from the card, never assumed.
        first = y + (36 if kind == "store" else 32)
        room = int((y + h - 4 - first) // 12) + 1
        plain_lines = wrap_words(plain, PLAIN_CHARS, max(1, room))
        g.append(
            '<g data-layer="job">'
            + "".join(
                L(x + w / 2, first + k * 12, line, TEXT_PX, INK_3, "400")
                for k, line in enumerate(plain_lines)
            )
            + "</g>"
        )

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
    edge_index = {(a, b): i for i, (a, b, _art, _k) in enumerate(FLOWS)}
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
            "labels": label_boxes,
            "cards": {cid: list(box) for cid, box in boxes.items()},
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
        _svg_style(svg_id, T),
    ]
    p.extend(floor)
    p.append(f'<g data-layer="zones">{"".join(zones)}</g>')
    p.append(f'<g data-layer="flow">{"".join(flow_parts)}</g>')
    p.extend(cards)
    p.append(
        '<g data-layer="flow">' + "".join(label_parts[i] for i in sorted(label_parts)) + "</g>"
    )
    p.append('<g data-layer="tags"></g>')
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
    draws the relationship wheel in the panel. The same script serves the
    atlas page and a lesson figure, so the two cannot behave differently.
    The page adds layer and journey controls on top through `svg.atlas`.

    The detail JSON is inlined; `</` is broken up so no artifact label can
    close the script early.
    """
    # The layout audit (label boxes, card boxes) is for checkers, not the
    # page; it is dropped from the inlined copy to keep a figure small.
    parsed = json.loads(detail_json)
    meta = dict(parsed.get("_meta") or {})
    meta.pop("labels", None)
    meta.pop("cards", None)
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
var nodeOf = {}, labelOf = {};
nodes.forEach(function(n){ nodeOf[n.dataset.id] = n; });
labels.forEach(function(l){ labelOf[l.dataset.edge] = l; });
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
    if(panel && step){ panel.innerHTML = ''; panel.classList.remove('on'); }
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
"""
