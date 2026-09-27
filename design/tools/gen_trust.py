"""Trust: the autonomy ladder, where snippetvault is on it, what the next level needs, and what lowers it."""
from __future__ import annotations

from pathlib import Path

from comp import cmdline, steps
from level_common import page, top

# nine of fifteen clean merges, drawn as a step sequence (the words under it say the numbers)
TICKS = steps(['done'] * 9 + [''] * 6)

steps = [
  ('L1', 'Supervised', ['Every merge waits for you.', 'Plans need your approval (nothing reads this yet).'], 'past'),
  ('L2', 'Gated merge', ['Every merge waits for you.', 'Plans go ahead without you.'], 'now'),
  ('L3', 'Enveloped auto-merge', ['Parts merge themselves when green.', 'New dependencies are allowed.'], 'next'),
  ('L4', 'Deploy', ['L3, and deploys are allowed.'], 'later'),
]
TILE = {'past': 'k-tile k-tile-dense', 'now': 'k-tile k-tile-dense k-tile-ink', 'next': 'k-tile k-tile-dense', 'later': 'k-tile k-tile-dense k-tile-idle'}
lad = ''.join(f'''<div class="{TILE[cls]} st {cls}" style="margin-top:{(3-i)*26}px"><div class="sl"><span class="lv">{lv}</span><span class="nm2">{nm}</span></div>{''.join(f'<p>{b}</p>' for b in bl)}{'<div class="here">in force</div>' if cls=='now' else ''}{'<div class="tk">' + TICKS + '</div><div class="tkl">9 of 15 clean merges in a row</div>' if cls=='next' else ''}</div>''' for i, (lv, nm, bl, cls) in enumerate(steps))
crit = [
  ('pass', 'Runs that decided something, at L2', '11', 'needs 8'),
  ('wait', 'Clean merges in a row', '9', 'needs 15'),
  ('absent', 'Policy violations', '0', 'none can occur: off'),
  ('pass', 'Cool-down after a demotion', 'none', ''),
  ('absent', 'Policy checks', 'off', 'L3 runs as L2 until on'),
]
crows = ''.join(f'<div class="cr"><span class="k-mk sm {m}"></span><span>{t}</span><b>{v}</b><em>{n}</em></div>' for m, t, v, n in crit)
body = '    ' + top('<span class="nm">Trust</span>', 'snippetvault · L2 in force · merges wait for you', None, ['Ladder', 'History', 'Replay'], 0) + f'''
    <div class="tr">
      <div class="lad"><div class="k-label ladl">How much kstrl may do without you</div>{lad}</div>
      <div class="k-tile nx">
        <div class="k-label">What L3 needs</div>
        <div class="crs">{crows}</div>
        <p class="ft">A merge counts as clean when its part completes; kstrl does not record whether you changed the code first. Every threshold here is a placeholder in kstrl, not a measured value. Evidence counts only while [autonomy] is on in kstrl.toml, as it is here, and is kept in autonomy.json in your kstrl state directory.</p>
      </div>
      <div class="k-tile pr">
        <div class="k-label">Raising it</div>
        <p>Promotion happens only in a terminal, one level at a time, with your name and your reason. An agent can pass both; a controlling terminal is what an unattended process does not have.</p>
        <div class="k-well k-well-code cmdw"><div class="cmd">{cmdline('ks autonomy promote --actor <you> --ack <why>')}<button class="k-button k-button-sm">Copy</button></div></div>
        <p class="t3n">Refused today at 9 of 15. <span class="v-measure">--force</span> records the override, and L3 still runs as L2 while policy checks are off.</p>
      </div>
      <div class="k-tile dm">
        <div class="k-label">Lowering it</div>
        <div class="ar"><span class="k-mk sm absent"></span><span>on a policy violation</span><em>never, policy is off</em></div>
        <div class="ar"><span class="k-mk sm absent"></span><span>on a health breach</span><em>off</em></div>
        <div class="ar"><span class="k-mk sm absent"></span><span>on a calibration slip</span><em>off</em></div>
        <p class="warn">Nothing lowers trust on its own in this project.</p>
        <button class="k-button">Lower to L1 now</button>
        <p class="t3n">One level down, then 10 runs before it can rise again.</p>
      </div>
    </div>'''

css = """
  .tr { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); grid-template-rows:auto minmax(0,1fr); gap:14px; }
  /* the ladder: four dense tiles on the canvas, climbing to the right; its label sits in the space the climb leaves */
  .lad { grid-column:1 / span 4; position:relative; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:14px; align-items:start; }
  .ladl { position:absolute; left:0; top:0; }
  /* one height (L3's, the tallest, measured), so tops and bottoms both climb by 26 */
  .st { height:146px; }
  .sl { display:flex; align-items:baseline; gap:10px; }
  .lv { font:var(--t-stat-sm); letter-spacing:var(--t-stat-sm-ls); }
  .nm2 { font:var(--t-body); font-weight:600; }
  .st p { margin:6px 0 0; font:var(--t-small); color:var(--text-2); text-wrap:pretty; }
  .st.past .lv, .st.past .nm2 { color:var(--text-2); }
  .st.now p { color:var(--window); opacity:.75; }
  .here { margin-top:10px; font:var(--t-micro); letter-spacing:var(--t-micro-ls); opacity:.75; }
  .st.later .lv, .st.later .nm2, .st.later p { color:var(--text-3); }
  .tk { margin-top:12px; }
  .tkl { font:var(--t-label); font-weight:400; color:var(--text-3); margin-top:6px; }
  .nx { grid-column:1 / span 2; }
  .crs { margin-top:8px; }
  .cr { display:grid; grid-template-columns:18px 1fr 56px 150px; align-items:baseline; column-gap:8px; padding:7px 0; border-bottom:1px solid var(--line); font:var(--t-small); } .cr > .k-mk { align-self:center; }
  .cr b { font:var(--t-measure); font-weight:600; text-align:right; }
  .cr em { font-style:normal; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .ft { margin:auto 0 0; font:var(--t-label); font-weight:400; color:var(--text-3); }

  .pr p { margin:8px 0 0; font:var(--t-small); color:var(--text-2); }
  .cmdw { margin-top:12px; } .cmd { display:flex; align-items:center; gap:8px; }
  .cmd span:first-child { flex:1; min-width:0; }

  .t3n { margin:8px 0 0 !important; font:var(--t-label); font-weight:400; color:var(--text-3) !important; }
  .ar { display:grid; grid-template-columns:18px 1fr auto; align-items:baseline; column-gap:6px; padding:6px 0; border-bottom:1px solid var(--line); font:var(--t-small); } .ar > .k-mk { align-self:center; }
  .ar em { font-style:normal; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .warn { margin:10px 0 12px; font:var(--t-small); font-weight:600; }
.dm .k-button { align-self:flex-start; }
  .dm .t3n { margin-top:6px !important; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="11 · Trust: the autonomy ladder, what the next level needs, what lowers it" -->',
           'Trust', ['Trust'], 0, body, css)
d = Path('../system/project/components/Trust'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
