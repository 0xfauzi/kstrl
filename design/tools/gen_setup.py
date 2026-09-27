"""First run: pointing kstrl at a repository. How it will check the work, which agent builds, what it writes, and whether the repo is ready."""
from __future__ import annotations

from pathlib import Path

from level_common import page, top

verify = [
  ('Tests', 'uv run pytest', 'kstrl’s default for Python'),
  ('Typecheck', 'uv run mypy', 'pyproject.toml configures mypy, so no path is given'),
  ('Lint', 'uv run ruff check .', 'kstrl’s default for Python'),
]
vrows = ''.join(f'<div class="vr"><span class="vl">{a}</span><div class="k-field k-field-measure"><div class="k-field-box"><input class="k-field-input" value="{c}" aria-label="{a} command" autocomplete="off" spellcheck="false"></div></div><span class="vs">{s}</span></div>' for a, c, s in verify)
files = [('create', 'kstrl.toml', '[verify] only if you change a command above'), ('create', 'scripts/kstrl/', 'the engineer prompt, your guidance and golden patterns, and 4 more'),
         ('append', 'CLAUDE.md', 'a block about kstrl at the end'), ('append', '.gitignore', 'kstrl’s working files'), ('keep', 'AGENTS.md', 'already has the block')]
frows = ''.join(f'<div class="fr2"><span class="act {a}">{a}</span><span class="fp">{p}</span><span class="fd">{d}</span></div>' for a, p, d in files)
checks = [('pass', 'git_repo', 'a git repository'), ('pass', 'git_clean', 'nothing uncommitted'), ('pass', 'github_cli', 'gh is signed in, so PRs can open'),
          ('wait', 'kstrl_config', 'written by Set up'), ('pass', 'build_manifest', 'pyproject.toml'), ('pass', 'verify_commands', 'all three resolve'),
          ('pass', 'source_root', 'src/snippetvault'), ('pass', 'test_root', 'tests/'), ('wait', 'gitignore', 'written by Set up'), ('pass', 'protected_paths', 'none set')]
crows = ''.join(f'<div class="cr2"><span class="k-mk sm {m}"></span><span class="cn2">{n}</span><span class="cd">{d}</span></div>' for m, n, d in checks)
body = '    ' + top('<span class="nm">Set up kstrl</span>', '~/code/snippetvault · a Python project, run with uv', None) + f'''
    <div class="su">
      <div class="k-tile stp k-tile-hero">
        <div class="sec"><span class="n">1</span><div class="sb"><div class="sh2">How kstrl checks the work</div>
          <p class="lead">Verify runs these on every part before any reviewer reads it. They are what “working” means here.</p>
          <div class="vrs">{vrows}</div></div></div>
        <div class="sec"><span class="n">2</span><div class="sb"><div class="sh2">Which agent builds</div>
          <div class="ag"><div class="k-seg" role="radiogroup" aria-label="Engineer agent"><span class="k-seg-thumb" aria-hidden="true"></span><button class="k-seg-item" role="radio" aria-checked="true" tabindex="0">Claude Code</button><button class="k-seg-item" role="radio" aria-checked="false" tabindex="-1">Codex</button></div><span class="t3 ty-small">the model and reasoning its CLI is set to</span></div></div></div>
        <div class="sec"><span class="n">3</span><div class="sb"><div class="sh2">What kstrl will write</div>
          <div class="frs">{frows}</div></div></div>
        <div class="go"><button class="k-button k-button-primary">Set up <span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">↵</span></span></button><span class="t2 ty-small">Then write your first spec, in your own words.</span></div>
      </div>
      <div class="k-tile rdy k-tile-hero">
        <div class="k-label">Is this repository ready?</div>
        <div class="rh"><span class="k-stat-value">8<small>/10</small></span><span>ready now · 2 written by Set up</span></div>
        <div class="crs2">{crows}</div>
        <p class="ft">ks doctor checks only what kstrl reads, and names what reads it. It runs nothing and spends nothing.</p>
      </div>
    </div>'''
FOOT = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <span class="k-needs-none">Nothing yet</span>
    <span class="k-needs-hint">Nothing is written until you press Set up, and nothing that exists is overwritten.</span>
  </div>'''
css = """
  .su { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 400px; gap:14px; }

  .sec { display:grid; grid-template-columns:30px 1fr; gap:10px; padding:14px 0; border-bottom:1px solid var(--line); align-items:baseline; }
  .sec:first-child { padding-top:0; }
  .n { font:var(--t-measure); font-weight:600; color:var(--text-3); }
  .sh2 { font:var(--t-input); font-weight:600; letter-spacing:var(--t-input-ls); }
  .lead { margin:4px 0 0; font:var(--t-small); color:var(--text-2); }
  .vrs { margin-top:10px; display:flex; flex-direction:column; gap:6px; }
  .vr { display:grid; grid-template-columns:78px 220px 1fr; align-items:baseline; column-gap:12px; }
  .vl { font:var(--t-small); color:var(--text-2); }
  .vs { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .ag { display:flex; align-items:center; gap:12px; margin-top:8px; }
  .frs { margin-top:8px; }
  .fr2 { display:grid; grid-template-columns:58px 150px 1fr; align-items:baseline; column-gap:10px; padding:4px 0; font:var(--t-small); }
  .act { font:var(--t-measure-small); font-weight:600; letter-spacing:var(--t-measure-small-ls); color:var(--text-2); }
  .act.keep { color:var(--text-3); }
  .fp { font:var(--t-measure); }
  .fd { color:var(--text-3); }
  .go { display:flex; align-items:center; gap:16px; margin-top:auto; padding-top:16px; }

  .rh { display:flex; align-items:baseline; gap:10px; margin-top:6px; }
  .rh span:last-child { font:var(--t-small); color:var(--text-3); }
  .crs2 { margin-top:12px; }
  .cr2 { display:grid; grid-template-columns:18px 118px 1fr; align-items:baseline; column-gap:8px; padding:6px 0; border-bottom:1px solid var(--line); font:var(--t-small); } .cr2 > .k-mk { align-self:center; }
  .cn2 { font:var(--t-measure-inline); }
  .cd { color:var(--text-2); }
  .ft { margin:auto 0 0; font:var(--t-label); font-weight:400; color:var(--text-3); }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="15 · First run: pointing kstrl at a repository" -->',
           'Set up kstrl', ['Set up'], 0, body, css, FOOT)
import re

out = re.sub(r'<span class="k-conds">.*?</span></span>', '', out, count=1, flags=re.S)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">no runs yet</span>').replace('≥$31.10 <small>of $40.00 today</small>', '<span class="t3">no budget is set</span>')
d = Path('../system/project/components/Setup'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
