from card import card
from beh import BEZ, TOGGLE

# ---------------- Toggle
WORD = '<span class="k-toggle-word"><span class="k-toggle-on">on</span><span class="k-toggle-off">off</span></span>'
def tog(on, state=None, disabled=False, label='Policy checks', tid='', desc=''):
    a = f' role="switch" aria-checked="{"true" if on else "false"}"'
    if state: a += f' data-state="{state}"'
    if disabled: a += ' aria-disabled="true"'
    if desc: a += f' aria-describedby="{desc}"'
    return (f'<button class="k-toggle"{a}{tid} aria-label="{label}"><span class="k-toggle-track"><span class="k-toggle-knob"></span></span>'
            f'{WORD}</button>')
rows = [('Off', tog(False)), ('On', tog(True)), ('Hover, off', tog(False, 'hover')), ('Focus', tog(True, 'focus')), ('Unavailable, off', tog(False, disabled=True)), ('Unavailable, on', tog(True, disabled=True))]
# the control's cell is a flex box, so it is the control's height and the label centres on the control, not on a taller line box
grid = ''.join(f'<span class="c-gr">{n}</span><div class="tcell">{b}</div>' for n, b in rows)
tbody = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">States. The word always says the state; on is ink, never rufous.</div><div class="c-grid" style="grid-template-columns:150px 1fr">{grid}</div>
    <div class="c-lab" style="margin:22px 0 8px">Anatomy</div><div class="c-anat" style="height:70px"><div id="anatTg" style="position:absolute;left:40px;top:10px">{tog(True)}</div></div></div>
  <div class="c-tile"><div class="c-lab">Live: click, or Tab then Space or Enter.</div>
    <div class="set"><div><b>Policy checks</b><span class="k">[policy] enabled</span><p>With it off, the level in force stays at L2 or below.</p></div>{tog(False, tid=' id="tg1"')}</div>
    <div class="set"><div><b>Test adequacy</b><span class="k">[adequacy] enabled</span><p>Checks for deleted tests and removed assertions.</p></div>{tog(True, tid=' id="tg2"', label='Test adequacy')}</div>
    <div class="set"><div><b>Signals polling</b><span class="k">[signals] enabled</span><p>Reads new and growing issues from Bugsink. Queues nothing.</p><p class="lk" id="sigwhy">Set by <span class="v-measure">KSTRL_SIGNALS_ENABLED</span>, which wins over kstrl.toml.</p></div>{tog(True, disabled=True, tid=' id="tg3"', label='Signals polling', desc='sigwhy')}</div>
    <div class="c-lab" style="margin:18px 0 8px">Motion, off to on: knob on <span class="v-measure">dur-base</span>, colours on <span class="v-measure">dur-fast</span>, both <span class="v-measure">ease-standard</span></div>
    <div class="fm" aria-hidden="true" data-audit-film id="tfilm">{''.join('<div class="fr"><span class="k-toggle" aria-checked="false" style="pointer-events:none"><span class="k-toggle-track"><span class="k-toggle-knob"></span></span></span><span class="ft c-cap"></span></div>' for _ in range(6))}</div>
  </div></div>'''
tcss = """
  .tcell { display:flex; align-items:center; }
  .set { display:grid; grid-template-columns:1fr auto; align-items:center; gap:16px; padding:10px 0; border-top:1px solid var(--line); }
  .set b { font:var(--t-body); font-weight:600; margin-right:8px; } .set .k { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .set p { margin:2px 0 0; font:var(--t-label); font-weight:400; color:var(--text-2); }
  .set .lk { color:var(--text-2); } .set .lk .v-measure { font:var(--t-measure-small); font-weight:400; color:var(--text); }
  .fm { display:flex; gap:18px; } .fr { display:flex; flex-direction:column; gap:6px; align-items:flex-start; } .ft { font:var(--t-measure-small); font-weight:400; }
"""
tscript = BEZ + TOGGLE + r"""
  kToggle(document.getElementById('tg1')); kToggle(document.getElementById('tg2')); kToggle(document.getElementById('tg3'));
  // Each frame is the computed state at t: the knob travels on dur-base, the track and knob colours change on dur-fast, both ease-standard.
  // Colours of legacy (hex) tokens transition in sRGB, so color-mix in srgb gives the same in-between values.
  var ease=tokenEase('ease-standard'), dur=tokenMs('dur-base'), fast=tokenMs('dur-fast'), frames=document.querySelectorAll('#tfilm .fr');
  [].forEach.call(frames, function(fr, n){ var t=dur*n/(frames.length-1), pm=ease(Math.min(1, t/dur)), pc=ease(Math.min(1, t/fast));
    var tr=fr.querySelector('.k-toggle-track'), k=fr.querySelector('.k-toggle-knob'); tr.style.transition=k.style.transition='none';
    tr.style.background='color-mix(in srgb, var(--text) '+(100*pc).toFixed(1)+'%, var(--selected))';
    tr.style.boxShadow='inset 0 0 0 1px color-mix(in srgb, var(--line-input) '+(100*(1-pc)).toFixed(1)+'%, transparent)';
    k.style.background='color-mix(in srgb, var(--window) '+(100*pc).toFixed(1)+'%, var(--text-3))';
    k.style.transform='translateX('+(14*pm).toFixed(2)+'px)'; fr.querySelector('.ft').textContent=Math.round(t)+'ms'; });
  var host=document.querySelector('.c-anat'), b=document.querySelector('#anatTg .k-toggle'), tr=b.querySelector('.k-toggle-track'), kn=b.querySelector('.k-toggle-knob');
  anat(host, tr, [{kind:'h'}, {kind:'w'}, {kind:'note', row:0, dx:52, label:'knob '+Math.round(kn.getBoundingClientRect().width)+', travel 14'},
    {kind:'note', row:1, dx:52, label:'hit area '+Math.round(b.getBoundingClientRect().width)+' x '+Math.round(b.getBoundingClientRect().height)}]);
"""
card('Toggle', 'Controls', 422, 'On or off, with the state in words: states, live, anatomy, motion', tbody, tcss, tscript)

# ---------------- Field
_fn = [0]
def field(label, value='', ph='', affix='', measure=False, state=None, invalid='', locked='', fid=''):
    # locked: the value is set somewhere the page cannot change (an env var over kstrl.toml). The input is
    # readonly, not disabled, so it still takes focus, can be selected and copied, and reads its reason.
    _fn[0] += 1
    fid = fid or f'f{_fn[0]}'
    cls = 'k-field' + (' k-field-measure' if measure else '')
    at = (' data-invalid="true"' if invalid else '') + (' data-locked="true"' if locked else '')
    st = f' data-state="{state}"' if state else ''
    msg = ''
    if invalid:
        msg = f'<span class="k-field-msg" id="{fid}-msg"><span class="k-mk sm fail"></span><span>{invalid}</span></span>'
    elif locked:
        msg = f'<span class="k-field-msg" id="{fid}-msg"><span>{locked}</span></span>'
    aff = f'<span class="k-field-affix">{affix}</span>' if affix else ''
    extra = (' aria-invalid="true"' if invalid else '') + (' readonly' if locked else '') + (f' aria-describedby="{fid}-msg"' if msg else '')
    inp = f'<input class="k-field-input" id="{fid}" value="{value}" placeholder="{ph}" aria-label="{label}"{extra}>'
    return f'<div class="{cls}"{at}><div class="k-field-box"{st}>{aff}{inp}</div>{msg}</div>'
fr = [('Rest', field('Tests', 'uv run pytest', measure=True)), ('Hover', field('Tests', 'uv run pytest', measure=True, state='hover')),
      ('Focus', field('Tests', 'uv run pytest', measure=True, state='focus')), ('Empty', field('Typecheck', ph='kstrl’s default: uv run mypy', measure=True)),
      ('With an affix', field('Daily budget', '40.00', affix='$', measure=True)),
      ('Invalid', field('Daily budget', '-5', affix='$', measure=True, invalid='Must be 0 or more. 0 means no budget.')),
      ('Set elsewhere', field('Daily budget', '25.00', affix='$', measure=True, locked='Set by <span class="v-measure">KSTRL_SERVE_DAILY_BUDGET_USD</span>, which wins over kstrl.toml. Change it there.'))]
fgrid = ''.join(f'<span class="c-gr">{n}</span><div style="width:340px">{b}</div>' for n, b in fr)
fbody = f'''<div class="c-two">
  <div class="c-tile"><div class="c-lab">States</div><div class="c-grid fg" style="grid-template-columns:120px 1fr">{fgrid}</div></div>
  <div class="c-tile"><div class="c-lab">Live: type a budget. kstrl refuses a negative or non-numeric value, so the field says so before anything is saved.</div>
    <div style="width:320px">{field('Daily budget', '40.00', affix='$', measure=True, fid='live')}</div>
    <div class="c-lab" style="margin:22px 0 8px">Area: several lines in your words, in <span class="v-measure">intent</span>. It grows as you write.</div>
    <div style="width:420px"><div class="k-field k-field-area"><div class="k-field-box"><textarea class="k-field-input" rows="2" style="max-height:calc(3 * var(--t-intent-lh))" aria-label="Guidance for the engineers">Keep the tokenizer as it is; merge overlapping matches where they are drawn.</textarea></div></div></div>
    <div class="c-lab" style="margin:22px 0 8px">Anatomy</div><div class="c-anat" style="height:80px"><div id="anatF" style="position:absolute;left:40px;top:35px;width:260px">{field('Tests', 'uv run pytest', measure=True)}</div></div>
  </div></div>'''
fcss = """
  .fg { align-items:baseline; } .fg > .c-gr { font:var(--t-small); }
"""
fscript = r"""
  var inp=document.getElementById('live'), wrap=inp.closest('.k-field');
  inp.addEventListener('input', function(){ var v=inp.value.trim(), ok=v!=='' && isFinite(Number(v)) && Number(v)>=0, m=wrap.querySelector('.k-field-msg');
    if(ok){ wrap.removeAttribute('data-invalid'); inp.removeAttribute('aria-invalid'); inp.removeAttribute('aria-describedby'); if(m) m.remove(); }
    else { wrap.setAttribute('data-invalid','true'); inp.setAttribute('aria-invalid','true'); if(!m){ m=document.createElement('span'); m.className='k-field-msg'; m.id='live-msg'; m.innerHTML='<span class="k-mk sm fail"></span><span></span>'; wrap.appendChild(m); }
      inp.setAttribute('aria-describedby','live-msg'); m.lastChild.textContent = v==='' ? 'Needs a number. 0 means no budget.' : (isFinite(Number(v)) ? 'Must be 0 or more. 0 means no budget.' : 'Must be a number, like 40 or 12.50.'); } });
  var host=document.querySelector('.c-anat'), box=document.querySelector('#anatF .k-field-box'), cs=getComputedStyle(box), ci=getComputedStyle(box.querySelector('input'));
  anat(host, box, [{kind:'h'}, {kind:'pad', value:parseFloat(cs.paddingLeft)}, {kind:'note', row:0, dx:28, label:'radius '+parseFloat(cs.borderTopLeftRadius)},
    {kind:'note', row:1, dx:28, label:'value '+parseFloat(ci.fontSize)+' measure'}, {kind:'note', row:2, dx:28, label:'edge 1, line-input'}]);
"""
card('Field', 'Controls', 456, 'One value, with its reason when it is wrong: states, live validation, anatomy', fbody, fcss, fscript)

# ---------------- Keycap
from glyph_svg import key as K
def keys(*chs, extra=''): return '<span class="k-keys">' + ''.join(K(c, extra) for c in chs) + '</span>'
legend_row = ''.join(K(c) for c in ['⌘', '⇧', '⌥', '⌃', '↵', '⌫', '↑', '↓', '←', '→', 'esc', 'tab', '/', 'P'])
pal = [('Add guidance for the engineers', keys('⌘', 'G')), ('Open the engineer’s log', keys('⌘', 'O'))]
palette = ''.join(f'<div class="k-row" aria-selected="{"true" if i == 0 else "false"}"><span></span><span class="k-row-title">{t}</span><span class="k-row-meta">{k}</span></div>'
                  for i, (t, k) in enumerate(pal))
def big1(c):
    n = {'⌘': ('cmd', 'Command'), '⌥': ('opt', 'Option'), '⌃': ('ctrl', 'Control')}
    return f'<span class="bg"><span class="k-kg k-kg-{n[c][0]}" role="img" aria-label="{n[c][1]}"></span></span>' if c in n else f'<span class="bg">{c}</span>'
big = '<span class="gd gd-t"></span><span class="gd gd-b"></span>' + ''.join(big1(c) for c in ['⌘', '⌥', '⌃', '↵', '⇧', '⌫'])
kb = f"""<div class="c-two">
  <div class="c-tile"><div class="c-lab">Legends. Command, Option and Control are drawn; Geist Mono draws the rest.</div>
    <div class="c-row" style="gap:6px">{legend_row}</div>
    <div class="c-lab" style="margin:18px 0 8px">At 33px, on cap-height guides: three drawn, then three from Geist Mono.</div>
    <div class="big" data-illustration="cap-height guides for the legends">{big}</div>
    <div class="c-lab" style="margin:18px 0 8px">Chords: keys side by side, 4px apart, never joined by +</div>
    <div class="c-row" style="gap:18px">{keys('⌘', 'K')}{keys('⇧', '⌘', 'P')}{keys('⌘', '↵')}</div>
    <div class="c-lab" style="margin:18px 0 8px">Beside what it does: right-aligned, in the row it triggers</div>
    <div class="pal" aria-hidden="true">{palette}</div>
    <div class="c-lab" style="margin:18px 0 8px">Sizes</div>
    <div class="c-row" style="gap:18px;align-items:center">{keys('⌘', 'K')}<span class="c-cap">20</span>{keys('⌘', 'K', extra='k-key-sm')}<span class="c-cap">18, k-key-sm</span><span class="k-button k-button-sm" aria-hidden="true"><span>Copy</span>{keys('⌘', 'C')}</span><span class="c-cap">18 inside a small button</span></div>
  </div>
  <div class="c-tile"><div class="c-lab">On every ground it sits on</div>
    <div class="grounds" data-illustration="the surfaces a keycap sits on, as swatches">
      <div class="gr" style="background:var(--window)">{keys('⌘', 'K')}<span class="c-cap">window</span></div>
      <div class="gr" style="background:var(--raised)">{keys('⌘', 'G')}<span class="c-cap">raised</span></div>
      <div class="gr" style="background:var(--selected)">{keys('↵')}<span class="c-cap">selected</span></div>
      <div class="gr" style="background:var(--window)"><span class="k-button k-button-primary k-button-sm" aria-hidden="true"><span>Approve</span>{keys('⌘', '↵', extra='k-key-sm')}</span><span class="c-cap">on primary</span></div>
      <div class="gr k-ink c-ink">{keys('esc')}<span class="cp2">on ink</span></div>
    </div>
    <div class="c-lab" style="margin:16px 0 8px">Anatomy</div><div class="c-anat" style="height:72px"><div id="anatK" style="position:absolute;left:40px;top:12px">{K('esc')}</div></div>
  </div></div>"""
kcss = """
  .pal { display:grid; gap:4px; max-width:420px; }
  /* the legend line at 33px, line-height 1: Geist Mono's caps sit from .145em to .855em of the line (ascent 1.005, descent .295, cap .71) */
  .big { position:relative; display:flex; gap:26px; align-items:center; height:33px; margin:14px 0 10px; padding-left:8px; font:500 33px/1 var(--font-measure); color:var(--text-2); }
  .bg { display:inline-flex; align-items:center; height:33px; }
  .gd { position:absolute; left:0; right:0; height:1px; background:var(--line-strong); }
  .gd-t { top:calc(.145em - .5px); } .gd-b { top:calc(.855em - .5px); }
  .grounds { display:grid; gap:6px; }
  .gr { display:flex; align-items:center; gap:12px; height:36px; padding:0 12px; border-radius:10px; box-shadow:inset 0 0 0 1px var(--line); }
  .gr.c-ink { box-shadow:none; }
  .cp2 { font-size:12px; color:var(--window); }
"""
kscript = r"""
  var host=document.querySelector('.c-anat'), k=document.querySelector('#anatK .k-key'), cs=getComputedStyle(k);
  anat(host, k, [{kind:'h'}, {kind:'w'}, {kind:'note', row:0, dx:24, label:'min width '+parseFloat(cs.minWidth)+', padding '+parseFloat(cs.paddingLeft)}, {kind:'note', row:1, dx:24, label:'legend '+parseFloat(cs.fontSize)+' / '+cs.fontWeight+' measure, radius '+parseFloat(cs.borderTopLeftRadius)}]);
"""
card('Keycap', 'Controls', 479, 'A key or chord, as a hint beside what it does: legends, chords, sizes, every ground', kb, kcss, kscript)
print('ok')
