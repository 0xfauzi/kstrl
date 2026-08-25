"""Check the atlas geometry and the meaning tables mechanically.

The atlas is hand-placed (positions) and hand-authored (relations), so two
kinds of quiet lie are possible: a card drawn where the model does not claim
it, or a sentence that names a flow the model no longer has. Both fail here
instead of shipping.

What is checked, in order:

    placement ..... every card inside its band, no two cards overlapping,
                    every flow naming known components (logical_model)
    labels ........ every edge label seated without touching a card, a
                    header or another label (the schematic's collision pass,
                    re-verified from the boxes it reports)
    type size ..... nothing in the figure set below 11px
    meaning ....... every flow has a layer and a sentence, every component a
                    plain word, every journey step a real edge and real ids,
                    every verb override a real edge
    wheel ......... for every component, the relationship wheel's name labels
                    stay inside the drawing and off each other and the centre

Exit 0 when clean, 1 with one line per problem.

Usage:  uv run python scripts/atlas/check_layout.py [--atlas docs/atlas/atlas.json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logical_model import COMPONENTS, FLOWS, layout_problems
from relations import (
    JOURNEYS,
    LAYER_OVERRIDES,
    LAYERS,
    PLAIN,
    RELATIONS,
    VERB_OVERRIDES,
    layer_for,
)
from schematic import TEXT_PX
from schematic import render as render_schematic

Box = tuple[float, float, float, float]


def _overlap(a: Box, b: Box) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def check_labels(meta: dict[str, object]) -> list[str]:
    out = [f"label collision: {line}" for line in meta.get("collisions", [])]  # type: ignore[union-attr]
    labels: list[dict[str, object]] = meta.get("labels", [])  # type: ignore[assignment]
    cards: dict[str, list[float]] = meta.get("cards", {})  # type: ignore[assignment]
    for k, lab in enumerate(labels):
        lb = tuple(lab["box"])  # type: ignore[arg-type]
        for cid, cb in cards.items():
            if _overlap(lb, tuple(cb)):  # type: ignore[arg-type]
                out.append(f"label '{lab['artifact']}' touches card {cid}")
        for other in labels[k + 1 :]:
            if _overlap(lb, tuple(other["box"])):  # type: ignore[arg-type]
                out.append(f"label '{lab['artifact']}' touches label '{other['artifact']}'")
    return out


def check_type_size(svg: str) -> list[str]:
    small = sorted(
        {
            float(m)
            for m in re.findall(r"font-size:\s*([0-9.]+)px", svg)
            if float(m) < TEXT_PX
        }
    )
    return [f"text set at {s}px, below {TEXT_PX}px" for s in small]


def check_meaning() -> list[str]:
    out: list[str] = []
    ids = {c["id"] for c in COMPONENTS}
    edges = {(a, b) for a, b, _art, _k in FLOWS}
    layer_ids = {layer["id"] for layer in LAYERS}
    for a, b, _art, kind in FLOWS:
        if (a, b) not in RELATIONS:
            out.append(f"flow {a} -> {b} has no sentence in RELATIONS")
        try:
            layer = layer_for((a, b), kind)
        except KeyError:
            out.append(f"flow {a} -> {b} has kind {kind} with no layer")
            continue
        if layer not in layer_ids:
            out.append(f"flow {a} -> {b} names unknown layer {layer}")
    for edge in RELATIONS:
        if edge not in edges:
            out.append(f"RELATIONS names a flow the model does not have: {edge[0]} -> {edge[1]}")
    for edge in LAYER_OVERRIDES:
        if edge not in edges:
            out.append(f"LAYER_OVERRIDES names an unknown flow: {edge[0]} -> {edge[1]}")
    for edge in VERB_OVERRIDES:
        if edge not in edges:
            out.append(f"VERB_OVERRIDES names an unknown flow: {edge[0]} -> {edge[1]}")
    for cid in ids:
        if cid not in PLAIN:
            out.append(f"{cid} has no plain word")
    for cid in PLAIN:
        if cid not in ids:
            out.append(f"PLAIN names an unknown component: {cid}")
    for j in JOURNEYS:
        for k, step in enumerate(j["steps"], start=1):  # type: ignore[union-attr]
            where = f"journey {j['id']} step {k}"
            for role in ("acts", "measures"):
                for cid in step[role]:
                    if cid not in ids:
                        out.append(f"{where} {role} names unknown component {cid}")
            edge = tuple(step["edge"])
            if edge not in edges:
                out.append(f"{where} traces a flow the model does not have: {edge[0]} -> {edge[1]}")
    return out


# ---- the wheel, mirrored from schematic._INTERACTIVE_JS ----------------------
# The page lays the wheel out in the browser; this is the same arithmetic in
# Python so the label geometry can be checked without one. Keep the two in
# step: a change to one is a change to both.

W, H, CX, CY, R = 400.0, 400.0, 200.0, 200.0, 118.0
MONO_CHAR_W = 6.6
NAME_LINE_H = 13.0


def wrap_name(cid: str) -> list[str]:
    parts = re.findall(r"[A-Z]+[a-z0-9]*|[a-z0-9]+", cid) or [cid]
    lines: list[str] = []
    cur = ""
    for p in parts:
        if cur and len(cur + p) > 10:
            lines.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        lines.append(cur)
    return lines[:3]


def wheel_boxes(cid: str, edges: list[dict[str, str]]) -> tuple[Box, list[tuple[str, Box]]]:
    order = {layer["id"]: i for i, layer in enumerate(LAYERS)}
    idx = sorted(
        (i for i, e in enumerate(edges) if cid in (e["from"], e["to"])),
        key=lambda i: (order[edges[i]["layer"]], i),
    )
    groups = 0
    prev: str | None = None
    for i in idx:
        if edges[i]["layer"] != prev:
            groups += 1
            prev = edges[i]["layer"]
    gap = 0.5 if groups > 1 else 0.0
    step = 360.0 / (len(idx) + gap * groups) if idx else 360.0
    hw, hh = max(34.0, len(cid) * 3.7 + 12), 15.0
    centre: Box = (CX - hw, CY - hh, 2 * hw, 2 * hh)
    boxes: list[tuple[str, Box]] = []
    a = -90.0
    prev = None
    for i in idx:
        e = edges[i]
        if prev is not None and e["layer"] != prev:
            a += gap * step
        prev = e["layer"]
        th = math.radians(a)
        a += step
        ux, uy = math.cos(th), math.sin(th)
        other = e["to"] if e["from"] == cid else e["from"]
        lines = wrap_name(other)
        n = len(lines)
        lw = max(len(line) for line in lines) * MONO_CHAR_W
        ex, ey = CX + (R + 9) * ux, CY + (R + 9) * uy
        if abs(ux) < 0.35:
            first = ey - 4 - (n - 1) * NAME_LINE_H if uy < 0 else ey + 12
            left = ex - lw / 2
        else:
            first = ey + 4 - (n - 1) * 6.5
            left = ex + 2 if ux > 0 else ex - 2 - lw
        top = first - 10
        boxes.append((other, (left, top, lw, (n - 1) * NAME_LINE_H + NAME_LINE_H)))
    return centre, boxes


def check_wheels(edges: list[dict[str, str]]) -> list[str]:
    out: list[str] = []
    for c in COMPONENTS:
        cid = c["id"]
        centre, boxes = wheel_boxes(cid, edges)
        for k, (name, box) in enumerate(boxes):
            x, y, w, h = box
            if x < 0 or y < 0 or x + w > W or y + h > H:
                out.append(f"wheel of {cid}: label {name} leaves the drawing")
            if _overlap(box, centre):
                out.append(f"wheel of {cid}: label {name} touches the centre")
            for other, ob in boxes[k + 1 :]:
                if _overlap(box, ob):
                    out.append(f"wheel of {cid}: labels {name} and {other} touch")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlas", default="docs/atlas/atlas.json")
    args = parser.parse_args()

    problems = [f"placement: {p}" for p in layout_problems()]
    problems += check_meaning()
    if not problems:
        atlas = json.loads(Path(args.atlas).read_text(encoding="utf-8"))
        svg, detail = render_schematic(atlas)
        meta = json.loads(detail)["_meta"]
        problems += check_labels(meta)
        problems += check_type_size(svg)
        problems += check_wheels(meta["edges"])
    if problems:
        print(f"atlas layout: {len(problems)} problem{'s' if len(problems) != 1 else ''}")
        for line in problems:
            print(f"  {line}")
        return 1
    print(
        f"atlas layout: clean ({len(COMPONENTS)} cards, {len(FLOWS)} labelled edges, "
        f"{len(COMPONENTS)} wheels, nothing below {TEXT_PX:g}px)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
