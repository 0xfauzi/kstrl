"""Step level, Grid, at eight agents: the import spec with max_parallel = 8. Thu 10:14."""
from __future__ import annotations
from pathlib import Path
from level_common import page, ring, checks, top

agents = [  # (role, part, age, done, total, try, line) ; role 'rv' shows the check sequence instead of a ring
  ('eng', 'import-gist-client', '3s', 5, 10, 1, 'Fetching a gist by id; the API returns files keyed by name.'),
  ('eng', 'import-map-fields', '9s', 7, 10, 1, 'Mapping a gist’s description onto the snippet title.'),
  ('rv', 'import-validate', '4s', ['done', 'now', '', ''], 0, 1, 'Story 2 asks for a clear error on a private gist. The test only checks the status code.'),
  ('eng', 'import-dedupe', '12s', 2, 10, 1, 'Two files with the same content should become one snippet.'),
  ('eng', 'import-api', '6s', 4, 10, 1, 'POST /import takes a gist URL and answers 202 with a job id.'),
  ('eng', 'import-cli', '21s', 1, 10, 1, 'Adding snip import &lt;url&gt;.'),
  ('rv2', 'import-rate-limit', '2s', ['done', 'done', 'now', ''], 0, 1, ''),
  ('eng', 'import-progress', '15s', 0, 10, 2, 'Working through review’s two findings from try 1.'),
]
def tile(a) -> str:
    role, part, age, done, total, tr, line = a
    chip = '<span class="k-chip">engineer</span>' if role == 'eng' else ('<span class="k-chip k-chip-checker">reviewer</span>' if role == 'rv' else '<span class="k-chip k-chip-checker">security</span>')
    if role == 'eng':
        body = f'<div class="tb">{ring(done, total, 44, 5)}<div class="num"><b>{done + 1}</b><span>/10{" · try " + str(tr) if tr > 1 else ""}</span></div></div>'
    else:
        body = f'<div class="ckw">{checks(done)}</div>'
    txt = f'<p>{line}</p>' if line else '<p class="q2">see the latest statement</p>'
    return f'<div class="k-tile k-tile-dense b ag"><div class="th">{chip}<span class="ag2">{age}</span></div><div class="p">{part}</div>{body}{txt}</div>'
tiles = [tile(a) for a in agents]
# the plan, top to bottom: what merged, the eight working, what waits
names = ['gist-client', 'map-fields', 'validate', 'dedupe', 'api', 'cli', 'rate-limit', 'progress']
kinds = ['w', 'w', 'r', 'w', 'w', 'w', 'r', 'w']
pills = ''.join(f'<span class="k-chip{" k-chip-checker" if k == "r" else ""} pl">{n}</span>' for n, k in zip(names, kinds))
plan = f'''<div class="plan"><div class="pm"><span class="k-mk sm landed"></span>parse, fetch merged</div><div class="pls">{pills}</div><div class="pm"><span class="k-mk sm wait"></span>report waits for api and cli</div><div class="pn">import- is left off the names here</div></div>'''
hero = f'''<div class="k-tile b hero">
        <div class="big"><b>8</b> agents working</div>
        <div class="sub">on 8 of import’s 11 parts · 2 merged · 1 waiting</div>
        {plan}
        <div class="stats"><div><b>41</b><span>iterations</span></div><div><b>≥$14.30</b><span>counted</span></div></div>
      </div>'''
quote = '''<div class="k-tile k-tile-ink k-tile-dense b quote"><div class="qh"><span class="k-chip k-chip-checker">security</span>import-rate-limit · just now</div><p>“fetch.py logs the gist token at debug level on line 88.”</p><div class="qf">High meets the blocking threshold (fail_threshold is high), so the part goes back to its engineer with this finding.</div></div>'''
body = '    ' + top('<span class="v-human">import</span>', '8 agents working · 10:14 · max_parallel 8', 3, ['Stage', 'Grid'], 1) + f'''
    <div class="bento">
      {hero}
      {"".join(tiles[:6])}
      {"".join(tiles[6:])}
      {quote}
    </div>'''
NEEDS = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <span class="k-needs-none">Nothing right now</span>
    
    <span class="k-needs-hint">At eight, a tile keeps its ring and two lines. <span class="k-keys k-keys-inline"><span class="k-key">↵</span></span> on any agent opens everything it has written.</span>
  </div>'''
css = """
  .bento { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); grid-template-rows:1fr 1fr 166px; gap:14px; }
  .b { min-width:0; min-height:0; overflow:hidden; }
  .hero { grid-column:1; grid-row:1 / span 2; }
  .big { font:var(--t-body); color:var(--text-2); }
  .big b { font:var(--t-stat-lg); letter-spacing:var(--t-stat-lg-ls); color:var(--text); margin-right:6px; }
  .sub { font:var(--t-small); color:var(--text-3); margin-top:4px; }
  .plan { flex:1; display:flex; flex-direction:column; justify-content:center; gap:10px; }
  .pm { display:flex; align-items:center; gap:6px; font:var(--t-label); font-weight:400; color:var(--text-2); }
  .pls { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
  .pl { justify-self:start; }
  .pn { font:var(--t-micro); font-weight:400; color:var(--text-3); }
  .stats { display:grid; grid-template-columns:1fr 1fr; row-gap:10px; border-top:1px solid var(--line); padding-top:12px; }
  .stats b { display:block; font:var(--t-stat-sm); letter-spacing:var(--t-stat-sm-ls); }
  .stats span { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .th { display:flex; align-items:center; justify-content:space-between; }
  .ag2 { font:var(--t-measure-small); color:var(--text-3); }
  .b .p { font:var(--t-body); font-weight:600; margin-top:8px; letter-spacing:var(--t-body-ls); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tb { display:flex; align-items:center; gap:10px; margin-top:8px; }
  .num b { font:var(--t-stat-sm); letter-spacing:var(--t-stat-sm-ls); }
  .num span { font:var(--t-measure-inline); font-weight:500; color:var(--text-3); margin-left:2px; }
  .ckw { margin-top:12px; }
  .b > p { margin:auto 0 0; font:var(--t-small); color:var(--text-2); display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .b > p.q2 { color:var(--text-3); }
  .quote { grid-column:3 / span 2; }
  .quote .qh { display:flex; align-items:center; gap:10px; font:var(--t-label); font-weight:400; opacity:.8; }
  .quote p { margin:10px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); color:var(--window); -webkit-line-clamp:3; }
  .quote .qf { margin-top:auto; font:var(--t-label); font-weight:400; opacity:.7; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="3d · Step level, Grid at eight agents" -->',
           'Step level: Grid at eight', ['import', 'being built'], 3, body, css, NEEDS)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">live · last event 2s ago</span>').replace('≥$31.10 <small>of $40.00 today</small>', '≥$15.16 <small>of $40.00 today</small>')
d = Path('../system/project/components/Map3Grid8'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
