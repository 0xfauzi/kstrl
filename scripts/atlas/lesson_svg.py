"""Emit a figure of the system for embedding in a lesson.

A lesson needs a picture of where a change landed, or of what a plan will
reach. The atlas already draws that picture. Drawing a second one by hand
would produce a rival diagram of the same system, and the hand-drawn one
would drift the moment a component moved. So this runs the same generator on
the same data and wraps the result in a figure.

The picture is therefore a citation, not a restatement: it is regenerated from
atlas.json every time, and it cannot disagree with the atlas it links to.

Three sources for what is marked:

    --base REF [--head REF]   what a git range changed (mode change)
    --components ID,ID,...    what a plan reaches, no git consulted (mode change)
    --mode system             nothing marked: the plain system figure

With --interactive the figure carries the atlas's focus interaction as a
self-contained fragment: clicking a component dims the rest, thickens its
edges in their layer colours with their labels, tags each neighbour with the
verb that relates it, frames the component and its neighbours, and draws
the relationship wheel in a panel under the figure. The figure opens at Fit
and pans and zooms like the atlas page (wheel, pinch, drag, Fit / 100% / +
/ -, Escape to go back). Plain DOM, no libraries.

Usage:
  uv run python scripts/atlas/lesson_svg.py --base <ref> [--head <ref>] \
      [--caption TEXT] [--out FILE] [--interactive]
  uv run python scripts/atlas/lesson_svg.py --components Sense,Pipeline \
      [--caption TEXT] [--out FILE] [--interactive]
  uv run python scripts/atlas/lesson_svg.py --mode system [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import change as change_mod
import theme as theme_mod
from logical_model import COMPONENTS, layout_problems
from schematic import interactive_script, layer_rows, legend_rows, panel_css
from schematic import render as render_schematic

ATLAS_URL = "docs/atlas/index.html"
GENERATOR = "scripts/atlas/schematic.py"


def figure(
    svg: str,
    caption: str,
    legend: list[tuple[str, str, str]],
    svg_id: str,
    detail_json: str | None,
) -> str:
    """The figure element. A detail JSON makes it interactive."""
    t = theme_mod.get()
    swatches = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:.4em;'
        f'margin-right:1.1em;white-space:nowrap">'
        f'<span style="width:.75em;height:.75em;border-radius:2px;'
        f"background:{fill};border:1px solid {stroke};"
        f'display:inline-block"></span>{label}</span>'
        for fill, stroke, label in legend
    )
    swatches += "".join(
        f'<span style="display:inline-flex;align-items:center;gap:.4em;'
        f'margin-right:1.1em;white-space:nowrap">'
        f'<span style="width:1em;height:3px;border-radius:2px;background:{colour};'
        f'display:inline-block"></span>{label}</span>'
        for _lid, colour, label in layer_rows()
    )
    controls = ""
    panel = ""
    script = ""
    hint = ""
    if detail_json is not None:
        panel_id = f"{svg_id}-panel"
        btn = (
            'style="font:inherit;color:inherit;background:none;border:1px solid '
            f"{t['line']};border-radius:4px;min-height:24px;min-width:2.2em;"
            'padding:0 .5em;cursor:pointer"'
        )
        controls = (
            f'<span style="display:inline-flex;gap:.3em;margin-left:1em;white-space:nowrap">'
            f'<button type="button" data-zoom="fit" data-for="{svg_id}" {btn}>Fit</button>'
            f'<button type="button" data-zoom="actual" data-for="{svg_id}" {btn}>100%</button>'
            f'<button type="button" data-zoom="in" data-for="{svg_id}" {btn} '
            f'aria-label="Zoom in">+</button>'
            f'<button type="button" data-zoom="out" data-for="{svg_id}" {btn} '
            f'aria-label="Zoom out">-</button></span>'
            f'<label style="display:inline-flex;align-items:center;gap:.4em;'
            f'margin-left:1em;white-space:nowrap;cursor:pointer;min-height:24px">'
            f'<input type="checkbox" data-endstate="{svg_id}"> end state: planned '
            f"components at full strength</label>"
        )
        hint = (
            f'<p style="margin:.4em 0 0;font-size:.76rem;color:{t["ink_3"]}">Scroll to zoom, '
            "drag to pan, click a component to frame it, Escape to go back.</p>"
        )
        panel = (
            f'<div id="{panel_id}" class="atlas-panel" style="margin-top:.9em" '
            f'aria-live="polite"></div>'
        )
        script = (
            f"<style>{panel_css()}</style>"
            + interactive_script(svg_id, panel_id, detail_json)
            + "<script>(function(){"
            f"var box = document.querySelector('[data-endstate=\"{svg_id}\"]');"
            f'var svg = document.getElementById("{svg_id}");'
            "if(!box || !svg || !svg.atlas){ return; }"
            "box.addEventListener('change', function(){ svg.atlas.setEndstate(box.checked); });"
            f"Array.prototype.slice.call(document.querySelectorAll('[data-for=\"{svg_id}\"]'))"
            ".forEach(function(b){ b.addEventListener('click', function(){"
            "var z = b.dataset.zoom, v = svg.atlas.view;"
            "if(z === 'fit'){ v.fit(); } else if(z === 'actual'){ v.actual(); }"
            "else { v.zoomBy(z === 'in' ? 1.25 : 1 / 1.25); } }); });"
            "document.addEventListener('keydown', function(e){"
            "if(e.key === 'Escape'){ svg.atlas.clear(); svg.atlas.view.back(); } });"
            "})();</script>"
        )
    # Legend labels and node names are generated, so a prose linter should
    # skip the figure rather than judge text nobody wrote by hand. The figure
    # opens at Fit, the whole map across the column; text is drawn at 11px
    # and zoom brings it back to size.
    return (
        f'<figure data-generated="atlas" '
        f'style="margin:2.4em 0;padding:1.1em 1.1em .9em;'
        f"background:{t['bg']};border:1px solid {t['line']};border-radius:8px;"
        f'overflow-x:auto;color:{t["ink_2"]};font-family:{t["font_ui"]}">'
        f"{svg}{hint}"
        f'<div style="margin-top:.9em;font-size:.78rem;line-height:1.9;'
        f'color:{t["ink_3"]}">{swatches}{controls}</div>'
        f"{panel}"
        f'<figcaption style="margin-top:.7em;font-size:.82rem;line-height:1.5;'
        f'color:{t["ink_3"]}">{caption}</figcaption>'
        f"{script}"
        f"</figure>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlas", default="docs/atlas/atlas.json")
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--components",
        default="",
        help="comma-separated logical component ids to mark as reached",
    )
    parser.add_argument("--mode", choices=["change", "system"], default="")
    parser.add_argument("--svg-id", default="lessonmap")
    parser.add_argument("--caption", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()

    problems = layout_problems()
    if problems:
        for p in problems:
            print(f"layout: {p}", file=sys.stderr)
        return 1

    atlas = json.loads(Path(args.atlas).read_text(encoding="utf-8"))
    repo = Path(__file__).resolve().parents[2]
    mode = args.mode or ("change" if (args.base or args.components) else "system")
    known = {c["id"] for c in COMPONENTS}

    changed: set[str] = set()
    changed_modules: set[str] = set()
    adjacent: set[str] = set()
    gained: dict[str, dict[str, int]] = {}
    hot: set[str] = set()
    legend_mode = "system"
    caption = args.caption

    if mode == "change" and args.components:
        ids = [s.strip() for s in args.components.split(",") if s.strip()]
        unknown = sorted(set(ids) - known)
        if unknown:
            print(f"unknown component ids: {', '.join(unknown)}", file=sys.stderr)
            return 1
        changed = set(ids)
        legend_mode = "reach"
        caption = caption or (
            f"The system, with the {len(changed)} components a plan reaches as "
            f"the figure. This marks a plan's reach, not a diff. Drawn by "
            f"<code>{GENERATOR}</code>, the same generator the atlas uses, so "
            f"this cannot disagree with <code>{ATLAS_URL}</code>."
        )
    elif mode == "change":
        if not args.base:
            print("--base is required for a change figure", file=sys.stderr)
            return 1
        ch = change_mod.compute(repo, args.base, atlas, args.head)
        if not ch["has_change"]:
            print(
                f"no change between {args.base} and {args.head}: nothing to draw",
                file=sys.stderr,
            )
            return 1
        changed = ch["direct"]
        changed_modules = ch["modules"]
        adjacent = ch["adjacent"]
        gained = {k: v["gained"] for k, v in ch["per_component"].items()}
        hot = ch["flow_artifacts"]
        legend_mode = "change"
        caption = caption or (
            f"The system, with this change as the figure. Drawn by "
            f"<code>{GENERATOR}</code>, the same generator the atlas uses, so "
            f"this cannot disagree with <code>{ATLAS_URL}</code>."
        )
    else:
        caption = caption or (
            f"The system as the atlas describes it. Drawn by <code>{GENERATOR}</code> "
            f"from <code>docs/atlas/atlas.json</code>; planned components are the "
            f"dashed ghosts. Click a component to read what it is to its neighbours."
        )

    svg, detail = render_schematic(
        atlas,
        changed=changed,
        changed_modules=changed_modules,
        adjacent=adjacent,
        mode=mode,
        svg_id=args.svg_id,
        gained=gained,
        hot_artifacts=hot,
    )
    meta = json.loads(detail).get("_meta", {})
    for line in meta.get("collisions", []):
        print(f"label collision: {line}", file=sys.stderr)

    if legend_mode == "reach":
        t = theme_mod.get()
        rows = [
            (legend_rows("change")[0][0], t["change"], "in the plan's reach"),
            (t["ghost"][0], t["ghost"][1], "not in the plan"),
        ]
    else:
        rows = legend_rows(legend_mode)
    # Trailing newline so end-of-file-fixer has nothing to add: without it the
    # hook appends one and refresh.sh strips it back off on the next run.
    out = figure(svg, caption, rows, args.svg_id, detail if args.interactive else None) + "\n"
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"wrote {args.out} ({len(out) / 1024:.0f} KB)")
    else:
        sys.stdout.write(out)
    return 0 if not meta.get("collisions") else 2


if __name__ == "__main__":
    raise SystemExit(main())
