"""Route every flow orthogonally through the gutters between cards.

A flow is a Manhattan path: it leaves its source card through a port on one
side, runs along lanes in the gutters between cards, bends at right angles,
and enters its target card through a port. It never passes through a card it
does not connect and never enters a region it neither starts nor ends in;
those two are hard walls, checked again by check_layout.py from the paths
this module reports.

The lanes are derived from the card grid, not typed by hand: every gap
between two card columns carries a few parallel vertical lanes, every gap
between two card rows a few horizontal ones, and the cards' own port
positions are lanes too, so a stub leaving a card meets the grid at once.
Each flow is routed by Dijkstra over that grid with a cost that prefers a
short path, then few bends, then lanes no other flow already runs along and
ports no other flow already uses. Flows are routed shortest first, so a
local flow takes the direct lane and a long one goes round it.

When no corridor exists that avoids every foreign region the flow is routed
once more with regions as costs rather than walls and the reason is
recorded on the route, so the failure is listed, never hidden.

The artifact label sits on the longest segment of its path, centred on the
line where that touches nothing, never on a corner and never over the
arrowhead. What could not be placed cleanly is reported alongside.
"""

from __future__ import annotations

import bisect
import heapq
import math
from dataclasses import dataclass, field

Box = tuple[float, float, float, float]
Point = tuple[float, float]

CARD_MARGIN = 4.0
LANE_STEP = 9.0
# The first lane sits far enough from a card wall that a label centred on
# it (13 tall) clears the card and the 3-unit margin the label check keeps.
LANE_EDGE = 11.0
MAX_LANES = 7
# Ports along a side, in preference order; the first is the side's centre.
PORT_DX = (0.0, -25.0, 25.0, -50.0, 50.0)
PORT_DY = (0.0, -8.0, 8.0, -16.0, 16.0)
PORT_SPREAD_COST = 4.0
BEND_COST = 40.0
USED_LANE_FACTOR = 5.0
USED_PORT_COST = 500.0
FOREIGN_REGION_COST = 40.0

SIDES = ("left", "right", "top", "bottom")


@dataclass
class Route:
    """One routed flow: its polyline from port to port, and how it got there."""

    points: list[Point]
    src_side: str
    dst_side: str
    fallback: str = ""


@dataclass
class Placed:
    """Where a label ended up and whether that was the rule or a compromise."""

    box: Box
    segment: int
    on_longest: bool
    cost: float
    hits: list[str] = field(default_factory=list)


def _lanes(a: float, b: float) -> list[float]:
    usable = (b - a) - 2 * LANE_EDGE
    if usable <= 0:
        return [(a + b) / 2]
    n = min(MAX_LANES, int(usable // LANE_STEP) + 1)
    if n == 1:
        return [(a + b) / 2]
    step = usable / (n - 1)
    return [a + LANE_EDGE + k * step for k in range(n)]


def _gaps(spans: list[tuple[float, float]], lo: float, hi: float) -> list[tuple[float, float]]:
    """The free intervals in [lo, hi] once the given spans are covered."""
    out: list[tuple[float, float]] = []
    cursor = lo
    for a, b in sorted(spans):
        if a > cursor:
            out.append((cursor, a))
        cursor = max(cursor, b)
    if hi > cursor:
        out.append((cursor, hi))
    return out


def _seg_hits(a: Point, b: Point, box: Box) -> bool:
    """Does the axis-aligned segment a-b cross the interior of box?"""
    bx, by, bw, bh = box
    (x0, y0), (x1, y1) = a, b
    if abs(y0 - y1) < 1e-9:
        if not (by < y0 < by + bh):
            return False
        lo, hi = min(x0, x1), max(x0, x1)
        return lo < bx + bw and hi > bx
    if not (bx < x0 < bx + bw):
        return False
    lo, hi = min(y0, y1), max(y0, y1)
    return lo < by + bh and hi > by


def _inside(p: Point, box: Box) -> bool:
    bx, by, bw, bh = box
    return bx < p[0] < bx + bw and by < p[1] < by + bh


class Router:
    """The lane grid and the state every routed flow leaves on it."""

    def __init__(
        self,
        cards: dict[str, Box],
        actors: set[str],
        blocks: list[Box],
        regions: dict[str, Box],
        region_of: dict[str, str],
        canvas: tuple[float, float],
    ) -> None:
        self.cards = cards
        self.regions = regions
        self.region_of = region_of
        w, h = canvas
        xs: set[float] = set()
        ys: set[float] = set()
        for x, y, cw, ch in cards.values():
            cx, cy = x + cw / 2, y + ch / 2
            xs.update(cx + d for d in PORT_DX)
            ys.update(cy + d for d in PORT_DY)
        # Lanes in every gap between card columns and card rows, seen once
        # with every card and once without the actors (whose containers sit
        # in the margins and would otherwise narrow a gutter they only touch
        # at one end).
        for view in (set(cards), set(cards) - actors):
            boxes = [cards[c] for c in view]
            for a, b in _gaps([(x, x + cw) for x, _y, cw, _h in boxes], 8.0, w - 8.0):
                xs.update(_lanes(a, b))
            for a, b in _gaps([(y, y + ch) for _x, y, _w, ch in boxes], 24.0, h - 8.0):
                ys.update(_lanes(a, b))
        self.xs = sorted(xs)
        self.ys = sorted(ys)
        nx, ny = len(self.xs), len(self.ys)
        # Per grid segment: the cards it crosses (with margin) and whether it
        # crosses a block (a header, an empty container). Horizontal segment
        # (i, j) runs from xs[i] to xs[i+1] at ys[j]; vertical from ys[j] to
        # ys[j+1] at xs[i].
        self.h_cards: dict[tuple[int, int], set[str]] = {}
        self.v_cards: dict[tuple[int, int], set[str]] = {}
        self.h_block: set[tuple[int, int]] = set()
        self.v_block: set[tuple[int, int]] = set()
        for cid, (x, y, cw, ch) in cards.items():
            m = CARD_MARGIN
            self._mark((x - m, y - m, cw + 2 * m, ch + 2 * m), cid)
        for box in blocks:
            self._mark(box, None)
        # Per node: the region it sits in, for the foreign-region wall.
        self.node_region: dict[tuple[int, int], str] = {}
        for i, x in enumerate(self.xs):
            for j, y in enumerate(self.ys):
                for rid, box in regions.items():
                    if _inside((x, y), box):
                        self.node_region[(i, j)] = rid
                        break
        self.h_used: dict[tuple[int, int], int] = {}
        self.v_used: dict[tuple[int, int], int] = {}
        self.ports_used: set[tuple[str, str, float]] = set()
        self._nx, self._ny = nx, ny

    def _mark(self, box: Box, cid: str | None) -> None:
        bx, by, bw, bh = box
        xs, ys = self.xs, self.ys
        j0 = bisect.bisect_right(ys, by)
        j1 = bisect.bisect_left(ys, by + bh)
        i0 = bisect.bisect_right(xs, bx)
        i1 = bisect.bisect_left(xs, bx + bw)
        # Horizontal segments at rows strictly inside, overlapping in x.
        for j in range(j0, j1):
            for i in range(max(0, i0 - 1), min(len(xs) - 1, i1)):
                if xs[i] < bx + bw and xs[i + 1] > bx:
                    if cid is None:
                        self.h_block.add((i, j))
                    else:
                        self.h_cards.setdefault((i, j), set()).add(cid)
        for i in range(i0, i1):
            for j in range(max(0, j0 - 1), min(len(ys) - 1, j1)):
                if ys[j] < by + bh and ys[j + 1] > by:
                    if cid is None:
                        self.v_block.add((i, j))
                    else:
                        self.v_cards.setdefault((i, j), set()).add(cid)

    # ---- ports --------------------------------------------------------
    def _ports(self, cid: str) -> list[tuple[str, float, Point, tuple[int, int], int, float]]:
        """(side, offset, port point, exit node, exit orientation, cost)."""
        x, y, w, h = self.cards[cid]
        cx, cy = x + w / 2, y + h / 2
        xs, ys = self.xs, self.ys
        out: list[tuple[str, float, Point, tuple[int, int], int, float]] = []
        for k, dy in enumerate(PORT_DY):
            if abs(dy) > h / 2 - 6:
                continue
            py = cy + dy
            j = bisect.bisect_left(ys, py)
            if j >= len(ys) or abs(ys[j] - py) > 1e-6:
                continue
            i = bisect.bisect_right(xs, x - CARD_MARGIN) - 1
            if i >= 0:
                out.append(("left", dy, (x, py), (i, j), 0, self._port_cost(cid, "left", dy, k)))
            i = bisect.bisect_left(xs, x + w + CARD_MARGIN)
            if i < len(xs):
                cost = self._port_cost(cid, "right", dy, k)
                out.append(("right", dy, (x + w, py), (i, j), 0, cost))
        for k, dx in enumerate(PORT_DX):
            px = cx + dx
            i = bisect.bisect_left(xs, px)
            if i >= len(xs) or abs(xs[i] - px) > 1e-6:
                continue
            j = bisect.bisect_right(ys, y - CARD_MARGIN) - 1
            if j >= 0:
                out.append(("top", dx, (px, y), (i, j), 1, self._port_cost(cid, "top", dx, k)))
            j = bisect.bisect_left(ys, y + h + CARD_MARGIN)
            if j < len(ys):
                cost = self._port_cost(cid, "bottom", dx, k)
                out.append(("bottom", dx, (px, y + h), (i, j), 1, cost))
        return out

    def _port_cost(self, cid: str, side: str, off: float, rank: int) -> float:
        cost = rank * PORT_SPREAD_COST
        if (cid, side, off) in self.ports_used:
            cost += USED_PORT_COST
        return cost

    # ---- one flow -----------------------------------------------------
    def route(self, src: str, dst: str) -> Route:
        allowed = {self.region_of.get(src, ""), self.region_of.get(dst, "")}
        found = self._search(src, dst, allowed, strict=True)
        fallback = ""
        if found is None:
            found = self._search(src, dst, allowed, strict=False)
            if found is None:
                raise RuntimeError(f"no route at all for {src} -> {dst}")
            crossed = sorted(
                {
                    self.node_region[n]
                    for n in found[3]
                    if n in self.node_region and self.node_region[n] not in allowed
                }
            )
            fallback = "no corridor avoids " + ", ".join(crossed or ["a foreign region"])
        points, src_side, dst_side, nodes = found
        for a, b in zip(nodes, nodes[1:], strict=False):
            (i0, j0), (i1, j1) = a, b
            if j0 == j1:
                key = (min(i0, i1), j0)
                self.h_used[key] = self.h_used.get(key, 0) + 1
            else:
                key = (i0, min(j0, j1))
                self.v_used[key] = self.v_used.get(key, 0) + 1
        return Route(points=points, src_side=src_side, dst_side=dst_side, fallback=fallback)

    def _search(
        self, src: str, dst: str, allowed: set[str], strict: bool
    ) -> tuple[list[Point], str, str, list[tuple[int, int]]] | None:
        xs, ys = self.xs, self.ys
        nx, ny = self._nx, self._ny
        entries: dict[tuple[int, int], list[tuple[str, float, Point, int, float]]] = {}
        for side, off, p, node, orient, cost in self._ports(dst):
            entries.setdefault(node, []).append((side, off, p, orient, cost))
        dist: dict[tuple[int, int, int], float] = {}
        prev: dict[tuple[int, int, int], tuple[int, int, int] | None] = {}
        start_port: dict[tuple[int, int, int], tuple[str, float, Point]] = {}
        heap: list[tuple[float, int, int, int]] = []
        for side, off, p, node, orient, cost in self._ports(src):
            i, j = node
            stub = abs(xs[i] - p[0]) + abs(ys[j] - p[1])
            state = (i, j, orient)
            c = stub + cost
            if c < dist.get(state, math.inf):
                dist[state] = c
                prev[state] = None
                start_port[state] = (side, off, p)
                heapq.heappush(heap, (c, i, j, orient))
        best: tuple[float, tuple[int, int, int], str, float, Point] | None = None
        while heap:
            d, i, j, o = heapq.heappop(heap)
            state = (i, j, o)
            if d > dist.get(state, math.inf):
                continue
            if best is not None and d >= best[0]:
                break
            if (i, j) in entries:
                for side, off, p, orient, cost in entries[(i, j)]:
                    stub = abs(xs[i] - p[0]) + abs(ys[j] - p[1])
                    total = d + stub + cost + (BEND_COST if orient != o else 0.0)
                    if best is None or total < best[0]:
                        best = (total, state, side, off, p)
            for di, dj, orient in ((1, 0, 0), (-1, 0, 0), (0, 1, 1), (0, -1, 1)):
                ni, nj = i + di, j + dj
                if not (0 <= ni < nx and 0 <= nj < ny):
                    continue
                if orient == 0:
                    key = (min(i, ni), j)
                    if key in self.h_block or self.h_cards.get(key):
                        continue
                    length = abs(xs[ni] - xs[i])
                    used = self.h_used.get(key, 0)
                else:
                    key = (i, min(j, nj))
                    if key in self.v_block or self.v_cards.get(key):
                        continue
                    length = abs(ys[nj] - ys[j])
                    used = self.v_used.get(key, 0)
                region = self.node_region.get((ni, nj))
                foreign = region is not None and region not in allowed
                mid_region = self.node_region.get((i, j))
                foreign = foreign or (mid_region is not None and mid_region not in allowed)
                if foreign and strict:
                    continue
                step = length * (USED_LANE_FACTOR if used else 1.0)
                if orient != o:
                    step += BEND_COST
                if foreign:
                    step += FOREIGN_REGION_COST + length
                nstate = (ni, nj, orient)
                nd = d + step
                if nd < dist.get(nstate, math.inf):
                    dist[nstate] = nd
                    prev[nstate] = state
                    heapq.heappush(heap, (nd, ni, nj, orient))
        if best is None:
            return None
        _total, state, dst_side, _off, dst_p = best
        nodes: list[tuple[int, int]] = []
        cur: tuple[int, int, int] | None = state
        first = state
        while cur is not None:
            nodes.append((cur[0], cur[1]))
            first = cur
            cur = prev[cur]
        nodes.reverse()
        src_side, _soff, src_p = start_port[first]
        points = [src_p] + [(xs[i], ys[j]) for i, j in nodes] + [dst_p]
        self.ports_used.add((src, src_side, _soff))
        self.ports_used.add((dst, dst_side, _off))
        return _simplify(points), src_side, dst_side, nodes


def _simplify(points: list[Point]) -> list[Point]:
    out: list[Point] = []
    for p in points:
        if out and abs(out[-1][0] - p[0]) < 1e-6 and abs(out[-1][1] - p[1]) < 1e-6:
            continue
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            if (abs(a[0] - b[0]) < 1e-6 and abs(b[0] - p[0]) < 1e-6) or (
                abs(a[1] - b[1]) < 1e-6 and abs(b[1] - p[1]) < 1e-6
            ):
                out[-1] = p
                continue
        out.append(p)
    return out


def route_all(
    edges: list[tuple[str, str]],
    cards: dict[str, Box],
    actors: set[str],
    blocks: list[Box],
    regions: dict[str, Box],
    region_of: dict[str, str],
    canvas: tuple[float, float],
) -> dict[int, Route]:
    """Route every (src, dst) pair, shortest first. Keyed by edge index."""
    router = Router(cards, actors, blocks, regions, region_of, canvas)

    def span(k: int) -> float:
        a, b = cards[edges[k][0]], cards[edges[k][1]]
        return abs(a[0] + a[2] / 2 - b[0] - b[2] / 2) + abs(a[1] + a[3] / 2 - b[1] - b[3] / 2)

    out: dict[int, Route] = {}
    for k in sorted(range(len(edges)), key=span):
        out[k] = router.route(*edges[k])
    return out


def path_d(points: list[Point], radius: float = 6.0) -> str:
    """The SVG path: straight runs with a quarter-circle at every bend."""
    if len(points) < 2:
        return ""
    d = [f"M {points[0][0]:.1f} {points[0][1]:.1f}"]
    for k in range(1, len(points) - 1):
        p0, p1, p2 = points[k - 1], points[k], points[k + 1]
        r = min(
            radius,
            math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / 2,
            math.hypot(p2[0] - p1[0], p2[1] - p1[1]) / 2,
        )
        ux, uy = _unit(p0, p1)
        vx, vy = _unit(p1, p2)
        a = (p1[0] - ux * r, p1[1] - uy * r)
        b = (p1[0] + vx * r, p1[1] + vy * r)
        d.append(f"L {a[0]:.1f} {a[1]:.1f}")
        d.append(f"Q {p1[0]:.1f} {p1[1]:.1f} {b[0]:.1f} {b[1]:.1f}")
    d.append(f"L {points[-1][0]:.1f} {points[-1][1]:.1f}")
    return " ".join(d)


def _unit(a: Point, b: Point) -> Point:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


# ---- labels ----------------------------------------------------------------

CORNER_CLEAR = 12.0
ARROW_CLEAR = 16.0
PORT_CLEAR = 6.0
BESIDE = 4.0
# A step badge on the line is 8.5 in radius; a label beside the line clears
# it at this much further out.
BADGE_CLEAR = 10.0
# A short edge between two cards in one row cannot carry its label on the
# line; the label sits in the row gutter above or below instead, aligned to
# the edge: 38 clears a component card (28 half-height, 3 margin, half a
# label), 51 is the second lane in a 36-unit gutter. The rest are for a
# port off the card's centre line (8 or 16 from it) over a 28-unit gutter:
# 30 and 46 seat a label from a port 8 off centre, 26 and 54 one 16 off.
GUTTER_OFFSETS = (-38.0, 38.0, -51.0, 51.0, -30.0, 30.0, -46.0, 46.0, -26.0, 26.0, -54.0, 54.0)
# Where along a segment a label may sit, as a fraction of the free run:
# the middle first, then outward in both directions.
T_ORDER = (0.5, *(v for k in range(1, 11) for v in (0.5 - k * 0.05, 0.5 + k * 0.05)))


def _overlap_area(a: Box, b: Box) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return w * h if w > 0 and h > 0 else 0.0


def label_candidates(
    points: list[Point], lw: float, lh: float, canvas: tuple[float, float]
) -> list[tuple[int, bool, Box]]:
    """Every place this label may sit: (segment, on the longest, box).

    Segments are tried longest first; on each, the centre first and then
    outward, on the line first and then just beside it. The label never
    covers a corner, the port, or the arrowhead.
    """
    segs = list(zip(points, points[1:], strict=False))
    lengths = [abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in segs]
    order = sorted(range(len(segs)), key=lambda k: -lengths[k])
    longest = order[0] if order else -1
    out: list[tuple[int, bool, Box]] = []
    cw, ch = canvas

    def keep(box: Box) -> bool:
        return not (box[0] < -20 or box[1] < -20 or box[0] + lw > cw + 20 or box[1] + lh > ch + 20)

    for k in order:
        (x0, y0), (x1, y1) = segs[k]
        horizontal = abs(y1 - y0) < 1e-6
        along = lw if horizontal else lh
        clear0 = CORNER_CLEAR if k > 0 else PORT_CLEAR
        clear1 = CORNER_CLEAR if k < len(segs) - 1 else ARROW_CLEAR
        free = lengths[k] - clear0 - clear1 - along
        if free >= 0:
            for t in T_ORDER:
                s = clear0 + along / 2 + free * t
                if horizontal:
                    sign = 1.0 if x1 > x0 else -1.0
                    cx, cy = x0 + sign * s, y0
                    offs = (0.0, -(lh / 2 + BESIDE), lh / 2 + BESIDE)
                else:
                    sign = 1.0 if y1 > y0 else -1.0
                    cx, cy = x0, y0 + sign * s
                    offs = (0.0, -(lw / 2 + BESIDE), lw / 2 + BESIDE)
                for off in offs:
                    bx, by = (cx, cy + off) if horizontal else (cx + off, cy)
                    box: Box = (bx - lw / 2, by - lh / 2, lw, lh)
                    if keep(box):
                        out.append((k, k == longest, box))
        if horizontal:
            # Beside the edge in the row gutter, centred on the run and then
            # stepped along it, for the labels the run itself cannot hold.
            mid = (x0 + x1) / 2
            span = max(0.0, lengths[k] - 12)
            for t in T_ORDER:
                cx = mid + (t - 0.5) * span
                for off in GUTTER_OFFSETS:
                    box = (cx - lw / 2, y0 + off - lh / 2, lw, lh)
                    if keep(box):
                        out.append((k, k == longest, box))
        else:
            # Beside a vertical run too short to hold the label along it:
            # in the gap the run crosses, just right of the line and then
            # just left, centred on the run and then stepped along it; then
            # a badge's width further out, for a run that carries one.
            mid = (y0 + y1) / 2
            span = max(0.0, lengths[k] - 12)
            near, far = lw / 2 + BESIDE, lw / 2 + BESIDE + BADGE_CLEAR
            for t in T_ORDER:
                cy = mid + (t - 0.5) * span
                for off in (near, -near, far, -far):
                    box = (x0 + off - lw / 2, cy - lh / 2, lw, lh)
                    if keep(box):
                        out.append((k, k == longest, box))
    return out


def place_labels(
    routes: dict[int, Route],
    widths: dict[int, float],
    lh: float,
    obstacles: list[tuple[str, Box]],
    canvas: tuple[float, float],
    gap: float = 2.0,
) -> dict[int, Placed]:
    """Seat every label; shortest path first, since it has the fewest places."""

    def pad(b: Box) -> Box:
        return (b[0] - gap, b[1] - gap, b[2] + 2 * gap, b[3] + 2 * gap)

    def plen(k: int) -> float:
        pts = routes[k].points
        return sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(pts, pts[1:], strict=False))

    placed: dict[int, Placed] = {}
    cands = {k: label_candidates(routes[k].points, widths[k], lh, canvas) for k in routes}
    fixed_cost: dict[tuple[int, int], float] = {}

    def fixed(k: int, n: int) -> float:
        key = (k, n)
        if key not in fixed_cost:
            fixed_cost[key] = sum(_overlap_area(cands[k][n][2], ob) for _n, ob in obstacles)
        return fixed_cost[key]

    def soft(box: Box, skip: set[int]) -> float:
        return sum(_overlap_area(pad(box), o.box) for j, o in placed.items() if j not in skip)

    def seat(k: int, skip: set[int]) -> Placed | None:
        """The best seat for k given what is placed, or None if none is clean.

        The rule: the longest segment. A clean seat elsewhere beats a
        collision on the longest, and a collision on the longest beats one
        elsewhere.
        """
        best: Placed | None = None
        for n, (seg, on_longest, box) in enumerate(cands[k]):
            cost = fixed(k, n) + soft(box, skip | {k})
            rank = (cost > 0, not on_longest, cost)
            if best is None or rank < (best.cost > 0, not best.on_longest, best.cost):
                best = Placed(box=box, segment=seg, on_longest=on_longest, cost=cost)
            if cost == 0 and on_longest:
                break
        return best

    for k in sorted(routes, key=plen):
        best = seat(k, set())
        if best is not None and best.cost > 0:
            # Nothing clean: try to move whichever earlier labels block a
            # seat that touches no card, as long as each finds another clean
            # seat of its own. Two blockers at most; beyond that the gutter
            # is full and the collision should be reported.
            for n, (seg, on_longest, box) in enumerate(cands[k]):
                if fixed(k, n) > 0:
                    continue
                blockers = [j for j, o in placed.items() if _overlap_area(pad(box), o.box)]
                if not blockers or len(blockers) > 2:
                    continue
                saved = {j: placed[j] for j in blockers}
                placed[k] = Placed(box=box, segment=seg, on_longest=on_longest, cost=0.0)
                moved = True
                for j in blockers:
                    alt = seat(j, set())
                    if alt is None or alt.cost > 0:
                        moved = False
                        break
                    placed[j] = alt
                if moved:
                    best = placed[k]
                    break
                placed.update(saved)
                del placed[k]
        if best is None:
            # A path too short for any seat: put the label at its middle.
            pts = routes[k].points
            mx, my = pts[len(pts) // 2]
            box = (mx - widths[k] / 2, my - lh / 2, widths[k], lh)
            best = Placed(box=box, segment=0, on_longest=True, cost=1.0)
        if best.cost > 0:
            named = list(obstacles)
            named += [(f"label {j}", other.box) for j, other in placed.items() if j != k]
            best.hits = [n for n, ob in named if _overlap_area(best.box, ob)]
        placed[k] = best
    return placed
