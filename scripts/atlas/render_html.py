"""Render the system atlas as one page that teaches the system in layers.

The page is the map, at full width. It opens at Fit (the whole map across
the column) and the reader zooms with the wheel, a pinch, or the Fit / 100%
/ + / - controls, and pans by dragging; selecting a component frames it and
its neighbours, and Escape returns the view. Above it, a layer switch (one map,
seven readings), a today/end-state toggle, the journeys a reader can step
through, and a slim strip carrying the active layer's question and its
components. Click a component and the focus panel opens as a drawer over the
map, docked on the side away from the component: it leads with the plain
word, draws the relationship wheel, and reads the sentence for whichever
spoke the reader touches. Below the map, a one-line index of every component
by region and the invariants. Nothing about the code is shown beyond the
single "lives in" line; the counts stay in atlas.json for the change detector.

Every picture comes from schematic.py, every sentence from relations.py, and
the page is self-contained: no fonts, scripts or images are fetched.

Usage:  uv run python scripts/atlas/render_html.py [--out docs/atlas/index.html]
        --check   exit 1 when the committed page differs from what would render
        --base origin/main   also draw a change map for the current branch
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
from logical_model import (
    COMPONENTS,
    FLOWS,
    GOVERNED_BY,
    INVARIANTS,
    REGIONS,
    layout_problems,
)
from relations import JOURNEYS, LAYERS, PLAIN
from schematic import interactive_script, layer_rows, legend_rows, panel_css
from schematic import render as render_schematic

STATE_WORD = {"built": "built", "partial": "part built", "planned": "planned", "actor": "outside"}


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _index_entry(c: dict[str, Any], state: str) -> str:
    cid = c["id"]
    return (
        f'<button type="button" class="ix" data-go="{esc(cid)}">'
        f'<span class="ix__plain">{esc(PLAIN.get(cid, cid))}</span>'
        f"<code>{esc(cid)}</code>"
        f'<span class="chip chip--{esc(state)}">{esc(STATE_WORD[state])}</span>'
        "</button>"
    )


def build(atlas: dict[str, Any], ch: dict[str, Any]) -> str:
    T = theme_mod.get()
    system_svg, detail = render_schematic(atlas, svg_id="schematic")
    states = {cid: rec["state"] for cid, rec in json.loads(detail).items() if cid != "_meta"}
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
        )

    commit = (atlas.get("built_at_commit") or "")[:10]
    n_flows = len(FLOWS)
    n_comp = len([c for c in COMPONENTS if c["kind"] != "actor"])

    o: list[str] = []
    o.append("<!doctype html>")
    o.append('<html lang="en"><head><meta charset="utf-8">')
    o.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    o.append("<title>kstrl system atlas</title>")
    o.append(f"<style>{CSS.format(ROOT=':root{' + theme_mod.css_vars(T) + '}')}")
    o.append(f"{panel_css()}</style></head><body>")

    # ---------------- header ----------------
    o.append('<header class="bar">')
    o.append("<h1>kstrl <span>atlas</span></h1>")
    o.append(
        '<p class="meta">A generated map of what the parts of kstrl are and what they '
        f"are to each other: {n_comp} components, {n_flows} flows, seven layers."
        + (f' Built at <code title="commit">{esc(commit)}</code>.' if commit else "")
        + "</p>"
    )
    o.append('<nav class="nav">')
    if change_svg:
        o.append('<a href="#change">Change</a>')
    o.append('<a href="#map">Map</a><a href="#components">Components</a>')
    o.append('<a href="#invariants">Invariants</a>')
    o.append("</nav></header>")

    o.append('<main class="main">')

    # ---------------- change map (only with --base) ----------------
    if change_svg:
        pr = ch.get("pr") or {}
        title = pr.get("title") or f"{ch['base']}..{ch['head']}"
        rows = "".join(
            f'<span class="lg"><i style="background:{fill};border-color:{stroke}"></i>'
            f"{esc(label)}</span>"
            for fill, stroke, label in legend_rows("change")
        )
        o.append('<section class="map" id="change">')
        o.append(
            f"<h2>Change <span>{esc(title)}: {len(ch['direct'])} moved, "
            f"{len(ch['adjacent'])} reached, {ch['files']} files</span></h2>"
        )
        o.append(f'<div class="stage">{change_svg}</div>')
        o.append(f'<div class="legend">{rows}</div>')
        o.append("</section>")

    # ---------------- controls ----------------
    o.append('<section class="map" id="map">')
    o.append('<div class="controls">')
    o.append('<div class="ctl"><span class="ctl__k">Layer</span>')
    o.append('<div class="seg" role="group" aria-label="Layer">')
    for layer in LAYERS:
        o.append(
            f'<button type="button" class="seg__b" data-layer-btn="{layer["id"]}" '
            f'aria-pressed="false" style="--c:{T["layers"][layer["id"]]}">'
            f"<i></i>{esc(layer['label'])}</button>"
        )
    o.append(
        '<button type="button" class="seg__b" data-layer-btn="all" aria-pressed="false">'
        "All</button>"
    )
    o.append("</div></div>")
    o.append('<div class="ctl ctl--row">')
    o.append('<div class="ctl"><span class="ctl__k">Show</span>')
    o.append('<div class="seg" role="group" aria-label="Build state">')
    o.append(
        '<button type="button" class="seg__b" data-endstate="0" aria-pressed="true">'
        "Today</button>"
        '<button type="button" class="seg__b" data-endstate="1" aria-pressed="false">'
        "End state</button></div></div>"
    )
    o.append('<div class="ctl"><span class="ctl__k">Zoom</span>')
    o.append('<div class="seg" role="group" aria-label="Zoom">')
    o.append(
        '<button type="button" class="seg__b" data-zoom="fit" aria-pressed="true">Fit</button>'
        '<button type="button" class="seg__b" data-zoom="actual" aria-pressed="false">'
        "100%</button>"
        '<button type="button" class="seg__b seg__b--step" data-zoom="in" '
        'aria-label="Zoom in">+</button>'
        '<button type="button" class="seg__b seg__b--step" data-zoom="out" '
        'aria-label="Zoom out">-</button></div>'
        '<span class="zpct" id="zpct" aria-live="off" title="zoom"></span></div>'
    )
    o.append('<div class="ctl"><span class="ctl__k">Journey</span>')
    o.append('<select id="journey" aria-label="Journey"><option value="">none</option>')
    for k, j in enumerate(JOURNEYS):
        o.append(f'<option value="{k}">{esc(j["label"])}</option>')
    o.append("</select>")
    o.append(
        '<button type="button" class="jb" id="jprev" aria-label="Previous step" disabled>'
        "&#8249; Previous</button>"
        '<span class="jcount" id="jcount" aria-live="polite"></span>'
        '<button type="button" class="jb" id="jnext" aria-label="Next step" disabled>'
        "Next &#8250;</button>"
    )
    o.append("</div></div>")
    o.append("</div>")  # controls

    # The slim strip above the map: the active layer's question and the
    # components it touches, or the steps of the journey in progress.
    o.append('<div class="lstrip" id="lstrip" aria-live="polite"></div>')

    # ---------------- the map ----------------
    o.append('<div class="mapwrap" id="mapwrap">')
    o.append(f'<div class="stage" id="stage">{system_svg}</div>')
    o.append('<aside class="drawer" id="drawer" data-dock="right" hidden>')
    o.append('<div class="drawer__in">')
    o.append(
        '<button type="button" class="drawer__x" id="drawerclose" '
        'aria-label="Close the panel">Close</button>'
    )
    o.append('<div class="atlas-panel" id="panel" aria-live="polite"></div>')
    o.append("</div></aside>")
    o.append("</div>")  # mapwrap
    o.append(
        '<p class="hint">Scroll to zoom, drag to pan, click a component to frame it, '
        "Escape to go back.</p>"
    )
    o.append('<div class="strip" id="strip" hidden><span class="strip__n" id="stripn"></span>')
    o.append('<span class="strip__say" id="stripsay"></span>')
    o.append('<span class="strip__meas" id="stripmeas"></span></div>')
    o.append('<div class="legend">')
    for _lid, colour, label in layer_rows():
        o.append(
            f'<span class="lg"><i class="lg--line" style="background:{colour}"></i>'
            f"{esc(label)}</span>"
        )
    o.append('<span class="lg lg--gap"></span>')
    for fill, stroke, label in legend_rows("system"):
        dashed = " lg--dashed" if label == "planned" else ""
        o.append(
            f'<span class="lg"><i class="{dashed}" style="background:{fill};'
            f'border-color:{stroke}"></i>{esc(label)}</span>'
        )
    o.append(
        f'<span class="lg"><i class="lg--ring" style="border-color:{T["accent"]}"></i>'
        f'acts</span><span class="lg"><i class="lg--ring" style="border-color:{T["steel"]}">'
        "</i>measures</span>"
    )
    o.append("</div>")
    o.append(
        '<p class="key">Fill is build state, derived: a component is built when the entry '
        "named in the model exists in the modules named. Every line carries what it moves "
        "and is coloured by the layer it belongs to. Click a component; press Escape to "
        "clear it and return the view; arrow keys step a journey; double-click a region's "
        "name to frame the region. Text is drawn at 11px and never smaller: at Fit it "
        "is scaled down, and zoom brings it back.</p>"
    )
    o.append("</section>")

    # ---------------- index by region ----------------
    o.append(
        '<section class="list" id="components"><h2>Components <span>by region; '
        "click one to focus it</span></h2>"
    )
    by_region: dict[str, list[dict[str, Any]]] = {}
    for c in COMPONENTS:
        by_region.setdefault(c.get("region") or "outside", []).append(c)
    o.append('<div class="ixgrid">')
    for k, region in enumerate(REGIONS, start=1):
        items = by_region.get(region["id"], [])
        if not items:
            continue
        o.append(f'<div class="ixgroup"><h3 class="region"><i>{k}</i>{esc(region["label"])}</h3>')
        for c in items:
            o.append(_index_entry(c, states[c["id"]]))
        o.append("</div>")
    outside = by_region.get("outside", [])
    if outside:
        o.append('<div class="ixgroup"><h3 class="region"><i>&middot;</i>OUTSIDE THE FACTORY</h3>')
        for c in outside:
            o.append(_index_entry(c, states[c["id"]]))
        o.append("</div>")
    o.append("</div></section>")

    # ---------------- invariants ----------------
    o.append(
        '<section class="list" id="invariants"><h2>Invariants <span>the rules the '
        "chips in the panel point at</span></h2>"
    )
    o.append('<ol class="rules">')
    for n in sorted(INVARIANTS):
        ids = sorted(k for k, v in GOVERNED_BY.items() if n in v)
        o.append(
            f'<li id="rule-{n}" value="{n}"><span class="rules__t">{esc(INVARIANTS[n])}</span>'
        )
        o.append(
            '<span class="governs">'
            + " ".join(
                f'<button type="button" class="gv" data-go="{esc(i)}">{esc(i)}</button>'
                for i in ids
            )
            + "</span></li>"
        )
    o.append("</ol></section></main>")

    o.append(
        '<footer class="foot">Generated by <code>scripts/atlas/render_html.py</code> '
        "from <code>docs/atlas/atlas.json</code>, <code>scripts/atlas/logical_model.py</code> "
        "and <code>scripts/atlas/relations.py</code>. Refresh with "
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
button{{font:inherit;color:inherit}}
.bar{{padding:.9rem 1.4rem .7rem;border-bottom:1px solid var(--line);
background:var(--surface);display:flex;flex-wrap:wrap;align-items:baseline;gap:.3rem 1.4rem}}
.bar h1{{margin:0;font-size:17px;font-weight:600;letter-spacing:-.01em;font-family:var(--fm)}}
.bar h1 span{{color:var(--accent);font-weight:400}}
.meta{{margin:0;font-size:12.5px;color:var(--ink-3);max-width:60rem}}
.meta code{{color:var(--ink-2)}}
.nav{{margin-left:auto;display:flex;gap:.9rem;font-size:12.5px}}
.nav a{{color:var(--ink-2);text-decoration:none}}
.nav a:hover{{color:var(--accent)}}
.main{{padding:.8rem 1.4rem 3rem}}
h2{{font-size:15px;font-weight:600;margin:1.8rem 0 .5rem;letter-spacing:-.01em}}
h2 span{{color:var(--ink-3);font-weight:400;font-size:12.5px;margin-left:.6rem}}
.controls{{display:flex;flex-direction:column;gap:.45rem;margin:.4rem 0 .5rem}}
.ctl{{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem .7rem}}
.ctl--row{{gap:.5rem 1.6rem}}
.ctl__k{{font-family:var(--fm);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
color:var(--ink-3);min-width:3.6rem}}
.seg{{display:inline-flex;flex-wrap:wrap;border:1px solid var(--line-2);border-radius:6px;
overflow:hidden;background:var(--surface)}}
.seg__b{{appearance:none;background:none;border:0;border-right:1px solid var(--line);
min-height:30px;padding:0 .75rem;font-size:12.5px;color:var(--ink-2);cursor:pointer;
display:inline-flex;align-items:center;gap:.45rem}}
.seg__b:last-child{{border-right:0}}
.seg__b i{{width:14px;height:3px;border-radius:2px;background:var(--c,var(--ink-3))}}
.seg__b:hover{{color:var(--ink);background:var(--raised)}}
.seg__b[aria-pressed="true"]{{color:var(--ink);background:var(--raised);
box-shadow:inset 0 -2px 0 var(--c,var(--accent))}}
select#journey{{font:inherit;font-size:12.5px;color:var(--ink);background:var(--surface);
border:1px solid var(--line-2);border-radius:6px;min-height:30px;padding:0 .5rem;
max-width:22rem}}
.jb{{appearance:none;background:var(--surface);border:1px solid var(--line-2);border-radius:6px;
min-height:30px;padding:0 .7rem;font-size:12.5px;color:var(--ink-2);cursor:pointer}}
.jb:hover:not(:disabled){{color:var(--ink);background:var(--raised)}}
.jb:disabled{{opacity:.4;cursor:default}}
.jcount{{font-family:var(--fm);font-size:11.5px;color:var(--ink-3);min-width:3.2rem;
text-align:center}}
/* the slim strip above the map: the layer's question and its components */
.lstrip{{display:flex;flex-wrap:wrap;align-items:center;gap:.35rem .9rem;margin:0 0 .6rem;
padding:.5rem .8rem;background:var(--surface);border:1px solid var(--line);border-radius:6px;
font-size:13px;min-height:2.6rem}}
.lstrip__l{{font-family:var(--fm);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
color:var(--ink-3);display:inline-flex;align-items:center;gap:.5rem;white-space:nowrap}}
.lstrip__l i{{width:16px;height:3px;border-radius:2px;background:var(--c,var(--ink-3))}}
.lstrip__q{{font-size:14.5px;color:var(--ink);font-weight:600;line-height:1.3}}
.lstrip__s{{color:var(--ink-3)}}
.lstrip__row{{display:flex;flex-wrap:wrap;gap:.3rem;align-items:center}}
.lstrip__row button{{appearance:none;background:var(--raised);border:1px solid var(--line);
border-radius:4px;min-height:24px;padding:0 .45rem;font-family:var(--fm);font-size:11px;
color:var(--ink-2);cursor:pointer}}
.lstrip__row button:hover{{color:var(--accent);border-color:var(--accent)}}
.lstrip__row button.on{{color:var(--ink);border-color:var(--accent);
box-shadow:inset 0 -2px 0 var(--accent)}}
.lstrip__row button b{{font-weight:600;margin-right:.35em;color:var(--ink-3)}}
.lstrip__row button.on b{{color:var(--accent)}}
.lstrip__row button i{{font-style:normal;color:var(--ink-3);margin:0 .3em}}
.seg__b--step{{min-width:2.2rem;justify-content:center;font-family:var(--fm);font-size:14px}}
.zpct{{font-family:var(--fm);font-size:11.5px;color:var(--ink-3);min-width:2.8rem}}
/* the map: full width; the drawing pans and zooms inside the stage */
.mapwrap{{position:relative}}
.stage{{background:var(--surface);border:1px solid var(--line);border-radius:8px;
padding:.4rem;overflow:hidden}}
.hint{{margin:.4rem 0 0;font-size:12px;color:var(--ink-3)}}
/* the focus panel: a drawer over the map, docked away from the selection */
.drawer{{position:absolute;top:0;bottom:0;width:380px;max-width:calc(100% - 2rem);
z-index:2;pointer-events:none}}
.drawer[data-dock="right"]{{right:.6rem}}
.drawer[data-dock="left"]{{left:.6rem}}
.drawer[hidden]{{display:none}}
.drawer__in{{position:sticky;top:.8rem;pointer-events:auto;max-height:calc(100vh - 1.6rem);
overflow-y:auto;border-radius:8px;box-shadow:0 12px 34px rgba(0,0,0,.55),0 0 0 1px var(--line-2)}}
.drawer__in .atlas-panel{{border-radius:0 0 8px 8px;border-top:0}}
.drawer__x{{appearance:none;width:100%;display:flex;align-items:center;justify-content:flex-end;
gap:.5rem;min-height:30px;padding:0 .8rem;background:var(--raised);border:0;
border-bottom:1px solid var(--line);border-radius:8px 8px 0 0;font-family:var(--fm);
font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);cursor:pointer}}
.drawer__x::after{{content:"\\00d7";font-size:16px;line-height:1;color:var(--ink-2)}}
.drawer__x:hover{{color:var(--ink)}}
.drawer__x:hover::after{{color:var(--accent)}}
.strip{{display:flex;flex-wrap:wrap;align-items:baseline;gap:.3rem 1rem;margin:.6rem 0 0;
padding:.6rem .9rem;background:var(--raised);border-radius:6px;
border-left:3px solid var(--accent);font-size:13.5px;color:var(--ink)}}
.strip[hidden]{{display:none}}
.strip__n{{font-family:var(--fm);font-size:11px;color:var(--accent);letter-spacing:.06em}}
.strip__meas{{font-family:var(--fm);font-size:11px;color:var(--steel);margin-left:auto}}
.strip__meas.none{{color:var(--bad)}}
.legend{{display:flex;flex-wrap:wrap;gap:.35rem .9rem;margin:.6rem 0 .2rem;align-items:center;
font-family:var(--fm);font-size:11px;color:var(--ink-3)}}
.lg{{display:inline-flex;align-items:center;gap:.4rem}}
.lg i{{width:13px;height:9px;border:1px solid;border-radius:2px;display:inline-block}}
.lg i.lg--line{{height:3px;border:0;width:16px}}
.lg i.lg--dashed{{border-style:dashed}}
.lg i.lg--ring{{background:none;border-width:2px;border-radius:3px}}
.lg--gap{{width:.6rem}}
.key{{font-size:12.5px;color:var(--ink-3);max-width:62rem;margin:.2rem 0 0}}
.ixgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(22rem,1fr));gap:.6rem 1.4rem}}
.ixgroup{{min-width:0}}
.region{{font-family:var(--fm);font-size:11px;letter-spacing:.12em;text-transform:uppercase;
color:var(--ink-3);margin:.6rem 0 .3rem;padding-bottom:.3rem;border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:.5rem}}
.region i{{font-style:normal;color:var(--ink-3);width:1.4em;text-align:center;
border:1px solid var(--line-2);border-radius:50%;font-size:11px;line-height:1.4em}}
.ix{{display:flex;width:100%;align-items:center;gap:.6rem;min-height:30px;padding:.15rem .4rem;
background:none;border:0;border-radius:5px;text-align:left;cursor:pointer;font-size:13px}}
.ix:hover{{background:var(--raised)}}
.ix__plain{{color:var(--ink);flex:1 1 auto;min-width:0;line-height:1.3}}
.ix code{{color:var(--ink-3);font-size:11px;white-space:nowrap}}
.chip{{font-family:var(--fm);font-size:11px;letter-spacing:.05em;padding:.1rem .4rem;
border-radius:3px;background:var(--raised);color:var(--ink-3);white-space:nowrap;
border:1px solid transparent}}
.chip--built{{color:var(--good)}}
.chip--partial{{color:var(--warn)}}
.chip--planned,.chip--actor{{background:none;border-color:var(--line-2)}}
.chip--planned{{border-style:dashed}}
.rules{{padding-left:1.8rem;font-size:13px;color:var(--ink-2);max-width:70rem;margin:0}}
.rules li{{margin:0 0 .45rem;padding-left:.3rem}}
.rules li::marker{{font-family:var(--fm);color:var(--violet)}}
.rules li:target{{outline:2px solid var(--accent);outline-offset:4px}}
.rules__t{{color:var(--ink)}}
.governs{{display:block;margin-top:.1rem}}
.gv{{appearance:none;background:none;border:0;padding:0 .25rem;min-height:24px;
font-family:var(--fm);font-size:11px;color:var(--ink-3);cursor:pointer}}
.gv:hover{{color:var(--accent)}}
.foot{{padding:1rem 1.4rem 2rem;font-size:11.5px;color:var(--ink-3);
border-top:1px solid var(--line)}}
@media (max-width:1000px){{
.drawer{{position:static;width:auto;max-width:none;margin:.6rem 0 0;pointer-events:auto}}
.drawer__in{{position:static;max-height:none;box-shadow:0 0 0 1px var(--line-2)}}
}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important;animation:none!important}}}}
"""

JS = r"""
(function(){
  var svg = document.getElementById('schematic');
  if(!svg || !svg.atlas){ return; }
  var A = svg.atlas;
  var panel = document.getElementById('panel');
  var drawer = document.getElementById('drawer');
  var stage = document.getElementById('stage');
  var lstrip = document.getElementById('lstrip');
  var LAY = {};
  A.layers.forEach(function(l){ LAY[l.id] = l; });
  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function all(sel, root){
    return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // ---- the strip above the map: the active layer and what it touches ----
  function layerStrip(){
    if(!lstrip || cur.j >= 0){ return; }
    var L = A.state.layer, h = '';
    if(L === 'all'){
      h += '<span class="lstrip__l">All layers</span>';
      h += '<span class="lstrip__q">Every flow at once, each in the colour of its layer.</span>';
      h += '<span class="lstrip__row">';
      A.layers.forEach(function(l){
        h += '<button type="button" data-pick="' + esc(l.id) + '" title="' + esc(l.question)
           + '" style="border-bottom:2px solid ' + l.colour + '">' + esc(l.label) + '</button>';
      });
      h += '</span>';
    } else {
      var l = LAY[L], ids = {}, list = [];
      A.edges.forEach(function(e){
        if(e.layer === L){ ids[e.from] = true; ids[e.to] = true; } });
      Object.keys(A.detail).forEach(function(id){
        if(id !== '_meta' && ids[id]){ list.push(id); } });
      h += '<span class="lstrip__l" style="--c:' + l.colour + '"><i></i>' + esc(l.label)
         + ' layer</span>';
      h += '<span class="lstrip__q">' + esc(l.question) + '</span>';
      h += '<span class="lstrip__s">' + esc(l.sub) + '. ' + list.length
         + ' components; click one.</span>';
      h += '<span class="lstrip__row">';
      list.forEach(function(id){
        h += '<button type="button" data-go="' + esc(id) + '">' + esc(id) + '</button>'; });
      h += '</span>';
    }
    lstrip.innerHTML = h;
    all('[data-go]', lstrip).forEach(function(b){
      b.addEventListener('click', function(){ A.select(b.dataset.go); });
    });
    all('[data-pick]', lstrip).forEach(function(b){
      b.addEventListener('click', function(){ setLayer(b.dataset.pick); });
    });
  }

  // ---- layer switch -----------------------------------------------------
  var layerBtns = all('[data-layer-btn]');
  function setLayer(id){
    A.setLayer(id);
    layerBtns.forEach(function(b){
      b.setAttribute('aria-pressed', b.dataset.layerBtn === id ? 'true' : 'false'); });
    layerStrip();
  }
  layerBtns.forEach(function(b){
    b.addEventListener('click', function(){ setLayer(b.dataset.layerBtn); }); });

  // ---- today / end state -----------------------------------------------
  var stateBtns = all('[data-endstate]');
  stateBtns.forEach(function(b){
    b.addEventListener('click', function(){
      var on = b.dataset.endstate === '1';
      A.setEndstate(on);
      stateBtns.forEach(function(x){
        var mine = (x.dataset.endstate === '1') === on;
        x.setAttribute('aria-pressed', mine ? 'true' : 'false'); });
    });
  });

  // ---- the drawer: opens on selection, docks away from the node ---------
  function openDrawer(id, instant){
    // The figure has already framed the neighbourhood; the card's position
    // in that view picks the side. The drawer then covers that side of the
    // stage, so the neighbourhood is framed again into the part it leaves.
    if(!drawer){ return; }
    drawer.dataset.dock = A.view.fracOf(id) > 0.6 ? 'left' : 'right';
    drawer.hidden = false;
    var over = window.innerWidth > 1000 ? drawer.offsetWidth + 12 : 0;
    A.view.frameFocus(drawer.dataset.dock === 'left' ? {left:over} : {right:over}, instant);
    reveal();
  }
  function closeDrawer(){ if(drawer){ drawer.hidden = true; } }
  function reveal(){
    // The page scrolls so the map is on screen; the map itself has moved.
    if(!stage){ return; }
    var s = stage.getBoundingClientRect();
    if(s.top < 0 || s.top > window.innerHeight - 200){
      document.getElementById('map').scrollIntoView({block:'start'});
    }
  }
  var closeBtn = document.getElementById('drawerclose');
  if(closeBtn){ closeBtn.addEventListener('click', function(){ A.clear(); }); }

  // ---- zoom: Fit and 100% are the named states; + and - step by 1.25 ----
  var zoomBtns = all('[data-zoom]'), zpct = document.getElementById('zpct');
  zoomBtns.forEach(function(b){
    b.addEventListener('click', function(){
      var z = b.dataset.zoom;
      if(z === 'fit'){ A.view.fit(); }
      else if(z === 'actual'){ A.view.actual(); }
      else { A.view.zoomBy(z === 'in' ? 1.25 : 1 / 1.25); }
    });
  });
  function showZoom(zoom, fit){
    zoomBtns.forEach(function(b){
      if(b.dataset.zoom === 'fit'){ b.setAttribute('aria-pressed', fit ? 'true' : 'false'); }
      if(b.dataset.zoom === 'actual'){
        b.setAttribute('aria-pressed', Math.abs(zoom - 1) < 0.01 ? 'true' : 'false'); }
    });
    if(zpct){ zpct.textContent = Math.round(zoom * 100) + '%'; }
  }
  svg.addEventListener('atlas:view', function(e){ showZoom(e.detail.zoom, e.detail.fit); });

  // ---- journeys ---------------------------------------------------------
  var sel = document.getElementById('journey');
  var prev = document.getElementById('jprev'), next = document.getElementById('jnext');
  var count = document.getElementById('jcount');
  var strip = document.getElementById('strip');
  var stripN = document.getElementById('stripn'), stripSay = document.getElementById('stripsay');
  var stripMeas = document.getElementById('stripmeas');
  var cur = {j:-1, s:0};
  function journeyStrip(j){
    // The strip during a journey: every step as the edge it traces, the
    // current one lit, each one a jump. The sentence lives under the map.
    if(!lstrip){ return; }
    var h = '<span class="lstrip__l" style="--c:var(--accent)"><i></i>journey</span>'
          + '<span class="lstrip__q">' + esc(j.label) + '</span><span class="lstrip__row">';
    j.steps.forEach(function(s, k){
      var e = A.edges[s.edge] || {from:'', to:'', art:''};
      h += '<button type="button" class="' + (k === cur.s ? 'on' : '') + '" data-step="' + k
         + '" title="' + esc(e.art) + '"><b>' + (k + 1) + '</b>' + esc(e.from) + '<i>to</i>'
         + esc(e.to) + '</button>';
    });
    lstrip.innerHTML = h + '</span>';
    all('[data-step]', lstrip).forEach(function(b){
      b.addEventListener('click', function(){ cur.s = +b.dataset.step; showStep(); });
    });
  }
  function showStep(){
    var j = A.journeys[cur.j];
    if(!j){ return; }
    var step = j.steps[cur.s];
    A.setJourney(step);
    closeDrawer();
    journeyStrip(j);
    if(strip){
      strip.hidden = false;
      stripN.textContent = (cur.s + 1) + ' / ' + j.steps.length;
      stripSay.textContent = step.say;
      var m = step.measures || [];
      stripMeas.textContent = m.length ? 'measured by ' + m.join(', ')
        : 'nothing measures this step';
      stripMeas.classList.toggle('none', !m.length);
    }
    if(count){ count.textContent = (cur.s + 1) + '/' + j.steps.length; }
    prev.disabled = cur.s === 0; next.disabled = cur.s >= j.steps.length - 1;
  }
  function endJourney(){
    cur.j = -1; cur.s = 0;
    if(strip){ strip.hidden = true; }
    if(count){ count.textContent = ''; }
    prev.disabled = true; next.disabled = true;
    if(sel){ sel.value = ''; }
    layerStrip();
  }
  function startJourney(k){
    cur.j = k; cur.s = 0;
    showStep();
    document.getElementById('map').scrollIntoView({block:'start'});
  }
  if(sel){ sel.addEventListener('change', function(){
    if(sel.value === ''){ endJourney(); A.setJourney(null); }
    else { startJourney(+sel.value); }
  }); }
  function stepBy(d){
    var j = A.journeys[cur.j];
    if(!j){ return false; }
    var s = cur.s + d;
    if(s < 0 || s >= j.steps.length){ return true; }
    cur.s = s; showStep();
    return true;
  }
  if(prev){ prev.addEventListener('click', function(){ stepBy(-1); }); }
  if(next){ next.addEventListener('click', function(){ stepBy(1); }); }

  // ---- keyboard ---------------------------------------------------------
  document.addEventListener('keydown', function(e){
    var t = e.target, tag = t && t.tagName;
    if(tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA'){ return; }
    if(e.key === 'Escape'){
      if(cur.j >= 0){ endJourney(); }
      A.clear();
      A.view.back();
      e.preventDefault();
    } else if(e.key === 'ArrowRight' || e.key === 'ArrowLeft'){
      if(cur.j >= 0 && stepBy(e.key === 'ArrowRight' ? 1 : -1)){ e.preventDefault(); }
    }
  });

  // ---- selection, hash, index ------------------------------------------
  svg.addEventListener('atlas:select', function(e){
    if(cur.j >= 0){ endJourney(); }
    var id = e.detail.id;
    openDrawer(id);
    if(location.hash !== '#' + id){ history.replaceState(null, '', '#' + id); }
  });
  svg.addEventListener('atlas:clear', function(){
    closeDrawer();
    if(location.hash){ history.replaceState(null, '', location.pathname + location.search); }
  });
  all('.ix[data-go], .gv[data-go]').forEach(function(b){
    b.addEventListener('click', function(){
      A.select(b.dataset.go);
      document.getElementById('map').scrollIntoView({behavior:'smooth', block:'start'});
    });
  });
  window.addEventListener('resize', function(){ showZoom(A.view.zoom(), A.view.isFit()); });

  setLayer('work');
  showZoom(A.view.zoom(), A.view.isFit());
  if(A.state.focus){ openDrawer(A.state.focus, true); }
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
        "--check",
        action="store_true",
        help="exit 1 when the committed page differs from what would be rendered",
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
    # A committed text file ends with a newline, and end-of-file-fixer enforces
    # that on every file in the repo. Emitting it here is what stops that hook
    # and this --check contradicting each other. Measured: without it the hook
    # appended the newline and --check then called the page stale on every run,
    # with refresh.sh stripping it straight back off.
    page = build(atlas, ch) + "\n"

    if args.check:
        current = out.read_text(encoding="utf-8") if out.is_file() else ""
        if current != page:
            print(
                f"{out} is stale: it differs from what render_html.py would write.\n"
                "run: scripts/atlas/refresh.sh, then commit docs/atlas/",
                file=sys.stderr,
            )
            return 1
        print(f"{out} is current")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    size = out.stat().st_size
    print(f"wrote {out} ({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
