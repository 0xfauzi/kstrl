"""Notifications 7b: one event (search-index parks at 21:28:04) and every place it reaches, with where each lands you."""
from __future__ import annotations
from pathlib import Path
from level_common import page, top, LOGO
from comp import ill_tab, ILL_CSS, notice_compact

ROW_H, GAP, TOP0 = 138, 12, 0
rows = [
  ('In the app', 'At once, while kstrl is open on any screen.',
   '''<div class="mini-note">''' + notice_compact() + '''</div>
      <div class="side2"><span class="k-need k-need-ask"><span class="k-mk you"></span><b>Approve search-index</b><span class="k-need-sub">every check agreed · just now</span></span>''' + ill_tab('(2) kstrl') + '''</div>''',
   'The inbox, with this item selected.', ''),
  ('On your desktop', 'At once, while kstrl is open but not in front. Asks and trust changes only.',
   f'''<div class="banner" data-illustration="the operating system's notification banner">{LOGO}<div><div class="bt1"><b>kstrl</b><span>snippetvault</span></div><div class="bt2">search-index is ready to merge</div><div class="bt3">Every check agreed. search-api waits on it.</div></div></div>''',
   'The inbox, with this item selected.', ''),
  ('Your hook', 'On the machine running kstrl, even with the app closed. The first merge approval of this run only.',
   '''<div class="k-well k-well-code code"><div class="k-well-line"><span class="c3">[notify]</span> on_inbox_item = <span class="c2">"your command"</span></div><div class="k-well-line">KSTRL_NOTIFY_EVENT=<span class="c2">inbox_merge_gate</span></div><div class="k-well-line">KSTRL_NOTIFY_COMPONENT=<span class="c2">search-index</span></div><div class="k-well-line">KSTRL_NOTIFY_DETAIL=<span class="c2">search-index awaiting merge approval</span></div></div>''',
   'Wherever your command sends it.', ''),
  ('The GitHub issue', 'Only for a spec that came from an issue, when its run ends.',
   '''<p class="gh">search came from a local spec, so nothing is posted. For a spec from an issue, kstrl sets the label <span class="v-measure">kstrl:awaiting_approval</span> and comments with the command that approves it.</p>''',
   'The issue.', 'off'),
]
html = []
for i, (name, when, body, lands, cls) in enumerate(rows):
    tile = 'k-tile k-tile-idle' if cls == 'off' else 'k-tile'
    html.append(f'''      <div class="{tile} row" style="top:{TOP0 + i * (ROW_H + GAP)}px"><div class="rg">
        <div class="ch"><div class="cn">{name}</div><div class="cw">{when}</div></div>
        <div class="cb">{body}</div>
        <div class="cl"><div class="k-label">Lands you</div><div>{lands}</div></div>
      </div></div>''')
# fan-out from the event tile to each row
EX, EY = 0, 294
svg = ['<svg class="fan" viewBox="0 0 56 588" width="56" height="588" aria-hidden="true">']
for i in range(4):
    y = TOP0 + i * (ROW_H + GAP) + ROW_H / 2
    dash = ' dashed' if i == 3 else ''
    svg.append(f'<path d="M0 {EY} C28 {EY}, 28 {y}, 52 {y}" class="f{dash}"/><circle cx="52" cy="{y}" r="3" class="fd{dash}"/>')
svg.append('</svg>')
body = '    ' + top('<span class="nm">Notifications</span>', 'one event, every place it reaches, and where each one lands you', None, ['One event', 'Every event'], 0) + f'''
    <div class="stage2">
      <div class="k-tile k-tile-ink ev">
        <div class="k-label">21:28:04 · filed in the inbox</div>
        <div class="et">search-index is ready to merge</div>
        <div class="kv">
          <div><span>kind</span>merge_gate</div><div><span>part</span>search-index</div><div><span>run</span>7c21d0</div>
          <div><span>key</span>merge-gate:search-index</div><div><span>commit</span>7e19b4c</div><div><span>priority</span>normal · needs a person</div><div><span>seen</span>once</div>
        </div>
        <ul class="evr"><li><b>Asks interrupt, once.</b> A repeat adds to its item and stays quiet.</li><li><b>Notices never interrupt.</b> They wait in the inbox.</li><li><b>Closed means quiet.</b> With kstrl closed, only your hooks and GitHub reach you.</li></ul>
        <p class="ef">Everything to the right is this one record, delivered. Answering it anywhere answers it everywhere.</p>
      </div>
      {''.join(svg)}
      <div class="rows">
{chr(10).join(html)}
      </div>
    </div>'''

css = """
  .stage2 { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:300px 56px minmax(0,1fr); }

  .ev .et { font:var(--t-title); letter-spacing:var(--t-title-ls); margin-top:10px; }
  .kv { margin-top:18px; display:flex; flex-direction:column; gap:6px; font:var(--t-measure); }
  .kv span { display:inline-block; width:62px; opacity:.6; font:var(--t-label); font-weight:400; }
  .ef { margin:auto 0 0; font:var(--t-small); opacity:.75; }
  .fan { align-self:start; }
  .fan .f { fill:none; stroke:var(--line-strong); stroke-width:1.5; }
  .fan .f.dashed { stroke-dasharray:3 4; }
  .fan .fd { fill:var(--line-strong); } .fan .fd.dashed { fill:var(--canvas); stroke:var(--line-strong); stroke-width:1.5; }
  .rows { position:relative; }
  /* each row is a tile; the one where nothing happens for this event is idle */
  .row { position:absolute; left:0; right:0; height:138px; }
  .rg { flex:1; display:grid; grid-template-columns:172px minmax(0,1fr) 132px; column-gap:22px; align-items:baseline; align-content:center; } .rg > .cb:not(:has(> .gh)) { align-self:center; }
  .cn { font:var(--t-body); font-weight:600; letter-spacing:var(--t-body-ls); }
  .cw { font:var(--t-small); color:var(--text-2); margin-top:4px; }
  .cl { font:var(--t-small); }
  .cl .k-label { margin-bottom:3px; }
  .cb { display:flex; align-items:center; gap:18px; min-width:0; }
  .mini-note { width:240px; flex:none; }
  .side2 { display:flex; flex-direction:column; gap:8px; font:var(--t-label); font-weight:400; }
  .banner { display:flex; gap:10px; width:330px; padding:10px 12px; border-radius:14px; background:var(--raised); box-shadow:var(--shadow-panel); }
  .banner .k-logo { width:22px; height:22px; flex:none; margin-top:1px; }
  .bt1 { display:flex; gap:8px; font-size:11.5px; color:var(--text-3); } .bt1 b { color:var(--text); font-weight:600; }
  .bt2 { font-size:13px; font-weight:600; margin-top:1px; } .bt3 { font-size:12px; color:var(--text-2); }
  .code { min-width:0; }
  .code .c2 { color:var(--text-2); } .code .c3 { color:var(--text-3); }
  .gh { margin:0; font:var(--t-small); color:var(--text-2); max-width:52ch; }
  .gh .v-measure { font:var(--t-measure-inline); color:var(--text); }
  .evr { display:flex; flex-direction:column; gap:8px; margin:22px 0 0; padding:0; list-style:none; font:var(--t-small); }
  .evr li { opacity:.75; } .evr b { font-weight:600; }
""" + ILL_CSS
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="7b · Notifications: one event, every place it reaches" -->',
           'Notifications: one event', ['Notifications'], 0, body, css)
d = Path('../system/project/components/Notify2Paths'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
