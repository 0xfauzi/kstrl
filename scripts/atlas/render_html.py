"""Render the system atlas as one page that teaches the system in layers.

The page is the map. Above it, a layer switch (one map, seven readings), a
today/end-state toggle, and the journeys a reader can step through. Beside
it, the focus panel: click a component and the panel leads with its plain
word, draws the relationship wheel, and reads the sentence for whichever
spoke the reader touches. Below it, a one-line index of every component by
region and the invariants. Nothing about the code is shown beyond the single
"lives in" line; the counts stay in atlas.json for the change detector.

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
from logical_model import COMPONENTS, FLOWS, GOVERNED_BY, INVARIANTS, REGIONS, layout_problems
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
            f'<i></i>{esc(layer["label"])}</button>'
        )
    o.append(
        '<button type="button" class="seg__b" data-layer-btn="all" aria-pressed="false">'
        "All</button>"
    )
    o.append("</div></div>")
    o.append('<p class="question" id="question"></p>')
    o.append('<div class="ctl ctl--row">')
    o.append('<div class="ctl"><span class="ctl__k">Show</span>')
    o.append('<div class="seg" role="group" aria-label="Build state">')
    o.append(
        '<button type="button" class="seg__b" data-endstate="0" aria-pressed="true">'
        "Today</button>"
        '<button type="button" class="seg__b" data-endstate="1" aria-pressed="false">'
        "End state</button></div></div>"
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

    # ---------------- the map ----------------
    o.append('<div class="mapwrap">')
    o.append('<div class="mapcol">')
    o.append(f'<div class="stage" id="stage">{system_svg}</div>')
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
        "clear; arrow keys step a journey.</p>"
    )
    o.append("</div>")  # mapcol
    o.append('<aside class="atlas-panel side" id="panel" aria-live="polite"></aside>')
    o.append("</div>")  # mapwrap
    o.append("</section>")

    # ---------------- index by region ----------------
    o.append('<section class="list" id="components"><h2>Components <span>by region; '
             "click one to focus it</span></h2>")
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
    o.append('<section class="list" id="invariants"><h2>Invariants <span>the rules the '
             "chips in the panel point at</span></h2>")
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
.main{{padding:.8rem 1.4rem 3rem;max-width:1560px;margin:0 auto}}
h2{{font-size:15px;font-weight:600;margin:1.8rem 0 .5rem;letter-spacing:-.01em}}
h2 span{{color:var(--ink-3);font-weight:400;font-size:12.5px;margin-left:.6rem}}
.controls{{display:flex;flex-direction:column;gap:.45rem;margin:.4rem 0 .6rem}}
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
.question{{margin:0;font-size:14.5px;color:var(--ink);padding-left:4.3rem}}
select#journey{{font:inherit;font-size:12.5px;color:var(--ink);background:var(--surface);
border:1px solid var(--line-2);border-radius:6px;min-height:30px;padding:0 .5rem;
max-width:22rem}}
.jb{{appearance:none;background:var(--surface);border:1px solid var(--line-2);border-radius:6px;
min-height:30px;padding:0 .7rem;font-size:12.5px;color:var(--ink-2);cursor:pointer}}
.jb:hover:not(:disabled){{color:var(--ink);background:var(--raised)}}
.jb:disabled{{opacity:.4;cursor:default}}
.jcount{{font-family:var(--fm);font-size:11.5px;color:var(--ink-3);min-width:3.2rem;
text-align:center}}
.mapwrap{{display:grid;grid-template-columns:minmax(0,1fr) 25rem;gap:1rem;align-items:start}}
.mapcol{{min-width:0}}
.stage{{background:var(--surface);border:1px solid var(--line);border-radius:8px;
padding:.4rem;overflow:auto}}
.stage svg{{width:100%;height:auto;display:block}}
.side{{position:sticky;top:.8rem;max-height:calc(100vh - 1.6rem);overflow-y:auto}}
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
.empty{{font-size:13px}}
.empty__l{{font-family:var(--fm);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
color:var(--ink-3);margin:0 0 .2rem;display:flex;align-items:center;gap:.5rem}}
.empty__l i{{width:16px;height:3px;border-radius:2px;background:var(--c,var(--ink-3))}}
.empty__q{{margin:0 0 .2rem;font-size:15px;color:var(--ink);font-weight:600;line-height:1.3}}
.empty__s{{margin:0 0 .7rem;color:var(--ink-3)}}
.empty__row{{display:flex;flex-wrap:wrap;gap:.3rem}}
.empty__row button{{appearance:none;background:var(--raised);border:1px solid var(--line);
border-radius:4px;min-height:24px;padding:0 .45rem;font-family:var(--fm);font-size:11px;
color:var(--ink-2);cursor:pointer}}
.empty__row button:hover{{color:var(--accent);border-color:var(--accent)}}
.jlist{{margin:0;padding:0;list-style:none}}
.jlist li{{display:flex;gap:.6rem;align-items:baseline;padding:.3rem .4rem;min-height:24px;
border-left:3px solid transparent;cursor:pointer;font-size:12.5px;color:var(--ink-3)}}
.jlist li:hover{{background:var(--raised)}}
.jlist li.on{{border-left-color:var(--accent);background:var(--raised);color:var(--ink)}}
.jlist__n{{font-family:var(--fm);font-size:11px;min-width:1.4em;text-align:right;color:var(--ink-3)}}
.jlist li.on .jlist__n{{color:var(--accent)}}
.jlist__e b{{font-family:var(--fm);font-weight:600;font-size:11.5px}}
.jlist__e i{{font-style:normal;color:var(--ink-3)}}
.empty__all{{margin:0;padding:0;list-style:none}}
.empty__all li{{display:flex;gap:.6rem;align-items:baseline;padding:.3rem 0;
border-bottom:1px solid var(--line);cursor:pointer}}
.empty__all li:last-child{{border-bottom:0}}
.empty__all li:hover b{{color:var(--accent)}}
.empty__all i{{width:14px;height:3px;flex:none;border-radius:2px;position:relative;top:-3px}}
.empty__all b{{font-weight:600;color:var(--ink);white-space:nowrap;font-size:12.5px}}
.empty__all span{{color:var(--ink-3);font-size:12.5px}}
@media (max-width:68rem){{
.mapwrap{{grid-template-columns:1fr}}
.side{{position:static;max-height:none}}
.question{{padding-left:0}}
}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important;animation:none!important}}}}
"""

JS = r"""
(function(){
  var svg = document.getElementById('schematic');
  if(!svg || !svg.atlas){ return; }
  var A = svg.atlas;
  var panel = document.getElementById('panel');
  var question = document.getElementById('question');
  var LAY = {};
  A.layers.forEach(function(l){ LAY[l.id] = l; });
  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

  // ---- the empty panel: the active layer, and what it touches ----------
  function emptyPanel(){
    if(!panel || A.state.focus || A.state.journey){ return; }
    var L = A.state.layer, h = '<div class="empty">';
    if(L === 'all'){
      h += '<p class="empty__l">Seven layers</p><ul class="empty__all">';
      A.layers.forEach(function(l){
        h += '<li data-pick="' + esc(l.id) + '"><i style="background:' + l.colour + '"></i>'
           + '<div><b>' + esc(l.label) + '</b> <span>' + esc(l.question) + '</span></div></li>';
      });
      h += '</ul>';
    } else {
      var l = LAY[L], ids = {}, list = [];
      A.edges.forEach(function(e){
        if(e.layer === L){ ids[e.from] = true; ids[e.to] = true; } });
      Object.keys(A.detail).forEach(function(id){
        if(id !== '_meta' && ids[id]){ list.push(id); } });
      h += '<p class="empty__l" style="--c:' + l.colour + '"><i></i>' + esc(l.label)
         + ' layer</p>';
      h += '<p class="empty__q">' + esc(l.question) + '</p>';
      h += '<p class="empty__s">' + esc(l.sub) + '. ' + list.length
         + ' components; click one.</p>';
      h += '<div class="empty__row">';
      list.forEach(function(id){
        h += '<button type="button" data-go="' + esc(id) + '">' + esc(id) + '</button>'; });
      h += '</div>';
    }
    panel.innerHTML = h + '</div>';
    Array.prototype.slice.call(panel.querySelectorAll('[data-go]')).forEach(function(b){
      b.addEventListener('click', function(){ A.select(b.dataset.go); });
    });
    Array.prototype.slice.call(panel.querySelectorAll('[data-pick]')).forEach(function(li){
      li.addEventListener('click', function(){ setLayer(li.dataset.pick); });
    });
  }

  // ---- layer switch -----------------------------------------------------
  var layerBtns = Array.prototype.slice.call(document.querySelectorAll('[data-layer-btn]'));
  function setLayer(id){
    A.setLayer(id);
    layerBtns.forEach(function(b){
      b.setAttribute('aria-pressed', b.dataset.layerBtn === id ? 'true' : 'false'); });
    if(question){
      question.textContent = id === 'all' ? 'Every flow at once, each in the colour of its layer.'
        : (LAY[id] ? LAY[id].question : '');
    }
    emptyPanel();
  }
  layerBtns.forEach(function(b){
    b.addEventListener('click', function(){ setLayer(b.dataset.layerBtn); }); });

  // ---- today / end state -----------------------------------------------
  var stateBtns = Array.prototype.slice.call(document.querySelectorAll('[data-endstate]'));
  stateBtns.forEach(function(b){
    b.addEventListener('click', function(){
      var on = b.dataset.endstate === '1';
      A.setEndstate(on);
      stateBtns.forEach(function(x){
        var mine = (x.dataset.endstate === '1') === on;
        x.setAttribute('aria-pressed', mine ? 'true' : 'false'); });
    });
  });

  // ---- journeys ---------------------------------------------------------
  var sel = document.getElementById('journey');
  var prev = document.getElementById('jprev'), next = document.getElementById('jnext');
  var count = document.getElementById('jcount');
  var strip = document.getElementById('strip');
  var stripN = document.getElementById('stripn'), stripSay = document.getElementById('stripsay');
  var stripMeas = document.getElementById('stripmeas');
  var cur = {j:-1, s:0};
  function journeyPanel(j){
    // The panel during a journey: every step as the edge it traces, the
    // current one lit, each one a jump. The sentence lives in the strip.
    if(!panel){ return; }
    var h = '<div class="empty"><p class="empty__l"><i style="background:var(--accent)"></i>'
          + 'journey</p><p class="empty__q">' + esc(j.label) + '</p><ol class="jlist">';
    j.steps.forEach(function(s, k){
      var e = A.edges[s.edge] || {from:'', to:'', art:''};
      h += '<li class="' + (k === cur.s ? 'on' : '') + '" data-step="' + k + '" tabindex="0">'
         + '<span class="jlist__n">' + (k + 1) + '</span>'
         + '<span class="jlist__e"><b>' + esc(e.from) + '</b> <i>' + esc(e.art) + '</i> <b>'
         + esc(e.to) + '</b></span></li>';
    });
    panel.innerHTML = h + '</ol></div>';
    Array.prototype.slice.call(panel.querySelectorAll('[data-step]')).forEach(function(li){
      function go(){ cur.s = +li.dataset.step; showStep(); }
      li.addEventListener('click', go);
      li.addEventListener('keydown', function(e){ if(e.key === 'Enter'){ go(); } });
    });
  }
  function showStep(){
    var j = A.journeys[cur.j];
    if(!j){ return; }
    var step = j.steps[cur.s];
    A.setJourney(step);
    journeyPanel(j);
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
  }
  function startJourney(k){
    cur.j = k; cur.s = 0;
    showStep();
    document.getElementById('map').scrollIntoView({block:'start'});
  }
  if(sel){ sel.addEventListener('change', function(){
    if(sel.value === ''){ endJourney(); A.setJourney(null); emptyPanel(); }
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
      e.preventDefault();
    } else if(e.key === 'ArrowRight' || e.key === 'ArrowLeft'){
      if(cur.j >= 0 && stepBy(e.key === 'ArrowRight' ? 1 : -1)){ e.preventDefault(); }
    }
  });

  // ---- selection, hash, index ------------------------------------------
  svg.addEventListener('atlas:select', function(e){
    if(cur.j >= 0){ endJourney(); }
    var id = e.detail.id;
    if(location.hash !== '#' + id){ history.replaceState(null, '', '#' + id); }
  });
  svg.addEventListener('atlas:clear', function(){
    if(location.hash){ history.replaceState(null, '', location.pathname + location.search); }
    emptyPanel();
  });
  var goers = document.querySelectorAll('.ix[data-go], .gv[data-go]');
  Array.prototype.slice.call(goers).forEach(function(b){
    b.addEventListener('click', function(){
      A.select(b.dataset.go);
      document.getElementById('map').scrollIntoView({behavior:'smooth', block:'start'});
    });
  });

  setLayer('work');
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
    page = build(atlas, ch)

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
