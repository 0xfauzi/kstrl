"""Wire the frames that share a moment: their crumbs, zoom, views and Needs you move between screens that are drawn.

A frame is one screen at one moment. Screens drawn at the same moment (the same spend today, the same last event) form a
set. In every frame of a set, a control whose target is another screen of the set is wired; a control whose target is not
drawn at that moment stays static, because showing a different moment would move time as well as place. Each frame keeps
its own screen (and, for the two command-window frames, its open window) as its first state, so its picture is unchanged.

Runs last in build_all: it reads the generators' output and rewrites the frames of each set in place. The table below is
the whole wiring; every label in it is asserted against the markup, so a generator that renames a crumb fails the build."""
from __future__ import annotations
import json
import re
from pathlib import Path
from beh import SEG, TABS

C = Path('../system/project/components')
STATIC = Path('out/static')
MARK = 'data-wired-frames'

# ---------------------------------------------------------------------------------------------------------------- wiring
# zoom: four targets or None (static). crumbs, tabs, needs: (label, target) per control in order; target None is static.
# A target is a screen, an overlay (needs, ask), or '@group' for the screen of that group seen last (the Spec level's
# Graph or Text; the Step level's Stage or Grid), as the app would return you to the view you left.
A_ZOOM = ['Map0Factory', '@spec', 'Map2Part', 'Map3Reviewer']
A_NEEDS = [('Approve search-index', 'approve'), ('search-highlight stopped', None)]
SETS = [
  {'name': 'A: search being built, ≥$31.10 today, last event 3s ago',
   'groups': {'spec': ['Map1Spec', 'Map4Plan']},
   'screens': {
     'Map0Factory': {'say': 'Factory level: snippetvault', 'level': 0, 'zoom': A_ZOOM,
                     'crumbs': [('snippetvault', None)], 'needs': A_NEEDS},
     'Map1Spec': {'say': 'Spec level, as a graph: search', 'level': 1, 'zoom': A_ZOOM, 'tab': 0,
                  'crumbs': [('snippetvault', 'Map0Factory'), ('search', None)],
                  'tabs': [('Graph', None), ('Text', 'Map4Plan')], 'needs': A_NEEDS, 'ask': 'question'},
     'Map4Plan': {'say': 'Spec level, as text: search', 'level': 1, 'zoom': A_ZOOM, 'tab': 1,
                  'crumbs': [('snippetvault', 'Map0Factory'), ('search', None)],
                  'tabs': [('Graph', 'Map1Spec'), ('Text', None)], 'needs': A_NEEDS, 'ask': 'question'},
     'Map2Part': {'say': 'Part level: search-query', 'level': 2, 'zoom': A_ZOOM,
                  'crumbs': [('snippetvault', 'Map0Factory'), ('search', '@spec'), ('search-query', None)], 'needs': A_NEEDS},
     'Map3Reviewer': {'say': 'Step level: search-query, review, try 2', 'level': 3, 'zoom': A_ZOOM,
                      'crumbs': [('snippetvault', 'Map0Factory'), ('search', '@spec'), ('search-query', 'Map2Part'), ('review, try 2', None)],
                      'needs': A_NEEDS},
     'Notify2Paths': {'say': 'Notifications: one event', 'tab': 0,
                      'crumbs': [('snippetvault', 'Map0Factory'), ('Notifications', None)],
                      'tabs': [('One event', None), ('Every event', 'Notify3Channels')], 'needs': A_NEEDS},
     'Notify3Channels': {'say': 'Notifications: every event', 'tab': 1,
                         'crumbs': [('snippetvault', 'Map0Factory'), ('Notifications', None)],
                         'tabs': [('One event', 'Notify2Paths'), ('Every event', None)], 'needs': A_NEEDS},
     # pages beside the map drawn at this moment: their root crumb is the Factory, their views are not drawn
     'Inbox': {'say': 'Inbox', 'crumbs': [('snippetvault', 'Map0Factory'), ('Inbox', None)]},
     'Learning': {'say': 'Learning', 'crumbs': [('snippetvault', 'Map0Factory'), ('Learning', None)], 'needs': A_NEEDS},
     'Queue': {'say': 'Queue', 'crumbs': [('snippetvault', 'Map0Factory'), ('Queue', None)], 'needs': A_NEEDS},
     'Settings': {'say': 'Settings', 'crumbs': [('snippetvault', 'Map0Factory'), ('Settings', None)], 'needs': A_NEEDS},
     'Trust': {'say': 'Trust', 'crumbs': [('snippetvault', 'Map0Factory'), ('Trust', None)], 'needs': A_NEEDS},
   },
   # overlay: (the frame it is drawn in, the frame drawn under it)
   'overlays': {'approve': ('Map6Approve', 'Map1Spec'), 'question': ('Map5Question', 'Map1Spec')},
   # frame: (its screen, the overlay open over it at first)
   'frames': {'Map0Factory': ('Map0Factory', None), 'Map1Spec': ('Map1Spec', None), 'Map4Plan': ('Map4Plan', None),
              'Map2Part': ('Map2Part', None), 'Map3Reviewer': ('Map3Reviewer', None), 'Notify2Paths': ('Notify2Paths', None),
              'Notify3Channels': ('Notify3Channels', None), 'Map5Question': ('Map1Spec', 'question'), 'Map6Approve': ('Map1Spec', 'approve'),
              'Inbox': ('Inbox', None), 'Learning': ('Learning', None), 'Queue': ('Queue', None), 'Settings': ('Settings', None), 'Trust': ('Trust', None)}},
  {'name': 'B: search being built at 20:46, ≥$19.40 today, last event 2s ago',
   'groups': {'stage': ['Map3Step', 'Map3StepGrid']},
   'screens': {
     'Map3Step': {'say': 'Step level, Stage: search, being built', 'tab': 0,
                  'crumbs': [('snippetvault', None), ('search', None), ('being built', None)],
                  'tabs': [('Stage', None), ('Grid', 'Map3StepGrid')]},
     'Map3StepGrid': {'say': 'Step level, Grid: search, being built', 'tab': 1,
                      'crumbs': [('snippetvault', None), ('search', None), ('being built', None)],
                      'tabs': [('Stage', 'Map3Step'), ('Grid', None)]},
     'Map3Agent': {'say': 'Step level: search-index, everything it has written',
                   'crumbs': [('snippetvault', None), ('search', None), ('being built', '@stage'), ('search-index', None)]},
   },
   'overlays': {},
   'frames': {'Map3Step': ('Map3Step', None), 'Map3StepGrid': ('Map3StepGrid', None), 'Map3Agent': ('Map3Agent', None)}},
]
ZOOM_LABELS = ['Factory', 'Spec', 'Part', 'Step']

# ---------------------------------------------------------------------------------------------------------------- parsing
def read(name: str) -> dict[str, str]:
    s = (C / name / 'preview.html').read_text()
    assert MARK not in s, f'{name} is already wired: run its generator (or build_all.sh) first'
    # the frame as its generator drew it, kept for probe/frame_pixels.py: every wired state must equal one of these
    STATIC.mkdir(parents=True, exist_ok=True); (STATIC / f'{name}.html').write_text(s)
    card, _ = s.split('\n', 1)
    title = re.search(r'<title>(.*?)</title>', s).group(1)
    styles = re.findall(r'<style>(.*?)</style>', s, re.S)
    assert len(styles) == 1, name
    body = s[s.index('<body>') + len('<body>'):s.rindex('</body>')]
    sprite = re.search(r'<svg width="0" height="0".*?</svg>', body, re.S).group(0)
    rest = body.replace(sprite, '', 1).strip()
    assert rest.startswith('<div class="scr">') and rest.endswith('</div>'), name
    return {'card': card, 'title': title, 'css': styles[0], 'sprite': sprite, 'inner': rest[len('<div class="scr">'):-len('</div>')]}

def top_split(sel: str) -> list[str]:
    out, depth, cur = [], 0, ''
    for ch in sel:
        depth += ch in '(['
        depth -= ch in ')]'
        if ch == ',' and depth == 0:
            out.append(cur.strip()); cur = ''
        else:
            cur += ch
    return out + [cur.strip()]

RULE = re.compile(r'([^{}]+)\{([^{}]*)\}')
def scoped(css: str, attr: str, drop_scr: bool = False) -> tuple[str, list[str]]:
    """Every rule of a frame's own CSS, limited to its screen. :where() adds no specificity, so each rule still wins or
    loses against the components exactly as it did alone. html and body rules are the page's and come back separately."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    assert not RULE.sub('', css).strip(), 'unparsed CSS: ' + RULE.sub('', css).strip()[:80]
    out, glob = [], []
    for m in RULE.finditer(css):
        sels, decl = top_split(m.group(1).strip()), m.group(2).strip()
        if all(re.match(r'(html|body)\b', x) for x in sels):
            glob.append(f'{", ".join(sels)} {{ {decl} }}'); continue
        assert not any(re.match(r'(html|body)\b', x) for x in sels), sels
        if drop_scr and all(re.match(r'\.scr(?![\w-])', x) for x in sels):
            assert decl.replace(' ', '') == 'position:relative;', decl  # the screen already is
            continue
        new = [('.scr:where(' + attr + ')' + x[4:]) if re.match(r'\.scr(?![\w-])', x) else (':where(' + attr + ') ' + x) for x in sels]
        out.append(f'{", ".join(new)} {{ {decl} }}')
    return '\n'.join(out), glob

REF = re.compile(r'(url\(#|href="#|aria-(?:controls|activedescendant|labelledby|describedby)=")([\w-]+)')
def unique_ids(html: str, prefix: str) -> str:
    """Two screens can both draw a gradient called kr1: every id a screen defines gets the screen's name, and so does every
    reference to it inside that screen."""
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    html = re.sub(r'\bid="([^"]+)"', lambda m: f'id="{prefix}-{m.group(1)}"', html)
    return REF.sub(lambda m: m.group(1) + (f'{prefix}-{m.group(2)}' if m.group(2) in ids else m.group(2)), html)

# ---------------------------------------------------------------------------------------------------------------- checks
def labels(html: str, cls: str) -> list[str]:
    return [re.sub(r'<[^>]+>', '', m).strip() for m in re.findall(rf'<button class="{cls}\b[^"]*"[^>]*>(.*?)</button>', html, re.S)]

def verify(name: str, inner: str, c: dict) -> None:
    head = inner[:inner.index('</header>')]
    got = labels(head, 'k-crumb')
    assert got == [l for l, _ in c['crumbs']], f'{name} crumbs {got}'
    if 'zoom' in c:
        z = re.search(r'<div class="k-seg" role="radiogroup" aria-label="Zoom level">(.*?)</div>', inner, re.S).group(1)
        assert labels(z, 'k-seg-item') == ZOOM_LABELS, name
        assert re.findall(r'aria-checked="true"[^>]*>([^<]+)', z) == [ZOOM_LABELS[c['level']]], f'{name} zoom checked'
    if 'tabs' in c:
        t = re.search(r'<div class="k-tabs" role="tablist"[^>]*>(.*?)</div>', inner, re.S).group(1)
        assert labels(t, 'k-tab') == [l for l, _ in c['tabs']], f'{name} tabs {labels(t, "k-tab")}'
        assert re.findall(r'aria-selected="true"[^>]*>([^<]+)', t) == [c['tabs'][c['tab']][0]], f'{name} tab selected'
        assert c['tabs'][c['tab']][1] is None
    if 'needs' in c:
        band = inner[inner.index('<div class="k-needs'):]
        got = [re.sub(r'<[^>]+>', ' ', m) for m in re.findall(r'<button class="k-need\b[^"]*">(.*?)</button>', band, re.S)]
        assert len(got) == len(c['needs']) and all(l in g for (l, _), g in zip(c['needs'], got)), f'{name} needs {got}'
    if c.get('ask'):
        assert inner.count('class="k-ask"') == 1, name

def reach(st: dict, start: str) -> list[str]:
    seen, todo = [start], [start]
    def targets(c: dict) -> list[str]:
        ts = list(c.get('zoom') or []) + [t for _, t in c.get('crumbs', [])] + [t for _, t in c.get('tabs', [])]
        out = []
        for t in ts:
            if t is None: continue
            out += st['groups'][t[1:]] if t.startswith('@') else [t]
        return out
    while todo:
        for t in targets(st['screens'][todo.pop()]):
            if t not in seen: seen.append(t); todo.append(t)
    return seen

# ---------------------------------------------------------------------------------------------------------------- runtime
RUNTIME = r"""
// The frame's wiring. W: every screen drawn at this moment and what each of its controls leads to (null: static).
var W=__W__;
var cur=W.start, mem=W.memory, ctl={}, open=null, opener=null, closing=false, said=document.querySelector('[data-said]');
function scr(id){ return document.querySelector('[data-screen="'+id+'"]'); }
function resolve(t){ return t && t.charAt(0)==='@' ? mem[t.slice(1)] : t; }
function remember(id){ for(var g in W.groups){ if(W.groups[g].indexOf(id)>=0) mem[g]=id; } }
// A screen's controls are set up the first time it is shown, when it has a layout to measure.
function init(id){
  if(ctl[id]) return ctl[id];
  var s=scr(id), c=W.screens[id], o={mute:false}; ctl[id]=o;
  if(c.zoom){ o.zoom=kSeg(s.querySelector('.k-seg[aria-label="Zoom level"]'), function(it){ if(o.mute) return;
    var j=o.zoom.items.indexOf(it), t=resolve(c.zoom[j]); if(t && t!==id) go(t, {kind:'zoom', from:c.level, to:j}); }); }
  if(c.tabs){ o.tabs=kTabs(s.querySelector('.k-titletools .k-tabs'), function(t){ if(o.mute) return;
    var j=o.tabs.tabs.indexOf(t), g=resolve(c.tabs[j]); if(g && g!==id) go(g, {kind:'tabs', from:c.tab, to:j}); }); }
  [].forEach.call(s.querySelectorAll('.k-header .k-crumb'), function(b, i){ var t=c.crumbs[i];
    if(t) b.addEventListener('click', function(){ go(resolve(t), {kind:'crumb', index:i}); }); });
  [].forEach.call(s.querySelectorAll('.k-needs .k-need'), function(b, i){ var t=c.needs && c.needs[i];
    if(t) b.addEventListener('click', function(){ show(t, b); }); });
  var ask=s.querySelector('.k-ask'); if(c.ask) ask.addEventListener('click', function(){ show(c.ask, ask); });
  return o; }
// Put a screen's zoom and views back on its own level and view, without a slide (it is being left, or has just appeared).
function settle(id){ var o=ctl[id], c=W.screens[id]; if(!o) return;
  if(o.zoom) o.zoom.select(o.zoom.items[c.level], false, true);
  if(o.tabs) o.tabs.select(o.tabs.tabs[c.tab], false, true); }
// Move to another screen of this moment. The control you used is in the new screen too: it slides from where it was
// to where it now is, and it keeps focus, so the keyboard carries on from the same place.
function go(t, via){
  if(open) return;
  var from=cur; settle(from); scr(from).hidden=true; scr(t).hidden=false; cur=t; remember(t);
  var o=init(t); settle(t);
  if(via.kind==='zoom' && o.zoom){ o.zoom.place(o.zoom.items[via.from], false); o.mute=true; o.zoom.select(o.zoom.items[via.to], true); o.mute=false; }
  else if(via.kind==='tabs' && o.tabs){ o.tabs.place(o.tabs.tabs[via.from], false); o.mute=true; o.tabs.select(o.tabs.tabs[via.to], true); o.mute=false; }
  else if(via.kind==='crumb'){ scr(t).querySelectorAll('.k-header .k-crumb')[via.index].focus(); }
  said.textContent=W.screens[t].say; }
// The command window over the screen you are on: the page under it is inert; Tab stays inside; esc or the scrim closes
// it and focus goes back to what opened it, or to the ask field when that is gone (CommandWindow).
function ov(name){ return document.querySelector('[data-overlay="'+name+'"]'); }
function under(on){ [].forEach.call(scr(cur).children, function(ch){ if(!ch.hasAttribute('data-overlay')) ch.inert=on; }); }
function show(name, from){
  if(open && !closing){ var q0=ov(open).querySelector('.k-cmd-q'); q0.focus(); q0.select(); return; }
  var o=ov(name), s=scr(cur); if(o.parentNode!==s) s.appendChild(o);
  o.hidden=false; open=name; closing=false; opener=from||null; under(true);
  var win=o.querySelector('.k-cmd'), sc=o.querySelector('.k-scrim'); win.dataset.motion='enter'; sc.dataset.motion='enter';
  // once it has arrived it rests as drawn: a finished animation left in place keeps the window on its own layer
  win.addEventListener('animationend', function(){ if(win.dataset.motion==='enter'){ delete win.dataset.motion; delete sc.dataset.motion; } }, {once:true});
  o.querySelector('.k-cmd-q').focus(); }
function hide(){
  if(!open || closing) return; closing=true;
  var o=ov(open), win=o.querySelector('.k-cmd'), sc=o.querySelector('.k-scrim'), done=false;
  win.dataset.motion='exit'; sc.dataset.motion='exit';
  var ms=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dur-exit'))||200;
  function end(){ if(done) return; done=true; o.hidden=true; delete win.dataset.motion; delete sc.dataset.motion; under(false); open=null; closing=false;
    var back=opener && document.contains(opener) && !opener.closest('[hidden]') ? opener : scr(cur).querySelector('.k-ask'); back.focus(); }
  win.addEventListener('animationend', end, {once:true}); setTimeout(end, ms+80); }
[].forEach.call(document.querySelectorAll('[data-overlay]'), function(o){
  o.querySelector('.k-scrim').addEventListener('click', hide);
  o.addEventListener('keydown', function(e){
    if(e.key==='Tab'){ var f=[].filter.call(o.querySelectorAll('input, button, [tabindex="0"]'), function(x){ return !x.closest('[hidden]') && !x.disabled; });
      var i=f.indexOf(document.activeElement); e.preventDefault(); f[(i + (e.shiftKey ? f.length-1 : 1) + f.length) % f.length].focus(); } }); });
// esc is the document's while a window is open: a frame that opens with its window showing has focus on nothing yet.
document.addEventListener('keydown', function(e){ if(open && e.key==='Escape'){ e.preventDefault(); hide(); return; }
  if((e.metaKey||e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase()==='k'){
  var a=W.screens[cur].ask; if(open){ e.preventDefault(); show(open); } else if(a){ e.preventDefault(); show(a, document.activeElement); } } });
init(cur);
if(W.open){ open=W.open; under(true); }
"""

# ---------------------------------------------------------------------------------------------------------------- build
COMPOSITE_CSS = """
  /* the frame's screens and windows: one screen shows at a time; a window's box is the screen's while it is open */
  .scr[hidden], [data-overlay][hidden] { display:none; }
  [data-overlay] { display:contents; }
"""

def build(st: dict) -> None:
    raw = {n: read(n) for n in set(st['screens']) | set(st['frames']) | {f for f, _ in st['overlays'].values()}}
    sprite = raw[next(iter(st['screens']))]['sprite']
    assert all(r['sprite'] == sprite for r in raw.values()), 'sprites differ'
    for n, c in st['screens'].items():
        verify(n, raw[n]['inner'], c)
    # a window is what its frame adds to the frame drawn under it
    ovs = {}
    for name, (frame, base) in st['overlays'].items():
        f, b = raw[frame], raw[base]
        assert f['css'].startswith(b['css'].rstrip('\n ')), f'{frame} css does not extend {base}'
        assert f['inner'].startswith(b['inner']), f'{frame} does not extend {base}'
        extra_css = f['css'][len(b['css'].rstrip('\n ')):]
        html = f['inner'][len(b['inner']):].strip('\n')
        assert html.lstrip().startswith('<div class="k-scrim"></div>'), frame
        css, glob = scoped(extra_css, f'[data-overlay="{name}"]', drop_scr=True)
        assert not glob
        ovs[name] = (html, css)
    for frame, (start, first_open) in st['frames'].items():
        screens = reach(st, start)
        cfg = {n: st['screens'][n] for n in screens}
        used = {c.get('ask') for c in cfg.values()} | {t for c in cfg.values() for _, t in c.get('needs', [])} | {first_open}
        used.discard(None)
        css_parts, glob_all, blocks = [], [], []
        for n in screens:
            css, glob = scoped(raw[n]['css'], f'[data-screen="{n}"]')
            css_parts.append(f'/* {n} */\n{css}')
            glob_all += [g for g in glob if g not in glob_all]
            inner = unique_ids(raw[n]['inner'], n)
            if n == start and first_open:
                inner = inner.rstrip('\n') + f'\n<div data-overlay="{first_open}">\n{ovs[first_open][0]}\n</div>\n'
            blocks.append(f'<div class="scr" data-screen="{n}"{"" if n == start else " hidden"}>{inner}</div>')
        for name in sorted(used):
            css_parts.append(f'/* window: {name} */\n{ovs[name][1]}')
            if name != first_open:
                blocks.append(f'<div data-overlay="{name}" hidden>\n{ovs[name][0]}\n</div>')
        w = {'start': start, 'open': first_open,
             'memory': {g: (start if start in ms else ms[0]) for g, ms in st['groups'].items()},
             'groups': st['groups'],
             'screens': {n: {'say': c['say'], 'level': c.get('level'), 'tab': c.get('tab'), 'zoom': c.get('zoom'),
                             'crumbs': [t for _, t in c.get('crumbs', [])],
                             'tabs': [t if t is not None else (n if i == c.get('tab') else None) for i, (_, t) in enumerate(c['tabs'])] if 'tabs' in c else None,
                             'needs': [t for _, t in c.get('needs', [])], 'ask': c.get('ask')} for n, c in cfg.items()}}
        # a tablist or zoom is wired only when every item leads somewhere drawn (its own item leads to itself)
        for n, c in w['screens'].items():
            if c['tabs'] is not None and any(t is None for t in c['tabs']): c['tabs'] = None
            if c['zoom'] is not None and any(t is None for t in c['zoom']): c['zoom'] = None
        runtime = RUNTIME.replace('__W__', json.dumps(w, ensure_ascii=False))
        own = raw[frame]
        html = f'''{own["card"]}
<!doctype html>
<html lang="en" {MARK}="{st["name"][0]}">
<head>
<meta charset="utf-8">
<title>{own["title"]}</title>
<style>
  {chr(10).join(glob_all)}
{COMPOSITE_CSS}
{chr(10).join(css_parts)}
</style>
</head>
<body>
{sprite}
{chr(10).join(blocks)}
<div class="k-sr" aria-live="polite" data-said></div>
<script>
{SEG}
{TABS}
// Everything below measures rendered text, so it runs once the web fonts have loaded.
(document.fonts ? document.fonts.ready : Promise.resolve()).then(function(){{
{runtime}
}});
</script>
</body>
</html>
'''
        ids = re.findall(r'\bid="([^"]+)"', html)
        dup = {i for i in ids if ids.count(i) > 1}
        assert not dup, f'{frame}: duplicate ids {dup}'
        for m in REF.finditer(html):
            assert m.group(2) in ids, f'{frame}: {m.group(0)} points at nothing'
        (C / frame / 'preview.html').write_text(html)
        print(f'{frame:16} set {st["name"][0]}  screens {len(screens)}  windows {sorted(used)}  {len(html)} bytes')

if __name__ == '__main__':
    for st in SETS:
        build(st)
