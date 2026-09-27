from card import card
from beh import BEZ
from comp import ring, steps, chip, stat, keys

L = ['verify', 'review', 'security', 'distill']

# ---------------- Tile
def t(cls, inner, extra=''):
    return f'<div class="k-tile{cls}"{extra}>{inner}</div>'
P = lambda s: f'<p class="tp">{s}</p>'
tiles = [
    ('Default', t('', stat('Spend today', '≥$31.10', 'of $40.00'))),
    ('Ink: the one loud tile', t(' k-tile-ink', '<span class="k-label">Why review sent it back</span>' + P('Quoted phrases are split into words, so “exact phrase” matches the words in any order.'))),
    ('Idle: nothing is happening', t(' k-tile-idle', '<span class="k-label">Queue</span>' + P('2 specs wait: import, then tags.'))),
    ('Ask: needs you', t(' k-tile-ask', '<span class="k-label">Needs you</span>' + P('<span class="k-mk sm you"></span> Approve search-index') + '<span class="k-label" style="margin-top:4px">every check agreed · 12m</span>')),
    ('Alert: stopped or refused', t(' k-tile-alert', '<span class="k-label">kstrl.toml</span>' + P('<span class="k-mk sm fail"></span> 1 problem: every command refuses to start'))),
    ('Work: an agent is working', t(' k-tile-work', f'<div class="hr">{chip("engineer")}<span class="k-label">search-query</span></div><div class="hr" style="margin-top:12px">{ring(7, 10)}<div class="k-stat k-stat-sm"><span class="k-stat-value">8<small>/10</small></span></div></div>')),
    ('Selected (and focused)', t(' k-tile-selected', f'<div class="hr"><span class="tn">search-query</span><span class="k-label">try 2</span></div><div style="margin-top:14px">{steps(["now", "", "", ""], L)}</div>', ' tabindex="0" aria-label="search-query, try 2, verify running, selected"')),
    ('Selected and working', t(' k-tile-selected k-tile-work', f'<div class="hr">{chip("security")}<span class="k-label">search-index</span></div><div style="margin-top:14px">{steps(["done", "done", "work", ""], L)}</div>')),
]
tgrid = ''.join(f'<div class="tv"><span class="c-cap">{n}</span>{h}</div>' for n, h in tiles)
# Sizes: the same content in each, so only the padding differs; each states its padding and radius as rendered (script)
SZ = [('Cell: in a grid of runs', ' k-tile-cell'), ('Dense: six or more', ' k-tile-dense'), ('Default', ''), ('Hero: a stage’s lead tile', ' k-tile-hero')]
SZIN = '<span class="tn">search-query</span><span class="szm szp"></span><span class="szm szr"></span>'
szgrid = ''.join(f'<div class="tv"><span class="c-cap">{n}</span>{t(c + " sz", SZIN)}</div>' for n, c in SZ)
szgrid += ('<div class="tv"><span class="c-cap">Flush: content pads itself</span>'
           + t(' k-tile-flush sz', '<div class="flr"><span class="tn">search-query</span></div><div class="flr"><span class="szm szp"></span><span class="szm szr"></span></div><div class="flr"><span class="szm">each row pads 10 / 16</span></div>') + '</div>')
tbody = f'''<div class="tg">{tgrid}</div>
<div class="c-lab szh">Sizes. A page picks one and never sets a tile’s padding; an idle tile gives its border’s pixel back.</div>
<div class="tsz">{szgrid}</div>
<div class="c-two">
  <div class="c-tile"><div class="c-lab">Edges and the ring compose</div>
    <div class="rules">
      <div><b>State edges</b><p>1.5px, inset, in the state’s own colour (<span class="v-measure">you</span>, <span class="v-measure">fail</span>, <span class="v-measure">work</span>). At least 4.45:1 on every ground, always with a mark and a word.</p></div>
      <div><b>Selection</b><p>One 2px <span class="v-measure">text</span> ring, 2px off, as an outline so it stacks on any edge. Keyboard focus on a tile is the same ring: where you are is never rufous.</p></div>
      <div><b>Idle</b><p>No fill; a 1px dashed <span class="v-measure">line-input</span> edge, 3.12:1 on canvas by day. The padding gives the border’s pixel back, so content lines up.</p></div>
      <div><b>Ink</b><p>One per screen, for the statement that most needs reading. Controls inside it take <span class="v-measure">focus-on-ink</span>.</p></div>
    </div></div>
  <div class="c-tile"><div class="c-lab">Anatomy</div><div class="c-anat" style="height:133px"><div id="anatT" style="position:absolute;left:40px;top:35px;width:230px">{t('', stat('Spend today', '≥$31.10', 'of $40.00'))}</div></div></div>
</div>'''
tcss = """
  .tg { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:22px 18px; }
  .tv { display:flex; flex-direction:column; gap:8px; min-width:0; }
  .tv > .k-tile { min-height:124px; }
  .tp { margin:8px 0 0; font:var(--t-body); }
  .tp .k-mk { vertical-align:-1px; }
  .hr { display:flex; align-items:baseline; gap:10px; } .hr > :is(.k-chip, .k-ring, .k-mk) { align-self:center; } .tn { font:var(--t-body); font-weight:600; }
  .rules { display:grid; grid-template-columns:1fr 1fr; gap:14px 22px; }
  .rules b { font:var(--t-small); font-weight:600; } .rules p { margin:3px 0 0; font:var(--t-small); color:var(--text-2); }
  .rules .v-measure { font:var(--t-measure-inline); color:var(--text); }
  .szh { margin:18px 0 -6px; }
  .tsz { display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:18px; align-items:start; }
  .tsz .tv > .k-tile { min-height:0; }
  .szm { display:block; font:var(--t-measure-small); font-weight:400; color:var(--text-3); } .szp { margin-top:4px; }
  .flr { padding:10px 16px; } .flr + .flr { border-top:1px solid var(--line); }
"""
tscript = r"""
  [].forEach.call(document.querySelectorAll('.sz'), function(z){ var c=getComputedStyle(z);
    z.querySelector('.szp').textContent = 'padding '+(parseFloat(c.paddingTop) ? parseFloat(c.paddingTop)+' / '+parseFloat(c.paddingLeft) : '0');
    z.querySelector('.szr').textContent = 'radius '+parseFloat(c.borderTopLeftRadius); });
  var host=document.querySelector('.c-anat'), tl=document.querySelector('#anatT .k-tile'), cs=getComputedStyle(tl);
  anat(host, tl, [{kind:'h'}, {kind:'pad', value:parseFloat(cs.paddingLeft)}, {kind:'note', row:0, dx:28, label:'radius '+parseFloat(cs.borderTopLeftRadius)+' (radius-xl)'},
    {kind:'note', row:1, dx:28, label:'padding '+parseFloat(cs.paddingTop)+' / '+parseFloat(cs.paddingLeft)}, {kind:'note', row:2, dx:28, label:'window, shadow-card'}]);
"""
card('Tile', 'Surfaces', 862, 'The surface of every screen: kinds, state edges, the selection ring', tbody, tcss, tscript)

# ---------------- Chip
cbody = f'''<div class="c-two">
  <div class="c-tile kt"><div class="c-lab">Kinds: who is working decides the chip</div>
    <div class="cg">
      <span class="c-gr">Maker</span><div class="c-row">{chip('engineer')}{chip('architect')}{chip('distiller')}</div><span class="c-cap">writes code, a plan or facts</span>
      <span class="c-gr">Checker</span><div class="c-row">{chip('reviewer')}{chip('security')}</div><span class="c-cap">judges what a maker wrote</span>
      <span class="c-gr">You</span><div class="c-row">{chip('you')}</div><span class="c-cap">a person at a checkpoint</span>
      <span class="c-gr">On ink</span><div class="k-tile k-tile-ink k-tile-dense cink"><div class="c-row">{chip('engineer')}{chip('reviewer')}</div></div><span class="c-cap">a checker turns light</span>
    </div>
    <p class="c-note">kstrl's verifier and contract tester are not agents: they run commands and get no chip. No chip at all when no agent is working.</p></div>
  <div class="c-tile"><div class="c-lab">Beside the work it names</div>
    <div class="ttlx"><span class="nm">search-query</span>{chip('reviewer')}<span class="tm">try 3 of 4 · checking</span></div>
    <div class="k-tile k-tile-dense" style="margin-top:14px"><div class="hr">{chip('engineer')}<span class="k-label">search-highlight</span></div><p class="tp">Working through review’s three findings from try 1.</p></div>
    <div class="c-lab" style="margin:18px 0 8px">Anatomy</div><div class="c-anat" style="height:68px"><div id="anatC" style="position:absolute;left:40px;top:8px">{chip('reviewer')}</div></div>
  </div></div>'''
ccss = """
  .cg { display:grid; grid-template-columns:78px max-content 1fr; gap:14px 16px; align-items:center; }
  .kt { display:flex; flex-direction:column; } .kt > .c-note { margin-top:auto; padding-top:12px; } /* the footnote sits at the tile's foot */
  .cink { justify-self:start; }
  .ttlx { display:flex; align-items:baseline; gap:12px; } .ttlx .nm { font:var(--t-page-title); letter-spacing:var(--t-page-title-ls); } .ttlx .k-chip { align-self:center; } .ttlx .tm { font:var(--t-small); color:var(--text-2); }
  .hr { display:flex; align-items:center; gap:10px; } .tp { margin:8px 0 0; font:var(--t-small); }
"""
cscript = r"""
  var host=document.querySelector('.c-anat'), c=document.querySelector('#anatC .k-chip'), cs=getComputedStyle(c);
  anat(host, c, [{kind:'h'}, {kind:'w'}, {kind:'note', row:0, dx:24, label:'11 / 600, padding '+parseFloat(cs.paddingLeft)+', radius '+parseFloat(cs.borderTopLeftRadius)}, {kind:'note', row:1, dx:24, label:'maker work on work-tint, checker ink'}]);
"""
card('Chip', 'Readouts', 340, 'Names the agent working: maker, checker, you', cbody, ccss, cscript)

# ---------------- Stat
sbody = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">Sizes: 30px on a tile of its own, 22px among others</div>
    <div class="sg">{stat('Spend today', '≥$31.10', 'of $40.00')}{stat('Parts merged', '1', '/7')}</div>
    <div class="sg" style="margin-top:18px">{stat('Iterations', '25', sm=True)}{stat('Sent back', '1', sm=True)}{stat('Counted', '≥$14.30', sm=True)}</div>
    <div class="c-lab" style="margin:22px 0 8px">Anatomy</div><div class="c-anat" style="height:68px"><div id="anatS" style="position:absolute;left:40px;top:6px">{stat('Spend today', '≥$31.10', 'of $40.00')}</div></div></div>
  <div class="c-tile"><div class="c-lab">The lower bound</div>
    <p class="lb">kstrl adds up what each call reported it cost. A call that reports nothing adds nothing, and kstrl never estimates one, so a total is the least that was spent. It is written with <span class="v-measure">≥</span>, in the measure voice, which is the one that draws it.</p>
    <div class="c-lab" style="margin:18px 0 8px">On ink</div>
    <div class="k-tile k-tile-ink" style="max-width:300px">{stat('Blocking findings, try 3', '2', 'from 5')}</div></div>
</div>'''
scss = """
  .sg { display:flex; gap:40px; align-items:flex-end; }
  .lb { margin:0; font:var(--t-body); max-width:440px; } .lb .v-measure { }
"""
sscript = r"""
  var host=document.querySelector('.c-anat'), v=document.querySelector('#anatS .k-stat-value'), cs=getComputedStyle(v), sm=getComputedStyle(v.querySelector('small'));
  anat(host, v, [{kind:'h'}, {kind:'note', row:0, dx:24, label:parseFloat(cs.fontSize)+' / '+cs.fontWeight+' measure, '+(parseFloat(cs.letterSpacing)/parseFloat(cs.fontSize)).toFixed(2)+'em'}, {kind:'note', row:1, dx:24, label:'unit '+parseFloat(sm.fontSize)+' / '+sm.fontWeight+', text-3'}]);
"""
card('Stat', 'Readouts', 348, 'A measured number with its unit and label; spend as a lower bound', sbody, scss, sscript)

# ---------------- Ring
rstates = [(0, 'iteration 1'), (3, 'iteration 4'), (7, 'iteration 8'), (9, 'the last')]
rrow = ''.join(f'<div class="rc">{ring(d, 10)}<span class="c-cap">{w}</span></div>' for d, w in rrow_src) if False else ''
rrow = ''.join(f'<div class="rc">{ring(d, 10)}<span class="c-cap">{w}</span></div>' for d, w in rstates) + f'<div class="rc">{ring(10, 10, running=False)}<span class="c-cap">ten used</span></div>'
sizes = ''.join(f'<div class="rc">{ring(3, 10, s, w)}<span class="c-cap">{s} / {w}</span></div>' for s, w in [(44, 5), (56, 6), (64, 6)])
dens = f'<div class="rc">{ring(4, 20, 64, 6)}<span class="c-cap">20 at 64: 9.1px each, cut</span></div><div class="rc">{ring(12, 30, 64, 6)}<span class="c-cap">30 at 64: 6.1px, whole</span></div>'
film = ''.join(f'<div class="rc fr">{ring(7, 10, 56, 6)}<span class="c-cap ft"></span></div>' for _ in range(5))
rbody = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">One segment per iteration, up to the part’s limit (kstrl’s default is 10)</div>
    <div class="rr">{rrow}</div>
    <div class="c-lab" style="margin:18px 0 8px">Sizes (diameter / stroke) and when segments are cut</div>
    <div class="rr">{sizes}{dens}</div>
  </div>
  <div class="c-tile"><div class="c-lab">Live: an iteration finishes</div>
    <div class="hr">{ring(7, 10, 64, 6, label='Iteration 8 of up to 10').replace('class="k-ring"', 'class="k-ring" id="liveR"', 1)}<div class="k-stat k-stat-sm"><span class="k-stat-value" id="liveN" aria-live="polite">8<small>/10</small></span></div>
      <button class="k-button k-button-sm" id="next"><span>Finish iteration</span><span class="k-spin" aria-hidden="true"></span></button><button class="k-button k-button-sm k-button-quiet" id="reset"><span>Start over</span><span class="k-spin" aria-hidden="true"></span></button></div>
    <div class="c-lab" style="margin:16px 0 8px">Anatomy</div><div class="c-anat" style="height:83px"><div id="anatR" style="position:absolute;left:40px;top:13px">{ring(7, 10, 64, 6)}</div></div>
  </div></div>
<div class="c-tile"><div class="c-lab">Motion: 8 finishes and 9 starts, both arcs on <span class="v-measure">dur-ring</span>, <span class="v-measure">ease-standard</span></div>
  <div class="rr" id="rfilm" aria-hidden="true" data-audit-film>{film}</div></div>'''
rcss = """
  .rr { display:flex; flex-wrap:wrap; gap:18px; align-items:flex-end; }
  .rc { display:flex; flex-direction:column; align-items:center; gap:6px; }
  .hr { display:flex; align-items:center; gap:12px; }
  .ft { font:var(--t-measure-small); font-weight:400; }
"""
rscript = BEZ + r"""
  var R=document.getElementById('liveR'), N=document.getElementById('liveN'), total=+R.dataset.total, slot=+R.dataset.slot, done=+R.dataset.done;
  var c=2*Math.PI*parseFloat(R.querySelector('.k-ring-done').getAttribute('r'));
  function draw(){ var d=R.querySelector('.k-ring-done'), n=R.querySelector('.k-ring-now'), run=done<total;
    d.style.strokeDasharray=(slot*done)+' '+c; n.style.strokeDashoffset=(-slot*done); n.style.strokeDasharray=(run?slot:0)+' '+c;
    R.setAttribute('aria-label', run ? 'Iteration '+(done+1)+' of up to '+total : 'All '+total+' iterations used');
    N.innerHTML = run ? (done+1)+'<small>/'+total+'</small>' : total+'<small>/'+total+' used</small>';
    document.getElementById('next').setAttribute('aria-disabled', run?'false':'true'); }
  document.getElementById('next').addEventListener('click', function(){ if(done<total){ done++; draw(); } });
  document.getElementById('reset').addEventListener('click', function(){ done=0; draw(); });
  // Filmstrip: iteration 8 finishing, sampled from the real tokens.
  var ease=tokenEase('ease-standard'), dur=tokenMs('dur-ring'), frames=document.querySelectorAll('#rfilm .fr');
  [].forEach.call(frames, function(fr, i){ var q=i/(frames.length-1), p=ease(q), r=fr.querySelector('svg'), s=+r.dataset.slot, cc=2*Math.PI*parseFloat(r.querySelector('.k-ring-done').getAttribute('r'));
    var d=r.querySelector('.k-ring-done'), n=r.querySelector('.k-ring-now'); d.style.transition=n.style.transition='none';
    d.style.strokeDasharray=(s*(7+p))+' '+cc; n.style.strokeDashoffset=(-s*(7+p)); fr.querySelector('.ft').textContent=Math.round(dur*q)+'ms'; });
  var host=document.querySelector('.c-anat'), a=document.querySelector('#anatR svg');
  anat(host, a, [{kind:'h'}, {kind:'note', row:0, dx:28, label:'stroke 6, gap 2 between iterations'}, {kind:'note', row:1, dx:28, label:'done text-3, now work, rest selected'}, {kind:'note', row:2, dx:28, label:'a new arc moves on dur-ring'}]);
"""
card('Ring', 'Readouts', 474, 'Iterations as countable segments: states, sizes, density, live, motion', rbody, rcss, rscript)

# ---------------- Steps
ST = ['Build', 'Check', 'Your approval', 'Merge']
lines = [(['work', '', '', ''], 'work', 'building', 'engineer, iteration 4'), (['done', 'work', '', ''], 'work', 'checking', 'review running'),
         (['done', 'done', 'you', ''], 'you', 'your approval', 'every check agreed'), (['fail', '', '', ''], 'fail', 'stopped', '3 iterations changed nothing'),
         (['done', 'done', 'done', 'done'], 'landed', 'merged', 'CI passing')]
pl = ''.join(f'<div class="pl"><div class="plh"><span class="k-mk sm {m}"></span><b>{w}</b><span class="c-cap">{s}</span></div>{steps(st)}</div>' for st, m, w, s in lines)
seqs = [['now', '', '', ''], ['done', 'done', 'work', ''], ['done', 'fail', '', ''], ['done', 'done', 'done', 'you'], ['done', 'done', 'done', 'done']]
live_seq = [(['now', '', '', ''], 'verify runs the tests'), (['done', 'work', '', ''], 'review reads the diff'), (['done', 'done', 'work', ''], 'security reads it'),
            (['done', 'done', 'done', 'work'], 'the distiller writes facts'), (['done', 'done', 'done', 'done'], 'every check passed')]
import json
live_html = [steps(s, L) for s, _ in live_seq]
sbody2 = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">A part’s line: Build, Check, Your approval, Merge. Unlabelled, so always beside the mark and word it draws.</div>
    <div class="pls">{pl}</div></div>
  <div class="c-tile"><div class="c-lab">Live: one try through its checks</div>
    <div id="liveS" style="width:320px">{live_html[0]}</div>
    <div class="hr" style="margin-top:14px"><button class="k-button k-button-sm" id="adv"><span>Next event</span><span class="k-spin" aria-hidden="true"></span></button><span class="c-cap" id="ev" aria-live="polite">{live_seq[0][1]}</span></div>
    <div class="c-lab" style="margin:22px 0 8px">Anatomy</div><div class="c-anat" style="height:46px"><div id="anatSt" style="position:absolute;left:40px;top:11px;width:250px">{steps(['done', 'work', '', ''], L)}</div></div></div>
</div>
<div class="c-tile"><div class="c-lab">A try’s checks, labelled. From 300px each step names itself; narrower, the labels go to screen readers only and one line names the step that matters now.</div>
  <div class="sq"><span></span><span class="c-cap">320px</span><span class="c-cap">250px</span>{"".join(f'<span class="c-gr">{n}</span><div style="width:320px">{steps(q, L)}</div><div style="width:250px">{steps(q, L)}</div>' for q, n in zip(seqs, ['kstrl runs verify', 'security at work', 'review failed', 'distill waits', 'all passed']))}</div></div>'''
stcss = """
  .pls { display:grid; grid-template-columns:repeat(2, 236px); gap:14px 28px; }
  .pl { width:236px; } .plh { display:flex; align-items:baseline; gap:7px; margin-bottom:8px; font:var(--t-small); } .plh b { font-weight:600; } .plh > .k-mk { align-self:center; }
  .sq { display:grid; grid-template-columns:130px 320px 250px; gap:14px 32px; align-items:start; }
  .hr { display:flex; align-items:center; gap:12px; }
"""
stscript = r"""
  var SEQ=""" + json.dumps([[h, w] for h, (_, w) in zip(live_html, live_seq)]) + r""", k=0, box=document.getElementById('liveS'), ev=document.getElementById('ev');
  document.getElementById('adv').addEventListener('click', function(){ k=(k+1)%SEQ.length; box.innerHTML=SEQ[k][0]; ev.textContent=SEQ[k][1]; });
  var host=document.querySelector('.c-anat'), wrap=document.querySelector('#anatSt .k-steps-wrap'), bar=wrap.querySelector('.k-step-bar'), cs=getComputedStyle(bar), ol=wrap.querySelector('.k-steps');
  anat(host, wrap, [{kind:'note', row:0, dx:24, label:'bar '+parseFloat(cs.height)+', radius '+parseFloat(cs.borderTopLeftRadius)}, {kind:'note', row:1, dx:24, label:'gap '+parseFloat(getComputedStyle(ol).columnGap)+', status 7 below'}]);
"""
card('Steps', 'Readouts', 583, 'A part’s line and a try’s checks, with a cue beyond colour for every state', sbody2, stcss, stscript)
print('ok')

# ---------------- Well
from comp import cmdline
wmeas = '''<div class="k-well"><div class="wm">
  <span class="k-mk pass"></span><span>verify</span><span class="v-measure">31 passed · typecheck · lint · scope</span>
  <span class="k-mk pass"></span><span>review</span><span class="v-measure">0 blocking · 2 advisory</span>
  <span class="k-mk pass"></span><span>security</span><span class="v-measure">0 findings</span>
  <span class="k-mk pass"></span><span>distill</span><span class="v-measure">2 facts recorded</span></div></div>'''
wlog = ('<div class="k-well"><p class="wt">Overlapping matches ("sea" inside "search") still render as two spans. I have not found a way to merge them '
        'without changing the tokenizer, which is outside this part’s scope.</p><span class="v-measure ws">engineer.log · iteration 6</span></div>')
wcode = ('<div class="k-well k-well-code"><span class="k-well-line"><span class="t3">[notify]</span> on_inbox_item = <span class="t2">"your command"</span></span>'
         '<span class="k-well-line">KSTRL_NOTIFY_EVENT=<span class="t2">inbox_merge_gate</span></span>'
         '<span class="k-well-line">KSTRL_NOTIFY_COMPONENT=<span class="t2">search-index</span></span>'
         '<span class="k-well-line">KSTRL_NOTIFY_DETAIL=<span class="t2">search-index awaiting merge approval</span></span></div>')
wcmd = ('<div class="k-well k-well-code wnarrow"><div class="wcr">' + cmdline('ks autonomy promote --actor <you> --ack <why>')
        + '<button class="k-button k-button-sm">Copy</button></div></div>')
walert = ('<div class="k-well k-well-alert"><div class="wh"><span class="v-measure">US-2</span><b>Quoted phrases match exactly</b><span class="grow"></span>'
          '<span class="wv">reviewer: 1 of 1 not met</span></div><div class="wc"><span class="k-mk sm fail"></span><span>A quoted phrase matches only those words, in that order</span></div></div>')
wbody = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">Evidence: what something that did not write the work measured</div>{wmeas}
    <div class="c-lab wl">A log excerpt, with where it came from</div>{wlog}
    <div class="c-lab wl">Alert: a record that failed its check, with its mark and its word</div>{walert}</div>
  <div class="c-tile"><div class="c-lab">Code or configuration: one line each, in <span class="v-measure">measure</span></div>{wcode}
    <div class="c-lab wl">A command in a narrow column: it wraps at its spaces and hangs 2 characters in, so a continuation never reads as a new line</div>{wcmd}
    <div class="c-lab wl">Anatomy</div><div class="c-anat" style="height:95px"><div id="anatW" style="position:absolute;left:40px;top:35px;width:240px"><div class="k-well"><p class="wt">A delete removes the index row.</p><span class="v-measure ws">search-index · passed review</span></div></div></div></div>
</div>'''
wcss = """
  .wl { margin:18px 0 8px; }
  .wm { display:grid; grid-template-columns:16px 72px minmax(0,1fr); column-gap:8px; row-gap:5px; align-items:baseline; } .wm > .k-mk { align-self:center; }
  .wm .v-measure { color:var(--text-2); }
  .wt { margin:0; font:var(--t-small); }
  .ws { display:block; font:var(--t-measure-inline); color:var(--text-3); }
  .wh { display:flex; align-items:baseline; gap:8px; } .wh b { font:var(--t-body); font-weight:600; } .wv { font:var(--t-label); font-weight:400; color:var(--text-2); }
  .wc { display:flex; align-items:center; gap:8px; margin-top:6px; font:var(--t-small); }
  .wnarrow { max-width:300px; } .wcr { display:flex; align-items:center; gap:8px; } .wcr .k-well-line { flex:1; min-width:0; }
"""
wscript = r"""
  var host=document.querySelector('#anatW').parentElement, w=document.querySelector('#anatW .k-well'), cs=getComputedStyle(w);
  anat(host, w, [{kind:'h'}, {kind:'pad', value:parseFloat(cs.paddingLeft)}, {kind:'note', row:0, dx:28, label:'radius '+parseFloat(cs.borderTopLeftRadius)+' (radius-md)'},
    {kind:'note', row:1, dx:28, label:'padding '+parseFloat(cs.paddingTop)+' / '+parseFloat(cs.paddingLeft)}, {kind:'note', row:2, dx:28, label:'fill raised'}]);
"""
card('Well', 'Surfaces', 487, 'A record set into a tile: evidence, a log excerpt, code, a command, a failed check', wbody, wcss, wscript)

# ---------------- Meter (and Bar)
from comp import meter
M = [('The queue', 6, '3 open of 50. At 50, ks serve takes no new spec.'), ('Today’s spend', 78, '<span class="v-measure">≥$31.10</span> of <span class="v-measure">$40.00</span>. At the cap the queue pauses.'),
     ('The distiller’s notes', 91, '3,180 of 3,500 tokens')]
mrows = ''.join(f'<div class="mr"><span class="c-gr">{n}</span><div>{meter(v)}<span class="mw">{w}</span></div></div>' for n, v, w in M)
WEEK = [('snippets', 41.20), ('search', 33.20), ('sharing', 17.40), ('export', 11.90)]
brows = ''.join(f'<div class="br"><span class="bn">{n}</span><span class="bt"><i class="k-bar" style="--k-bar:{v / 41.20 * 100:.1f}%"></i></span><span class="v-measure bv">${v:.2f}</span></div>' for n, v in WEEK)
mbody = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">Meter: how much of a whole is used. The numbers are always written beside it.</div>{mrows}
    <div class="c-lab ml">A count toward a threshold is a step sequence, one step per unit (Steps)</div>
    <div class="mc">{steps(['done'] * 9 + [''] * 6)}<span class="mw">9 of 15 clean merges toward L3</span></div></div>
  <div class="c-tile"><div class="c-lab">Bar: a magnitude in a chart, every bar on one scale against the largest</div>
    <div class="bc">{brows}</div>
    <div class="c-lab ml">Anatomy</div><div class="c-anat" style="height:68px"><div id="anatM" style="position:absolute;left:40px;top:13px;width:170px">{meter(78)}</div><div id="anatB" style="position:absolute;left:40px;top:51px;width:150px"><i class="k-bar" style="--k-bar:100%"></i></div></div></div>
</div>'''
mcss = """
  .mr { display:grid; grid-template-columns:150px minmax(0,1fr); gap:14px; align-items:start; padding:8px 0; }
  .mr + .mr { border-top:1px solid var(--line); }
  .mr .c-gr { font:var(--t-small); } .mr .k-meter { margin-top:6px; }
  .mw { display:block; margin-top:6px; font:var(--t-small); color:var(--text-2); }
  .mw .v-measure { font:var(--t-measure-inline); color:var(--text); }
  .ml { margin:18px 0 8px; }
  .bc { display:grid; gap:10px; }
  .br { display:grid; grid-template-columns:90px minmax(0,1fr) 64px; gap:12px; align-items:center; }
  .bn { font:var(--t-small); } .bv { font:var(--t-measure); text-align:right; }
"""
mscript = r"""
  var host=document.querySelector('#anatM').parentElement, m=document.querySelector('#anatM .k-meter'), b=document.querySelector('#anatB .k-bar'), cm=getComputedStyle(m), cb=getComputedStyle(b);
  anat(host, m, [{kind:'h'}, {kind:'note', row:0, dx:28, label:'meter '+parseFloat(cm.height)+', radius '+parseFloat(cm.borderTopLeftRadius)}, {kind:'note', row:1, dx:28, label:'text-3 on selected, 4.56 / 4.58:1'}]);
  anat(host, b, [{kind:'h'}, {kind:'note', row:0, dx:48, label:'bar '+parseFloat(cb.height)+', data end '+parseFloat(cb.borderTopRightRadius)+', text-3'}]);
"""
card('Meter', 'Readouts', 338, 'How much of a whole is used, and a magnitude in a chart: always with its numbers', mbody, mcss, mscript)

# ---------------- Ref
rbody = '''<div class="c-two">
  <div class="c-tile"><div class="c-lab">A passage of your words and the tag that points at it, matched by the tag’s id</div>
    <p class="rp">A tag is <mark class="k-hl">a short word</mark><span class="k-ref rpin">2</span> such as python or sql. <mark class="k-hl k-hl-ask">Anyone who can see a snippet can see its tags.</mark><span class="k-ref k-ref-ask rpin">?</span> <mark class="k-hl">Tags can be renamed.</mark><span class="k-ref rpin">1</span></p>
    <div class="rd"><span class="k-ref">1</span><div><b>decided</b> Renaming a tag renames it on every snippet.</div></div>
    <div class="rd"><span class="k-ref">2</span><div><b>assumed</b> Lowercase, 1 to 32 characters, pinned as an acceptance criterion so a test checks it.</div></div>
    <div class="rd"><span class="k-ref k-ref-ask">?</span><div><b>asks you</b> Are tags shared by everyone who can see a snippet, or does each person keep their own?</div></div></div>
  <div class="c-tile"><div class="c-lab">Kinds</div>
    <div class="rk"><span class="k-ref">3</span><span>A decision the architect closed itself, or a record it points at</span>
      <span class="k-ref k-ref-ask">?</span><span>A question waiting for you, with the passage it is about in <span class="v-measure">you-tint</span></span>
      <span class="k-ref k-ref-ink">IF-1</span><span>The finding the ink tile states; a row that carries it wears the ink</span></div>
    <div class="c-lab rl">Anatomy</div><div class="c-anat" style="height:80px"><div id="anatR" style="position:absolute;left:40px;top:32px"><span class="k-ref k-ref-ink">IF-1</span></div></div></div>
</div>'''
rcss = """
  .rp { margin:0 0 14px; font:var(--t-intent); max-width:31em; }
  .rpin { margin-left:4px; vertical-align:3px; }
  .rd { display:grid; grid-template-columns:22px 1fr; gap:8px; padding:8px 0; border-top:1px solid var(--line); font:var(--t-small); }
  .rd .k-ref { margin-top:1px; vertical-align:0; justify-self:start; }
  .rd b { font:var(--t-measure-small); font-weight:600; color:var(--text-2); margin-right:4px; }
  .rk { display:grid; grid-template-columns:44px 1fr; gap:12px 10px; align-items:center; font:var(--t-small); }
  .rk .k-ref { justify-self:start; }
  .rl { margin:20px 0 8px; }
"""
rscript = r"""
  var host=document.querySelector('#anatR').parentElement, r=document.querySelector('#anatR .k-ref'), cs=getComputedStyle(r);
  anat(host, r, [{kind:'h'}, {kind:'pad', value:parseFloat(cs.paddingLeft)}, {kind:'note', row:0, dx:28, label:parseFloat(cs.fontSize)+' / '+cs.fontWeight+' measure, radius '+parseFloat(cs.borderTopLeftRadius)},
    {kind:'note', row:1, dx:28, label:'passage: selected, 2px line-strong under'}, {kind:'note', row:2, dx:28, label:'cloned across lines'}]);
"""
card('Ref', 'Readouts', 327, 'A passage of your words and the tag that points at it: a decision, a question, a finding', rbody, rcss, rscript)

# ---------------- Dot
IT = [('7', '3m 31s', '', ''), ('6', '3m 55s', 'f', 'tests failed'), ('5', '4m 48s', '', ''), ('4', '3m 20s', '', ''), ('3', '6m 03s', 't', 'typecheck failed'), ('2', '4m 12s', '', ''), ('1', '5m 40s', 'f', 'tests failed')]
def _dots(f):
    a = ' k-dot-fail' if f == 'f' else ''; b = ' k-dot-fail' if f == 't' else ''
    return f'<span class="k-dot{a}"></span><span class="k-dot{b}"></span>'
irs = ''.join(f'<div class="di"><span class="v-measure">{n}</span><span class="v-measure">{d}</span><span class="dd">{_dots(f)}</span><span class="dw">{w}</span></div>' for n, d, f, w in IT)
KINDS = [('k-dot k-dot-ink', 'interrupts you', 'a notice, a banner'), ('k-dot', 'sent by kstrl', 'a hook, a GitHub comment; a check that agreed'),
         ('k-dot k-dot-ring', 'waits quietly', 'a line in the inbox'), ('k-dot k-dot-none', 'does not reach you', 'nothing is sent'), ('k-dot k-dot-fail', 'a check failed', 'always beside the words that say which')]
kr = ''.join(f'<span class="{c}"></span><span class="dk">{w}</span><span class="c-cap">{u}</span>' for c, w, u in KINDS)
dbody = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">A fast check’s result: tests, then typecheck, after each iteration</div>{irs}
    <p class="c-note"><span class="k-dot"></span><span class="k-dot"></span> tests and typecheck. A dot is never alone: the words beside it say what failed.</p></div>
  <div class="c-tile"><div class="c-lab">Kinds: how something reaches you, and a check’s result</div><div class="dkg">{kr}</div>
    <div class="c-lab dl2">Anatomy</div><div class="c-anat" style="height:32px"><div id="anatD" style="position:absolute;left:40px;top:6px"><span class="k-dot k-dot-ring"></span></div></div></div>
</div>'''
dcss = """
  .di { display:grid; grid-template-columns:18px 64px 22px 1fr; align-items:center; gap:8px; padding:3px 0; }
  .di .v-measure { } .dd { display:inline-flex; gap:4px; } .dw { font:var(--t-small); color:var(--fail); }
  .c-note .k-dot { margin-right:2px; }
  .dkg { display:grid; grid-template-columns:10px 130px 1fr; gap:8px 10px; align-items:baseline; font:var(--t-small); } .dkg > .k-dot { align-self:center; }
  .dk { font-weight:500; }
  .dl2 { margin:18px 0 8px; }
"""
dscript = r"""
  var host=document.querySelector('#anatD').parentElement, dt=document.querySelector('#anatD .k-dot'), cs=getComputedStyle(dt);
  anat(host, dt, [{kind:'h'}, {kind:'note', row:0, dx:24, label:parseFloat(cs.width)+'px, round; the ring 1.5px in text-3'}]);
"""
card('Dot', 'Readouts', 334, 'A small accent that is not a mark: a fast check’s result, how something reaches you', dbody, dcss, dscript)
