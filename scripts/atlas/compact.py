"""A compact figure: only the components a layer or a journey touches.

The atlas draws every component at one scale, and a README column shows
that drawing at about a third of its size, where an 11px card name is 4px
of grey. A layer or a journey touches a fraction of the components, so the
figure a document embeds draws only those, laid out afresh, at type that
reads at 900px. Nothing here is drawn by hand: the participants come from
the model, the layout from a rule, the routes from route.py, and the cards,
edges and labels from schematic's own primitives, so a compact figure and
the atlas cannot disagree about what a card or an edge looks like.

What takes part:

    layer ...... every component with at least one edge in the layer
    journey .... every component a step acts with or measures, plus the two
                 ends of every step's edge

The layout. Each region that has a participant is a column of its
participating cards, in the order they appear in logical_model.COMPONENTS,
with the region's label in a strip above the cards and a thin outline
round both. The columns run in the atlas's forward order (intake, plan,
build, decide, measure, ship), then trust, learn, observe, then the outside
actors, each in the thin outline of the container it lives in. When more
columns take part than fit MAX_COLS the forward path keeps the first row
and the rest wrap to a second row beneath it; type is never shrunk to fit.
Corridors above the first row, between rows and below the last carry the
edges that span columns; the gutters between columns carry the rest.

Edges are routed by route.py against the same walls the atlas uses (cards,
the label strips, foreign regions) and labelled by its placer, so `meta`
carries the label boxes, the paths and every compromise, and `problems`
re-verifies all of it from that record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import theme as theme_mod
from logical_model import COMPONENTS, CONTAINERS, FLOWS, REGIONS, build_state
from relations import PLAIN, layer_for
from route import Box, Point, _seg_hits, place_labels, route_all
from schematic import (
    ACTOR_H,
    CARD_H,
    CARD_W,
    STORE_H,
    TEXT_PX,
    TextStyles,
    _defs,
    _static_style,
    badge_svg,
    card_svg,
    flow_svg,
    label_svg,
    mix,
)

# Type. Names are the largest text on the map and the one a reader must
# read at README width; the floor for anything is the atlas's 11px.
NAME_PX = 13.0
LABEL_PX = 12.0
REGION_PX = 12.0
LABEL_CHAR_W = 6.1 * LABEL_PX / 11.0
LABEL_H = 14.0
REGION_CHAR_W = 9.0  # 12px mono with .13em spacing
# The column grid.
COL_PITCH = 190.0
ROW_PITCH = 84.0
SIDE = 14.0
REGION_PAD = 6.0
HEAD = 26.0
# The first region top: the router's horizontal lanes start at 24, so this
# leaves a corridor of five lanes above the first row.
FIRST_TOP = 74.0
CORRIDOR = 50.0
# Seven columns is 1318 units, 1398 in the panel, which puts a 13px name at
# 8.4px in a 900px column. An eighth column would put it under 8.
MAX_COLS = 7
FORWARD = ("intake", "plan", "build", "decide", "measure", "ship")
REST = ("trust", "learn", "observe")
# A step badge sits on its edge this far from the source port, and moves
# along the path by BADGE_STEP when that spot is taken.
BADGE_R = 8.5
BADGE_FROM_PORT = 13.0
BADGE_STEP = 10.0
# The smallest a name may render in a 900px column.
MIN_NAME_AT_900 = 8.0
COLUMN_PX = 900.0


@dataclass(frozen=True)
class Column:
    """One region or container drawn as a column of cards."""

    id: str
    label: str
    kind: str  # region or container
    ids: tuple[str, ...]
    row: int
    x: float
    top: float

    @property
    def height(self) -> float:
        last = _card_h(self.ids[-1])
        return HEAD + (len(self.ids) - 1) * ROW_PITCH + last + REGION_PAD

    @property
    def box(self) -> Box:
        return (self.x - REGION_PAD, self.top, CARD_W + 2 * REGION_PAD, self.height)

    @property
    def header(self) -> Box:
        return (self.x + 2, self.top + 5, len(self.label) * REGION_CHAR_W + 6, 16.0)


_BY_ID = {c["id"]: c for c in COMPONENTS}
_REGION_LABEL = {r["id"]: str(r["label"]) for r in REGIONS}
_CONTAINER = {c["id"]: c for c in CONTAINERS}


def _card_h(cid: str) -> float:
    return {"store": STORE_H, "actor": ACTOR_H}.get(_BY_ID[cid]["kind"], CARD_H)


def _home(cid: str) -> str:
    c = _BY_ID[cid]
    return str(c.get("region") or c.get("container") or "")


def participants(
    layer: str = "", journey: dict[str, Any] | None = None
) -> tuple[list[int], list[str], dict[int, list[int]], set[str], set[str]]:
    """(edge indices into FLOWS, component ids in model order, steps per edge, acts, measures)."""
    edge_index = {(a, b): i for i, (a, b, _art, _k) in enumerate(FLOWS)}
    edges: list[int] = []
    traced: dict[int, list[int]] = {}
    acts: set[str] = set()
    meas: set[str] = set()
    ids: set[str] = set()
    if journey is not None:
        for n, step in enumerate(journey["steps"], start=1):
            i = edge_index[tuple(step["edge"])]
            if i not in traced:
                edges.append(i)
            traced.setdefault(i, []).append(n)
            acts.update(step["acts"])
            meas.update(step["measures"])
        ids |= acts | meas
    else:
        edges = [i for i, (a, b, _art, k) in enumerate(FLOWS) if layer_for((a, b), k) == layer]
    for i in edges:
        ids.update(FLOWS[i][:2])
    ordered = [c["id"] for c in COMPONENTS if c["id"] in ids]
    return edges, ordered, traced, acts, meas


def layout(ids: list[str]) -> tuple[list[Column], tuple[float, float]]:
    """The columns and the canvas they need; deterministic in the ids."""
    groups: dict[str, list[str]] = {}
    for cid in ids:
        groups.setdefault(_home(cid), []).append(cid)
    actor_homes = [
        c["id"] for c in CONTAINERS if c["id"] in groups and c["id"] not in _REGION_LABEL
    ]
    order = [*FORWARD, *REST, *actor_homes]
    keys = [k for k in order if k in groups]
    missing = set(groups) - set(keys)
    if missing:
        raise ValueError(f"no column order for {sorted(missing)}")
    if len(keys) <= MAX_COLS:
        rows = [keys]
    else:
        first = [k for k in keys if k in FORWARD]
        rows = [first, [k for k in keys if k not in FORWARD]]
    columns: list[Column] = []
    top = FIRST_TOP
    for r, row in enumerate(rows):
        for k, key in enumerate(row):
            if key in _REGION_LABEL:
                label, kind = _REGION_LABEL[key], "region"
            else:
                label, kind = str(_CONTAINER[key]["label"]), "container"
            columns.append(
                Column(key, label, kind, tuple(groups[key]), r, SIDE + k * COL_PITCH, top)
            )
        top = max(c.top + c.height for c in columns if c.row == r) + CORRIDOR
    width = 2 * SIDE + max(len(row) for row in rows) * COL_PITCH - (COL_PITCH - CARD_W)
    height = top
    return columns, (width, height)


def _along(points: list[Point], d: float) -> tuple[Point, Point] | None:
    """The point d units along the polyline and the unit direction there.

    None past the end.
    """
    for a, b in zip(points, points[1:], strict=False):
        seg = abs(b[0] - a[0]) + abs(b[1] - a[1])
        if d <= seg:
            t = d / seg if seg else 0.0
            u = ((b[0] - a[0]) / seg, (b[1] - a[1]) / seg) if seg else (1.0, 0.0)
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), u
        d -= seg
    return None


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _pad(box: Box, by: float) -> Box:
    return (box[0] - by, box[1] - by, box[2] + 2 * by, box[3] + 2 * by)


def _badge_seat(points: list[Point], r: float, taken: list[Box]) -> Point:
    """On the edge near its start, off every card, label strip and badge.

    Walks the path from the source port outward. At each distance the spot
    on the line is tried first, then the two just beside it (two edges that
    share a gutter cannot both carry a badge on the line). The first clear
    spot wins. A path with no clear spot keeps its badge at the port, and
    `problems` reports what that touches.
    """
    d = BADGE_FROM_PORT
    while True:
        hit = _along(points, d)
        if hit is None:
            break
        (px, py), (ux, uy) = hit
        for side in (0.0, r + 2.0, -(r + 2.0)):
            p = (px - uy * side, py + ux * side)
            box = (p[0] - r, p[1] - r, 2 * r, 2 * r)
            if not any(_overlaps(box, t) for t in taken):
                return p
        d += BADGE_STEP
    first = _along(points, BADGE_FROM_PORT)
    return first[0] if first else points[0]


def render(
    atlas: dict[str, Any],
    layer: str = "",
    journey: dict[str, Any] | None = None,
    svg_id: str = "map",
) -> tuple[str, dict[str, Any]]:
    """(svg, meta) for one layer or one journey; exactly one must be given."""
    if bool(layer) == (journey is not None):
        raise ValueError("give a layer or a journey, not both, not neither")
    T = theme_mod.get()
    L = TextStyles(svg_id, T)
    states = {c["id"]: build_state(c, atlas) for c in COMPONENTS}
    edge_ids, ids, traced, acts, meas = participants(layer, journey)
    columns, canvas = layout(ids)
    w, h = canvas

    # ---- geometry -----------------------------------------------------------
    cards: dict[str, Box] = {}
    region_of: dict[str, str] = {}
    region_boxes: dict[str, Box] = {}
    blocks: list[Box] = []
    obstacles: list[tuple[str, Box]] = []
    headers: list[tuple[str, Box]] = []
    for col in columns:
        region_boxes[col.id] = col.box
        blocks.append(col.header)
        headers.append((f"{col.id} header", col.header))
        obstacles.append(headers[-1])
        y = col.top + HEAD
        for cid in col.ids:
            cards[cid] = (col.x, y, CARD_W, _card_h(cid))
            region_of[cid] = col.id
            y += ROW_PITCH
    for cid, (x, y, cw, ch) in cards.items():
        obstacles.append((cid, (x - 3, y - 3, cw + 6, ch + 6)))

    # ---- routes and labels ------------------------------------------------
    edges = [(FLOWS[i][0], FLOWS[i][1]) for i in edge_ids]
    routes = route_all(edges, cards, set(), blocks, region_boxes, region_of, canvas)
    widths = {k: len(FLOWS[i][2]) * LABEL_CHAR_W + 6 for k, i in enumerate(edge_ids)}

    # Step badges first: each sits on its own edge near the source port
    # and has one natural spot, so it goes down before the labels, which
    # have many seats and are placed round it. A badge must not touch a
    # card or a label strip (a hair of clearance) or another badge.
    badge_parts: list[str] = []
    badge_boxes: list[Box] = []
    taken = [_pad(box, 1.0) for box in cards.values()] + [_pad(box, 1.0) for box in blocks]
    for k, i in enumerate(edge_ids):
        if i not in traced:
            continue
        n = ",".join(str(s) for s in traced[i])
        r = BADGE_R + 3.2 * (len(n) - 1)
        bx, by = _badge_seat(routes[k].points, r, taken)
        badge_parts.append(badge_svg(L, T, i, n, bx, by))
        box: Box = (bx - r, by - r, 2 * r, 2 * r)
        badge_boxes.append(box)
        taken.append(_pad(box, 2.0))
        obstacles.append((f"badge {len(badge_boxes) - 1}", box))
    seats = place_labels(routes, widths, LABEL_H, obstacles, canvas)

    flow_parts: list[str] = []
    label_parts: list[str] = []
    label_boxes: list[dict[str, Any]] = []
    collisions: list[str] = []
    notes: list[str] = []
    paths: dict[int, list[list[float]]] = {}
    for k, i in enumerate(edge_ids):
        src, dst, artifact, kind = FLOWS[i]
        layer_ = layer_for((src, dst), kind)
        colour = T["layers"][layer_]
        route = routes[k]
        seat = seats[k]
        paths[i] = [[round(x, 1), round(y, 1)] for x, y in route.points]
        if route.fallback:
            notes.append(f"{src} -> {dst}: {route.fallback}")
        if seat.cost > 0:
            collisions.append(f"'{artifact}' ({src} -> {dst}) overlaps {', '.join(seat.hits[:3])}")
        if not seat.on_longest:
            notes.append(f"'{artifact}' ({src} -> {dst}) sits on a shorter segment")
        ghost = states[src] == "planned" or states[dst] == "planned"
        hot = i in traced
        flow_parts.append(
            flow_svg(
                svg_id,
                i,
                src,
                dst,
                artifact,
                kind,
                layer_,
                route.points,
                colour,
                layer_,
                ghost=ghost,
                off=False,
                hot=hot,
            )
        )
        label_parts.append(
            label_svg(
                L,
                i,
                src,
                dst,
                kind,
                layer_,
                artifact,
                seat.box,
                colour,
                ghost=ghost,
                off=False,
                hot=hot,
                label_px=LABEL_PX,
            )
        )
        label_boxes.append(
            {"from": src, "to": dst, "artifact": artifact, "box": [round(v, 1) for v in seat.box]}
        )

    # ---- the ground: outlines and their labels ------------------------------
    floor: list[str] = []
    for col in columns:
        x, y, cw, ch = col.box
        if col.kind == "region":
            floor.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cw:.1f}" height="{ch:.1f}" rx="5" '
                f'fill="none" stroke="{T["region"]}" stroke-opacity=".28" stroke-width="1" '
                f'stroke-dasharray="3 4"/>'
            )
            ink = T["region"]
        else:
            stroke, _fill = T["container"][_CONTAINER[col.id]["tone"]]
            floor.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cw:.1f}" height="{ch:.1f}" rx="6" '
                f'fill="none" stroke="{stroke}" stroke-width="1.2"/>'
            )
            ink = T["ink_3"]
        floor.append(
            L(col.x + 4, col.top + 17, col.label, REGION_PX, ink, "600", True, "start", ".13em")
        )

    # ---- cards --------------------------------------------------------------
    card_parts: list[str] = []
    for cid in ids:
        c = _BY_ID[cid]
        kind, state = c["kind"], states[cid]
        fill, stroke, state_label = T["state"][state]
        if kind == "actor":
            fill, stroke = mix(T["bg"], T["line_2"], 0.35), T["ink_3"]
            state_label = "outside"
        classes = state if kind != "actor" else "actor"
        if journey is not None:
            classes += (" acts" if cid in acts else "") + (" meas" if cid in meas else "")
        card_parts.append(
            card_svg(
                L,
                T,
                cid,
                kind,
                state,
                cards[cid],
                fill,
                stroke,
                classes,
                region_of[cid],
                state_label,
                PLAIN.get(cid, ""),
                name_px=NAME_PX,
                text_px=TEXT_PX,
            )
            + "</g>"
        )

    svg = (
        f'<svg id="{svg_id}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" '
        f'role="img" aria-label="{len(ids)} components and {len(edge_ids)} labelled flows">'
        + _defs(svg_id, T)
        + _static_style(svg_id, T)
        + "".join(floor)
        + f'<g data-layer="flow">{"".join(flow_parts)}</g>'
        + "".join(card_parts)
        + f'<g data-layer="flow">{"".join(label_parts)}</g>'
        + (f'<g data-layer="steps">{"".join(badge_parts)}</g>' if badge_parts else "")
        + L.css()
        + "</svg>"
    )
    meta: dict[str, Any] = {
        "canvas": [w, h],
        "columns": len({(c.row, c.x) for c in columns}),
        "rows": 1 + max(c.row for c in columns),
        "ids": ids,
        "edges": [[FLOWS[i][0], FLOWS[i][1], FLOWS[i][2]] for i in edge_ids],
        "cards": {cid: list(box) for cid, box in cards.items()},
        "headers": [[name, list(box)] for name, box in headers],
        "regions": {rid: list(box) for rid, box in region_boxes.items()},
        "region_of": region_of,
        "labels": label_boxes,
        "badges": [list(box) for box in badge_boxes],
        "paths": {str(i): pts for i, pts in paths.items()},
        "collisions": collisions,
        "notes": notes,
    }
    return svg, meta


def problems(
    meta: dict[str, Any], svg: str, panel_width: float
) -> tuple[list[str], dict[str, Any]]:
    """What a compact figure must satisfy, re-verified from what it drew.

    Every participant present as a card; every edge on a path that passes
    through no card it does not connect; every label off every card, label
    strip, badge and other label; nothing set below 11px; and a card name
    that renders at MIN_NAME_AT_900 or more when the whole panel is fitted
    to a COLUMN_PX column. Region crossings are counted, not failed: a
    column layout has fewer corridors than the atlas, and the router's
    fallback is recorded in `notes` when it had to use one. Returns the
    problems and the numbers behind them.
    """
    out: list[str] = []
    drawn = set(re.findall(r'data-id="([^"]+)"', svg))
    missing = [cid for cid in meta["ids"] if cid not in drawn]
    for cid in missing:
        out.append(f"participant {cid} is not drawn")
    cards: dict[str, Box] = {cid: tuple(b) for cid, b in meta["cards"].items()}
    regions: dict[str, Box] = {rid: tuple(b) for rid, b in meta["regions"].items()}
    region_of: dict[str, str] = meta["region_of"]
    through = across = 0
    for src, dst, art in meta["edges"]:
        i = next(i for i, (a, b, _art, _k) in enumerate(FLOWS) if (a, b) == (src, dst))
        pts = meta["paths"].get(str(i))
        if not pts:
            out.append(f"route: {src} -> {dst} ('{art}') has no path")
            continue
        segs = list(zip(pts, pts[1:], strict=False))
        hit = sorted(
            cid
            for cid, box in cards.items()
            if cid not in (src, dst) and any(_seg_hits(tuple(a), tuple(b), box) for a, b in segs)
        )
        if hit:
            through += 1
            out.append(f"route: {src} -> {dst} passes through {', '.join(hit)}")
        foreign = sorted(
            rid
            for rid, box in regions.items()
            if rid not in (region_of[src], region_of[dst])
            and any(_seg_hits(tuple(a), tuple(b), box) for a, b in segs)
        )
        if foreign:
            across += 1
    for line in meta["collisions"]:
        out.append(f"label collision: {line}")
    solids: list[tuple[str, Box]] = [(cid, box) for cid, box in cards.items()]
    solids += [(name, tuple(box)) for name, box in meta["headers"]]
    badges: list[Box] = [tuple(box) for box in meta["badges"]]
    for k, bb in enumerate(badges):
        for name, box in solids:
            if _overlaps(bb, box):
                out.append(f"badge {k} touches {name}")
        for j, other in enumerate(badges[k + 1 :], start=k + 1):
            if _overlaps(bb, other):
                out.append(f"badge {k} touches badge {j}")
    solids += [(f"badge {k}", bb) for k, bb in enumerate(badges)]
    labels = meta["labels"]
    for k, lab in enumerate(labels):
        lb = tuple(lab["box"])
        for name, box in solids:
            if _overlaps(lb, box):
                out.append(f"label '{lab['artifact']}' touches {name}")
        for other in labels[k + 1 :]:
            if _overlaps(lb, tuple(other["box"])):
                out.append(f"label '{lab['artifact']}' touches label '{other['artifact']}'")
    sizes = sorted({float(m) for m in re.findall(r"font-size:\s*([0-9.]+)px", svg)})
    for s in sizes:
        if s < TEXT_PX:
            out.append(f"text set at {s}px, below {TEXT_PX}px")
    name_at_900 = NAME_PX * COLUMN_PX / panel_width
    if name_at_900 < MIN_NAME_AT_900:
        out.append(
            f"card names render at {name_at_900:.2f}px in a {COLUMN_PX:.0f}px column, "
            f"below {MIN_NAME_AT_900}px: the panel is {panel_width:.0f} wide"
        )
    stats = {
        "components": len(meta["ids"]),
        "edges": len(meta["edges"]),
        "canvas": meta["canvas"],
        "columns": meta["columns"],
        "rows": meta["rows"],
        "through_cards": through,
        "across_regions": across,
        "label_collisions": len(meta["collisions"]),
        "min_px": min(sizes) if sizes else 0.0,
        "name_px_at_900": round(name_at_900, 2),
    }
    return out, stats
