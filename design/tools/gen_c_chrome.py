"""Chrome (header, title row, Needs you) and CommandWindow: the two cards that compose the app's frame."""
import json
import re

from beh import FIT, SEG, TABS
from card import card
from comp import chip, ikeys, keys, seg, tabs

LOGO = '<svg class="k-logo" viewBox="0 0 64 64" aria-hidden="true"><g fill="var(--text)"><path d="M30 20 L6 4 L9 12 L18 18 L28 27 Z"/><path d="M34 20 L58 4 L55 12 L46 18 L36 27 Z"/><path d="M32 9 L35 15 L36 24 L32 46 L28 24 L29 15 Z"/><path d="M32 44 L24 58 L32 55 L40 58 Z"/></g></svg>'
CONDS = ('<span class="k-conds" data-fit-drop="1"><span>L2 · merges wait for you</span>'
         '<span><span class="k-mk sm pass"></span>all checks ran</span><span><span class="k-mk sm pass"></span>CI passing</span></span>')
HINT = f"Select a part and press {ikeys('↵')} to zoom in · {ikeys('⌘', 'K')} to ask why, what it cost, what runs next"


def crumb(i: int, name: str, n: int) -> str:
    cur = ' aria-current="page"' if i == n - 1 else ''
    root = ' k-crumb-root' if i == 0 else ''
    return f'<li><button class="k-crumb{root}"{cur}>{name}</button></li>'


def header(crumbs: list[str], hid: str, ask_id: str) -> str:
    items = ''.join(crumb(i, c, len(crumbs)) for i, c in enumerate(crumbs))
    conds = CONDS if len(crumbs) == 2 else CONDS.replace('<span class="k-conds"', '<span class="k-conds" hidden', 1)
    return (f'<header class="k-header" id="{hid}">{LOGO}<nav aria-label="Where you are"><ol class="k-crumbs">{items}</ol></nav>{conds}'
            f'<span class="grow"></span><span class="k-live" data-fit-drop="3">live · last event 3s ago</span>'
            f'<span class="k-spend">≥$31.10 <small data-fit-drop="2">of $40.00 today</small></span>'
            f'<button class="k-ask" id="{ask_id}" aria-label="Ask or do anything"><span data-fit-drop="4">Ask or do anything</span>{keys("⌘", "K")}</button></header>')


NEEDS = (f'<div class="k-needs" id="{{nid}}"><span class="k-needs-label">Needs you</span>'
         '<button class="k-need k-need-ask" data-need="approve" data-fit-drop="5"><span class="k-mk you"></span><b>Approve search-index</b><span class="k-need-sub">every check agreed · 12m</span></button>'
         '<button class="k-need" data-need="stopped" data-fit-drop="4"><span class="k-mk fail"></span><b>search-highlight stopped</b><span class="k-need-sub">retry it, or add guidance first</span></button>'
         '<button class="k-need k-need-more" data-fit-more hidden><b>1 more</b><span class="k-need-sub">in the inbox</span></button>'
         f'<span class="k-needs-hint" data-fit-drop="1">{HINT}</span></div>')


def measure(meta: str) -> str:
    """A lower-bound spend is a measurement: set in measure, as the level pages' title rows do."""
    return re.sub(r'≥\$[0-9][0-9.,]*', lambda m: f'<span class="v-measure">{m.group(0)}</span>', meta)


def title(voice: str, text: str, meta: str, who: str = '') -> str:
    cls = 'k-title-human' if voice == 'human' else 'k-title-name'
    return f'<span class="{cls}">{text}</span>{chip(who) if who else ""}<span class="k-title-meta">{measure(meta)}</span>'


# ---------------- Chrome: one screen's three bands, live across the four levels
LV = ['Factory', 'Spec', 'Part', 'Step']
PARTS = {
    'search-query': {'step': 'review, try 3',
                     'part': title('agent', 'search-query', 'try 3 of 4 · checking · ≥$9.20', 'reviewer'),
                     'stepTitle': title('agent', 'search-query', 'try 3 of 4 · review · started 21:12', 'reviewer')},
    'search-highlight': {'step': 'engineer, try 2',
                         'part': title('agent', 'search-highlight', 'stopped on try 2 of 4 · no progress · ≥$3.22'),
                         'stepTitle': title('agent', 'search-highlight', 'try 2 of 4 · engineer · 20:41 to 21:03 · stopped', 'engineer')},
}
TOP = [title('human', 'snippetvault', '3 specs built this week · 1 building · 2 queued'),
       title('human', 'search', '7 parts · 3 of 4 slots in use · ≥$21.84 since 19:40')]
VIEWS = tabs(['Graph', 'Text'], 0, 'View').replace('class="k-tabs"', 'class="k-tabs" id="views" hidden', 1)
WIDTH = seg(['1280', '1024', '768'], 0, 'Screen width', sm=True).replace('class="k-seg', 'id="width" class="k-seg', 1)
ZOOM = seg(LV, 2, 'Zoom level').replace('class="k-seg"', 'class="k-seg" id="zoom"', 1)
assert 'id="views"' in VIEWS and 'id="zoom"' in ZOOM

chrome = f'''<div class="c-stage shell" id="shell">
  {header(['snippetvault', 'search', 'search-query'], 'hd', 'ask0')}
  <div class="tr"><div class="k-titlerow"><h1 class="k-title" id="ttl">{PARTS['search-query']['part']}</h1><div class="k-titletools">{VIEWS}{ZOOM}</div></div></div>
  <div class="body"><p class="c-cap" id="where" aria-live="polite">A crumb zooms back out to its level, and the last is where you are. Try a crumb, the zoom, a need, or a narrower screen (the width in px).</p>{WIDTH}</div>
  {NEEDS.format(nid='needs')}
</div>
<div class="c-tile"><div class="c-lab">The three bands</div>
  <dl class="rules">
    <dt>Header, 56</dt><dd>The logo; where you are, each level a crumb that zooms back out to it (the last is <span class="v-measure">aria-current</span>); one level in (a spec, the inbox, settings), the standing conditions, which the Factory level shows as tiles instead; then whether the view is live, today’s spend (a floor, so <span class="v-measure">≥</span>) and the ask field, which opens the command window, as {ikeys('⌘', 'K')} does from anywhere.</dd>
    <dt>Title row</dt><dd>What this screen is, in the voice that owns it: a spec in your words, a part by its name with the agent working on it; then one line of measured facts. On the right, its views and the zoom, which moves with the crumbs.</dd>
    <dt>Needs you, 72</dt><dd>What waits on you, each a button that lands on it (an approval opens with its evidence; a stopped part on its Part level), and one hint for this screen.</dd>
    <dt>Narrower</dt><dd>The header and Needs you stay one line and give way in order. The header drops the standing conditions, then the cap after today’s spend, then the live text, then the ask field’s words (its keycaps stay). Needs you drops its hint, then needs from the right, into a count that lands on the inbox. The title row wraps its facts under the title. The crumbs and the spend always stay.</dd>
    <dt>Edges</dt><dd>The header and Needs you pad 24; the title row and everything under it sit at 32, a step in from the bands around them.</dd>
  </dl></div>'''
chrome_css = """
  /* a screen does not grow: when the title row wraps its facts (768, the Spec level: +17px) the body row gives the height back */
  .shell { position:relative; display:grid; grid-template-columns:minmax(0,1fr); grid-template-rows:56px auto minmax(0,1fr) 72px; height:248px; max-width:100%; overflow:hidden; }
  .tr { padding:16px 32px 0; }
  .body { display:flex; align-items:center; gap:24px; min-width:0; padding:0 32px; }
  .body .c-cap { flex:1; min-width:0; margin:0; }
  .body .k-seg { flex:none; }
  .rules { display:grid; grid-template-columns:120px minmax(0,1fr); gap:10px 16px; margin:0; font:var(--t-small); } .rules dt { color:var(--text-2); } .rules dd { margin:0; max-width:820px; }
  .rules .v-measure { white-space:nowrap; }
"""
chrome_js = SEG + TABS + FIT + r"""
  var P=""" + json.dumps(PARTS) + r""", TOP=""" + json.dumps(TOP) + r""", LV=""" + json.dumps(LV) + r""";
  var hd=document.getElementById('hd'), ttl=document.getElementById('ttl'), where=document.getElementById('where'), views=document.getElementById('views');
  var st={level:2, part:'search-query'}, quiet=false, viewsInit=false;
  var fitHd=kFit(hd), fitN=kFit(document.getElementById('needs'));
  function names(s){ return ['snippetvault', 'search', s.part, P[s.part].step].slice(0, s.level+1); }
  // Crumbs are updated in place, never rebuilt: the crumb you pressed keeps focus as it becomes the current one.
  function crumbs(s){ var ol=hd.querySelector('.k-crumbs'), want=names(s), lis=[].slice.call(ol.children);
    lis.forEach(function(li, i){ if(i>=want.length) li.remove(); });
    want.forEach(function(n, i){ var li=ol.children[i];
      if(!li){ li=document.createElement('li'); var b=document.createElement('button'); b.className='k-crumb'+(i===0?' k-crumb-root':''); li.appendChild(b); ol.appendChild(li); }
      var b=li.firstChild; if(b.textContent!==n) b.textContent=n; if(i===want.length-1) b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current'); }); }
  function go(s, said){ var from=st.level; st=s; crumbs(s);
    hd.querySelector('.k-conds').hidden = s.level!==1;
    ttl.innerHTML = s.level<2 ? TOP[s.level] : (s.level===2 ? P[s.part].part : P[s.part].stepTitle);
    views.hidden = s.level!==1; if(!views.hidden && !viewsInit){ viewsInit=true; kTabs(views); }
    quiet=true; zoom.select(zoom.items[s.level], false); quiet=false;
    fitHd.update();
    where.textContent = said || ((s.level<from ? 'Zoomed out to ' : 'Zoomed in to ')+LV[s.level]+': '+names(s)[s.level]+'.'); }
  var zoom=kSeg(document.getElementById('zoom'), function(it){ if(quiet) return; var l=zoom.items.indexOf(it); if(l!==st.level) go({level:l, part:st.part}); });
  hd.addEventListener('click', function(e){ var b=e.target.closest('.k-crumb'); if(!b||b.hasAttribute('aria-current')) return;
    go({level:[].indexOf.call(hd.querySelectorAll('.k-crumb'), b), part:st.part}); });
  document.getElementById('needs').addEventListener('click', function(e){ var b=e.target.closest('.k-need'); if(!b) return;
    if(b.dataset.need==='stopped') go({level:2, part:'search-highlight'}, 'Landed on search-highlight, at the Part level, where its tries and the breaker’s reason are.');
    else if(b.dataset.need==='approve') where.textContent='The approval for search-index opens over this screen with its evidence: it is the command window, in its approval form. Nothing is approved from the strip.';
    else where.textContent='The rest wait in the inbox.'; });
  var widths=['', '1024px', '768px'], shell=document.getElementById('shell');
  kSeg(document.getElementById('width'), function(it){ var i=[].indexOf.call(it.parentNode.querySelectorAll('.k-seg-item'), it); shell.style.width=widths[i]; });
  function ask(){ where.textContent='The ask field and ⌘K open the command window over this screen. It has its own card, CommandWindow.'; }
  document.getElementById('ask0').addEventListener('click', ask);
  document.addEventListener('keydown', function(e){ if((e.metaKey||e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase()==='k'){ e.preventDefault(); document.getElementById('ask0').focus(); ask(); } });
"""
card('Chrome', 'Surfaces', 616, 'The three bands every screen has: header, title row, Needs you', chrome, chrome_css, chrome_js, width=1328)

# ---------------- Command window
# kind: q (a question, answered from the run records) or a (an action). go: what the action bar's primary does.
ROWS = [
    {'sec': 'Answer from the run records', 'kind': 'q', 'title': 'Why did search-highlight stop?',
     'head': 'search-highlight stopped at 21:03',
     'text': ['The no-progress breaker failed it on try 2 of 4: iterations 4, 5 and 6 left both the code and the test results unchanged. Its two tries cost <span class="v-measure">≥$3.22</span>.',
              'The breaker fails a part at once rather than spend its last two tries on the same prompt and the same tree. search-cli was skipped, because it needs search-highlight.'],
     'srcs': ['events.jsonl', 'manifest.json', 'engineer.log'],
     'go': 'Go to search-highlight', 'note': 'Answers come from the run records', 'done': 'Went to search-highlight, at the Part level.'},
    {'sec': 'Do something about it', 'kind': 'a', 'title': 'Retry search-highlight', 'meta': 'after this run', 'off': True,
     'head': 'Retry search-highlight',
     'text': ['Not while this run is going: a retry re-enters the factory, which needs the lock this run holds.',
              'Once it ends, a retry starts search-highlight again from the base branch with its tries reset, and search-cli with it, under the limits this run was launched with. The same as ks retry search-highlight.'],
     'srcs': [], 'go': 'Retry search-highlight', 'note': 'Available when this run ends', 'done': ''},
    {'sec': 'Do something about it', 'kind': 'a', 'title': 'Add guidance for the engineers', 'keys': ['⌘', 'G'],
     'head': 'Add guidance for the engineers',
     'text': ['Adds a line under Guidance in scripts/kstrl/memory.md. Every part that starts after you save it reads it, in this run and later ones. Parts already running do not.'],
     'srcs': [], 'go': 'Write guidance', 'note': 'Nothing is saved until you save the note', 'done': 'Opened the guidance note, scripts/kstrl/memory.md.'},
    {'sec': 'Do something about it', 'kind': 'a', 'title': 'Open the engineer’s log', 'keys': ['⌘', 'O'],
     'head': 'The engineer’s log for search-highlight',
     'text': ['engineer.log: everything the engineer wrote in both tries of this run, in the run’s folder for search-highlight.'],
     'srcs': [], 'go': 'Open the log', 'note': 'Nothing changes', 'done': 'Opened engineer.log for search-highlight.'},
    {'sec': 'Other questions about search', 'kind': 'q', 'title': 'What is waiting on me?',
     'head': 'One merge waits on you',
     'text': ['search-index: every check agreed, waiting 12m. At L2 every merge waits for your approval.'],
     'srcs': ['inbox.jsonl'], 'go': 'Open the approval', 'note': 'Opening it approves nothing', 'done': 'Opened the approval for search-index.'},
    {'sec': 'Other questions about search', 'kind': 'q', 'title': 'What has this run cost, by phase?',
     'head': 'At least $21.84 since 19:40',
     'text': ['architect <span class="v-measure">≥$2.90</span>, engineer <span class="v-measure">≥$11.20</span>, review <span class="v-measure">≥$5.02</span>, security <span class="v-measure">≥$2.72</span>.',
              '2 of 214 agent calls reported no cost, so they are not in it, and the total is a floor.'],
     'srcs': ['events.jsonl'], 'go': 'Go to spend', 'note': 'Answers come from the run records', 'done': 'Went to spend.'},
]


def rows_html() -> str:
    out, sec = [], None
    for i, r in enumerate(ROWS):
        if r['sec'] != sec:
            sec = r['sec']
            out.append(f'<div class="k-cmd-sec" role="presentation">{sec}</div>')
        mark = '<span class="k-cmd-q-mark" aria-hidden="true">?</span>' if r['kind'] == 'q' else '<span></span>'
        meta = keys(*r['keys']) if 'keys' in r else r.get('meta', '')
        off = ' aria-disabled="true"' if r.get('off') else ''
        sel = 'true' if i == 0 else 'false'
        out.append(f'<div class="k-row" role="option" id="cr{i}" aria-selected="{sel}"{off}>{mark}<span class="k-row-title">{r["title"]}</span><span class="k-row-meta">{meta}</span></div>')
    return ''.join(out)


def detail_html(r: dict) -> str:
    srcs = ''.join(f'<span class="k-cmd-src">{s}</span>' for s in r['srcs'])
    return (f'<h2 class="k-cmd-head">{r["head"]}</h2>' + ''.join(f'<p class="k-cmd-text">{t}</p>' for t in r['text'])
            + (f'<div class="k-cmd-srcs" aria-label="Sources">{srcs}</div>' if srcs else ''))


WINDOW = (f'<div class="k-scrim" id="scrim"></div><div class="k-cmd win" id="win" role="dialog" aria-modal="true" aria-label="Ask kstrl">'
          f'<div class="k-cmd-input">{LOGO}<input class="k-cmd-q" id="cq" placeholder="Ask or do anything" aria-label="Ask or do anything" role="combobox" aria-expanded="true" aria-controls="cl" aria-autocomplete="list" aria-activedescendant="cr0" autocomplete="off" spellcheck="false">{keys("esc")}</div>'
          f'<div class="k-cmd-body"><div class="k-cmd-list" role="listbox" id="cl" aria-label="Answers and actions">{rows_html()}</div><div class="k-cmd-detail" id="cd" aria-live="polite">{detail_html(ROWS[0])}</div></div>'
          f'<div class="k-cmd-bar"><span class="k-cmd-bar-note" id="cn">{ROWS[0]["note"]}</span><button class="k-cmd-act k-cmd-act-primary" id="cgo"><span id="cgol">{ROWS[0]["go"]}</span>{keys("↵")}</button></div></div>')
cmd = f'''<div class="c-stage stage" id="stage">
  <div class="under" id="under">{header(['snippetvault', 'search'], 'hd2', 'openask')}<p class="c-cap" id="said" aria-live="polite">The page under the window stays where it was. Close the window, then open it again with the ask field or {ikeys('⌘', 'K')}.</p></div>
  {WINDOW}
</div>
<div class="c-two">
  <div class="c-tile"><div class="c-lab">Keys, while it is open</div>
    <dl class="rules"><dt>{ikeys('⌘', 'K')}</dt><dd>opens it over any level, as the ask field does; pressed again, selects the query</dd>
      <dt>type</dt><dd>narrows the list; the first match becomes the active row</dd>
      <dt>{ikeys('↑')} {ikeys('↓')}</dt><dd>move the active row; focus never leaves the query</dd>
      <dt>{ikeys('↵')}</dt><dd>does what the action bar says; a row’s own keys ({ikeys('⌘', 'G')}) do its action from anywhere in the list</dd>
      <dt>{ikeys('esc')}</dt><dd>closes it, and focus goes back to where you were</dd></dl></div>
  <div class="c-tile"><div class="c-lab">Anatomy</div>
    <dl class="rules"><dt>query</dt><dd>56px; 17px text, a rufous caret and a 2px <span class="v-measure">focus</span> underline</dd>
      <dt>body</dt><dd>a 340px list that scrolls (sections in label type, rows as Row) and, beside it, the answer with its sources, or what the action does</dd>
      <dt>action bar</dt><dd>44px; on the left its consequence or when it will be available, on the right the action {ikeys('↵')} runs, in <span class="v-measure">you</span></dd>
      <dt>motion</dt><dd>opens on <span class="v-measure">dur-enter</span> from 98%, closes on <span class="v-measure">dur-exit</span>; the scrim fades with it</dd></dl></div>
</div>'''
cmd_css = """
  .stage { position:relative; height:604px; overflow:hidden; }
  .under .c-cap { position:absolute; left:24px; right:24px; bottom:14px; margin:0; }
  .win { position:absolute; left:0; right:0; top:72px; margin:0 auto; width:780px; height:484px; }
  .rules { display:grid; grid-template-columns:auto minmax(0,1fr); gap:8px 14px; margin:0; font:var(--t-small); } .rules dt { color:var(--text-2); } .rules dd { margin:0; }
"""
cmd_js = FIT + r"""
  var ROWS=""" + json.dumps([{k: r[k] for k in ('go', 'note', 'done', 'off') if k in r} | {'detail': detail_html(r), 'keys': r.get('keys')} for r in ROWS]) + r""";
  var stage=document.getElementById('stage'), under=document.getElementById('under'), said=document.getElementById('said');
  var win=document.getElementById('win'), scrim=document.getElementById('scrim'), q=document.getElementById('cq'), list=document.getElementById('cl');
  var go=document.getElementById('cgo'), opener=null, closing=false;
  kFit(document.getElementById('hd2'));
  under.inert=true;
  function isOpen(){ return !win.hidden && !closing; }
  function all(){ return [].slice.call(list.querySelectorAll('.k-row')); }
  function shown(){ return all().filter(function(r){ return !r.hidden; }); }
  function active(){ return all().filter(function(r){ return r.getAttribute('aria-selected')==='true' && !r.hidden; })[0]||null; }
  function activate(r){
    all().forEach(function(x){ x.setAttribute('aria-selected', x===r?'true':'false'); });
    var d=document.getElementById('cd'), n=document.getElementById('cn');
    if(!r){ q.removeAttribute('aria-activedescendant'); d.innerHTML='<h2 class="k-cmd-head">Nothing here matches</h2><p class="k-cmd-text">Try a part’s name, or what you want done: retry, approve, open.</p>'; n.textContent='Nothing to run'; go.hidden=true; return; }
    var row=ROWS[+r.id.slice(2)]; q.setAttribute('aria-activedescendant', r.id); d.innerHTML=row.detail; d.scrollTop=0;
    n.textContent=row.note; document.getElementById('cgol').textContent=row.go; go.hidden=false;
    if(row.off) go.setAttribute('aria-disabled','true'); else go.removeAttribute('aria-disabled');
    r.scrollIntoView({block:'nearest'}); }
  function norm(s){ return s.toLowerCase().replace(/[‘’]/g, "'"); }
  function filter(){ var v=norm(q.value.trim());
    all().forEach(function(r){ r.hidden = !!v && norm(r.querySelector('.k-row-title').textContent).indexOf(v)<0; });
    list.querySelectorAll('.k-cmd-sec').forEach(function(s){ var n=s.nextElementSibling, any=false; while(n && !n.classList.contains('k-cmd-sec')){ if(!n.hidden) any=true; n=n.nextElementSibling; } s.hidden=!any; });
    list.scrollTop=0; activate(shown()[0]||null); }
  function run(r){ if(!r) return; var row=ROWS[+r.id.slice(2)]; if(row.off) return; close(row.done); }
  function open(from){ if(isOpen()){ q.focus(); q.select(); return; }
    opener=(from && from!==document.body) ? from : null; closing=false;
    win.hidden=false; scrim.hidden=false; under.inert=true; q.value=''; filter();
    win.dataset.motion='enter'; scrim.dataset.motion='enter'; q.focus(); }
  function close(result){ if(!isOpen()) return; closing=true; win.dataset.motion='exit'; scrim.dataset.motion='exit';
    var ms=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dur-exit'))||200, done=false;
    function end(){ if(done) return; done=true; win.hidden=true; scrim.hidden=true; closing=false; under.inert=false;
      said.textContent = result || 'Closed. Focus went back to where you were.';
      var back=opener && document.contains(opener) ? opener : document.getElementById('openask'); back.focus(); }
    win.addEventListener('animationend', end, {once:true}); setTimeout(end, ms+80); }
  q.addEventListener('input', filter);
  win.addEventListener('keydown', function(e){
    if(e.isComposing) return;
    var rs=shown(), cur=rs.indexOf(active());
    if(e.key==='ArrowDown'){ e.preventDefault(); if(rs.length) activate(rs[Math.min(rs.length-1, cur+1)]); }
    else if(e.key==='ArrowUp'){ e.preventDefault(); if(rs.length) activate(rs[Math.max(0, cur-1)]); }
    else if(e.key==='Enter' && e.target===q){ e.preventDefault(); run(active()); }
    else if(e.key==='Escape'){ e.preventDefault(); close(); }
    else if(e.key==='Tab'){ var f=[q, go].filter(function(x){ return !x.hidden; }); var i=f.indexOf(document.activeElement);
      e.preventDefault(); f[(i + (e.shiftKey ? f.length-1 : 1)) % f.length].focus(); }
    else if((e.metaKey||e.ctrlKey) && !e.altKey && !e.shiftKey){ var k=e.key.toLowerCase();
      for(var j=0;j<ROWS.length;j++){ var ks=ROWS[j].keys; if(ks && ks[1].toLowerCase()===k){ e.preventDefault(); var r=document.getElementById('cr'+j); if(r.hidden){ q.value=''; filter(); } activate(r); run(r); return; } } } });
  // Pointer: moving over a row makes it active (a still pointer under a window that just opened does not); a click runs it.
  // Rows never take focus: pressing one keeps the query focused.
  list.addEventListener('mousedown', function(e){ e.preventDefault(); });
  list.addEventListener('mousemove', function(e){ var r=e.target.closest('.k-row'); if(r && r!==active()) activate(r); });
  list.addEventListener('click', function(e){ var r=e.target.closest('.k-row'); if(!r) return; activate(r); q.focus(); run(r); });
  go.addEventListener('click', function(){ run(active()); if(!win.hidden) q.focus(); });
  scrim.addEventListener('click', function(){ close(); });
  document.getElementById('openask').addEventListener('click', function(){ open(this); });
  document.addEventListener('keydown', function(e){ if((e.metaKey||e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase()==='k'){ e.preventDefault(); open(document.activeElement); } });
"""
card('CommandWindow', 'Surfaces', 879, 'Ask or do anything, over any level: a combobox that never lets go of the query', cmd, cmd_css, cmd_js, width=1328)
print('ok')
