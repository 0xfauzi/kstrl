"""Level 2 (Part): search-query's tries as rows across the stations of the line, with the step panel."""
from __future__ import annotations
from pathlib import Path
from level_common import page, top

X0 = 32
COLS = [('', 64), ('Build', 244), ('Verify', 150), ('Review', 176), ('Security', 150), ('Distill', 150), ('Your approval', 150), ('Merge', 132)]
WHAT = ['', 'the engineer, every 2 s', 'tests, lint, scope', 'a reviewer agent', 'a security agent', 'records facts', 'you, at L2', 'PR and CI']
GT, HH, RH, GAP = 100, 40, 50, 14
xs = [X0]
for _, w in COLS: xs.append(xs[-1] + w)
def row_top(i: int) -> int: return GT + HH + i * (RH + GAP)

rows = [
  # (label, cells) cell = (cls, mark, main, sub)
  ('try 1', [('done', '', '7 iterations', '34m'), ('ok', 'pass', '28 passed', '39s'), ('bad', 'fail', '5 blocking', '71s'), None, None, None, None]),
  ('try 2', [('done', '', '5 iterations', '26m'), ('ok', 'pass', '30 passed', '40s'), ('bad sel', 'fail', '2 blocking', '64s'), None, None, None, None]),
  ('try 3', [('done', '', '4 iterations', '24m'), ('ok', 'pass', '31 passed', '41s'), ('now', 'work', 'reviewing', 'started 1m ago'), ('todo',), ('todo',), ('todo',), ('todo',)]),
  ('try 4', 'left'),
]
html = []
html.append('<div class="grid">')
for j, ((name, w), what) in enumerate(zip(COLS, WHAT)):
    if j == 0: continue
    html.append(f'<div class="ch" style="left:{xs[j]+3}px;top:{GT}px;width:{w-6}px"><b>{name}</b><span>{what}</span></div>')
for i, (label, cells) in enumerate(rows):
    y = row_top(i)
    html.append(f'<div class="rl" style="left:{xs[0]}px;top:{y}px;height:{RH}px">{label}</div>')
    if cells == 'left':
        html.append(f'<div class="k-tile k-tile-cell k-tile-idle cell left" style="left:{xs[1]+3}px;top:{y}px;width:{xs[-1]-xs[1]-6}px;height:{RH}px"><p class="lt">one try left · if review still fails, search-query stops and waits for you</p></div>')
        continue
    for j, c in enumerate(cells):
        x, w = xs[j + 1], COLS[j + 1][1]
        if c is None:
            # a station this try never reached: a hairline in the grid's own dash, not a cell
            html.append(f'<svg class="gnone" style="left:{x+3}px;top:{y}px" width="{w-6}" height="{RH}" aria-hidden="true"><line class="gl" x1="10" x2="{w-16}" y1="{RH // 2 + .5}" y2="{RH // 2 + .5}"/></svg>')
        elif c[0] == 'todo':
            html.append(f'<div class="k-tile k-tile-cell k-tile-idle cell" style="left:{x+3}px;top:{y}px;width:{w-6}px;height:{RH}px"></div>')
        else:
            cls, mk, main, sub = c
            m = f'<span class="k-mk sm {mk}"></span>' if mk else ''
            state = {'done': '', 'ok': '', 'bad': ' k-tile-alert', 'bad sel': ' k-tile-alert k-tile-selected', 'now': ' k-tile-work'}[cls]
            html.append(f'<div class="k-tile k-tile-cell{state} cell" style="left:{x+3}px;top:{y}px;width:{w-6}px;height:{RH}px"><div class="a">{m}{main}</div><div class="b">{sub}</div></div>')
# return paths: from the failed review cell back to the next try's build
svg = ['<svg class="ret" viewBox="0 0 1280 520" width="1280" height="520" aria-hidden="true"><defs><marker id="ra2" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0.5 7 4 0 7.5Z" class="rh"/></marker></defs>']
notes = ['sent back with its 5 blocking findings', 'sent back with the 2 still failing']
for i in range(2):
    rx = xs[3] + COLS[3][1] / 2
    yb = row_top(i) + RH
    gy = yb + GAP / 2
    ny = row_top(i + 1) + RH / 2
    svg.append(f'<path d="M{rx} {yb} V{gy} H{xs[1]-10} V{ny} H{xs[1]+1}" class="rp" marker-end="url(#ra2)"/>')
    svg.append(f'<text x="{rx+10}" y="{gy+4}" class="rt">{notes[i]}</text>')
svg.append('</svg>')
html.append(''.join(svg))
html.append('</div>')

head = '    ' + top('<span class="nm">search-query</span><span class="k-chip k-chip-checker">reviewer</span>', 'try 3 of 4 · checking · ≥$9.20', 2) + """
    <div class="pd"><span>Runs a search against the index and returns the matches with their scores.</span>
      <span class="ctx"><span><span class="k-mk sm landed"></span>needs search-schema, merged</span><span><span class="k-mk sm wait"></span>needed by search-api</span><span>decision 1: under 100 ms for 10,000 snippets</span></span></div>"""
step = """    <div class="steps">
      <div class="k-tile k-tile-ink f1 k-tile-hero">
        <div class="sh"><span class="k-chip k-chip-checker">reviewer</span><span>try 2 · review · 64s</span><span class="grow"></span><span>2 blocking, both also found in try 1</span></div>
        <div class="sev">blocking · US-3, criterion 3</div>
        <p class="say">No test measures the 100 ms bound. The engineer’s benchmark runs on 100 snippets, not 10,000.</p>
        <div class="src">criterion 3 · tests/test_query.py</div>
      </div>
      <div class="k-tile f2">
        <div class="sev2">blocking · US-2 <span>search/query.py:41</span></div>
        <p>Quoted phrases are split into words, so “exact phrase” matches the words in any order.</p>
        <div class="tf">also found in try 1</div>
      </div>
      <div class="k-tile f3">
        <div class="k-label">The engineer said, before this review</div>
        <p class="cl">All 3 stories are done.</p>
        <div class="acts">
          <button class="k-button k-button-block">Tell the engineers…<span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">G</span></span></button>
          <button class="k-button k-button-block">Read the review log<span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">L</span></span></button>
          <button class="k-button k-button-block">Open decision 1 in the plan</button>
        </div>
        <div class="tf">Guidance goes into memory.md, which try 4 reads.</div>
      </div>
    </div>"""
css = """
  .ttl .k-chip { align-self:center; margin-left:-4px; }
  .pd { position:absolute; left:32px; right:32px; top:60px; font:var(--t-body); color:var(--text-2); display:flex; align-items:baseline; gap:22px; white-space:nowrap; }
  .pd .ctx { display:flex; gap:16px; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .pd .ctx > span { display:inline-flex; gap:6px; align-items:baseline; } .pd .ctx > span > .k-mk { align-self:center; }
  .pd .ctx > span:not(:last-child) { color:var(--text-2); }
  .grid { position:absolute; left:0; top:0; width:1280px; height:520px; }
  /* the grid is a positioning layer over the whole map, title row included, and comes after it: without this the grid
     took every click meant for the zoom (measured: all four zoom items hit .grid) */
  .top { z-index:1; }
  .ch { position:absolute; height:40px; padding:0 10px; box-sizing:border-box; display:flex; flex-direction:column; justify-content:center; border-bottom:1px solid var(--line); }
  .ch b { font:var(--t-small); font-weight:600; } .ch span { font:var(--t-micro); font-weight:400; color:var(--text-3); }
  .rl { position:absolute; width:64px; display:flex; align-items:center; font:var(--t-measure-inline); color:var(--text-3); }
  .cell { position:absolute; }
  .cell .a { display:flex; align-items:center; gap:6px; font:var(--t-measure); color:var(--text); }
  .cell .b { font:var(--t-measure-small); font-weight:400; color:var(--text-3); margin-top:1px; white-space:nowrap; }
  .gnone { position:absolute; } .gl { stroke:var(--line-strong); stroke-width:1; stroke-dasharray:1 3; }
  .lt { margin:auto 0 auto 4px; font:var(--t-small); color:var(--text-3); }
  .ret { position:absolute; inset:0; pointer-events:none; }
  .rp { fill:none; stroke:var(--fail); stroke-width:1.5; opacity:.8; }
  .rh { fill:var(--fail); }
  .rt { fill:var(--text-3); font:var(--t-micro); font-weight:400; }
  .steps { position:absolute; left:32px; right:32px; top:402px; bottom:14px; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:14px; }
  .f1 { grid-column:1 / span 2; }
  .f1 .sh { display:flex; align-items:center; gap:10px; font:var(--t-label); font-weight:400; opacity:.8; }
  .f1 .sev { margin-top:auto; font:var(--t-measure-small); font-weight:600; letter-spacing:var(--t-measure-small-ls); opacity:.7; }
  .f1 .say { margin:6px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); max-width:34ch; }
  .f1 .src { margin-top:12px; font:var(--t-measure-inline); opacity:.65; }
  .sev2 { font:var(--t-measure-small); font-weight:600; color:var(--text-2); letter-spacing:var(--t-measure-small-ls); }
  .sev2 span { font-weight:400; color:var(--text-3); letter-spacing:0; margin-left:6px; }
  .f2 p { margin:10px 0 0; font:var(--t-input); letter-spacing:var(--t-input-ls); }
  .k-tile .tf { margin-top:auto; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .f3 .cl { margin:6px 0 0; font:var(--t-body); }
  .acts { display:flex; flex-direction:column; gap:6px; margin-top:14px; }
  .f3 .tf { margin-top:auto; padding-top:8px; }
"""
body = head + '\n' + '\n'.join(html) + '\n' + step
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="2 · Part level: every try across the line, and one step open" -->',
           'Part level: search-query', ['search', 'search-query'], 2, body, css)
d = Path('../system/project/components/Map2Part'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
