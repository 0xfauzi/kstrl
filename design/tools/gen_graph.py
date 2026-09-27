"""Level 1 (Spec): the plan as a live dependency graph. Each part carries its place on the line.

DAG (consistent with kstrl scheduling: a part starts only when every dependency is completed):
  search-schema -> search-index, search-query, search-rank, search-highlight
  search-index, search-query, search-rank -> search-api
  search-api, search-highlight -> search-cli
"""
from __future__ import annotations

from pathlib import Path

from comp import pcard, steps
from level_common import page, top

CW, CH = 236, 108
TX = [52, 366, 680, 994]          # tier x
MID = 262                          # graph vertical centre (map coords)
def col(n: int) -> list[int]:
    total = n * CH + (n - 1) * 14
    top = MID - total // 2
    return [top + i * (CH + 14) for i in range(n)]
t1 = col(4)
pos = {
  'search-schema': (TX[0], col(1)[0]),
  'search-index': (TX[1], t1[0]), 'search-query': (TX[1], t1[1]), 'search-rank': (TX[1], t1[2]), 'search-highlight': (TX[1], t1[3]),
  'search-api': (TX[2], col(1)[0]),
  'search-cli': (TX[3], col(1)[0]),
}
# segs: build, check, approve, merge -> done | work | you | fail | todo
from parts import PARTS, SPEC_TOTAL
from parts import args as part_args

parts = PARTS
edges = [
  ('search-schema', 'search-index', 'ok'), ('search-schema', 'search-query', 'ok'), ('search-schema', 'search-rank', 'ok'), ('search-schema', 'search-highlight', 'ok'),
  ('search-index', 'search-api', 'wait'), ('search-query', 'search-api', 'wait'), ('search-rank', 'search-api', 'wait'),
  ('search-api', 'search-cli', 'wait'), ('search-highlight', 'search-cli', 'broken'),
]
svg = ['<svg class="edges" viewBox="0 0 1280 520" width="1280" height="520" aria-hidden="true">'
       '<defs><marker id="ea" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0.5 7 4 0 7.5Z" class="eh"/></marker>'
       '<marker id="eb" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0.5 7 4 0 7.5Z" class="ehb"/></marker></defs>']
for a, b, kind in edges:
    ax, ay = pos[a]; bx, by = pos[b]
    x0, y0 = ax + CW, ay + CH / 2; x1, y1 = bx - 4, by + CH / 2
    if a == 'search-highlight':   # route under search-api
        y1 = by + CH - 22
    dx = (x1 - x0) * 0.5
    svg.append(f'<path d="M{x0} {y0} C{x0+dx} {y0}, {x1-dx} {y1}, {x1} {y1}" class="e {kind}" marker-end="url(#{"eb" if kind=="broken" else "ea"})"/>')
svg.append('</svg>')

cards = []
for name in parts:
    x, y = pos[name]
    sel = name == 'search-query'
    cards.append('    ' + pcard(*part_args(name), sel=sel, tab=0 if sel else None, attrs=f' style="left:{x}px;top:{y}px"').replace('class="k-card', 'class="pc k-card', 1))
LEGEND = steps(['done'] * 4, ['Build', 'Check', 'Your approval', 'Merge'], legend=True)
strip = f'''    <div class="strip">
      <span class="sl">The line, left to right on every card</span>
      <span class="leg">{LEGEND}</span>
      <span class="grow"></span>
      <span class="cnt2"><span class="k-mk sm landed"></span>1 merged</span><span class="cnt2"><span class="k-mk sm you"></span>1 waiting for you</span><span class="cnt2"><span class="k-mk sm work"></span>2 in progress</span><span class="cnt2"><span class="k-mk sm fail"></span>1 stopped</span><span class="cnt2"><span class="k-mk sm wait"></span>1 waiting</span><span class="cnt2"><span class="k-mk sm skip"></span>1 skipped</span>
    </div>'''
head = '    ' + top('<span class="v-human">search</span>', f'7 parts · 3 of 4 slots in use · {SPEC_TOTAL} since 19:40', 1, ['Graph', 'Text'], 0)
body = head + '\n    <div class="graph">\n' + ''.join(svg) + '\n' + '\n'.join(cards) + '\n    </div>\n' + strip
css = """
  .graph { position:absolute; left:0; top:66px; width:1280px; height:520px; }
  .edges { position:absolute; inset:0; }
  .e { fill:none; stroke-width:1.5; }
  .e.ok { stroke:var(--line-strong); } .e.wait { stroke:var(--text-3); stroke-dasharray:3 4; } .e.broken { stroke:var(--fail); stroke-dasharray:3 4; }
  .eh { fill:var(--text-3); } .ehb { fill:var(--fail); }
  .pc { position:absolute; width:236px; height:108px; }
  .strip { position:absolute; left:32px; right:32px; bottom:16px; display:flex; align-items:center; gap:16px; font:var(--t-label); font-weight:400; color:var(--text-2); border-top:1px solid var(--line); padding-top:12px; }
  .strip .sl { color:var(--text-3); }
  .strip .leg { width:360px; flex:none; }
  .strip .cnt2 { display:inline-flex; gap:6px; align-items:center; }
"""
html = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="1 · Spec level: the plan, live" -->',
            'Spec level: the plan, live', ['search'], 1, body, css)
d = Path('../system/project/components/Map1Spec'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(html)
print('ok')
