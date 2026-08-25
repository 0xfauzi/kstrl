"""Draw the logical view as a flat figure. One generator for every picture.

A stranger needs five things, and each one owns exactly one visual channel so
that no channel says two things at once:

    what exists ....... a card, inside the boundary it belongs to
    how finished ...... the card's FILL, derived from entry points found
    how much code ..... the bar along its bottom edge, square-root scaled
    what it does ...... a line inside the card
    how work travels .. lines between cards, each labelled with what it carries

Planned components are drawn as ghosts (dashed, muted) in their own region, so
"today" and "end state" are the same drawing with one CSS class flipped on the
root (`endstate`) rather than two drawings that could disagree. Every node
carries `data-id` and a class for its build state (`built`, `partial`,
`planned`), and every edge carries its artifact as visible text.

Edge labels are placed by a collision pass, not by hand: each label tries a
run of positions along its own curve and takes the first that touches no card
and no other label. What could not be placed cleanly is reported in the detail
JSON under `_meta.collisions`, so a crowded layout fails loudly instead of
quietly drawing text over text.

Positions come from logical_model, so the same system always draws the same
figure and a moved card means the architecture moved.

Ported from the deckgen repository's atlas tooling.
"""

from __future__ import annotations

import html
import json
import math
from typing import Any

import theme as theme_mod
from logical_model import (
    CALL_BUDGET,
    CANVAS,
    COMPONENTS,
    CONTAINERS,
    FLOW_KINDS,
    FLOWS,
    GOVERNED_BY,
    INVARIANTS,
    REGIONS,
    SPEC_ANCHOR,
    build_state,
)

CARD_W = 150.0
CARD_H = 56.0
STORE_H = 46.0
ACTOR_H = 38.0
RADIUS = 4.0
# Both bars share a left edge and a scale, so the eye compares them directly.
BAR_INSET = 11.0
BAR_H = 3.0
# Edge labels: never below 11px. Width is estimated from the glyph count, so
# the collision pass can run without a renderer.
LABEL_PX = 11.0
LABEL_CHAR_W = 6.1
LABEL_H = 13.0
LABEL_GAP = 2.0
# Where along its curve a label may sit, in order of preference, and how far
# it may step off the line. Mid-curve first, then outwards.
LABEL_T = (0.5, *(v for k in range(1, 25) for v in (0.5 - k * 0.02, 0.5 + k * 0.02)))
# 38 clears a component card beside a short edge (28 half-height, 3 margin,
# half a label); 51 is the second lane in a 36-unit row gutter.
LABEL_OFFSETS = (0.0, 12.0, -12.0, 24.0, -24.0, 38.0, -38.0, 51.0, -51.0)

Box = tuple[float, float, float, float]


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def wrap_label(text: str, width: int = 17) -> list[str]:
    """Split a CamelCase component id across at most two lines."""
    out: list[str] = []
    current = ""
    for ch in text:
        breakable = ch.isupper() and current and len(current) >= 3
        if breakable and len(current) + 1 > width:
            out.append(current)
            current = ch
            continue
        current += ch
    if current:
        out.append(current)
    merged: list[str] = []
    for part in out:
        if merged and len(merged[-1]) + len(part) <= width:
            merged[-1] += part
        else:
            merged.append(part)
    return merged[:2] if merged else [text]


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


def _counts(component: dict[str, Any], atlas: dict[str, Any]) -> dict[str, int]:
    """Physical weight behind a logical component, summed from the atlas."""
    comps = atlas.get("components", {})
    got = [comps[m] for m in component.get("implemented_by", []) if m in comps]
    return {
        "modules": len(got),
        "loc": sum(c["loc"] for c in got),
        "operations": sum(len(c["functions"]) for c in got),
        "types": sum(len(c["classes"]) for c in got),
        "refusals": sum(len(c["errors"]) for c in got),
        "tests": sum(c.get("tests_total", 0) for c in got),
    }


def legend_rows(mode: str, theme: str | None = None) -> list[tuple[str, str, str]]:
    """(fill, stroke, label) for the legend the given mode needs."""
    t = theme_mod.get(theme)
    if mode == "change":
        d = t["delta"]
        return [
            (mix(t["bg"], t["change"], 0.14), t["change"], "changed here"),
            (mix(t["bg"], t["guards"], 0.12), t["guards"], "reached by it"),
            (t["ghost"][0], t["ghost"][1], "untouched"),
            (d["operations"], d["operations"], "new operations"),
            (d["types"], d["types"], "new types"),
            (d["refusals"], d["refusals"], "new refusals"),
            (d["tests"], d["tests"], "new tests"),
        ]
    return [(f, s, label) for f, s, label in t["state"].values()]


_SVG_STYLE = (
    "<style>"
    ".node{transition:opacity .18s ease;cursor:pointer}"
    ".node.dim{opacity:.18}"
    ".node.sel .node__box{stroke-width:2.6}"
    # Planned components are ghosts until the page asks for the end state.
    ".node.planned{opacity:.5}"
    ".endstate .node.planned{opacity:1}"
    ".endstate .node.planned.dim{opacity:.18}"
    ".flow{transition:opacity .18s ease}"
    ".flow.planned{opacity:.3}"
    ".endstate .flow.planned{opacity:1}"
    ".flow.dim{opacity:.06}"
    ".flow.hot{stroke-opacity:1;stroke-width:2}"
    ".flowlbl{transition:opacity .15s ease;pointer-events:none}"
    ".flowlbl.planned{opacity:.45}"
    ".endstate .flowlbl.planned{opacity:1}"
    ".flowlbl.dim{opacity:.1}"
    ".flowlbl.hot{opacity:1}"
    "</style>"
)


def _defs(t: dict[str, Any]) -> str:
    """Arrowheads, one per line colour, built per theme so a light page never
    inherits a dark head."""
    heads = {"a": t["flow"], "al": t["change"], "ac": t["calls"]}
    out = ["<defs>"]
    for name, colour in heads.items():
        out.append(
            f'<marker id="{name}" viewBox="0 0 8 8" refX="7" refY="4" '
            f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
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
    theme: str | None = None,
) -> tuple[str, str]:
    """(svg, json detail).

    `changed` marks logical components a change moved (or a plan reaches).
    `changed_modules` marks physical modules it touched. `hot_artifacts` names
    the flow labels whose owning module redefined part of its surface; the
    change detector is the one place that computes it, so this only draws
    what it is told.

    The detail JSON carries one record per component (what the interactive
    panel shows) plus a `_meta` key: the rules, the regions, the scale, and
    the edge labels the collision pass could not place cleanly.
    """
    changed = changed or set()
    changed_modules = changed_modules or set()
    adjacent = adjacent or set()
    gained = gained or {}
    hot_artifacts = hot_artifacts or set()
    change_mode = mode == "change"

    T = theme_mod.get(theme)
    INK, INK_2, INK_3 = T["ink"], T["ink_2"], T["ink_3"]
    HALO = T["bg"]
    GHOST_FILL, GHOST_STROKE = T["ghost"]

    # Text styling is deduplicated into classes: 240 text elements each
    # carrying a full font stack tripled the size of the figure for no
    # information, and a font stack with quoted names inside a quoted
    # attribute is not even well-formed markup. A lesson embeds this whole.
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
    kinds = {c["id"]: c["kind"] for c in COMPONENTS}
    counts = {c["id"]: _counts(c, atlas) for c in COMPONENTS}
    max_loc = max((v["loc"] for v in counts.values()), default=0) or 1
    max_tests = max((v["tests"] for v in counts.values()), default=0) or 1

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
        # The sub-line wraps to the box, so a narrow edge box can carry a
        # full sentence without spilling past its own border.
        sub_lines = wrap_words(box["sub"], max(12, int((w - 26) / 5.4)), 2)
        floor.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>'
            + L(x + 13, y + 19, box["label"], 11, stroke, "600", True, "start", ".13em")
            + "".join(
                L(x + 13, y + 33 + 12 * k, line, 10.5, INK_3, "400", False, "start")
                for k, line in enumerate(sub_lines)
            )
        )
        obstacles.append((f"{box['id']} header", (x + 8, y + 6, w - 16, 30 + 12 * len(sub_lines))))

    zones: list[str] = []
    for i, region in enumerate(REGIONS, start=1):
        x, y, w, h = region["box"]
        zones.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="none" '
            f'stroke="{T["region"]}" stroke-opacity=".3" stroke-width="1" '
            f'stroke-dasharray="3 4"/>'
            f'<circle cx="{x + 17:.0f}" cy="{y + 16:.0f}" r="8.5" fill="{HALO}" '
            f'stroke="{T["region"]}" stroke-opacity=".5"/>'
            + L(x + 17, y + 20, str(i), 10.5, T["region"], "600", True)
            + L(
                x + 31,
                y + 20,
                region["label"],
                11,
                T["region"],
                "600",
                True,
                "start",
                ".13em",
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
    # Geometry first, for every flow, in model order (which is drawing order).
    geometry: dict[int, tuple[tuple[float, float], ...]] = {}
    for i, (src, dst, artifact, kind) in enumerate(FLOWS):
        if src not in boxes or dst not in boxes:
            continue
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
        colour, marker = T["flow"], "a"
        if art_hot:
            colour, marker = T["calls"], "ac"
        ghost = states[src] == "planned" or states[dst] == "planned"
        classes = f"flow {kind}" + (" planned" if ghost else "")
        dash = ' stroke-dasharray="4 3"' if ghost else ""
        fid = f"{svg_id}-f{i}"
        flow_parts.append(
            f'<path id="{fid}" class="{classes}" data-from="{esc(src)}" '
            f'data-to="{esc(dst)}" data-art="{esc(artifact)}" '
            f'data-kind="{esc(kind)}" d="{path}" fill="none" stroke="{colour}" '
            f'stroke-opacity="{0.9 if art_hot else 0.55}" '
            f'stroke-width="{1.8 if art_hot else 1}"{dash} '
            f'marker-end="url(#{marker})"/>'
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
            # Step off the line across its local direction, not always
            # vertically, so a vertical edge is cleared sideways.
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
        # Two labels that touch read as one line of text; keep daylight.
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
            # Blocked only by labels already seated: try re-seating each
            # blocker somewhere it is still clean, so both fit. A card in the
            # way cannot be moved by this pass and stays reported.
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
        colour = T["calls"] if art_hot else T["flow"]
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
            f'<g class="flowlbl {kind}{" planned" if ghost else ""}" '
            f'data-from="{esc(src)}" data-to="{esc(dst)}">'
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
            fill, stroke = mix(T["bg"], T["line"], 0.5), INK_3
        moved, near = cid in changed, cid in adjacent
        tier = "moved" if moved else ("near" if near else "far")
        if change_mode and tier == "far":
            fill, stroke = GHOST_FILL, GHOST_STROKE
        if change_mode and moved:
            stroke = T["change"]
        elif change_mode and near:
            stroke = T["guards"]
        x, y, w, h = boxes[cid]
        rules = GOVERNED_BY.get(cid, [])
        state_class = state if kind != "actor" else "actor"

        g: list[str] = [
            f'<g class="node {state_class}'
            f'{f" node--{tier}" if change_mode else ""}" '
            f'data-id="{esc(cid)}" data-kind="{kind}" data-state="{state}" '
            f'data-region="{esc(c.get("region") or c.get("container") or "")}" '
            f'role="button" '
            f'tabindex="0" aria-label="{esc(cid)}, {esc(kind)}, '
            f'{esc(state_label)}, {counts[cid]["loc"]} lines">'
        ]
        dashes = ' stroke-dasharray="4 3"' if state == "planned" or kind == "actor" else ""
        g.append(
            f'<rect class="node__box" x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="{RADIUS}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{1.6 if change_mode and (moved or near) else 1.1}"'
            f"{dashes}/>"
        )
        # A store is the same card with a rule under its head: flat convention
        # for a thing that holds rows rather than does work.
        if kind == "store":
            g.append(
                f'<line x1="{x + 1}" y1="{y + 15}" x2="{x + w - 1}" y2="{y + 15}" '
                f'stroke="{stroke}" stroke-opacity=".45" stroke-width="1"/>'
            )

        lines = wrap_label(cid)
        name_y = y + (20 if len(lines) == 1 else 16)
        for j, line in enumerate(lines):
            g.append(L(x + w / 2, name_y + j * 12, line, 12.5, INK, "600"))

        # How many lines of description fit is derived from the card, never
        # assumed: a store is ten units shorter than a component.
        reserved = 4.0 if kind == "actor" else 11.0
        room = (y + h - reserved) - (name_y + (len(lines) - 1) * 12 + 3)
        job_lines = wrap_words(c.get("does", ""), 30, max(0, int(room // 10)))
        if job_lines:
            top = name_y + len(lines) * 12
            g.append(
                '<g data-layer="job">'
                + "".join(
                    L(x + w / 2, top + k * 10, line, 9, INK_2, "400")
                    for k, line in enumerate(job_lines)
                )
                + "</g>"
            )

        # Two rules on the card's bottom edge, sharing a left edge and a
        # scale so the eye compares them: how much code, and how much test.
        bar_w = w - BAR_INSET * 2
        loc = counts[cid]["loc"]
        if loc and kind != "actor":
            g.append(
                f'<rect x="{x + BAR_INSET}" y="{y + h - 4}" width="{bar_w}" '
                f'height="{BAR_H}" rx="1.5" fill="{stroke}" fill-opacity=".16"/>'
                f'<rect x="{x + BAR_INSET}" y="{y + h - 4}" '
                f'width="{bar_w * math.sqrt(loc / max_loc):.1f}" height="{BAR_H}" '
                f'rx="1.5" fill="{stroke}"/>'
            )
        tests = counts[cid]["tests"]
        if tests and kind != "actor":
            g.append(
                f'<g data-layer="guards"><rect x="{x + BAR_INSET}" '
                f'y="{y + h - 8}" width="{bar_w * math.sqrt(tests / max_tests):.1f}" '
                f'height="{BAR_H}" rx="1.5" fill="{T["guards"]}"/></g>'
            )
        if cid in CALL_BUDGET:
            g.append(
                f'<g data-layer="calls"><circle cx="{x + w - 10}" cy="{y + 10}" '
                f'r="3.4" fill="{T["calls"]}"/></g>'
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
                f'<circle cx="{x + 12}" cy="{y - 1}" r="9" fill="{T["change"]}"/>'
                + L(x + 12, y + 2.5, f"+{sum(delta.values())}", 9.5, HALO, "600", True)
            )
        elif change_mode and moved:
            g.append(
                L(
                    x + w / 2,
                    y - 5,
                    "IN REACH" if not changed_modules else "CHANGED INSIDE",
                    8.5,
                    T["change"],
                    "600",
                    True,
                    "middle",
                    ".1em",
                )
            )
        g.append("</g>")
        cards.append("".join(g))

        detail[cid] = {
            "id": cid,
            "kind": kind,
            "region": c.get("region") or c.get("container") or "",
            "does": c["does"],
            "interface": c["interface"],
            "state": state,
            "state_label": state_label if kind != "actor" else "actor",
            "tracker": c.get("tracker", ""),
            "note": c.get("note", ""),
            "modules": c.get("implemented_by", []),
            "entry": c.get("entry", ""),
            "moved": moved,
            "calls": CALL_BUDGET.get(cid, ""),
            "spec": SPEC_ANCHOR.get(cid, ""),
            "counts": counts[cid],
            # Numbers only; the text lives once, under _meta.rules.
            "rules": [n for n in rules if n in INVARIANTS],
            "changed_modules": sorted(set(c.get("implemented_by", [])) & changed_modules),
            "inputs": [
                {"from": a, "artifact": art, "kind": k} for a, b, art, k in FLOWS if b == cid
            ],
            "outputs": [
                {"to": b, "artifact": art, "kind": k} for a, b, art, k in FLOWS if a == cid
            ],
        }

    meta = {
        "_meta": {
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
            "kinds_of_node": {k: sum(1 for v in kinds.values() if v == k) for k in kinds.values()},
            "scale": {
                "max_loc": max_loc,
                "max_tests": max_tests,
                "total_loc": sum(v["loc"] for v in counts.values()),
            },
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
        f'aria-label="System map. Fill is build state, the bar under each name '
        f'is code volume, every line is labelled with what it carries.">',
        _defs(T),
        _SVG_STYLE,
    ]
    p.extend(floor)
    p.append(f'<g data-layer="zones">{"".join(zones)}</g>')
    p.append(f'<g data-layer="flow">{"".join(flow_parts)}</g>')
    p.extend(cards)
    p.append(
        '<g data-layer="flow">' + "".join(label_parts[i] for i in sorted(label_parts)) + "</g>"
    )
    # Text classes are known only once everything is drawn, so their style
    # block goes in last; the renderer does not care where a style sits.
    p.append(text_css())
    p.append("</svg>")
    return "".join(p), json.dumps({**detail, **meta}, ensure_ascii=False)


def panel_css(theme: str | None = None) -> str:
    """Styles for the detail panel the interactive script writes into.

    Shared by the atlas page and a lesson figure, so a component reads the
    same in both. Class names are prefixed so a host page's own styles are
    never caught by accident.
    """
    t = theme_mod.get(theme)
    good = t["state"]["built"][1]
    part = t["state"]["partial"][1]
    plan = t["state"]["planned"][1]
    return (
        f".atlas-panel{{font-family:{t['font_ui']};font-size:13px;line-height:1.5;"
        f"color:{t['ink_2']};background:{t['surface']};border:1px solid {t['line']};"
        "border-radius:7px;padding:.9rem 1rem;min-height:3rem}"
        f".atlas-panel:empty::before{{content:'Click a component to read it.';"
        f"color:{t['ink_3']}}}"
        ".atlas-d__eyebrow{font-size:10.5px;letter-spacing:.08em;text-transform:"
        f"uppercase;color:{t['ink_3']};font-family:{t['font_mono']}}}"
        f".atlas-d__name{{font-size:18px;font-weight:600;color:{t['ink']};"
        "margin:.2rem 0 .4rem;letter-spacing:-.01em}"
        ".atlas-d__does{margin:0 0 .6rem;font-size:13.5px}"
        f".atlas-d__code{{font-family:{t['font_mono']};font-size:11.5px;"
        f"background:{t['bg']};border:1px solid {t['line']};border-radius:5px;"
        f"padding:.45rem .6rem;margin:0 0 .6rem;white-space:pre-wrap;color:{t['accent']}}}"
        ".atlas-d__row{margin:0 0 .4rem;font-size:12.5px}"
        f".atlas-d__row b{{color:{t['ink']};font-weight:600}}"
        f".atlas-d__row--warn{{border-left:3px solid {t['change']};padding-left:.5rem}}"
        f".atlas-d__row code,.atlas-io code{{font-family:{t['font_mono']};"
        f"font-size:11.5px;color:{t['accent']}}}"
        f".atlas-d__h{{font-family:{t['font_mono']};font-size:10px;letter-spacing:.1em;"
        f"text-transform:uppercase;color:{t['ink_3']};margin:.9rem 0 .3rem}}"
        ".atlas-io,.atlas-rules{list-style:none;margin:0;padding:0;font-size:12.5px}"
        f".atlas-io li,.atlas-rules li{{padding:.22rem 0;border-bottom:1px solid {t['line']}}}"
        f".atlas-io__k{{font-family:{t['font_mono']};font-size:9.5px;letter-spacing:"
        f".05em;text-transform:uppercase;color:{t['ink_3']};display:inline-block;"
        "min-width:2.2em}"
        f".atlas-rules__n{{font-family:{t['font_mono']};font-size:10px;"
        f"color:{t['calls']};display:inline-block;min-width:1.6em}}"
        f".atlas-go{{color:{t['ink']};text-decoration:none;"
        f"border-bottom:1px dashed {t['line_2']}}}"
        f".atlas-go:hover{{color:{t['accent']};border-bottom-color:{t['accent']}}}"
        ".atlas-state{font-weight:600}"
        f".atlas-state--built{{color:{good}}}"
        f".atlas-state--partial{{color:{part}}}"
        f".atlas-state--planned{{color:{plan}}}"
    )


def interactive_script(svg_id: str, panel_id: str, detail_json: str) -> str:
    """The one script that makes a figure operable. Plain DOM, no libraries.

    Clicking a component (or pressing Enter on it) writes its detail into the
    panel and lights its in and out edges; clicking the background clears.
    The same script serves the atlas page and a lesson figure, so the two
    cannot behave differently. The detail JSON is inlined; `</` is broken up
    so no artifact label can close the script early.
    """
    # The layout audit (label boxes, card boxes) is for checkers, not the
    # page; it is dropped from the inlined copy to keep a figure small.
    parsed = json.loads(detail_json)
    meta = dict(parsed.get("_meta") or {})
    meta.pop("labels", None)
    meta.pop("cards", None)
    parsed["_meta"] = meta
    data = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (
        "<script>(function(){\n"
        f"var DETAIL = {data};\n"
        f"var svg = document.getElementById({json.dumps(svg_id)});\n"
        f"var panel = document.getElementById({json.dumps(panel_id)});\n"
        + _INTERACTIVE_JS
        + "})();</script>"
    )


_INTERACTIVE_JS = r"""
if(!svg){ return; }
function esc(s){ return String(s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
var nodes = Array.prototype.slice.call(svg.querySelectorAll('.node'));
var flows = Array.prototype.slice.call(svg.querySelectorAll('.flow'));
var labels = Array.prototype.slice.call(svg.querySelectorAll('.flowlbl'));
var selected = '';
function lightUp(cid){
  var near = {}; near[cid] = true;
  flows.forEach(function(f){
    var hot = f.dataset.from === cid || f.dataset.to === cid;
    f.classList.toggle('hot', hot); f.classList.toggle('dim', !hot);
    if(hot){ near[f.dataset.from] = true; near[f.dataset.to] = true; }
  });
  labels.forEach(function(l){
    var hot = l.dataset.from === cid || l.dataset.to === cid;
    l.classList.toggle('hot', hot); l.classList.toggle('dim', !hot);
  });
  nodes.forEach(function(n){
    n.classList.toggle('sel', n.dataset.id === cid);
    n.classList.toggle('dim', !near[n.dataset.id]);
  });
}
function clearAll(){
  selected = '';
  nodes.forEach(function(n){ n.classList.remove('sel', 'dim'); });
  flows.forEach(function(f){ f.classList.remove('hot', 'dim'); });
  labels.forEach(function(l){ l.classList.remove('hot', 'dim'); });
  if(panel){ panel.innerHTML = ''; panel.classList.remove('on'); }
}
function link(id){
  return '<a href="#" class="atlas-go" data-go="' + esc(id) + '">' + esc(id) + '</a>';
}
function describe(d){
  var h = '<div class="atlas-d">';
  h += '<div class="atlas-d__eyebrow">' + esc(d.kind) + ' &middot; ' + esc(d.region)
     + ' &middot; <span class="atlas-state atlas-state--' + esc(d.state) + '">'
     + esc(d.state_label) + '</span>'
     + (d.tracker ? ' &middot; ' + esc(d.tracker) : '') + '</div>';
  h += '<div class="atlas-d__name">' + esc(d.id) + '</div>';
  h += '<p class="atlas-d__does">' + esc(d.does) + '</p>';
  if(d.interface){ h += '<pre class="atlas-d__code">' + esc(d.interface) + '</pre>'; }
  if(d.calls){ h += '<p class="atlas-d__row"><b>Model calls.</b> ' + esc(d.calls) + '</p>'; }
  if(d.note){
    h += '<p class="atlas-d__row atlas-d__row--warn"><b>Caveat.</b> ' + esc(d.note) + '</p>';
  }
  if(d.modules && d.modules.length){
    h += '<p class="atlas-d__row"><b>Implemented by.</b> ' + d.modules.map(esc).join(', ')
       + (d.entry ? ' &middot; entry <code>' + esc(d.entry) + '</code>' : '') + '</p>';
  }
  var c = d.counts || {};
  if(c.modules){
    h += '<p class="atlas-d__row"><b>Weight.</b> ' + c.loc + ' lines, ' + c.operations
       + ' operations, ' + c.types + ' types, ' + c.refusals + ' refusals, '
       + c.tests + ' tests import it</p>';
  }
  if(d.spec){ h += '<p class="atlas-d__row"><b>Defined in.</b> ' + esc(d.spec) + '</p>'; }
  if((d.inputs && d.inputs.length) || (d.outputs && d.outputs.length)){
    h += '<div class="atlas-d__h">Flows</div><ul class="atlas-io">';
    (d.inputs || []).forEach(function(i){
      h += '<li><span class="atlas-io__k">in</span> <code>' + esc(i.artifact)
         + '</code> from ' + link(i.from) + '</li>';
    });
    (d.outputs || []).forEach(function(o){
      h += '<li><span class="atlas-io__k">out</span> <code>' + esc(o.artifact)
         + '</code> to ' + link(o.to) + '</li>';
    });
    h += '</ul>';
  }
  if(d.rules && d.rules.length){
    var RULES = {};
    ((DETAIL._meta || {}).rules || []).forEach(function(r){ RULES[r.n] = r.text; });
    h += '<div class="atlas-d__h">Invariants it serves</div><ul class="atlas-rules">';
    d.rules.forEach(function(n){
      h += '<li><span class="atlas-rules__n">' + n + '</span> ' + esc(RULES[n] || '') + '</li>';
    });
    h += '</ul>';
  }
  h += '</div>';
  return h;
}
function select(cid){
  var d = DETAIL[cid];
  if(!d){ return; }
  selected = cid;
  lightUp(cid);
  if(panel){
    panel.innerHTML = describe(d);
    panel.classList.add('on');
    Array.prototype.slice.call(panel.querySelectorAll('.atlas-go')).forEach(function(a){
      a.addEventListener('click', function(e){ e.preventDefault(); select(a.dataset.go); });
    });
  }
  svg.dispatchEvent(new CustomEvent('atlas:select', {detail: {id: cid}, bubbles: true}));
}
nodes.forEach(function(n){
  n.addEventListener('click', function(e){ e.stopPropagation(); select(n.dataset.id); });
  n.addEventListener('keydown', function(e){
    if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); select(n.dataset.id); }
  });
});
svg.addEventListener('click', function(e){ if(!e.target.closest('.node')){ clearAll(); } });
svg.atlasSelect = select;
svg.atlasClear = clearAll;
function openHash(){
  var id = decodeURIComponent((location.hash || '').slice(1));
  if(id && DETAIL[id] && id !== selected){ select(id); }
}
window.addEventListener('hashchange', openHash);
openHash();
"""
