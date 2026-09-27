"""The kstrl app as one clickable page: every screen drawn at one moment (search being built, 21:40, ≥$31.10 today),
joined into a single HTML file with the fonts, the tokens and the bundle inlined.

It reads the frames as their generators drew them (out/static, written by build_all.sh) and is built from nothing
else, so a screen in the prototype is the frame, scoped. LINKS is the whole navigation: a control whose screen is not
drawn is left out of it and does nothing. Output: ../prototype/kstrl-prototype.html, the page body the Artifact tool
publishes (it adds the doctype, head and body)."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from beh import SEG, TABS
from wire_frames import REF, scoped, unique_ids

STATIC = Path('out/static')
SYSTEM = Path('../system/project')
OUT = Path('../prototype/kstrl-prototype.html')
MOMENT = 'search being built · 21:40 · ≥$31.10 today'

# ---------------------------------------------------------------------------------------------------------------- screens
# id: (frame drawn by a generator, where it sits, name in the index)
SCREENS: dict[str, tuple[str, str, str]] = {
    'factory':          ('Map0Factory', 'The map', 'Factory'),
    'spec-graph':       ('Map1Spec', 'The map', 'Spec: search, as a graph'),
    'spec-text':        ('Map4Plan', 'The map', 'Spec: search, as text'),
    'part-search-query': ('Map2Part', 'The map', 'Part: search-query'),
    'part-search-schema': ('ProtoPart-search-schema', 'The map', 'Part: search-schema'),
    'part-search-index': ('ProtoPart-search-index', 'The map', 'Part: search-index'),
    'part-search-rank': ('ProtoPart-search-rank', 'The map', 'Part: search-rank'),
    'part-search-highlight': ('ProtoPart-search-highlight', 'The map', 'Part: search-highlight'),
    'part-search-api': ('ProtoPart-search-api', 'The map', 'Part: search-api'),
    'part-search-cli': ('ProtoPart-search-cli', 'The map', 'Part: search-cli'),
    'step-query-review-2': ('Map3Reviewer', 'The map', 'Step: search-query, review on try 2'),
    'step-stage':       ('ProtoStage', 'The map', 'Step: search being built, Stage'),
    'step-grid':        ('ProtoGrid', 'The map', 'Step: search being built, Grid'),
    'agent-search-rank': ('ProtoAgent-search-rank', 'The map', 'Step: search-rank, its engineer'),
    'inbox':            ('Inbox', 'Pages', 'Inbox'),
    'queue':            ('Queue', 'Pages', 'Queue'),
    'trust':            ('Trust', 'Pages', 'Trust'),
    'learning':         ('Learning', 'Pages', 'Learning'),
    'settings':         ('Settings', 'Pages', 'Settings'),
    'notify-one':       ('Notify2Paths', 'Pages', 'Notifications: one event'),
    'notify-every':     ('Notify3Channels', 'Pages', 'Notifications: every event'),
}
# overlay: (the frame it is drawn in, the frame drawn under it)
OVERLAYS = {'approve': ('Map6Approve', 'Map1Spec'), 'question': ('Map5Question', 'Map1Spec')}
START = 'factory'

# ---------------------------------------------------------------------------------------------------------------- links
# Per screen: zoom (four screens, or None where the level has no zoom), level, tab (its own view), then every other
# link as (kind, selector within the screen, nth match, target, label). kinds: crumb, tab, need, ask, open. A target
# is a screen or an overlay. A label names a link that is not a button already (a tile, a card, a cell).
PARTS = ['search-schema', 'search-index', 'search-query', 'search-rank', 'search-highlight', 'search-api', 'search-cli']
# The zoom's Part is the part you were last on (search-query, selected on the Spec level, until you open another).
# Its Step is, from a part, that part's open step where one is drawn (where none is, Step does nothing), and from the
# Factory or the Spec level the Step level of the whole spec: Stage or Grid, whichever you left.
ZOOM = ['factory', '@spec', '@part', '@step']
ZOOM_TOP = ['factory', '@spec', '@part', '@stage']
STEP_OF = {'part-search-query': 'step-query-review-2', 'part-search-rank': 'agent-search-rank'}
# a step screen belongs to a part: showing it makes that part the zoom's Part
PART_OF = {'step-query-review-2': 'part-search-query', 'agent-search-rank': 'part-search-rank'}
STAGE = ('crumb', '.k-header .k-crumb', 2, '@stage', None)
NEED = ('need', '.k-needs .k-need', 0, 'approve', None)
STOPPED = ('open', '.k-needs .k-need', 1, 'part-search-highlight', None)
ROOT = ('crumb', '.k-header .k-crumb', 0, 'factory', None)
SPEC = ('crumb', '.k-header .k-crumb', 1, '@spec', None)
ASK = ('ask', '.k-header .k-ask', 0, 'question', None)
BAND = [NEED, STOPPED]


def part_links(extra: list) -> dict:
    return {'level': 2, 'zoom': ZOOM, 'links': [ROOT, SPEC] + BAND + extra}


LINKS: dict[str, dict] = {
    'factory': {'level': 0, 'zoom': ZOOM_TOP, 'links': BAND + [
        ('open', '.k-tile.hero', 0, 'spec-graph', 'Open search, the spec being built'),
        ('open', '.k-tile.nx', 0, 'queue', 'Open the queue'),
        ('open', '.k-tile.c', 0, 'trust', 'Open Trust'),
        ('open', '.k-tile.c', 3, 'learning', 'Open Learning'),
    ]},
    'spec-graph': {'level': 1, 'zoom': ZOOM_TOP, 'tab': 0, 'select': '.pc.k-card', 'links': [ROOT] + BAND + [ASK,
        ('tab', '.k-titletools .k-tab', 1, 'spec-text', None)] +
        [('open', '.pc.k-card', n, f'part-{p}', None) for n, p in enumerate(PARTS)]},
    'spec-text': {'level': 1, 'zoom': ZOOM_TOP, 'tab': 1, 'links': [ROOT] + BAND + [ASK,
        ('tab', '.k-titletools .k-tab', 0, 'spec-graph', None)] +
        [('open', '.pts .pt', n, f'part-{p}', f'Open {p}') for n, p in enumerate(PARTS)]},
    'part-search-query': part_links([
        ('open', '.cell.k-tile-selected', 0, 'step-query-review-2', 'Open try 2, review'),
        ('open', '.steps .k-button', 2, 'spec-text', None)]),
    'part-search-schema': part_links([]),
    'part-search-index': part_links([('window', '.steps .k-button', 0, 'approve', None)]),
    'part-search-rank': part_links([('open', '.steps .k-button', 0, 'agent-search-rank', None)]),
    'part-search-highlight': part_links([]),
    'part-search-api': part_links([
        ('open', '.steps .k-button', 0, 'part-search-index', None),
        ('open', '.steps .k-button', 1, 'part-search-query', None),
        ('open', '.steps .k-button', 2, 'part-search-rank', None)]),
    'part-search-cli': part_links([
        ('open', '.steps .k-button', 0, 'spec-text', None),
        ('open', '.steps .k-button', 1, 'part-search-highlight', None),
        ('open', '.steps .k-button', 2, 'part-search-api', None)]),
    'step-query-review-2': {'level': 3, 'zoom': ZOOM, 'links': [ROOT, SPEC] + BAND + [
        ('crumb', '.k-header .k-crumb', 2, 'part-search-query', None)]},
    'step-stage': {'level': 3, 'zoom': ZOOM_TOP, 'tab': 0, 'links': [ROOT, SPEC] + BAND + [
        ('tab', '.k-titletools .k-tab', 1, 'step-grid', None),
        ('open', '.tiles > .k-tile', 0, 'agent-search-rank', 'Open search-rank’s engineer')] +
        [('open', '.rest .il', n, f'part-{p}', f'Open {p}') for n, p in enumerate(['search-schema', 'search-index', 'search-highlight', 'search-api', 'search-cli'])]},
    'step-grid': {'level': 3, 'zoom': ZOOM_TOP, 'tab': 1, 'links': [ROOT, SPEC] + BAND + [
        ('tab', '.k-titletools .k-tab', 0, 'step-stage', None),
        ('open', '.bento .ag', 0, 'agent-search-rank', 'Open search-rank’s engineer'),
        ('open', '.bento .ag', 2, 'part-search-index', 'Open search-index'),
        ('open', '.bento .ag', 3, 'part-search-highlight', 'Open search-highlight')] +
        [('open', '.wait .il', n, f'part-{p}', f'Open {p}') for n, p in enumerate(['search-schema', 'search-api', 'search-cli'])]},
    'agent-search-rank': {'level': 3, 'zoom': ZOOM, 'links': [ROOT, SPEC, STAGE] + BAND},
    'inbox': {'links': [ROOT]},
    'queue': {'links': [ROOT] + BAND},
    'trust': {'links': [ROOT] + BAND},
    'learning': {'links': [ROOT] + BAND},
    'settings': {'links': [ROOT] + BAND},
    'notify-one': {'tab': 0, 'links': [ROOT] + BAND + [('tab', '.k-titletools .k-tab', 1, 'notify-every', None)]},
    'notify-every': {'tab': 1, 'links': [ROOT] + BAND + [('tab', '.k-titletools .k-tab', 0, 'notify-one', None)]},
}
GROUPS = {'spec': ['spec-graph', 'spec-text'], 'stage': ['step-stage', 'step-grid'], 'part': [f'part-{p}' for p in ['search-query'] + [q for q in PARTS if q != 'search-query']]}


# ---------------------------------------------------------------------------------------------------------------- parts
def read_static(frame: str) -> dict[str, str]:
    s = (STATIC / f'{frame}.html').read_text()
    assert 'data-wired-frames' not in s, frame
    body = s[s.index('<body>') + len('<body>'):s.rindex('</body>')]
    sprite = re.search(r'<svg width="0" height="0".*?</svg>', body, re.S).group(0)
    rest = body.replace(sprite, '', 1).strip()
    assert rest.startswith('<div class="scr">') and rest.endswith('</div>'), frame
    return {'css': re.search(r'<style>(.*?)</style>', s, re.S).group(1), 'sprite': sprite,
            'inner': rest[len('<div class="scr">'):-len('</div>')]}


def tokens_css() -> str:
    """The tokens as the artifact viewer's themes: day on :root, night under a dark system setting unless the viewer
    chose light, and night again when the viewer chose dark. Fonts are the shipped files, inlined."""
    tok = json.loads((SYSTEM / 'tokens.json').read_text())
    day, night = (t['id'] for t in tok['color']['themes'])

    def theme(th: str) -> str:
        out = []
        for fam in ('color', 'shadow'):
            for t in tok.get(fam, {}).get('tokens', []):
                v = t['value'] if isinstance(t['value'], str) else t['value'].get(th, t['value'][day])
                if v.startswith('{') and v.endswith('}'):
                    v = f'var(--{v[1:-1]})'
                out.append(f'--{t["name"]}: {v};')
        return ' '.join(out)
    flat = [f'--{t["name"]}: {t["value"]};' for fam, body in tok.items()
            if isinstance(body, dict) and 'tokens' in body and fam not in ('color', 'shadow') for t in body['tokens']]
    flat += [f'--font-{k}: {v};' for k, v in tok['type']['families'].items()]
    faces = []
    for f in tok['type']['fonts']:
        data = base64.b64encode((SYSTEM / f['file']).read_bytes()).decode()
        faces.append(f"@font-face {{ font-family:'{f['family']}'; src:url(data:font/woff2;base64,{data}) format('woff2'); "
                     f"font-weight:{f['weight']}; font-style:{f.get('style', 'normal')}; }}")
    return '\n'.join(faces + [
        f':root {{ {theme(day)} {" ".join(flat)} color-scheme:light; }}',
        f'@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ {theme(night)} color-scheme:dark; }} }}',
        f':root[data-theme="dark"] {{ {theme(night)} color-scheme:dark; }}'])


PAGE_CSS = """
  body { background:var(--canvas); color:var(--text); font:var(--t-body); -webkit-font-smoothing:antialiased; }
  .proto { padding-block:16px 24px; padding-inline:16px; display:grid; grid-template-columns:minmax(0,1fr); gap:12px; justify-items:center; }
  .proto-bar { width:100%; max-width:1280px; display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; }
  .proto-name { font:var(--t-body); font-weight:600; }
  .proto-when { font:var(--t-small); color:var(--text-2); }
  .proto-bar .grow { flex:1; }
  .proto-go { display:inline-flex; align-items:center; gap:8px; font:var(--t-small); color:var(--text-2); }
  .proto-go select { font:var(--t-small); color:var(--text); background:var(--window); border:1px solid var(--line-input); border-radius:var(--radius-md); height:28px; padding:0 8px; }
  .proto-go select:focus-visible { outline:2px solid var(--focus); outline-offset:1px; }
  .proto-fit { width:100%; max-width:1280px; }
  .proto-stage { width:1280px; height:800px; transform-origin:0 0; position:relative; overflow:hidden; box-shadow:0 0 0 1px var(--line); }
  .proto-note { width:100%; max-width:1280px; font:var(--t-small); color:var(--text-3); margin:0; }
  /* one screen shows at a time; a window's box is the screen's while it is open */
  .scr[hidden], [data-overlay][hidden] { display:none; }
  [data-overlay] { display:contents; }
  /* a tile, card or cell that opens a screen: the system's own focus ring, and a pointer */
  [data-go] { cursor:pointer; }
  /* a link that is not a tile or a card (a part's name on the Spec level's text) takes the focus ring every control has */
  [data-go]:not(.k-tile):not(.k-card):focus-visible { outline:2px solid var(--focus); outline-offset:2px; border-radius:var(--radius-sm); }
"""

RUNTIME = r"""
var W=__W__;
var cur=null, hist=[], mem={}, ctl={}, open=null, opener=null, closing=false, said=document.querySelector('[data-said]');
for(var g in W.groups) mem[g]=W.groups[g][0];
function scr(id){ return document.querySelector('[data-screen="'+id+'"]'); }
function resolve(t){ if(t==='@step') return W.stepOf[mem.part]||null; return t && t.charAt(0)==='@' ? mem[t.slice(1)] : t; }
function remember(id){ for(var g in W.groups){ if(W.groups[g].indexOf(id)>=0) mem[g]=id; } if(W.partOf[id]) mem.part=W.partOf[id]; }
function nth(s, sel, n){ return s.querySelectorAll(sel)[n]||null; }
// A screen's controls are set up the first time it shows, when it has a layout to measure.
function init(id){
  if(ctl[id]) return ctl[id];
  var s=scr(id), c=W.screens[id], o={mute:false}; ctl[id]=o;
  if(c.zoom){ o.zoom=kSeg(s.querySelector('.k-seg[aria-label="Zoom level"]'), function(it){ if(o.mute) return;
    var j=o.zoom.items.indexOf(it); if(j===c.level) return; var t=resolve(c.zoom[j]);
    if(t && t!==id) go(t, {kind:'zoom', from:c.level, to:j});
    else if(!t){ o.mute=true; o.zoom.select(o.zoom.items[c.level], true, true); o.mute=false; } }); }
  var tl=s.querySelector('.k-titletools .k-tabs'), tabbed=c.links.filter(function(l){ return l[0]==='tab'; }).length;
  if(tl && tabbed===tl.querySelectorAll('.k-tab').length-1){ o.tabs=kTabs(tl, function(t){ if(o.mute) return;
    var j=o.tabs.tabs.indexOf(t), hit=c.links.filter(function(l){ return l[0]==='tab' && l[2]===j; })[0];
    if(hit) go(resolve(hit[3]), {kind:'tabs', from:c.tab, to:j}); }); }
  c.links.forEach(function(l){ var el=nth(s, l[1], l[2]); if(!el){ W.missing.push(id+': '+l[1]+' #'+l[2]); return; }
    if(l[0]==='crumb') el.addEventListener('click', function(){ go(resolve(l[3]), {kind:'crumb', index:l[2]}); });
    else if(l[0]==='need' || l[0]==='ask' || l[0]==='window') el.addEventListener('click', function(){ show(l[3], el); });
    else if(l[0]==='open'){ el.addEventListener('click', function(){ go(resolve(l[3]), {kind:'open', el:el}); }); } });
  // a screen whose cards are selected: focus and selection are one thing on the map (Tile), so focusing a card selects it
  if(c.select) s.addEventListener('focusin', function(e){ var k=e.target.closest && e.target.closest(c.select); if(k) choose(id, k); });
  return o; }
// move the selection to a card: its class, the one tab stop, and the ↵ that says what Enter does
function choose(id, card){ var s=scr(id), c=W.screens[id], all=[].slice.call(s.querySelectorAll(c.select)), was=all.filter(function(k){ return k.classList.contains('k-card-selected'); })[0];
  if(!card || card===was) return; var hint=was && was.querySelector('.k-card-meta .k-keys');
  all.forEach(function(k){ k.classList.toggle('k-card-selected', k===card); k.tabIndex=k===card?0:-1; });
  if(hint) card.querySelector('.k-card-meta').appendChild(hint); }
function settle(id){ var o=ctl[id], c=W.screens[id]; if(!o) return;
  if(o.zoom) o.zoom.select(o.zoom.items[c.level], false, true);
  if(o.tabs) o.tabs.select(o.tabs.tabs[c.tab], false, true); }
// Move to a screen. The control you used is in the new screen too (a zoom item, a tab, a crumb) and keeps focus;
// a tile or card you opened hands focus to the new screen's current crumb, which names where you now are.
function go(t, via, back){
  if(open || !t || t===cur) return;
  var from=cur; if(from){ settle(from); scr(from).hidden=true; if(!back) hist.push({id:from, el:via.el||document.activeElement}); }
  scr(t).hidden=false; cur=t; remember(t); var o=init(t); settle(t);
  var c0=W.screens[t]; if(c0.select){ var pick=c0.links.filter(function(l){ return l[0]==='open' && l[1]===c0.select && l[3]===mem.part; })[0]; if(pick) choose(t, nth(scr(t), pick[1], pick[2])); }
  if(via.kind==='zoom' && o.zoom){ o.zoom.place(o.zoom.items[via.from], false); o.mute=true; o.zoom.select(o.zoom.items[via.to], true); o.mute=false; }
  else if(via.kind==='tabs' && o.tabs){ o.tabs.place(o.tabs.tabs[via.from], false); o.mute=true; o.tabs.select(o.tabs.tabs[via.to], true); o.mute=false; }
  else if(via.kind==='crumb'){ nth(scr(t), '.k-header .k-crumb', via.index).focus(); }
  else if(via.kind==='open' || via.kind==='menu'){ var c=scr(t).querySelector('.k-header [aria-current="page"]'); if(c) c.focus(); }
  else if(via.kind==='back' && via.el && scr(t).contains(via.el)){ via.el.focus(); }
  said.textContent=W.screens[t].name; index.value=t;
  try{ history.replaceState(null, '', '#'+t); }catch(e){} }
function back(){ var h=hist.pop(); if(h) go(h.id, {kind:'back', el:h.el}, true); }
// The command window over the screen you are on (CommandWindow): the page under it is inert, Tab stays inside, esc
// or the scrim closes it, and focus goes back to what opened it, or to the ask field.
function ov(name){ return document.querySelector('[data-overlay="'+name+'"]'); }
function under(on){ [].forEach.call(scr(cur).children, function(ch){ if(!ch.hasAttribute('data-overlay')) ch.inert=on; }); }
function show(name, from){
  if(open && !closing){ var q0=ov(open).querySelector('.k-cmd-q'); q0.focus(); q0.select(); return; }
  var o=ov(name), s=scr(cur); if(o.parentNode!==s) s.appendChild(o);
  o.hidden=false; open=name; closing=false; opener=from||null; under(true);
  var win=o.querySelector('.k-cmd'), sc=o.querySelector('.k-scrim'); win.dataset.motion='enter'; sc.dataset.motion='enter';
  win.addEventListener('animationend', function(){ if(win.dataset.motion==='enter'){ delete win.dataset.motion; delete sc.dataset.motion; } }, {once:true});
  o.querySelector('.k-cmd-q').focus(); }
function hide(){
  if(!open || closing) return; closing=true;
  var o=ov(open), win=o.querySelector('.k-cmd'), sc=o.querySelector('.k-scrim'), done=false;
  win.dataset.motion='exit'; sc.dataset.motion='exit';
  var ms=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dur-exit'))||200;
  function end(){ if(done) return; done=true; o.hidden=true; delete win.dataset.motion; delete sc.dataset.motion; under(false); open=null; closing=false;
    var b=opener && document.contains(opener) && !opener.closest('[hidden]') ? opener : scr(cur).querySelector('.k-ask'); b.focus(); }
  win.addEventListener('animationend', end, {once:true}); setTimeout(end, ms+80); }
[].forEach.call(document.querySelectorAll('[data-overlay]'), function(o){
  o.querySelector('.k-scrim').addEventListener('click', hide);
  o.addEventListener('keydown', function(e){ if(e.key!=='Tab') return;
    var f=[].filter.call(o.querySelectorAll('input, button, [tabindex="0"]'), function(x){ return !x.closest('[hidden]') && !x.disabled; });
    var i=f.indexOf(document.activeElement); e.preventDefault(); f[(i + (e.shiftKey ? f.length-1 : 1) + f.length) % f.length].focus(); }); });
// Enter on a focused tile, card or cell opens it; esc closes a window, or else goes back to the screen before.
document.addEventListener('keydown', function(e){
  if(open && e.key==='Escape'){ e.preventDefault(); hide(); return; }
  if((e.metaKey||e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase()==='k'){
    var a=W.screens[cur].links.filter(function(l){ return l[0]==='ask'; })[0]; if(open){ e.preventDefault(); show(open); } else if(a){ e.preventDefault(); show(a[3], document.activeElement); } return; }
  if(open) return;
  var t=e.target, inField=t.closest && t.closest('input, textarea, select');
  if(e.key==='Enter' && t.hasAttribute && t.hasAttribute('data-go')){ e.preventDefault(); t.click(); }
  else if(e.key==='Escape' && !inField){ e.preventDefault(); back(); } });
// the index of screens: the prototype's own way to reach a screen the app would reach through something not drawn
var index=document.getElementById('proto-index');
index.addEventListener('change', function(){ go(index.value, {kind:'menu'}); });
document.getElementById('proto-back').addEventListener('click', back);
// the screen is 1280 by 800; narrower than that, it is scaled to fit rather than cropped. At full size it is snapped to
// whole pixels: centred, it can land on a half pixel, and then every 1px rule in it is drawn across two rows
var fit=document.querySelector('.proto-fit'), stage=document.querySelector('.proto-stage');
function scale(){ var k=Math.min(1, fit.clientWidth/1280); fit.style.height=(800*k)+'px';
  if(k<1){ stage.style.transform='scale('+k+')'; return; }
  var r=fit.getBoundingClientRect(), dx=Math.round(r.left)-r.left, dy=Math.round(r.top)-r.top;
  stage.style.transform=(dx||dy)?'translate('+dx+'px,'+dy+'px)':''; }
if(window.ResizeObserver){ var ro=new ResizeObserver(scale); ro.observe(fit); ro.observe(document.body); } scale();
var first=(location.hash||'').slice(1); cur=null; go(W.screens[first] ? first : W.start, {kind:'load'});
"""


def build() -> None:
    raw = {f: read_static(f) for f in {v[0] for v in SCREENS.values()} | {v for pair in OVERLAYS.values() for v in pair}}
    sprite = raw['Map0Factory']['sprite']
    css_parts, blocks = [], []
    for sid, (frame, _, _) in SCREENS.items():
        css, _ = scoped(raw[frame]['css'], f'[data-screen="{sid}"]')
        css_parts.append(f'/* {sid} */\n{css}')
        blocks.append(f'<div class="scr" data-screen="{sid}" hidden>{unique_ids(raw[frame]["inner"], sid)}</div>')
    for name, (frame, base) in OVERLAYS.items():
        f, b = raw[frame], raw[base]
        assert f['css'].startswith(b['css'].rstrip('\n ')) and f['inner'].startswith(b['inner']), name
        css, _ = scoped(f['css'][len(b['css'].rstrip('\n ')):], f'[data-overlay="{name}"]', drop_scr=True)
        css_parts.append(f'/* window: {name} */\n{css}')
        blocks.append(f'<div data-overlay="{name}" hidden>\n{f["inner"][len(b["inner"]):].strip()}\n</div>')
    # a tile, card or cell that opens a screen is focusable and named; a button is already both
    html = '\n'.join(blocks)
    screens = {}
    for sid, (frame, where, name) in SCREENS.items():
        c = LINKS[sid]
        screens[sid] = {'name': name, 'where': where, 'level': c.get('level'), 'tab': c.get('tab'), 'zoom': c.get('zoom'),
                        'select': c.get('select'), 'links': [list(l) for l in c['links']]}
        for target in [l[3] for l in c['links']] + list(c.get('zoom') or []):
            t = target[1:] if target.startswith('@') else target
            assert t in SCREENS or t in OVERLAYS or t in GROUPS or t == 'step', f'{sid}: {target} is not drawn'
    for step, part in PART_OF.items():
        assert step in SCREENS and part in SCREENS, (step, part)
    for part, step in STEP_OF.items():
        assert part in SCREENS and step in SCREENS, (part, step)
    # opening a tile: data-go and a name, applied at load (the frame's markup stays the frame's)
    w = {'start': START, 'groups': GROUPS, 'stepOf': STEP_OF, 'partOf': PART_OF, 'screens': screens, 'missing': []}
    options = []
    for where in ('The map', 'Pages'):
        opts = ''.join(f'<option value="{sid}">{n}</option>' for sid, (_, wh, n) in SCREENS.items() if wh == where)
        options.append(f'<optgroup label="{where}">{opts}</optgroup>')
    mark = r"""
  Object.keys(W.screens).forEach(function(id){ var s=scr(id); W.screens[id].links.forEach(function(l){ if(l[0]!=='open') return;
    var el=nth(s, l[1], l[2]); if(!el) return; el.setAttribute('data-go', l[3]);
    if(!/^(BUTTON|A)$/.test(el.tagName)){ el.tabIndex=0; el.setAttribute('role', 'link'); if(l[4]) el.setAttribute('aria-label', l[4]); } }); });
"""
    runtime = RUNTIME.replace('__W__', json.dumps(w, ensure_ascii=False))
    page = f'''<title>kstrl Prototype</title>
<style>
{tokens_css()}
{(SYSTEM / 'components/bundle.css').read_text()}
{PAGE_CSS}
{chr(10).join(css_parts)}
</style>
<div class="proto">
  <div class="proto-bar">
    <span class="proto-name">kstrl</span><span class="proto-when">{MOMENT}</span>
    <span class="grow"></span>
    <button class="k-button k-button-sm" id="proto-back" type="button">Back <span class="k-keys"><span class="k-key k-key-sm">esc</span></span></button>
    <label class="proto-go" for="proto-index">Screen <select id="proto-index">{"".join(options)}</select></label>
  </div>
  <div class="proto-fit"><div class="proto-stage">
{sprite}
{html}
  </div></div>
  <p class="proto-note">Every screen here is drawn at the same moment. A control whose screen is not drawn yet does nothing.</p>
  <div class="k-sr" aria-live="polite" data-said></div>
</div>
<script>
{SEG}
{TABS}
(document.fonts ? document.fonts.ready : Promise.resolve()).then(function(){{
{runtime}
}});
</script>
'''
    # mark the openable elements before the runtime's first go(): inserted right after W is defined
    page = page.replace("for(var g in W.groups) mem[g]=W.groups[g][0];", "for(var g in W.groups) mem[g]=W.groups[g][0];" + mark, 1)
    ids = re.findall(r'\bid="([^"]+)"', page)
    dup = {i for i in ids if ids.count(i) > 1}
    assert not dup, f'duplicate ids {dup}'
    for m in REF.finditer(page):
        assert m.group(2) in ids, f'{m.group(0)} points at nothing'
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page)
    print(f'{OUT}: {len(SCREENS)} screens, {len(OVERLAYS)} windows, {len(page)} bytes')


if __name__ == '__main__':
    build()
