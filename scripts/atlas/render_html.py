"""Render the system atlas as one navigable HTML page.

The page is the system: the schematic, then every logical component grouped
by region with what it does, its interface, the modules and entry that
implement it, its derived build state, the tests guarding it, the flows in
and out, and the invariants it serves. Every component and module has a deep
link (#<id>), which is how a lesson cites the atlas instead of restating it.

Facts read out of the code and the model written about it are styled
differently on purpose, so the reader can always tell a measurement from an
assertion. The page is self-contained: no fonts, scripts or images are
fetched.

Ported from the deckgen repository's atlas tooling, cut down to what kstrl
needs.

Usage:  uv run python scripts/atlas/render_html.py [--out docs/atlas/index.html]
        add --base origin/main to draw a change map for the current branch
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import change as change_mod
import theme as theme_mod
from extract_atlas import sentence
from logical_model import (
    CALL_BUDGET,
    COMPONENTS,
    FLOWS,
    GOVERNED_BY,
    INVARIANTS,
    REGIONS,
    SPEC_ANCHOR,
    build_state,
    layout_problems,
)
from schematic import interactive_script, legend_rows, panel_css
from schematic import render as render_schematic

PLANE_ORDER = ["core", "agents", "tui", "ui"]
PLANE_NOTE = {
    "core": "The factory: intake, planning, the loop, the sensors, the pipeline.",
    "agents": "Adapters that run a coding agent as a subprocess and scrape usage.",
    "tui": "The Textual dashboard: a view over the event stream, never the record.",
    "ui": "The plain and rich terminal renderers the CLI prints through.",
}
TESTS_SHOWN = 2
DOC_SHOWN = 90


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _module_link(module: str, atlas: dict[str, Any]) -> str:
    rec = atlas["components"].get(module)
    if rec is None:
        return f'<code class="missing" title="not in the tree">{esc(module)}</code>'
    return f'<a href="#{esc(module)}"><code>{esc(module)}</code></a>'


def _component_article(c: dict[str, Any], atlas: dict[str, Any], state: str) -> str:
    cid = c["id"]
    comps = atlas["components"]
    modules = c.get("implemented_by", [])
    present = [m for m in modules if m in comps]
    loc = sum(comps[m]["loc"] for m in present)
    tests_total = sum(comps[m].get("tests_total", 0) for m in present)
    entry = c.get("entry", "")
    entry_found = any(
        entry
        and (
            entry in [f["name"] for f in comps[m]["functions"]]
            or entry in [k["name"] for k in comps[m]["classes"]]
        )
        for m in present
    )
    label = {
        "built": "built",
        "partial": "part built",
        "planned": "planned",
        "actor": "outside",
    }[state]
    o: list[str] = [f'<article class="comp" id="{esc(cid)}">']
    o.append('<header class="comp__h">')
    o.append(f'<h3><a href="#{esc(cid)}">{esc(cid)}</a></h3>')
    o.append(f'<span class="tag tag--{esc(state)}">{esc(label)}</span>')
    o.append(f'<span class="tag">{esc(c["kind"])}</span>')
    if c.get("tracker"):
        o.append(f'<span class="tag tag--tracker">{esc(c["tracker"])}</span>')
    if cid in CALL_BUDGET:
        o.append('<span class="tag tag--llm">model call</span>')
    o.append(f'<button type="button" class="inmap" data-go="{esc(cid)}">show in map</button>')
    o.append("</header>")
    o.append(f'<p class="comp__does">{esc(c["does"])}</p>')
    if c.get("interface") and c["interface"] != "external":
        o.append(f'<pre class="comp__code">{esc(c["interface"])}</pre>')
    if c.get("note"):
        o.append(f'<p class="comp__note"><b>Caveat.</b> {esc(c["note"])}</p>')
    o.append('<dl class="facts">')
    if modules:
        o.append("<dt>Implemented by</dt><dd>")
        o.append(", ".join(_module_link(m, atlas) for m in modules))
        o.append("</dd>")
        o.append("<dt>Entry</dt><dd>")
        if entry:
            mark = "found" if entry_found else "not found in the tree"
            o.append(f"<code>{esc(entry)}</code> <i>({mark})</i>")
        else:
            o.append("<i>none named</i>")
        o.append("</dd>")
        if present:
            o.append(
                f"<dt>Weight</dt><dd>{loc:,} lines in {len(present)} "
                f"module{'s' if len(present) != 1 else ''}</dd>"
            )
        o.append("<dt>Guarded by</dt><dd>")
        if tests_total:
            names: list[str] = []
            for m in present:
                names.extend(comps[m].get("tests", []))
            sample = [sentence(n) for n in names[:TESTS_SHOWN]]
            o.append(
                f"{tests_total} tests import these modules, for example: "
                + "; ".join(esc(s) for s in sample)
            )
        else:
            o.append("<i>no test imports these modules</i>")
        o.append("</dd>")
    if cid in CALL_BUDGET:
        o.append(f"<dt>Model calls</dt><dd>{esc(CALL_BUDGET[cid])}</dd>")
    if cid in SPEC_ANCHOR:
        o.append(f"<dt>Defined in</dt><dd>{esc(SPEC_ANCHOR[cid])}</dd>")
    ins = [(a, art) for a, b, art, _k in FLOWS if b == cid]
    outs = [(b, art) for a, b, art, _k in FLOWS if a == cid]
    if ins:
        o.append("<dt>Flows in</dt><dd>")
        o.append(
            "; ".join(
                f'<code>{esc(art)}</code> from <a href="#{esc(a)}">{esc(a)}</a>' for a, art in ins
            )
        )
        o.append("</dd>")
    if outs:
        o.append("<dt>Flows out</dt><dd>")
        o.append(
            "; ".join(
                f'<code>{esc(art)}</code> to <a href="#{esc(b)}">{esc(b)}</a>' for b, art in outs
            )
        )
        o.append("</dd>")
    rules = GOVERNED_BY.get(cid, [])
    if rules:
        o.append("<dt>Invariants</dt><dd>")
        o.append(
            " ".join(
                f'<a class="rule" href="#rule-{n}" title="{esc(INVARIANTS[n])}">{n}</a>'
                for n in rules
            )
        )
        o.append("</dd>")
    o.append("</dl></article>")
    return "".join(o)


def build(atlas: dict[str, Any], ch: dict[str, Any], theme: str | None = None) -> str:
    T = theme_mod.get(theme)
    comps: dict[str, Any] = atlas["components"]
    states = {c["id"]: build_state(c, atlas) for c in COMPONENTS}
    for c in COMPONENTS:
        if c["kind"] == "actor":
            states[c["id"]] = "actor"

    system_svg, detail = render_schematic(atlas, svg_id="schematic", theme=theme)
    meta = json.loads(detail).get("_meta", {})
    change_svg, change_detail = "", ""
    if ch.get("has_change"):
        gained = {k: v["gained"] for k, v in ch["per_component"].items()}
        change_svg, change_detail = render_schematic(
            atlas,
            changed=ch["direct"],
            changed_modules=ch["modules"],
            adjacent=ch["adjacent"],
            mode="change",
            svg_id="changemap",
            gained=gained,
            hot_artifacts=ch["flow_artifacts"],
            theme=theme,
        )

    n_built = sum(1 for s in states.values() if s == "built")
    n_partial = sum(1 for s in states.values() if s == "partial")
    n_planned = sum(1 for s in states.values() if s == "planned")
    commit = (atlas.get("built_at_commit") or "")[:10]

    o: list[str] = []
    o.append("<!doctype html>")
    o.append('<html lang="en"><head><meta charset="utf-8">')
    o.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    o.append("<title>kstrl system atlas</title>")
    o.append(f"<style>{CSS.format(ROOT=':root{' + theme_mod.css_vars(T) + '}')}")
    o.append(f"{panel_css(theme)}</style></head><body>")

    # ---------------- header ----------------
    o.append('<header class="bar">')
    o.append("<h1>kstrl <span>atlas</span></h1>")
    o.append(
        f'<p class="meta">{len(comps)} modules &middot; '
        f"{len(COMPONENTS)} logical components ({n_built} built, {n_partial} "
        f"part built, {n_planned} planned) &middot; {len(FLOWS)} flows"
        + (f" &middot; at <code>{esc(commit)}</code>" if commit else "")
        + "</p>"
    )
    o.append('<nav class="nav">')
    if change_svg:
        o.append('<a href="#change">Change</a>')
    o.append('<a href="#map">Map</a><a href="#components">Components</a>')
    o.append('<a href="#invariants">Invariants</a><a href="#modules">Modules</a>')
    o.append("</nav></header>")

    o.append('<main class="main">')

    # ---------------- the map ----------------
    def legend(mode: str) -> str:
        rows = "".join(
            f'<span class="lg"><i style="background:{fill};border-color:{stroke}">'
            f"</i>{esc(label)}</span>"
            for fill, stroke, label in legend_rows(mode, theme)
        )
        if mode == "system":
            rows += '<span class="lg"><i class="lg--dot"></i>model call</span>'
            rows += '<span class="lg"><i class="lg--bar"></i>code volume, then tests</span>'
        return f'<div class="legend">{rows}</div>'

    if change_svg:
        pr = ch.get("pr") or {}
        title = pr.get("title") or f"{ch['base']}..{ch['head']}"
        o.append('<section class="map" id="change">')
        o.append(
            f"<h2>Change <span>{esc(title)} &middot; {len(ch['direct'])} moved, "
            f"{len(ch['adjacent'])} reached, {ch['files']} files</span></h2>"
        )
        o.append(f'<div class="stage">{change_svg}</div>')
        o.append(legend("change"))
        o.append("</section>")

    o.append('<section class="map" id="map">')
    o.append("<h2>The system <span>click a component; the panel reads it</span></h2>")
    o.append('<div class="controls">')
    o.append(
        '<label><input type="checkbox" id="endstate"> show planned components at '
        "full strength</label>"
    )
    o.append('<label><input type="checkbox" id="actual"> actual size</label>')
    o.append("</div>")
    o.append('<div class="mapwrap">')
    o.append(f'<div class="stage" id="stage">{system_svg}</div>')
    o.append('<aside class="atlas-panel side" id="panel" aria-live="polite"></aside>')
    o.append("</div>")
    o.append(legend("system"))
    o.append(
        '<p class="key">Fill is build state, derived: a component is built when '
        "the entry named in the model exists in the modules named. The bar "
        f"along a card's bottom edge is code volume (full width is "
        f"{meta.get('scale', {}).get('max_loc', 0):,} lines, square-root "
        "scaled); the bar above it is tests importing the part. Dashed ghosts "
        "are planned. Every line carries what it moves.</p>"
    )
    o.append("</section>")

    # ---------------- components by region ----------------
    o.append('<section class="list" id="components"><h2>Components by region</h2>')
    by_region: dict[str, list[dict[str, Any]]] = {}
    for c in COMPONENTS:
        by_region.setdefault(c.get("region") or "outside", []).append(c)
    for region in REGIONS:
        items = by_region.get(region["id"], [])
        if not items:
            continue
        o.append(f'<h3 class="region" id="region-{region["id"]}">{region["label"]}</h3>')
        for c in items:
            o.append(_component_article(c, atlas, states[c["id"]]))
    outside = by_region.get("outside", [])
    if outside:
        o.append('<h3 class="region" id="region-outside">OUTSIDE THE FACTORY</h3>')
        for c in outside:
            o.append(_component_article(c, atlas, states[c["id"]]))
    o.append("</section>")

    # ---------------- invariants ----------------
    o.append('<section class="list" id="invariants"><h2>Invariants</h2>')
    o.append(
        '<p class="lede">The load-bearing rules, from CLAUDE.md, the roadmap '
        "doctrine and the control-loop design. Each names the components it "
        'governs.</p><ol class="rules">'
    )
    for n in sorted(INVARIANTS):
        ids = sorted(k for k, v in GOVERNED_BY.items() if n in v)
        o.append(f'<li id="rule-{n}" value="{n}">{esc(INVARIANTS[n])} ')
        o.append(
            '<span class="governs">'
            + ", ".join(f'<a href="#{esc(i)}">{esc(i)}</a>' for i in ids)
            + "</span></li>"
        )
    o.append("</ol></section>")

    # ---------------- modules ----------------
    o.append('<section class="list" id="modules"><h2>Modules</h2>')
    o.append(
        '<p class="lede">Every module under <code>kstrl/</code>, read with '
        "<code>ast</code>: lines, public operations, types, refusals (error "
        "classes), and the tests that import it. Nothing here was typed by "
        "hand.</p>"
    )
    planes: dict[str, list[dict[str, Any]]] = {}
    for rec in comps.values():
        planes.setdefault(rec["plane"], []).append(rec)
    owner = {m: c["id"] for c in COMPONENTS for m in c.get("implemented_by", [])}
    for plane in [*PLANE_ORDER, *sorted(set(planes) - set(PLANE_ORDER))]:
        items = sorted(planes.get(plane, []), key=lambda r: r["id"])
        if not items:
            continue
        note = PLANE_NOTE.get(plane, "")
        o.append(f'<h3 class="region">{esc(plane)} <small>{esc(note)}</small></h3>')
        o.append('<table class="mods"><thead><tr><th>module</th><th>lines</th>')
        o.append("<th>ops</th><th>types</th><th>refusals</th><th>tests</th>")
        o.append("<th>component</th><th>opening line</th></tr></thead><tbody>")
        for rec in items:
            own = owner.get(rec["id"], "")
            o.append(f'<tr id="{esc(rec["id"])}"><td><code>{esc(rec["id"])}</code>')
            o.append(f'<br><span class="path">{esc(rec["file"])}</span></td>')
            o.append(f'<td class="n">{rec["loc"]}</td><td class="n">{len(rec["functions"])}</td>')
            o.append(f'<td class="n">{len(rec["classes"])}</td>')
            o.append(f'<td class="n">{len(rec["errors"])}</td>')
            o.append(f'<td class="n">{rec.get("tests_total", 0)}</td>')
            owner_link = f'<a href="#{esc(own)}">{esc(own)}</a>' if own else ""
            o.append(f"<td>{owner_link}</td>")
            doc = rec.get("docstring", "")
            if len(doc) > DOC_SHOWN:
                doc = doc[: DOC_SHOWN - 3].rsplit(" ", 1)[0] + "..."
            o.append(f'<td class="doc">{esc(doc)}</td></tr>')
        o.append("</tbody></table>")
    o.append("</section></main>")

    o.append(
        '<footer class="foot">Generated by <code>scripts/atlas/render_html.py</code> '
        "from <code>docs/atlas/atlas.json</code> and "
        "<code>scripts/atlas/logical_model.py</code>. Refresh with "
        "<code>scripts/atlas/refresh.sh</code>.</footer>"
    )

    o.append(interactive_script("schematic", "panel", detail))
    if change_svg:
        o.append(interactive_script("changemap", "panel", change_detail))
    o.append(f"<script>{JS}</script>")
    o.append("</body></html>")
    return "\n".join(o)


CSS = """
{ROOT}
*{{box-sizing:border-box}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:var(--fs);
font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}}
a{{color:var(--accent)}}
code{{font-family:var(--fm);font-size:.92em}}
.bar{{padding:1rem 1.4rem .6rem;border-bottom:1px solid var(--line);
background:var(--surface);display:flex;flex-wrap:wrap;align-items:baseline;gap:.4rem 1.4rem}}
.bar h1{{margin:0;font-size:17px;font-weight:600;letter-spacing:-.01em}}
.bar h1 span{{color:var(--ink-3);font-weight:400}}
.meta{{margin:0;font-size:12px;color:var(--ink-3)}}
.nav{{margin-left:auto;display:flex;gap:.9rem;font-size:12.5px}}
.nav a{{color:var(--ink-2);text-decoration:none}}
.nav a:hover{{color:var(--accent)}}
.main{{padding:1rem 1.4rem 3rem;max-width:1500px;margin:0 auto}}
h2{{font-size:15px;font-weight:600;margin:1.6rem 0 .5rem;letter-spacing:-.01em}}
h2 span{{color:var(--ink-3);font-weight:400;font-size:12.5px;margin-left:.6rem}}
.controls{{display:flex;gap:1.2rem;font-size:12.5px;color:var(--ink-2);margin:0 0 .5rem}}
.controls input{{accent-color:var(--accent)}}
.mapwrap{{display:grid;grid-template-columns:minmax(0,1fr) 22rem;gap:1rem;align-items:start}}
.stage{{background:var(--surface);border:1px solid var(--line);border-radius:8px;
padding:.4rem;overflow:auto}}
.stage svg{{width:100%;height:auto;display:block}}
.stage.actual svg{{width:1342px;max-width:none}}
.side{{position:sticky;top:1rem;max-height:calc(100vh - 2rem);overflow-y:auto}}
.legend{{display:flex;flex-wrap:wrap;gap:.35rem .9rem;margin:.6rem 0 .3rem;
font-family:var(--fm);font-size:10.5px;color:var(--ink-3)}}
.lg{{display:inline-flex;align-items:center;gap:.35rem}}
.lg i{{width:13px;height:9px;border:1px solid;border-radius:2px;display:inline-block}}
.lg i.lg--dot{{width:9px;height:9px;border-radius:50%;background:var(--violet);
border-color:var(--violet)}}
.lg i.lg--bar{{width:15px;height:4px;border:0;border-radius:2px;
background:linear-gradient(var(--amber) 0 40%,transparent 40% 60%,var(--good) 60%)}}
.key,.lede{{font-size:12.5px;color:var(--ink-3);max-width:60rem;margin:.3rem 0 0}}
.region{{font-family:var(--fm);font-size:11px;letter-spacing:.12em;text-transform:uppercase;
color:var(--accent);margin:1.6rem 0 .5rem;padding-bottom:.3rem;border-bottom:1px solid var(--line)}}
.region small{{font-family:var(--fs);font-size:11.5px;letter-spacing:0;text-transform:none;
color:var(--ink-3);margin-left:.6rem}}
.comp{{border:1px solid var(--line);border-radius:7px;background:var(--surface);
padding:.7rem .9rem .5rem;margin:0 0 .5rem}}
.comp:target,.mods tr:target{{outline:2px solid var(--accent);outline-offset:2px}}
.comp__h{{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem}}
.comp__h h3{{margin:0;font-size:14.5px;font-weight:600}}
.comp__h h3 a{{color:var(--ink);text-decoration:none}}
.comp__h h3 a:hover{{color:var(--accent)}}
.tag{{font-family:var(--fm);font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;
padding:.15rem .4rem;border-radius:3px;background:var(--raised);color:var(--ink-3)}}
.tag--built{{color:var(--good);background:color-mix(in srgb,var(--good) 12%,transparent)}}
.tag--partial{{color:var(--amber);background:color-mix(in srgb,var(--amber) 12%,transparent)}}
.tag--planned{{color:var(--ink-3);border:1px dashed var(--line-2);background:none}}
.tag--tracker{{color:var(--ink-2)}}
.tag--llm{{color:var(--violet);background:color-mix(in srgb,var(--violet) 12%,transparent)}}
.inmap{{margin-left:auto;appearance:none;background:none;border:1px solid var(--line);
border-radius:4px;color:var(--ink-2);font:inherit;font-size:11px;padding:.15rem .5rem;
cursor:pointer}}
.inmap:hover{{border-color:var(--accent);color:var(--accent)}}
.comp__does{{margin:.4rem 0;font-size:13.5px;color:var(--ink-2)}}
.comp__code{{font-family:var(--fm);font-size:11.5px;background:var(--bg);
border:1px solid var(--line);
border-radius:5px;padding:.4rem .6rem;margin:0 0 .5rem;white-space:pre-wrap;color:var(--accent)}}
.comp__note{{margin:.2rem 0 .5rem;font-size:12.5px;color:var(--ink-2);
border-left:3px solid var(--warn);padding-left:.5rem}}
.facts{{display:grid;grid-template-columns:7.5rem 1fr;gap:.15rem .6rem;margin:0;font-size:12.5px}}
.facts dt{{font-family:var(--fm);font-size:10px;letter-spacing:.06em;text-transform:uppercase;
color:var(--ink-3);padding-top:.15rem}}
.facts dd{{margin:0;color:var(--ink-2);overflow-wrap:anywhere}}
.facts dd i{{color:var(--ink-3)}}
.facts a{{text-decoration:none;border-bottom:1px dashed var(--line-2);color:var(--ink)}}
.facts a:hover{{color:var(--accent);border-bottom-color:var(--accent)}}
.facts a code{{color:inherit}}
code.missing{{color:var(--ink-3);text-decoration:line-through}}
.rule{{display:inline-block;min-width:1.6em;text-align:center;font-family:var(--fm);
font-size:10.5px;color:var(--violet)!important;border:1px solid var(--line)!important;
border-radius:3px;padding:0 .2rem;margin-right:.2rem}}
.rules{{padding-left:1.6rem;font-size:13px;color:var(--ink-2)}}
.rules li{{margin:0 0 .5rem;padding-left:.3rem}}
.rules li:target{{outline:2px solid var(--accent);outline-offset:4px}}
.governs{{display:block;font-size:11.5px;color:var(--ink-3)}}
.governs a{{text-decoration:none;color:var(--ink-2)}}
.mods{{width:100%;border-collapse:collapse;font-size:12px;margin:0 0 1rem}}
.mods th{{text-align:left;font-family:var(--fm);font-size:10px;letter-spacing:.06em;
text-transform:uppercase;color:var(--ink-3);padding:.3rem .4rem;
border-bottom:1px solid var(--line)}}
.mods td{{padding:.35rem .4rem;border-bottom:1px solid var(--line);vertical-align:top;
color:var(--ink-2)}}
.mods td.n{{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}}
.mods td.doc{{color:var(--ink-3);max-width:34rem}}
.mods .path{{font-family:var(--fm);font-size:10px;color:var(--ink-3)}}
.mods a{{text-decoration:none;color:var(--ink-2);border-bottom:1px dashed var(--line-2)}}
.foot{{padding:1rem 1.4rem 2rem;font-size:11.5px;color:var(--ink-3);
border-top:1px solid var(--line)}}
@media (max-width:64rem){{
.mapwrap{{grid-template-columns:1fr}}
.side{{position:static;max-height:none}}
}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
"""

JS = r"""
(function(){
  var svg = document.getElementById('schematic');
  var stage = document.getElementById('stage');
  var end = document.getElementById('endstate');
  var act = document.getElementById('actual');
  if(end && svg){ end.addEventListener('change', function(){
    svg.classList.toggle('endstate', end.checked); }); }
  if(act && stage){ act.addEventListener('change', function(){
    stage.classList.toggle('actual', act.checked); }); }
  Array.prototype.slice.call(document.querySelectorAll('.inmap')).forEach(function(b){
    b.addEventListener('click', function(){
      if(svg && svg.atlasSelect){ svg.atlasSelect(b.dataset.go); }
      document.getElementById('map').scrollIntoView({behavior:'smooth', block:'start'});
    });
  });
})();
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlas", default="docs/atlas/atlas.json")
    parser.add_argument("--out", default="docs/atlas/index.html")
    parser.add_argument("--base", default="", help="draw a change map for HEAD against this ref")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--pr", default="")
    parser.add_argument(
        "--theme",
        default=theme_mod.DEFAULT,
        choices=sorted(theme_mod.THEMES),
        help="which look to render",
    )
    args = parser.parse_args()

    problems = layout_problems()
    if problems:
        for p in problems:
            print(f"layout: {p}", file=sys.stderr)
        return 1

    atlas = json.loads(Path(args.atlas).read_text(encoding="utf-8"))
    repo = Path(__file__).resolve().parents[2]
    ch: dict[str, Any] = {"has_change": False}
    if args.base:
        ch = change_mod.compute(repo, args.base, atlas, args.head)
        ch["pr"] = change_mod.pr_meta(repo, args.pr)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    page = build(atlas, ch, args.theme)
    out.write_text(page, encoding="utf-8")
    size = out.stat().st_size
    print(f"wrote {out} ({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
