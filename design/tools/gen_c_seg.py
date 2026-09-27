from beh import BEZ, SEG
from card import card
from glyph_svg import key as K


def ik(*chs): return '<span class="k-keys k-keys-inline">' + ''.join(K(c) for c in chs) + '</span>'

def seg(opts, on, state=None, disabled=(), tag='button', label='Zoom level', ids='', sm=False):
    items = []
    for i, o in enumerate(opts):
        attrs = f' role="radio" aria-checked="{"true" if i == on else "false"}"'
        if tag == 'button':
            attrs += ' tabindex="-1"'
        if state and state[0] == i:
            attrs += f' data-state="{state[1]}"'
        if i in disabled:
            attrs += ' aria-disabled="true"'
        items.append(f'<{tag} class="k-seg-item"{attrs}>{o}</{tag}>')
    return f'<div class="k-seg{" k-seg-sm" if sm else ""}" role="radiogroup" aria-label="{label}"{ids}><span class="k-seg-thumb" aria-hidden="true"></span>{"".join(items)}</div>'
Z = ['Factory', 'Spec', 'Part', 'Step']
states = f'''<div class="c-tile"><div class="c-lab">States (the checked option paints its own ground until the script adds the sliding thumb)</div>
  <div class="c-grid" style="grid-template-columns:150px max-content 1fr; column-gap:28px">
    <span class="c-gr">Rest</span>{seg(Z, 1)}<span class="c-cap">Spec is checked: the thumb ground, text 600, card shadow.</span>
    <span class="c-gr">Hover</span>{seg(Z, 1, (2, 'hover'))}<span class="c-cap">Part under the pointer: its label darkens to text.</span>
    <span class="c-gr">Focus</span>{seg(Z, 1, (1, 'focus'))}<span class="c-cap">Keyboard focus rides on the checked option.</span>
    <span class="c-gr">An option unavailable</span>{seg(Z, 1, None, (3,))}<span class="c-cap">Step with nothing running: text-3, skipped by the arrows.</span>
    <span class="c-gr">Small, k-seg-sm</span>{seg(['Stop at a PR', 'Merge when green'], 0, label='When it is built', sm=True)}<span class="c-cap">28px, for dense rows: a queue item’s options, a setting’s values.</span>
  </div></div>'''
live = f'''<div class="c-tile"><div class="c-lab">Live: Tab in, then any arrow key moves, Home and End jump. Selection follows focus.</div>
  <div class="c-row">{seg(Z, 1, ids=' id="liveZ"')}{seg(['Stage', 'Grid'], 0, label='View', ids=' id="liveV"')}<span class="c-cap" id="readout" aria-live="polite">Spec · Stage</span></div><p class="c-note">On the map the zoom control also answers {ik('⌘', '+')} and {ik('⌘', '−')} from anywhere, and {ik('↵')} on a selection zooms in one level.</p></div>'''
def frame(n):
    checked = 2  # selection flips at t=0; only the thumb travels
    items = ''.join('<span class="k-seg-item" aria-checked="%s">%s</span>' % ('true' if i == checked else 'false', o) for i, o in enumerate(Z))
    return '<div class="fr"><span class="c-cap ft"></span><div class="k-seg k-seg-js"><span class="k-seg-thumb"></span>' + items + '</div></div>'
film = ('<div class="c-tile"><div class="c-lab">Motion: Spec to Part, the thumb only, <span class="v-measure">dur-base</span> on '
        '<span class="v-measure">ease-standard</span></div><div class="fm" aria-hidden="true" data-audit-film id="film">' + ''.join(frame(n) for n in range(5)) + '</div></div>')
anat = f'''<div class="c-tile"><div class="c-lab">Anatomy</div><div class="c-anat" style="height:90px"><div id="anatSeg" style="position:absolute;left:40px;top:13px">{seg(Z, 1)}</div></div></div>'''
body = states + f'<div class="c-two">{live}{anat}</div>' + film
css = """
  .fm { display:grid; grid-template-columns:max-content; row-gap:8px; }
  .fr { display:flex; gap:14px; align-items:center; }
  .fr .ft { width:96px; }
  .ft { font:var(--t-measure-small); font-weight:400; }
"""
script = BEZ + SEG + r"""
  var rz=kSeg(document.getElementById('liveZ'), upd), rv=kSeg(document.getElementById('liveV'), upd);
  function upd(){ var a=[].filter.call(document.querySelectorAll('#liveZ .k-seg-item'), function(i){return i.getAttribute('aria-checked')==='true';})[0];
    var b=[].filter.call(document.querySelectorAll('#liveV .k-seg-item'), function(i){return i.getAttribute('aria-checked')==='true';})[0];
    document.getElementById('readout').textContent=a.textContent+' · '+b.textContent; }
  // Filmstrip: sample the real tokens at 0, 1/4, 1/2, 3/4 and the full duration.
  var ease=tokenEase('ease-standard'), dur=tokenMs('dur-base');
  [].forEach.call(document.querySelectorAll('#film .fr'), function(fr, n){
    var s=fr.querySelector('.k-seg'), th=s.querySelector('.k-seg-thumb'), it=s.querySelectorAll('.k-seg-item'), a=it[1], b=it[2];
    var t=n/4, p=ease(t), x=a.offsetLeft+(b.offsetLeft-a.offsetLeft)*p, w=a.offsetWidth+(b.offsetWidth-a.offsetWidth)*p;
    th.style.transition='none'; th.style.transform='translateX('+x+'px)'; th.style.width=w+'px';
    fr.querySelector('.ft').textContent=Math.round(dur*t)+'ms · '+Math.round(p*100)+'%'; });
  // Anatomy, measured.
  var host=document.querySelector('.c-anat'), s=document.querySelector('#anatSeg .k-seg'), item=s.querySelectorAll('.k-seg-item')[1], cs=getComputedStyle(s), ci=getComputedStyle(item), cu=getComputedStyle(s.querySelectorAll('.k-seg-item')[0]);
  var last=s.querySelectorAll('.k-seg-item')[3];
  anat(host, s, [{kind:'h'}, {kind:'hr', of:last, label:Math.round(last.getBoundingClientRect().height)+' item'},
    {kind:'note', row:0, dx:82, label:'radius '+parseFloat(cs.borderTopLeftRadius)+', item '+parseFloat(ci.borderTopLeftRadius)},
    {kind:'note', row:1, dx:82, label:'padding '+parseFloat(cs.paddingLeft)+', gap '+parseFloat(cs.columnGap||cs.gap)},
    {kind:'note', row:2, dx:82, label:'item padding '+parseFloat(ci.paddingLeft)},
    {kind:'note', row:3, dx:82, label:'label '+parseFloat(cu.fontSize)+' / '+cu.fontWeight},
    {kind:'note', row:4, dx:82, label:'checked '+ci.fontWeight}]);
"""
card('Segmented', 'Controls', 746, 'An exclusive choice among a few short options: states, live keyboard, anatomy, motion', body, css, script)
print('ok')
