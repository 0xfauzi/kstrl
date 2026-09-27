from card import card
from beh import BEZ, TABS
from comp import vtabs
import json

def tabs(opts, on, state=None, ids='', panels=False, label='View'):
    items = []
    for i, o in enumerate(opts):
        a = f' role="tab" aria-selected="{"true" if i == on else "false"}" tabindex="-1"'
        if panels:
            a += f' id="t-{o}" aria-controls="p-{o}"'
        if state and state[0] == i:
            a += f' data-state="{state[1]}"'
        items.append(f'<button class="k-tab"{a}>{o}</button>')
    return f'<div class="k-tabs" role="tablist" aria-label="{label}"{ids}><span class="k-tabs-bar" aria-hidden="true"></span>{"".join(items)}</div>'
V = ['Graph', 'Text', 'Receipt']
states = f'''<div class="c-tile"><div class="c-lab">States (the selected tab draws its own underline until the script adds the sliding one)</div>
  <div class="c-grid" style="grid-template-columns:150px max-content 1fr; column-gap:28px">
    <span class="c-gr">Rest</span>{tabs(V, 0)}<span class="c-cap">Graph selected: text 600 with a 2px text underline; the others text-3.</span>
    <span class="c-gr">Hover</span>{tabs(V, 0, (1, 'hover'))}<span class="c-cap">Text under the pointer: text-2.</span>
    <span class="c-gr">Focus</span>{tabs(V, 0, (0, 'focus'))}<span class="c-cap">Keyboard focus on the selected tab.</span>
  </div></div>'''
live = f'''<div class="c-tile"><div class="c-lab">Live: Tab in, then the arrow keys move, Home and End jump. Showing a view is cheap, so selection follows focus.</div>
  {tabs(['Log', 'Notes', 'Prompt'], 0, ids=' id="liveT"', panels=True, label='What the agent wrote')}
  <div class="pn" role="tabpanel" id="p-Log" aria-labelledby="t-Log">engineer.log: the agent's text and the commands it ran.</div>
  <div class="pn" role="tabpanel" id="p-Notes" aria-labelledby="t-Notes" hidden>progress.txt: its per-story notes and self-critique.</div>
  <div class="pn" role="tabpanel" id="p-Prompt" aria-labelledby="t-Prompt" hidden>What it was sent, per call.</div></div>'''
def frame(n):
    t = tabs(V, 1).replace('class="k-tabs"', 'class="k-tabs k-tabs-js"').replace(' role="tablist"', '').replace(' role="tab"', '')
    return '<div class="fr"><span class="c-cap ft"></span>' + t + '</div>'
film = ('<div class="c-tile"><div class="c-lab">Motion: Graph to Text, the underline only, <span class="v-measure">dur-base</span> on '
        '<span class="v-measure">ease-standard</span></div><div class="fm" aria-hidden="true" data-audit-film id="film">' + ''.join(frame(n) for n in range(5)) + '</div></div>')
anat = f'''<div class="c-tile"><div class="c-lab">Anatomy</div><div class="c-anat" style="height:78px"><div id="anatT" style="position:absolute;left:40px;top:13px">{tabs(V, 0)}</div></div></div>'''
GROUPS = [('How work is checked', 9), ('How much runs at once', 6), ('Money', 4), ('Trust', 5), ('The queue', 7)]
vert = f'''<div class="c-tile"><div class="c-lab">Vertical: the sections of one long page, one shown at a time (Settings' groups). Up and Down move, Home and End jump; the count is read as "9 settings".</div>
  <div class="vt">{vtabs(GROUPS, 0, 'Setting groups', 'settings', ids='vg').replace('class="k-tabs k-tabs-v"', 'id="liveV" class="k-tabs k-tabs-v"', 1)}
    <div class="vp" role="tabpanel" id="vgp" aria-labelledby="vg0"><span class="k-label" id="vgh">How work is checked</span><p id="vgt">9 settings: the commands verify runs, and how review and security decide.</p></div>
    <dl class="vr"><dt>tab</dt><dd>32px, full width, 4px apart; label 13px/500 in <span class="v-measure">text-2</span>, 600 in <span class="v-measure">text</span> when selected</dd>
      <dt>bar</dt><dd>2 by 20, on the left edge, centred on the tab; slides on <span class="v-measure">dur-base</span></dd>
      <dt>count</dt><dd>11px measure in <span class="v-measure">text-3</span>, at the end</dd></dl></div></div>'''
body = states + f'<div class="c-two">{live}{anat}</div>' + vert + film
css = """
  .pn { margin-top:12px; font:var(--t-small); color:var(--text-2); }
  .vt { display:grid; grid-template-columns:240px minmax(0,1fr) 380px; column-gap:28px; align-items:start; }
  .vp p { margin:8px 0 0; font:var(--t-small); color:var(--text-2); }
  .vr { display:grid; grid-template-columns:44px minmax(0,1fr); gap:8px 12px; margin:0; font:var(--t-small); } .vr dt { color:var(--text-3); } .vr dd { margin:0; color:var(--text-2); }
  .vr .v-measure { font:var(--t-measure-inline); white-space:nowrap; }
  .fm { display:grid; grid-template-columns:max-content; row-gap:6px; }
  .fr { display:flex; gap:14px; align-items:center; }
  .fr .ft { width:96px; font:var(--t-measure-small); font-weight:400; }
  .fr .k-tab { pointer-events:none; }
"""
script = BEZ + TABS + r"""
  kTabs(document.getElementById('liveT'));
  var GT=""" + json.dumps(["the commands verify runs, and how review and security decide.", "max_parallel and the slots each run takes.", "the daily budget and the per-run ceilings.", "the level in force and what promotes or demotes it.", "what ks serve admits, and when it stops."]) + """;
  kTabs(document.getElementById('liveV'), function(t){ var i=+t.id.slice(2), n=t.querySelector('.k-tab-count').firstChild.textContent;
    document.getElementById('vgh').textContent=t.firstChild.textContent; document.getElementById('vgt').textContent=n+' settings: '+GT[i];
    document.getElementById('vgp').setAttribute('aria-labelledby', t.id); });
  var ease=tokenEase('ease-standard'), dur=tokenMs('dur-base');
  [].forEach.call(document.querySelectorAll('#film .fr'), function(fr, n){
    var t=fr.querySelectorAll('.k-tab'), a=t[0], b=t[1], bar=fr.querySelector('.k-tabs-bar'), q=n/4, p=ease(q);
    bar.style.transition='none'; bar.style.transform='translateX('+(a.offsetLeft+(b.offsetLeft-a.offsetLeft)*p)+'px)'; bar.style.width=(a.offsetWidth+(b.offsetWidth-a.offsetWidth)*p)+'px';
    fr.querySelector('.ft').textContent=Math.round(dur*q)+'ms · '+Math.round(p*100)+'%'; });
  var host=document.querySelector('.c-anat'), root=document.querySelector('#anatT .k-tabs'), t0=root.querySelectorAll('.k-tab')[0], t1=root.querySelectorAll('.k-tab')[1], ct=getComputedStyle(t0), cu=getComputedStyle(t1);
  anat(host, root, [{kind:'h', of:t0, label:Math.round(t0.getBoundingClientRect().height)+''}, {kind:'gap', a:t0, b:t1},
    {kind:'note', row:0, dx:40, label:'underline '+parseFloat(getComputedStyle(t0,'::after').height)+', in text'}, {kind:'note', row:1, dx:40, label:'label '+parseFloat(cu.fontSize)+' / '+cu.fontWeight+', on '+ct.fontWeight},
    {kind:'note', row:2, dx:40, label:'min width '+parseFloat(ct.minWidth)+', padding '+parseFloat(ct.paddingLeft)}]);
"""
card('Tabs', 'Controls', 868, 'Two or three views of one thing: states, live keyboard with panels, anatomy, motion', body, css, script)
print('ok')
