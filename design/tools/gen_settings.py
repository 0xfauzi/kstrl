"""Settings: kstrl.toml by the decision each setting governs, with its source and what it does, and the problems kstrl would refuse to start on."""
from __future__ import annotations
import re
from pathlib import Path
from level_common import page, top

groups = [('How work is checked', 9, True), ('How much runs at once', 6, False), ('Money', 4, False), ('Trust', 5, False), ('The queue', 7, False),
          ('Notifications', 7, False), ('What engineers read', 8, False), ('The agent', 5, False), ('Everything else', 116, False)]
from comp import seg as _seg, vtabs, keys
nav = vtabs([(g, n) for g, n, _ in groups], next(i for i, (_, _, on) in enumerate(groups) if on), 'Setting groups', 'settings', ids='sg')
def seg(opts, on, label): return _seg(opts, on, label, sm=True)
def field(v, label):
    return (f'<div class="k-field k-field-measure"><div class="k-field-box"><input class="k-field-input" value="{v}" aria-label="{label}" '
            f'autocomplete="off" spellcheck="false"></div></div>')
def tog(v, label):
    return (f'<button class="k-toggle" role="switch" aria-checked="{"true" if v else "false"}" aria-label="{label}">'
            '<span class="k-toggle-track"><span class="k-toggle-knob"></span></span>'
            '<span class="k-toggle-word"><span class="k-toggle-on">on</span><span class="k-toggle-off">off</span></span></button>')
rows = [
  ('Tests', '[verify] test_command', field('uv run pytest', 'Tests: [verify] test_command'), 'default', 'Verify runs it on every part, before any reviewer reads it.'),
  ('Typecheck', '[verify] typecheck_command', field('uv run mypy', 'Typecheck: [verify] typecheck_command'), 'default', 'No path is given because pyproject.toml configures mypy.'),
  ('Lint', '[verify] lint_command', field('uv run ruff check .', 'Lint: [verify] lint_command'), 'default', ''),
  ('Review', '[factory] review_mode', seg(['hard', 'advisory', 'skip'], 0, 'Review'), 'default', 'Hard sends a part back on a failed criterion or concern. Advisory records only.'),
  ('Security review', '[security] mode', seg(['hard', 'advisory', 'skip'], 0, 'Security review'), 'kstrl.toml', 'Off by default; this project turned it on.'),
  ('Security blocks at', '[security] fail_threshold', seg(['critical', 'high', 'medium'], 1, 'Security blocks at'), 'default', ''),
  ('Disputed claims', '[factory] claim_agreement', seg(['block', 'advisory'], 1, 'Disputed claims'), 'default', 'Blocks anyway at L1 and above, which is where this project is.'),
  ('Policy checks', '[policy] enabled', tog(False, 'Policy checks: [policy] enabled'), 'default', 'With it off, the level in force stays at L2 or below.'),
  ('Test adequacy', '[adequacy] enabled', tog(False, 'Test adequacy: [adequacy] enabled'), 'default', 'Checks for deleted tests and removed assertions.'),
]
rw = ''.join(f'''<div class="sr2"><div class="sl2"><b>{n}</b><span class="k">{k}</span>{f'<p>{d}</p>' if d else ''}</div><div class="sv">{c}</div><span class="src {s.replace('.', '')}">{s}</span></div>''' for n, k, c, s, d in rows)
FILTER = ('<div class="k-field flt"><div class="k-field-box"><input class="k-field-input" placeholder="Filter settings" aria-label="Filter settings" '
          f'aria-keyshortcuts="/" autocomplete="off" spellcheck="false">{keys("/")}</div></div>')
body = '    ' + top('<span class="nm">Settings</span>', 'kstrl.toml · 29 sections, 167 settings · 9 set in the file, 1 by the environment', None, None, 0,
                    tools=FILTER) + f'''
    <div class="st3">
      <div class="nav">{nav}<p class="nf">Grouped by what they decide, not by section.</p></div>
      <div class="k-tile main" role="tabpanel" id="sgp" aria-labelledby="sg0">
        <div class="mh2"><span class="k-label">How work is checked</span><span class="k-hint mhh">9 settings · 1 changed from its default</span></div>
        {rw}
      </div>
      <div class="side">
        <div class="k-tile k-tile-alert prob">
          <div class="kh"><span class="k-mk sm fail"></span><span class="pk">1 problem · every command refuses to start</span></div>
          <div class="pc"><span class="v-measure">[verfy] typecheck_command</span><p>No kstrl setting reads this. Did you mean <span class="v-measure">[verify]</span>?</p></div>
          <button class="k-button k-button-sm">Rename to <span class="v-measure">[verify]</span></button>
        </div>
        <div class="k-tile envt">
          <div class="k-label">Set by the environment</div>
          <div class="pc"><span class="v-measure">FACTORY_MAX_PARALLEL=8</span><p>Set where kstrl was started, and it wins over kstrl.toml, which says 4.</p></div>
        </div>
        <div class="k-tile sav">
          <div class="k-label">Saving</div>
          <p>Changes are written to kstrl.toml at the repository root. Before writing, the same checks every command runs are applied, so a value that would stop kstrl cannot be saved.</p>
          <p>kstrl reads the file again at the next command or run. A running spec keeps the values it started with.</p>
          <p class="t3n">Your inbox, trust level and spend live outside the repository, in ~/.local/state/kstrl/, shared by every clone of the same remote.</p>
        </div>
      </div>
    </div>'''

css = """
  .flt { width:240px; }
  .st3 { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:200px minmax(0,1fr) 330px; gap:14px; }
  .nav { display:flex; flex-direction:column; gap:2px; padding-top:4px; }
  .nf { margin:auto 0 0; font:var(--t-label); font-weight:400; color:var(--text-3); padding:0 10px; }

  .mh2 { display:flex; align-items:baseline; padding-bottom:6px; } .mhh { margin-left:auto; }
  .sr2 { display:grid; grid-template-columns:minmax(0,1fr) 210px 72px; column-gap:14px; align-items:center; padding:8px 0; border-top:1px solid var(--line); }
  .sl2 b { font:var(--t-body); font-weight:600; margin-right:8px; }
  .sl2 .k { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .sl2 p { margin:2px 0 0; font:var(--t-label); font-weight:400; color:var(--text-2); }
  .sv { display:flex; align-items:center; gap:8px; }
  .src { justify-self:end; font:var(--t-micro); font-weight:400; color:var(--text-3); }
  .src.kstrltoml { color:var(--text); font-weight:600; }
  .side { display:flex; flex-direction:column; gap:14px; min-height:0; }

  .kh { display:flex; align-items:center; gap:7px; }
  .pk { font:var(--t-small); font-weight:600; }
  .pc { margin-top:10px; }
  .pc > .v-measure { font:var(--t-measure-inline); } .pc p .v-measure { color:var(--text); }
  .prob .k-button .v-measure { }
  .pc p { margin:3px 0 0; font:var(--t-small); color:var(--text-2); }
.prob .k-button { align-self:flex-start; margin-top:12px; }

  .sav { flex:1; }
  .sav p { margin:8px 0 0; font:var(--t-small); color:var(--text-2); }
  .t3n { color:var(--text-3) !important; margin-top:auto !important; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="17 · Settings: kstrl.toml by what each setting decides" -->',
           'Settings', ['Settings'], 0, body, css)
out = re.sub(r'<span class="k-conds">.*?</span></span>', '', out, count=1, flags=re.S)
d = Path('../system/project/components/Settings'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
