import json
from card import card
from beh import BEZ
from comp import ring, steps, chip, stat, keys, ikeys, choices, pcard, notice, notice_compact, ill_tab, ILL_CSS

def K(*c): return keys(*c)

# ---------------- Change: fresh, arrive, typing, ages
film_f = ''.join('<div class="ff"><span class="fv"><span class="fvv">≥$33.20</span></span><span class="c-cap ft"></span></div>' for _ in range(5))
chg = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">Live: an event arrives. Only the value it changed is marked, and the mark fades over <span class="v-measure">dur-fresh</span>, linear.</div>
    <div class="lv">
      <div class="lr"><span class="k-label">Spend today</span><span class="v-measure lvv" id="spend">≥$31.10</span></div>
      <div class="lr"><span class="k-label">search-query</span><span class="lvv" id="stage">in review</span></div>
      <div class="lr"><span class="k-label">engineer, search-index</span><span class="lvv"><span id="age" class="v-measure">output 4s ago</span> <span class="k-typing" id="typ" aria-hidden="true"><i></i><i></i><i></i></span></span></div>
    </div>
    <div class="c-row" style="margin-top:14px"><button class="k-button k-button-sm" id="ev1"><span>component_usage arrives</span><span class="k-spin" aria-hidden="true"></span></button><button class="k-button k-button-sm" id="ev2"><span>phase_completed arrives</span><span class="k-spin" aria-hidden="true"></span></button><button class="k-button k-button-sm k-button-quiet" id="ev3"><span>Output stops</span><span class="k-spin" aria-hidden="true"></span></button></div>
    <p class="c-note">Ages tick once a second; past 60s an age reads plainly, “no output for 2m”, because kstrl does not act on silence. The typing dots show only while the output grows.</p></div>
  <div class="c-tile"><div class="c-lab">Motion: the fade, sampled from the tokens</div>
    <div class="rr" aria-hidden="true" id="ffilm" data-audit-film>{film_f}</div>
    <div class="c-lab" style="margin:18px 0 8px">Typing: three 4px <span class="v-measure">work</span> dots, each brightening in turn over <span class="v-measure">dur-typing</span></div>
    <div class="c-row"><span class="k-typing" aria-hidden="true"><i></i><i></i><i></i></span><span class="c-cap">1200ms a cycle; each dot 200ms after the one before</span></div>
    <div class="c-lab" style="margin:18px 0 8px">Reduced motion</div>
    <p class="rm">The mark still holds for 2 seconds, then goes at once. The dots stand still at three brightnesses. Nothing moves either way.</p></div>
</div>'''
chg_css = """
  .lv { display:grid; gap:10px; }
  .lr { display:grid; grid-template-columns:170px 1fr; align-items:baseline; }
  .lvv { justify-self:start; display:inline-flex; align-items:center; gap:8px; }
  .lvv.v-measure { }
  .rr { display:flex; gap:18px; flex-wrap:wrap; }
  .ff { display:flex; flex-direction:column; align-items:flex-start; gap:6px; }
  .fv { font:var(--t-measure); } .fvv { display:inline-block; border-radius:4px; }
  .ft { font:var(--t-measure-small); font-weight:400; }
  .rm { margin:0; font:var(--t-small); color:var(--text-2); }
"""
chg_js = BEZ + r"""
  function fresh(el){ el.classList.remove('k-fresh'); void el.offsetWidth; el.classList.add('k-fresh'); el.addEventListener('animationend', function h(){ el.classList.remove('k-fresh'); el.removeEventListener('animationend', h); }); }
  var spend=31.10, s=document.getElementById('spend'), st=document.getElementById('stage'), age=document.getElementById('age'), typ=document.getElementById('typ'), secs=4, growing=true;
  document.getElementById('ev1').addEventListener('click', function(){ spend+=2.10; s.textContent='≥$'+spend.toFixed(2); fresh(s); });
  document.getElementById('ev2').addEventListener('click', function(){ st.textContent = st.textContent==='in review' ? 'in security' : 'in review'; fresh(st); });
  document.getElementById('ev3').addEventListener('click', function(){ growing=!growing; typ.hidden=!growing; this.querySelector('span').textContent = growing ? 'Output stops' : 'Output resumes'; if(growing){ secs=0; } });
  setInterval(function(){ secs = growing ? 0 : secs+1; age.textContent = secs<60 ? 'output '+secs+'s ago' : 'no output for '+Math.floor(secs/60)+'m'; }, 1000);
  // filmstrip: the fade is linear in time, so frame n shows the start colour at (1 - n/4) strength
  var dur=tokenMs('dur-fresh');
  [].forEach.call(document.querySelectorAll('#ffilm .ff'), function(f, n){ var q=n/4, v=f.querySelector('.fvv'), k=(100*(1-q)).toFixed(0);
    v.style.background='color-mix(in srgb, var(--selected) '+k+'%, transparent)'; v.style.boxShadow='0 0 0 3px color-mix(in srgb, var(--selected) '+k+'%, transparent)';
    f.querySelector('.ft').textContent=Math.round(dur*q)+'ms'; });
"""
card('Change', 'Live', 308, 'The only motion: a changed value marked where it changed, ages that tick, typing dots', chg, chg_css, chg_js)

# ---------------- Needs you
def need(title, sub, ask=True, state=None, extra=''):
    st = f' data-state="{state}"' if state else ''
    return f'<button class="k-need{" k-need-ask" if ask else ""}"{st}{extra}><span class="k-mk {"you" if ask else "fail"}"></span><b>{title}</b><span class="k-need-sub">{sub}</span></button>'
nd = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">States</div>
    <div class="ng">
      <span class="c-gr">An ask</span><div>{need('Approve search-index', 'every check agreed · 12m')}</div>
      <span class="c-gr">Hover</span><div>{need('Approve search-index', 'every check agreed · 12m', state='hover')}</div>
      <span class="c-gr">Focus</span><div>{need('Approve search-index', 'every check agreed · 12m', state='focus')}</div>
      <span class="c-gr">A stopped part</span><div>{need('search-highlight stopped', 'retry it, or add guidance first', ask=False)}</div>
    </div>
    <p class="c-note">Only two kinds reach this strip: asks, and parts that stopped. Health and calibration notices wait in the inbox and never interrupt.</p></div>
  <div class="c-tile"><div class="c-lab">Live: a second ask arrives. It lands once, from 20% <span class="v-measure">you</span>, and the tab gains its count.</div>
    <div class="tabrow">{ill_tab('(1) kstrl', 'tabt', hidden=True)}</div>
    <div class="c-stage strip"><span class="k-label sl">Needs you</span><div class="items" id="items" aria-live="polite">{need('Approve search-index', 'every check agreed · 12m')}</div></div>
    <div class="c-row" style="margin-top:14px"><button class="k-button k-button-sm" id="arr"><span>An ask arrives</span><span class="k-spin" aria-hidden="true"></span></button><button class="k-button k-button-sm k-button-quiet" id="clr"><span>Start over</span><span class="k-spin" aria-hidden="true"></span></button></div>
    <div class="c-lab" style="margin:18px 0 8px">Anatomy</div><div class="c-anat" style="height:89px"><div id="anatN" style="position:absolute;left:40px;top:35px">{need('Approve search-index', 'every check agreed · 12m')}</div></div></div>
</div>'''
nd_css = """
  .ng { display:grid; grid-template-columns:120px 1fr; gap:12px 16px; align-items:center; }
  .strip { display:flex; align-items:center; gap:12px; min-height:60px; padding:8px 12px; }
  .sl { width:64px; flex:none; } .items { display:flex; gap:10px; flex-wrap:wrap; }
  .tabrow { display:flex; margin-bottom:10px; }
""" + ILL_CSS + """
"""
nd_js = r"""
  var n=1, items=document.getElementById('items'), tab=document.getElementById('tabt');
  document.getElementById('arr').addEventListener('click', function(){ if(n>=2) return; n++;
    var b=document.createElement('button'); b.className='k-need k-need-ask k-arrive';
    b.innerHTML='<span class="k-mk you"></span><b>Answer a question about search-rank</b><span class="k-need-sub">the architect asks · just now</span>';
    b.addEventListener('animationend', function(){ b.classList.remove('k-arrive'); });
    items.appendChild(b); tab.textContent='('+n+') kstrl'; });
  document.getElementById('clr').addEventListener('click', function(){ while(items.children.length>1) items.removeChild(items.lastChild); n=1; tab.textContent='(1) kstrl'; });
  var host=document.querySelector('.c-anat'), el=document.querySelector('#anatN .k-need'), cs=getComputedStyle(el);
  anat(host, el, [{kind:'h'}, {kind:'pad', value:parseFloat(cs.paddingLeft)}, {kind:'note', row:0, dx:28, label:'min height 44, radius '+parseFloat(cs.borderTopLeftRadius)}, {kind:'note', row:1, dx:28, label:'you-tint, 1px you edge'}]);
"""
card('NeedsYou', 'Live', 396, 'What waits on you, at the foot of every screen: states, arrival, anatomy', nd, nd_css, nd_js)

# ---------------- Notice
film_in = ''.join(f'<div class="nf"><div class="nfs">{notice()}</div><span class="c-cap ft"></span></div>' for _ in range(4))
nt = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">The card: kind, what it is, what waits on it, and two ways out. Never Approve: you approve where the evidence is.</div>
    <div class="c-anat na"><div id="anatT" class="nanat">{notice()}</div></div>
    <p class="c-cap nnotes" id="nnotes"><span></span><span></span></p>
    <p class="c-note">Only an ask gets a card, and it never dismisses itself. It does not take focus: it is announced, and F6 moves focus to Review. Inside it, esc means Later.</p>
    <div class="c-lab" style="margin:16px 0 8px">Compact: as a page shows it rather than as it arrives (Notifications)</div><div style="width:260px">{notice_compact()}</div></div>
  <div class="c-tile"><div class="c-lab">Live: it arrives while you work</div>
    <div class="c-stage stage" id="stage2"><span class="c-cap" id="note2" aria-live="polite">Nothing waiting.</span></div>
    <div class="c-row" style="margin-top:12px"><button class="k-button k-button-sm" id="come"><span>An ask arrives</span><span class="k-spin" aria-hidden="true"></span></button><span class="c-cap">then F6, and {ikeys('esc')} or {ikeys('↵')}</span></div>
    <div class="c-lab" style="margin:18px 0 8px">Motion: enters on <span class="v-measure">dur-enter</span>, <span class="v-measure">ease-enter</span> (8px up, fading in); leaves on <span class="v-measure">dur-exit</span>, <span class="v-measure">ease-exit</span></div>
    <div class="films" aria-hidden="true" id="nfilm" data-audit-film>{film_in}</div></div>
</div>'''
nt_css = """
  .stage { position:relative; height:260px; padding:12px; box-sizing:border-box; overflow:hidden; }
  .stage .k-notice { position:absolute; right:12px; top:12px; width:340px; }
  .films { display:flex; gap:10px; }
  .nf { display:flex; flex-direction:column; gap:6px; width:112px; }
  .na { height:232px; } .nanat { position:absolute; left:0; top:32px; } .nnotes { margin:4px 0 0; font:var(--t-measure-small); font-weight:400; } .nnotes span { display:block; }
  .nfs { position:relative; height:66px; overflow:hidden; border-radius:8px; background:var(--canvas); box-shadow:inset 0 0 0 1px var(--line); }
  .nfs .k-notice { position:absolute; left:0; top:4px; width:372px; max-width:none; transform-origin:0 0; }
  .ft { font:var(--t-measure-small); font-weight:400; }
"""
nt_js = BEZ + r"""
  var stage=document.getElementById('stage2'), note=document.getElementById('note2'), TPL=""" + json.dumps(notice(' tabindex="-1" id="live-notice"', 'enter')) + r""";
  function close(n, why){ n.dataset.motion='exit'; n.addEventListener('animationend', function(){ n.remove(); note.textContent = why; }, {once:true}); }
  document.getElementById('come').addEventListener('click', function(){ if(document.getElementById('live-notice')) return;
    stage.insertAdjacentHTML('beforeend', TPL); var n=document.getElementById('live-notice'); note.textContent='Merge approval: search-index is ready to merge.';
    n.querySelector('.nl').addEventListener('click', function(){ close(n, 'Later: it stays in Needs you.'); });
    n.querySelector('.nr').addEventListener('click', function(){ close(n, 'Review: this lands on the inbox item, beside the evidence.'); });
    n.addEventListener('keydown', function(e){ if(e.key==='Escape'){ e.preventDefault(); close(n, 'Later: it stays in Needs you.'); } }); });
  document.addEventListener('keydown', function(e){ if(e.key==='F6'){ var n=document.getElementById('live-notice'); if(n){ e.preventDefault(); n.querySelector('.nr').focus(); } } });
  // anatomy, measured from the notice as it renders
  var ah=document.querySelector('.na'), an=document.querySelector('#anatT .k-notice'), acs=getComputedStyle(an);
  anat(ah, an, [{kind:'pad', value:parseFloat(acs.paddingLeft)}, {kind:'hr'}, {kind:'w'}]);
  var nn=document.getElementById('nnotes'); nn.children[0].textContent='window, radius '+parseFloat(acs.borderTopLeftRadius)+' (radius-lg), shadow-panel'; nn.children[1].textContent='padding '+parseFloat(acs.paddingTop)+' by '+parseFloat(acs.paddingLeft)+', the kind in you';
  // filmstrip: entering, sampled from the tokens (scaled to fit the frame)
  var ease=tokenEase('ease-enter'), dur=tokenMs('dur-enter');
  [].forEach.call(document.querySelectorAll('#nfilm .nf'), function(f, i){ var q=i/3, p=ease(q), n=f.querySelector('.k-notice');
    n.style.opacity=p.toFixed(3); n.style.transform='scale(.3) translateY('+(8*(1-p)).toFixed(2)+'px)'; f.querySelector('.ft').textContent=Math.round(dur*q)+'ms'; });
"""
card('Notice', 'Live', 559, 'An ask arriving while you work: never auto-dismissed, never approving, F6 to reach', nt, nt_css, nt_js)

# ---------------- Choice
CH = [('Approve', ('↵',), 'Pushes kstrl/factory/search-index and merges its PR. search-index is done once the merge is confirmed.', False),
      ('Retry', ('⌘', '↵'), 'The engineer runs it again, told that a person asked for changes. Your own words are not passed on. Uses 1 of 3 retries.', False),
      ('Reject', ('⌘', '⌫'), 'search-index fails and search-api is skipped. Nothing is pushed.', True),
      ('Later', ('esc',), 'It keeps waiting here. Nothing times out.', False)]
ST = [('Rest', (), 'A plain row; the consequence always visible.', False), ('Hover', (), 'raised.', False), ('Chosen', ('↵',), 'you-tint, a 1px you edge, the title in you.', False),
      ('Chosen, destructive', ('⌘', '⌫'), 'The title stays fail.', True), ('Focus', (), 'The 2px focus ring, 2px off.', False)]
chs = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">Live: the checkpoint’s decisions. Arrows move the choice; the button names it; each decision’s own keys act on it directly.</div>
    {choices(CH, 0, 'Your decision on search-index', ' id="chg"')}
    <div class="c-row" style="margin-top:12px"><button class="k-button k-button-primary" id="doit"><span>Approve</span>{K('↵')}<span class="k-spin" aria-hidden="true"></span></button><span class="c-cap" id="said" aria-live="polite">The button always names the choice.</span></div></div>
  <div class="c-tile"><div class="c-lab">States</div>
    <div class="stg">{choices(ST[:2], -1, 'States, not chosen', state={1: 'hover'})}{choices(ST[2:3], 0, 'Chosen')}{choices(ST[3:4], 0, 'Chosen, destructive')}{choices(ST[4:], 0, 'Focus', state={0: 'focus'})}</div></div>
</div>'''
chs_css = """
  .stg { display:grid; gap:4px; }
"""
chs_js = r"""
  var g=document.getElementById('chg'), items=[].slice.call(g.querySelectorAll('.k-choice')), btn=document.getElementById('doit'), said=document.getElementById('said');
  var KEYS=""" + json.dumps([t for t, _, _, _ in CH]) + r""", KEYHTML=""" + json.dumps([keys(*k) for _, k, _, _ in CH]) + r""";
  function sel(i, focus){ items.forEach(function(x, j){ x.setAttribute('aria-checked', j===i?'true':'false'); x.tabIndex = j===i?0:-1; }); if(focus) items[i].focus();
    btn.querySelector('span').textContent=KEYS[i]; btn.className='k-button ' + (i===2 ? 'k-button-danger' : i===3 ? '' : 'k-button-primary');
    btn.querySelector('.k-keys').outerHTML=KEYHTML[i]; }
  items.forEach(function(x, i){ x.addEventListener('click', function(){ sel(i, false); });
    x.addEventListener('keydown', function(e){ var t=null; if(e.key==='ArrowDown'||e.key==='ArrowRight') t=(i+1)%items.length; if(e.key==='ArrowUp'||e.key==='ArrowLeft') t=(i-1+items.length)%items.length; if(e.key===' '){ t=i; }
      if(t!==null){ e.preventDefault(); sel(t, true); } }); });
  btn.addEventListener('click', function(){ said.textContent='You chose '+btn.querySelector('span').textContent+'.'; });
"""
card('Choice', 'Controls', 468, 'One of a few decisions, each with its keys and its consequence written before you take it', chs, chs_css, chs_js)

# ---------------- Row
def row(mk, title, sub, meta, sel=False, ask=False, state=None, tab=-1):
    a = f' aria-selected="{"true" if sel else "false"}"' + (f' data-state="{state}"' if state else '')
    m = '<span class="k-dot k-dot-ring" aria-hidden="true"></span>' if mk == 'notice' else f'<span class="k-mk {mk}"></span>'
    return (f'<div class="k-row{" k-row-ask" if ask else ""}" role="option"{a} tabindex="{tab}">{m}'
            f'<span><span class="k-row-title">{title}</span><span class="k-row-sub">{sub}</span></span><span class="k-row-meta">{meta}</span></div>')
INB = [('you', 'Approve search-index', 'every check agreed; search-api waits on it', '12m', True),
       ('fail', 'search-highlight stopped', '3 iterations changed nothing', '40m', False),
       ('you', 'Answer a question about search-rank', 'the architect asks how many matches to show', '1h', True),
       ('notice', 'retry_rate is above its control limit', 'health check, seen twice · closing it changes nothing', 'Tue', False),
       ('pass', 'Calibration finished', 'review detection held at its floor', '3h', False)]
live_rows = ''.join(row(m, t, s, me, i == 0, a, tab=0 if i == 0 else -1) for i, (m, t, s, me, a) in enumerate(INB))
rw = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">Live: the inbox. Up and Down move, Home and End jump; {ikeys('↵')} opens the selected row.</div>
    <div class="rl" role="listbox" aria-label="Inbox" id="rl">{live_rows}</div></div>
  <div class="c-tile"><div class="c-lab">States</div>
    <div class="rl">{row('fail', 'Rest', 'one line, in text-2', '40m')}{row('fail', 'Hover', 'raised', '40m', state='hover')}{row('fail', 'Selected', 'selected', '40m', True)}{row('you', 'Selected, an ask', 'you-tint and a 1px you edge', '12m', True, True)}{row('fail', 'Focus', 'the 2px focus ring, inside the list’s gutter', '40m', True, state='focus')}</div></div>
</div>'''
rw_css = """
  .rl { display:grid; gap:4px; padding:4px; }
"""
rw_js = r"""
  var l=document.getElementById('rl'), rs=[].slice.call(l.querySelectorAll('.k-row'));
  function go(i){ rs.forEach(function(r, j){ r.setAttribute('aria-selected', j===i?'true':'false'); r.tabIndex = j===i?0:-1; }); rs[i].focus(); }
  rs.forEach(function(r, i){ r.addEventListener('click', function(){ go(i); });
    r.addEventListener('keydown', function(e){ var t=null; if(e.key==='ArrowDown') t=Math.min(rs.length-1, i+1); if(e.key==='ArrowUp') t=Math.max(0, i-1); if(e.key==='Home') t=0; if(e.key==='End') t=rs.length-1; if(t!==null){ e.preventDefault(); go(t); } }); });
"""
card('Row', 'Surfaces', 388, 'One item in a list: the inbox, the queue, the command window', rw, rw_css, rw_js)

# ---------------- Part card
from parts import args as part
PC = [part(n) for n in ('search-query', 'search-index', 'search-highlight', 'search-schema', 'search-api')]
cards = ''.join(pcard(*p, sel=(i == 0), tab=0 if i == 0 else -1) for i, p in enumerate(PC[:4]))
pcb = f'''<div class="pcg">{cards}</div>
<div class="c-two">
  <div class="c-tile"><div class="c-lab">What a card says, top to bottom</div>
    <dl class="rules"><dt>Head</dt><dd>its mark and name; the agent working as a chip, or the state in a word when none is</dd>
      <dt>Line</dt><dd>Build, Check, Your approval, Merge (Steps, unlabelled: the mark and word above say the state)</dd>
      <dt>Sentence</dt><dd>what is happening, written to fit one line</dd>
      <dt>Tries</dt><dd>a dot each: filled <span class="v-measure">fail</span> for a try that failed, a <span class="v-measure">work</span> ring for the one running now</dd>
      <dt>Waiting</dt><dd>no fill and a dashed edge, like an idle tile, with the wait mark and what it needs; a skipped part the same, in text-3</dd></dl></div>
  <div class="c-tile"><div class="c-lab">Anatomy</div><div class="c-anat" style="height:140px"><div id="anatP" style="position:absolute;left:40px;top:35px;width:236px">{pcard(*PC[1])}</div></div></div>
</div>'''
pc_css = """
  .pcg { display:grid; grid-template-columns:repeat(4, 236px); gap:16px; justify-content:space-between; padding:4px 4px 0; }
  .rules { display:grid; grid-template-columns:72px 1fr; gap:8px 12px; margin:0; font:var(--t-small); } .rules dt { color:var(--text-2); } .rules dd { margin:0; }
  .rules .v-measure { }
"""
pc_js = r"""
  var host=document.querySelector('.c-anat'), el=document.querySelector('#anatP .k-card'), cs=getComputedStyle(el);
  anat(host, el, [{kind:'h'}, {kind:'pad', value:parseFloat(cs.paddingLeft)}, {kind:'note', row:0, dx:28, label:'radius '+parseFloat(cs.borderTopLeftRadius)+' (radius-lg)'}, {kind:'note', row:1, dx:28, label:'padding '+parseFloat(cs.paddingTop)+' / '+parseFloat(cs.paddingLeft)}, {kind:'note', row:2, dx:28, label:'state edge 1px'}]);
"""
card('PartCard', 'Surfaces', 426, 'A part on the Spec level: who works on it, its line, one sentence, its tries', pcb, pc_css, pc_js)
# ---------------- Live (Foundations): one card across ten minutes, and the rules every live view keeps
def fr(v):
    return f'<span class="k-fresh" data-state="still">{v}</span>'
HL = ('work', 'search-highlight')
LIVE = [
    ('20:31:02', 'worker_heartbeat',
     pcard(*HL, chip('engineer'), ['work', '', '', ''], 'iteration 4 of up to 10', 'try 1 of 4', ['now', '', '', ''], ' k-card-work'),
     'Nothing on the card changes. The header’s “last event” age starts again.'),
    ('20:35:40', 'iteration_started 5',
     pcard(*HL, chip('engineer'), ['work', '', '', ''], f'{fr("iteration 5")} of up to 10', 'try 1 of 4', ['now', '', '', ''], ' k-card-work'),
     'The value that changed is marked, and the mark fades over 2 s.'),
    ('20:40:12', 'phase_completed engineer · component_usage',
     pcard(*HL, fr('checking'), ['done', 'work', '', ''], 'in verify', f'try 1 of 4 · {fr("≥$2.60")}', ['now', '', '', ''], ' k-card-work', fresh_step=1),
     'The engineer’s phase ended, so its spend is counted and appears. Verify runs no agent, so the card names the state, not a role.'),
    ('20:41:30', 'review_result · component_usage · component_retrying 2',
     pcard(*HL, chip('engineer'), ['work', '', '', ''], fr('sent back: review found 3 blocking'), f'try 2 of 4 · {fr("≥$3.10")}', ['fail', 'now', '', ''], ' k-card-work', fresh_tries=True),
     'Each value that changed is marked: the reason, the tries, the spend. The engineer starts try 2 at once, so the line is back at Build.'),
]
RULES = [
    ('The layout never moves.', 'The plan fixes where every card is for the whole run; events change what is inside them.'),
    ('Say whether the view is live.', '“live · last event 3s ago” while events arrived in the last 60 s; otherwise “not live · last event 21:14”.'),
    ('Freshness is an age, not a colour.', 'A card whose agent is silent past 60 s says “no output for 2m”, plainly: kstrl does not act on silence.'),
    ('What you are reading does not change under you.', 'A notice offers the update (“try 3 finished: review failed · Show”); a growing log follows the end only while you are at the end.'),
    ('Spend moves when a phase ends.', 'A card shows a part’s spend once one of its phases has ended, and only the try before that.'),
    ('A new ask arrives once.', 'It appears in Needs you with the same fade, and the tab title gains its count: “(2) kstrl”.'),
]
lv = ('<div class="lv">' + ''.join(f'<figure class="lf"><figcaption class="lt">{tm}</figcaption>{c}<p class="le">{ev}</p><p class="ln">{nt}</p></figure>'
                                   for tm, ev, c, nt in LIVE) + '</div>'
      + '<div class="c-tile"><div class="c-lab">The rules every live view keeps</div><div class="lr">'
      + ''.join(f'<p><b>{h}</b> {b}</p>' for h, b in RULES) + '</div></div>')
lv_css = """
  .lv { display:grid; grid-template-columns:repeat(4, 236px); justify-content:space-between; padding:0 4px; }
  .lf { position:relative; margin:0; }
  .lf + .lf::before { content:"→"; position:absolute; left:-22px; top:72px; font:var(--t-measure); color:var(--text-3); }
  .lt { margin:0 0 8px; font:var(--t-measure-inline); color:var(--text-3); }
  .le { margin:10px 0 0; font:var(--t-measure-small); font-weight:400; color:var(--text-2); }
  .ln { margin:4px 0 0; font:var(--t-small); color:var(--text-2); }
  .lr { display:grid; grid-template-columns:1fr 1fr; gap:8px 40px; font:var(--t-small); } .lr p { margin:0; } .lr b { font-weight:600; }
"""
card('Live', 'Foundations', 509, 'Live: one card over ten minutes, and the rules', lv, lv_css)
print('ok')
