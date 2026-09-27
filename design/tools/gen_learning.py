"""Learning: what an engineer reads before it builds a part, in kstrl's order, and what kstrl has noticed across runs."""
from __future__ import annotations

from pathlib import Path

from level_common import HINT, page, top
from level_common import NEEDS as LC_NEEDS

layers = [
  ('1', 'Facts from earlier parts', 'kstrl', 'The distiller’s notes on this part, its dependencies, and one line from every other part.', 3180, 3500, 'tokens', ''),
  ('2', 'Golden patterns', 'you', 'scripts/kstrl/golden-patterns.md · the start is kept', 2900, 6000, 'characters', 'Edit'),
  ('3', 'The architect’s decisions', 'kstrl', 'The ones that bind this part, from decisions.json.', 0, 0, '', ''),
  ('4', 'What is in the codebase', 'kstrl', 'The codebase scan: files and symbols this part may reuse.', 0, 0, '', ''),
  ('5', 'Why the last try failed', 'kstrl', 'Only on a retry: the findings that sent it back.', 0, 0, '', ''),
  ('6', 'Your guidance', 'you', 'scripts/kstrl/memory.md · the newest entries are kept', 1240, 4000, 'characters', 'Add a line'),
  ('7', 'Your project notes and the engineer prompt', 'you', 'CLAUDE.md, then scripts/kstrl/prompt.md', 0, 0, '', 'Edit'),
]
def lrow(n, name, who, desc, used, cap, unit, act):
    meter = ''
    if cap:
        meter = f'<div class="mt"><span class="k-meter mb" aria-hidden="true" style="--k-meter:{used/cap*100:.0f}%"><i></i></span><span class="mv">{used:,} of {cap:,} {unit}</span></div>'
    btn = f'<button class="k-button k-button-sm">{act}</button>' if act else ''
    return f'''<div class="lr {'mine' if who == 'you' else ''}"><span class="no">{n}</span><div class="lb"><div class="lh"><b>{name}</b><span class="who {'w-you' if who == 'you' else 'w-k'}">{'you write it' if who == 'you' else 'kstrl writes it'}</span></div><p>{desc}</p>{meter}</div>{btn}</div>'''
stack = ''.join(lrow(*l) for l in layers)
body = '    ' + top('<span class="nm">Learning</span>', 'what an engineer reads before it builds, and what kstrl has noticed', None, ['Context', 'Facts', 'Patterns'], 0) + f'''
    <div class="lg">
      <div class="k-tile stk">
        <div class="k-label">Before it builds a part, an engineer reads these, in this order</div>
        <div class="rows">{stack}</div>
        <p class="ft">Read once when a part’s try starts. A change reaches the next try, not the one running.</p>
      </div>
      <div class="side">
        <div class="k-tile k-tile-ink k-ink pat k-tile-hero">
          <div class="k-label">Noticed in 3 of the last 10 runs</div>
          <p class="say">Parts are sent back because no test measures a stated speed or size limit.</p>
          <p class="sub">search-query, sharing-links, snippets-store · all at review</p>
          <div class="pf"><span>kstrl has nowhere to keep this yet: its playbook has no writer. A line in your guidance reaches every engineer.</span><button class="k-button k-button-inverse k-button-sm">Add to guidance</button></div>
        </div>
        <div class="k-tile fx">
          <div class="k-label">Facts kstrl has written</div>
          <div class="fh"><span class="k-stat-value">42</span><div class="cf"><span><b>29</b> passed review</span><span><b>8</b> backed by a test</span><span><b>5</b> asserted only</span></div></div>
          <div class="k-well ff"><span class="v-measure">search-index</span><p>A delete removes the index row in the same transaction as the snippet row.</p><em>passed review · referenced in 2 later parts</em></div>
          <p class="ft">“Referenced” counts where the first 30 characters of a fact reappear in an engineer’s notes or diff, so it undercounts.</p>
        </div>
      </div>
    </div>'''
# Needs you is the app's, not the page's: Learning is drawn at the Factory's moment (≥$31.10 today, last event 3s ago),
# when search-index waits for approval, so the band lists the same two needs. Only its hint is this page's.
LEARN_HINT = 'A /memory comment on a kstrl pull request also adds a line to your guidance.'
FOOT = LC_NEEDS.replace(HINT, LEARN_HINT)
assert LEARN_HINT in FOOT
css = """
  .lg { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 440px; gap:14px; }

  .rows { margin-top:8px; display:flex; flex-direction:column; }
  .lr { display:grid; grid-template-columns:26px minmax(0,1fr) auto; column-gap:12px; align-items:baseline; padding:9px 0; border-bottom:1px solid var(--line); } .lr > :is(.k-mk, .k-dot, .k-ring, .k-chip, .k-button, .k-keys) { align-self:center; }
  .lr:last-child { border-bottom:0; }
  .no { font:var(--t-measure-inline); font-weight:600; color:var(--text-3); }
  .lh { display:flex; align-items:baseline; gap:10px; }
  .lh b { font:var(--t-body); font-weight:600; }
  .who { font:var(--t-micro); }
  .who.w-k { color:var(--text-3); font-weight:500; }
  .who.w-you { color:var(--text); }
  .lr p { margin:2px 0 0; font:var(--t-small); color:var(--text-2); }
  .mt { display:flex; align-items:center; gap:10px; margin-top:6px; }
  .mb { width:160px; flex:none; }
  .mv { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
.k-button-sm { align-self:center; }
  .ft { margin:auto 0 0; padding-top:8px; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .side { display:flex; flex-direction:column; gap:14px; min-height:0; }

  .pat .say { margin:10px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); }
  .pat .sub { margin:8px 0 0; font:var(--t-small); opacity:.7; }
  .pf { display:flex; align-items:center; gap:12px; margin-top:16px; font:var(--t-small); }
  .pf span:first-child { flex:1; opacity:.8; }
  .k-button-inverse { flex:none; }
  .fx { flex:1; }
  .fh { display:flex; align-items:center; gap:18px; margin-top:8px; }
  .cf { display:flex; flex-direction:column; gap:2px; font:var(--t-small); color:var(--text-2); }
  .cf b { font:var(--t-measure); font-weight:600; color:var(--text); margin-right:4px; }
  .ff { margin-top:12px; }
  .ff .v-measure { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .ff p { margin:3px 0 0; font:var(--t-small); }
  .ff em { display:block; font-style:normal; font:var(--t-label); font-weight:400; color:var(--text-3); margin-top:4px; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="13 · Learning: what engineers read, what you write, what kstrl noticed" -->',
           'Learning', ['Learning'], 0, body, css, FOOT)
d = Path('../system/project/components/Learning'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
