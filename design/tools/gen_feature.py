"""A one-off feature run (ks feature) at its only checkpoint: understanding is done, implementation waits for you."""
from __future__ import annotations

import re
from pathlib import Path

from level_common import page, top

facts = [('PRD', 'scripts/kstrl/feature/bulk-delete/prd.json'), ('Branch', 'feature/bulk-delete'), ('Stories', 'BD-1, BD-2, BD-3'),
         ('Entry points', 'snip/cli.py delete, Store.delete'), ('Data touched', 'snippets table, search index'), ('Tests', 'uv run pytest tests/test_store.py')]
fk = ''.join(f'<div class="kv"><span>{k}</span><b>{v}</b></div>' for k, v in facts)
cov = [('BD-1', 'Delete several snippets with one command'), ('BD-2', 'Ask before deleting more than ten'), ('BD-3', 'Remove each from the search index in the same transaction')]
cv = ''.join(f'<div class="cv"><span class="k-mk sm pass"></span><span class="v-measure">{i}</span><span>{t}</span></div>' for i, t in cov)
body = '    ' + top('<span class="nm">bulk-delete</span>', 'ks feature · understood in 4 iterations, 6m · waiting for you to start', None, ['Understand', 'Implement', 'Repairs'], 0) + f'''
    <div class="two">
      <div class="k-tile us k-tile-hero">
        <div class="uh"><span class="k-label">What the engineer understood</span><span class="v-measure f">scripts/kstrl/feature/bulk-delete/understand.md</span></div>
        <div class="sec2"><div class="sh3">Quick facts</div><div class="kvs">{fk}</div></div>
        <div class="sec2"><div class="sh3">Story coverage · 3 of 3</div>{cv}</div>
        <div class="sec2"><div class="sh3">Risks it found</div>
          <p class="rk">Store.delete removes one row and a trigger removes its index row. A bulk delete through executemany would skip the trigger, so the index must be cleaned in the same transaction.</p></div>
        <div class="sec2"><div class="sh3">Open questions it left</div><p class="rk">None.</p></div>
      </div>
      <div class="side">
        <div class="k-tile k-tile-ask gate">
          <div class="kh"><span class="k-mk sm you"></span><span class="kind">Start implementing?</span></div>
          <div class="gch"><div class="k-choices" role="radiogroup" aria-label="Start implementing?"><div class="k-choice" role="radio" aria-checked="true" tabindex="0"><span class="k-choice-title">Start implementation</span><span class="k-keys"><span class="k-key">↵</span></span><span class="k-choice-then">Up to 3 iterations, one per story, in your checkout on feature/bulk-delete. If it does not finish, up to 5 repair runs of 5 iterations follow.</span></div><div class="k-choice" role="radio" aria-checked="false" tabindex="-1"><span class="k-choice-title">Quit to amend</span><span class="k-keys"><span class="k-key">esc</span></span><span class="k-choice-then">Edit understand.md, then run ks feature again. Nothing has changed in your code.</span></div></div></div>
        </div>
        <div class="k-tile k-tile-ink not">
          <div class="k-label">This is not a factory run</div>
          <p class="say">No reviewer, no security check, no commit, no pull request. You are the reviewer.</p>
          <p class="sub">It reads CLAUDE.md and the engineer prompt, not your guidance or kstrl’s facts. Verify reports at the start, after implementing and after each repair, and never stops anything.</p>
        </div>
        <div class="k-tile bl">
          <div class="k-label">Before it starts · a report, not a gate</div>
          <div class="br"><span class="k-mk sm pass"></span><span>tests</span><b>212 passed</b></div>
          <div class="br"><span class="k-mk sm pass"></span><span>typecheck</span><b>clean</b></div>
          <div class="br"><span class="k-mk sm pass"></span><span>lint</span><b>clean</b></div>
        </div>
      </div>
    </div>'''
FOOT = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <button class="k-need k-need-ask"><span class="k-mk you"></span><b>Start bulk-delete?</b><span class="k-need-sub">understanding done · 1m</span></button>

    <span class="k-needs-hint">For work that needs review, a commit and a PR, add a spec to the queue instead.</span>
  </div>'''
css = """
  .two { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 420px; gap:14px; }

  .uh { display:flex; justify-content:space-between; align-items:baseline; }
  .uh .f { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .sec2 { padding:12px 0; border-bottom:1px solid var(--line); }
  .sec2:last-child { border-bottom:0; }
  .sh3 { font:var(--t-body); font-weight:600; margin-bottom:8px; }
  .kvs { display:grid; grid-template-columns:1fr 1fr; gap:8px 24px; }
  .kv span { display:block; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .kv b { font:var(--t-measure); font-weight:500; }
  .cv { display:grid; grid-template-columns:18px 40px 1fr; align-items:baseline; font:var(--t-small); padding:3px 0; } .cv > .k-mk { align-self:center; }
  .cv .v-measure { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .rk { margin:0; font:var(--t-body); }
  .side { display:flex; flex-direction:column; gap:14px; min-height:0; }

  .gch { margin-top:10px; }
  .kh { display:flex; align-items:center; gap:7px; }
  .kind { font:var(--t-small); font-weight:600; color:var(--you); }

  .not .say { margin:8px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); }
  .not .sub { margin:8px 0 0; font:var(--t-label); font-weight:400; opacity:.75; }
  .bl { flex:1; }
  .br { display:grid; grid-template-columns:18px 80px 1fr; align-items:baseline; font:var(--t-small); margin-top:6px; } .br > .k-mk { align-self:center; }
  .br b { font:var(--t-measure); font-weight:500; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="16 · A feature run: understood, waiting for your go-ahead" -->',
           'Feature run', ['bulk-delete'], 0, body, css, FOOT)
out = re.sub(r'<span class="k-conds">.*?</span></span>', '', out, count=1, flags=re.S)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">live · last event 1m ago</span>')
d = Path('../system/project/components/FeatureRun'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
