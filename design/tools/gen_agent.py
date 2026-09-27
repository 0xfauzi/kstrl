"""Step level, one agent: everything search-index's engineer has written in try 1, at 20:46 (the moment of Stage and Grid)."""
from __future__ import annotations
from pathlib import Path
from level_common import page, ring, top

# engineer.log as kstrl writes it: the agent's text, and each tool call reduced to "[Tool] argument".
# No times and no iteration markers are in the file, so none are drawn in it.
log = [
  ('t', 'US-1 is done: saving a snippet writes its title and body to the FTS table. Moving to US-2.'),
  ('k', 'Read', 'store.py'), ('k', 'Read', 'index.py'), ('k', 'Grep', 'def delete'),
  ('t', 'The delete path removes the snippet row but leaves its entry in the index, so a deleted snippet still matches a search.'),
  ('k', 'Edit', 'index.py'), ('k', 'Edit', 'store.py'), ('k', 'Bash', 'uv run pytest tests/test_index.py -q'),
  ('t', 'One failure: the test deletes through the store, which opened its own connection. Passing the connection through instead.'),
  ('k', 'Edit', 'store.py'), ('k', 'Edit', 'test_index.py'), ('k', 'Bash', 'uv run pytest tests/test_index.py tests/test_store.py -q'),
]
rows = []
for e in log:
    if e[0] == 't':
        rows.append(f'<p class="lt">{e[1]}</p>')
    else:
        rows.append(f'<div class="tc"><span class="tn">{e[1]}</span><span class="ta">{e[2]}</span></div>')
# iterations, from engineer.jsonl: iteration_completed {duration_seconds, fast_checks_failed}
its = [(1, '5m 40s', ['f', 'p']), (2, '4m 12s', ['p', 'p']), (3, '6m 03s', ['p', 'f']), (4, '3m 20s', ['p', 'p']),
       (5, '4m 48s', ['p', 'p']), (6, '3m 55s', ['f', 'p']), (7, '3m 31s', ['p', 'p'])]
def dots(r: list[str]) -> str:
    return ''.join(f'<i class="k-dot{" k-dot-fail" if x == "f" else ""}"></i>' for x in r)
irows = ''.join(f'<div class="ir"><span class="n">{n}</span><span class="d">{d}</span><span class="dt">{dots(r)}</span><span class="w">{"tests failed" if r[0]=="f" else ("typecheck failed" if r[1]=="f" else "")}</span></div>' for n, d, r in reversed(its))
body = '    ' + top('<span class="nm">search-index</span><span class="k-chip">engineer</span>', 'try 1 of 4 · iteration 8 of up to 10 · output 6s ago', 3,
                    ['Log', 'Notes', 'Prompt'], 0, 'esc back to Stage') + f'''
    <div class="two">
      <div class="k-tile logt k-tile-flush">
        <div class="lh"><span class="v-measure">engineer.log</span><span>try 1, from 20:13</span><span class="grow"></span><span>Search <span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">F</span></span></span></div>
        <div class="flow">
          {''.join(rows)}
          <p class="last">Deletes now remove the index row in the same transaction. Both test files pass; next is US-3, folding case and accents before indexing.</p>
          <div class="ty"><span class="k-typing" aria-hidden="true"><i></i><i></i><i></i></span>writing</div>
        </div>
        <div class="lf"><span>Following the end. Scroll up to stop.</span><span class="grow"></span><span>Commands are recorded; what they printed is not.</span></div>
      </div>
      <div class="side">
        <div class="k-tile itt">
          <div class="ih"><div class="k-label">Iterations</div><span class="leg">from engineer.jsonl</span></div>
          <div class="ib">{ring(7, 10, 56, 6)}<div class="num"><b>8</b><span>/10</span></div><div class="now"><span class="k-mk sm work"></span>8 running · 47s</div></div>
          <div class="il">{irows}</div>
          <div class="tf"><span class="leg2"><i class="k-dot"></i><i class="k-dot"></i></span>tests and typecheck, run after each iteration. A failure goes into the next iteration’s prompt.</div>
        </div>
        <div class="k-tile st">
          <div class="k-label">Its stories, as it has marked them in prd.json</div>
          <div class="sr"><span class="v-measure">US-1</span><span>Index a snippet’s title and body when it is saved</span><em>claims done</em></div>
          <div class="sr"><span class="v-measure">US-2</span><span>Remove a snippet from the index when it is deleted</span><em class="n">not yet</em></div>
          <div class="sr"><span class="v-measure">US-3</span><span>Fold case and accents before indexing</span><em class="n">not yet</em></div>
        </div>
        <div class="k-tile ac k-tile-dense">
          <button class="k-button k-button-block">Tell the engineers…<span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">G</span></span></button>
          <div class="cq">Read when the next try starts. Try 1 will not see it.</div>
        </div>
      </div>
    </div>'''
NEEDS = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <span class="k-needs-none">Nothing right now</span>
    
    <span class="k-needs-hint">Notes is its progress.txt · Prompt is exactly what it was sent, per call</span>
  </div>'''
css = """
  .ttl .k-chip { align-self:center; margin-left:-4px; }
  .two { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 372px; gap:14px; }
  .logt { position:relative; overflow:hidden; }
  .lh { display:flex; align-items:center; gap:12px; padding:16px 24px 12px; font:var(--t-label); font-weight:400; color:var(--text-3); border-bottom:1px solid var(--line); }
  .lh .v-measure { color:var(--text-2); }
  .lh .k-keys { margin-left:2px; }
  /* older lines fade as they scroll away: a mask on the log itself, so it works on any surface in both themes and paints nothing */
  .flow { flex:1; display:flex; flex-direction:column; justify-content:flex-end; padding:0 40px 8px; min-height:0; overflow:hidden; -webkit-mask-image:linear-gradient(to bottom, transparent, black 80px); mask-image:linear-gradient(to bottom, transparent, black 80px); }
  .lt { margin:12px 0 4px; font:var(--t-body); color:var(--text-2); max-width:62ch; }
  .tc { display:grid; grid-template-columns:48px 1fr; align-items:baseline; font:var(--t-measure); }
  .tc .tn { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .tc .ta { color:var(--text-2); }
  .last { margin:16px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); color:var(--text); max-width:40ch; }
  .ty { display:flex; align-items:center; gap:8px; margin-top:10px; font:var(--t-label); font-weight:600; color:var(--work); }
  .lf { display:flex; gap:12px; padding:12px 24px 14px; border-top:1px solid var(--line); font:var(--t-label); font-weight:400; color:var(--text-3); }
  .side { display:flex; flex-direction:column; gap:14px; min-height:0; }
  .itt { flex:1; }
  .ih { display:flex; justify-content:space-between; align-items:baseline; }
  .leg { font:var(--t-micro); font-weight:400; color:var(--text-3); display:inline-flex; align-items:center; gap:4px; }
  .leg2 { display:inline-flex; gap:4px; margin-right:6px; vertical-align:1px; }
  .ib { display:flex; align-items:center; gap:12px; margin-top:10px; }
  .num b { font:var(--t-stat); letter-spacing:var(--t-stat-ls); }
  .num span { font:var(--t-measure); font-weight:500; color:var(--text-3); margin-left:2px; }
  .ib .now { margin-left:auto; display:inline-flex; gap:6px; align-items:center; font:var(--t-measure-inline); color:var(--work); }
  .il { margin-top:10px; }
  .ir { display:grid; grid-template-columns:22px 58px 24px 1fr; align-items:center; font:var(--t-measure-inline); color:var(--text-2); }
  .ir .n { color:var(--text-3); }
  .dt { display:inline-flex; gap:4px; }
  .ir .w { font:var(--t-label); font-weight:400; color:var(--fail); }
  .itt .tf { margin-top:auto; font:var(--t-label); font-weight:400; color:var(--text-3); }

  .sr { display:grid; grid-template-columns:40px 1fr auto; column-gap:8px; align-items:baseline; font:var(--t-small); padding:6px 0; border-bottom:1px solid var(--line); }
  .sr:last-child { border-bottom:0; padding-bottom:0; }
  .sr .v-measure { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .sr em { font-style:normal; font:var(--t-label); font-weight:400; color:var(--text-2); white-space:nowrap; }
  .sr em.n { color:var(--text-3); }
  .st .k-label { margin-bottom:4px; }

  .cq { font:var(--t-label); font-weight:400; color:var(--text-3); margin-top:8px; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="3c · Step level, one agent: everything it has written" -->',
           'Step level: one agent', ['search', 'being built', 'search-index'], 3, body, css, NEEDS)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">live · last event 2s ago</span>').replace('≥$31.10 <small>of $40.00 today</small>', '≥$19.40 <small>of $40.00 today</small>')
d = Path('../system/project/components/Map3Agent'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
