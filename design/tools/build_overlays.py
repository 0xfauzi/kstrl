"""Frames 2 and 3: the command window over the factory view (same page underneath)."""
from __future__ import annotations

from pathlib import Path

from comp import keys

base = Path('../system/project/components/Map1Spec/preview.html').read_text()
assert 'data-wired-frames' not in base, 'Map1Spec is already wired: run build_all.sh, which draws it before wiring it'
LOGO = '<svg class="k-logo" viewBox="0 0 64 64" aria-hidden="true"><g fill="var(--text)"><path d="M30 20 L6 4 L9 12 L18 18 L28 27 Z"/><path d="M34 20 L58 4 L55 12 L46 18 L36 27 Z"/><path d="M32 9 L35 15 L36 24 L32 46 L28 24 L29 15 Z"/><path d="M32 44 L24 58 L32 55 L40 58 Z"/></g></svg>'
common_css = """
  .scr { position:relative; }
  .ov { position:absolute; left:0; right:0; margin:0 auto; }
  .lab2 { font:var(--t-label); color:var(--text-3); margin:14px 0 6px; }
  .said { font:var(--t-body); }
"""
def page(subtitle: str, title: str, css: str, overlay: str) -> str:
    s = base.replace('subtitle="1 · Spec level: the plan, live"', f'subtitle="{subtitle}"')
    s = s.replace('<title>Spec level: the plan, live</title>', f'<title>{title}</title>')
    s = s.replace('</style>', common_css + css + '</style>', 1)
    i = s.rindex('</div>\n</body>')
    return s[:i] + '  <div class="k-scrim"></div>\n' + overlay + '\n' + s[i:]

# ---- frame 2: a question -----------------------------------------------------
q_css = """
  .ov.q { top:72px; width:900px; height:640px; }
  .ov.q .k-cmd-body { --k-cmd-list:340px; }
  .ov.q .k-cmd-detail h2 { margin:0; font:var(--t-title); letter-spacing:var(--t-title-ls); }
  .tries { display:grid; grid-template-columns:44px 16px 1fr; row-gap:4px; column-gap:8px; font:var(--t-small); }
  .tries .v-measure { font:var(--t-measure-inline); color:var(--text-3); }
  .lgt { margin:0; font:var(--t-small); }
  .lgs { display:block; font:var(--t-measure-inline); color:var(--text-3); }
"""
q_overlay = f"""  <div class="k-cmd ov q" role="dialog" aria-modal="true" aria-label="Ask kstrl">
    <div class="k-cmd-input">{LOGO}<input class="k-cmd-q" placeholder="Ask or do anything" aria-label="Ask or do anything" role="combobox" aria-expanded="true" aria-controls="cmd-list" aria-autocomplete="list" aria-activedescendant="cmd-r0" autocomplete="off" spellcheck="false">{keys('esc')}</div>
    <div class="k-cmd-body">
      <div class="k-cmd-list" role="listbox" id="cmd-list" aria-label="Answers and actions">
        <div class="k-cmd-sec" role="presentation">Answer from the run records</div>
        <div class="k-row" role="option" id="cmd-r0" aria-selected="true"><span class="k-cmd-q-mark" aria-hidden="true">?</span><span class="k-row-title">Why did search-highlight stop?</span><span class="k-row-meta"></span></div>
        <div class="k-cmd-sec" role="presentation">Do something about it</div>
        <div class="k-row" role="option" id="cmd-r1" aria-selected="false" aria-disabled="true"><span></span><span class="k-row-title">Retry search-highlight</span><span class="k-row-meta">after this run</span></div>
        <div class="k-row" role="option" id="cmd-r2" aria-selected="false"><span></span><span class="k-row-title">Add guidance for the engineers</span><span class="k-row-meta">{keys('⌘', 'G')}</span></div>
        <div class="k-row" role="option" id="cmd-r3" aria-selected="false"><span></span><span class="k-row-title">Open the engineer’s log</span><span class="k-row-meta">{keys('⌘', 'O')}</span></div>
        <div class="k-cmd-sec" role="presentation">Other questions about search</div>
        <div class="k-row" role="option" id="cmd-r4" aria-selected="false"><span class="k-cmd-q-mark" aria-hidden="true">?</span><span class="k-row-title">What is waiting on me?</span><span class="k-row-meta"></span></div>
        <div class="k-row" role="option" id="cmd-r5" aria-selected="false"><span class="k-cmd-q-mark" aria-hidden="true">?</span><span class="k-row-title">What has this run cost, by phase?</span><span class="k-row-meta"></span></div>
        <div class="k-row" role="option" id="cmd-r6" aria-selected="false"><span class="k-cmd-q-mark" aria-hidden="true">?</span><span class="k-row-title">What did the architect decide?</span><span class="k-row-meta"></span></div>
        <div class="k-row" role="option" id="cmd-r7" aria-selected="false"><span class="k-cmd-q-mark" aria-hidden="true">?</span><span class="k-row-title">What runs next, and why not yet?</span><span class="k-row-meta"></span></div>
        <div class="k-row" role="option" id="cmd-r8" aria-selected="false"><span class="k-cmd-q-mark" aria-hidden="true">?</span><span class="k-row-title">Is anything not being checked?</span><span class="k-row-meta"></span></div>
      </div>
      <div class="k-cmd-detail">
        <h2>search-highlight stopped at 21:03</h2>
        <div class="lab2">Why</div>
        <div class="said">The no-progress breaker failed it on try 2 of 4: iterations 4, 5 and 6 left both the code and the test results unchanged. It fails a part at once rather than spend its last two tries on the same prompt and the same tree.</div>
        <div class="lab2">Its tries · <span class="v-measure">≥$3.22</span></div>
        <div class="tries">
          <span class="v-measure">try 1</span><span class="k-mk sm fail"></span><span>review: 3 blocking findings, sent back with them</span>
          <span class="v-measure">try 2</span><span class="k-mk sm fail"></span><span>engineer: iterations 4, 5 and 6 left the code and tests unchanged</span>
        </div>
        <div class="lab2">The engineer’s last words</div>
        <div class="k-well"><p class="lgt">Overlapping matches ("sea" inside "search") still render as two spans. I have not found a way to merge them without changing the tokenizer, which is outside this part’s scope.</p><span class="v-measure lgs">engineer.log · iteration 6</span></div>
        <div class="lab2">What stopping it did</div>
        <div class="said">search-cli was skipped, because it needs search-highlight. Once this run ends, a retry starts both again.</div>
        <div class="k-cmd-srcs" aria-label="Sources"><span class="k-cmd-src">events.jsonl</span><span class="k-cmd-src">manifest.json</span><span class="k-cmd-src">engineer.log</span></div>
      </div>
    </div>
    <div class="k-cmd-bar"><span class="k-cmd-bar-note">Answers come from the run records</span><button class="k-cmd-act k-cmd-act-primary">Go to search-highlight {keys('↵')}</button></div>
  </div>"""
d = Path('../system/project/components/Map5Question'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(page('4 · Asking: questions answered from the run records', 'Frame 2: a question', q_css, q_overlay))

# ---- frame 3: the approval -----------------------------------------------------
a_css = """
  .ov.a { top:64px; width:1000px; height:660px; }
  .ov.a .k-cmd-body { grid-template-columns:minmax(0,1fr) 380px; }
  .evd { padding:16px 20px 0; overflow:hidden; }
  .evd h2 { margin:0; font:var(--t-title); letter-spacing:var(--t-title-ls); }
  .evd .desc { color:var(--text-2); margin-top:2px; }
  .stories { display:grid; grid-template-columns:52px minmax(0,1fr) auto; column-gap:10px; row-gap:6px; font:var(--t-body); align-items:baseline; }
  .stories .v-measure { font:var(--t-measure-inline); color:var(--text-3); }
  .stories .c { font:var(--t-label); font-weight:400; color:var(--text-2); display:flex; gap:5px; align-items:center; }
  .meas { display:grid; grid-template-columns:16px 72px minmax(0,1fr); column-gap:8px; row-gap:5px; align-items:baseline; } .meas > .k-mk { align-self:center; }
  .meas .v { font:var(--t-measure); color:var(--text-2); }
  .fnd { display:grid; grid-template-columns:104px minmax(0,1fr); column-gap:8px; font:var(--t-small); margin-bottom:6px; align-items:baseline; }
  .fnd .sev { font:var(--t-measure-small); font-weight:600; color:var(--text-2); }
  .fnd .loc { font:var(--t-measure-small); font-weight:400; color:var(--text-3); display:block; }
  .dec { border-left:1px solid var(--line); padding:16px 16px 0; overflow:hidden; }
  .sec2 { display:flex; justify-content:space-between; align-items:baseline; padding:8px 12px; font:var(--t-small); color:var(--text-2); }
"""
a_overlay = """  <div class="k-cmd ov a" role="dialog" aria-modal="true" aria-label="Approve search-index">
    <div class="k-cmd-input"><span class="k-keys"><span class="k-key">←</span></span><input class="k-cmd-q" value="Approve search-index?" aria-label="Ask or do anything"><span class="k-cmd-context">your approval · waiting 12m</span></div>
    <div class="k-cmd-body">
      <div class="evd">
        <h2>search-index</h2>
        <div class="desc">Keeps a full-text index of snippet titles and bodies, updated on every save and delete.</div>
        <div class="lab2">What the engineer says it did · 3 stories</div>
        <div class="stories">
          <span class="v-measure">US-1</span><span>Index a snippet's title and body when it is saved</span><span class="c">claims done</span>
          <span class="v-measure">US-2</span><span>Remove a snippet from the index when it is deleted</span><span class="c">claims done</span>
          <span class="v-measure">US-3</span><span>Fold case and accents before indexing</span><span class="c">claims done</span>
        </div>
        <div class="lab2">What was measured by something that did not write it</div>
        <div class="k-well"><div class="meas">
          <span class="k-mk pass"></span><span>verify</span><span class="v">31 passed · typecheck · lint · scope</span>
          <span class="k-mk pass"></span><span>review</span><span class="v">0 blocking · 2 advisory</span>
          <span class="k-mk pass"></span><span>security</span><span class="v">0 findings</span>
          <span class="k-mk pass"></span><span>distill</span><span class="v">2 facts recorded</span>
        </div></div>
        <div class="lab2">Advisory findings · they do not block</div>
        <div class="fnd"><span class="sev">error_handling</span><span>A failed reindex is logged and skipped, so the index can fall behind the table without anyone knowing.<span class="loc">search/index.py:52</span></span></div>
        <div class="fnd"><span class="sev">test_quality</span><span>The accent test covers é and ü but no combining characters.<span class="loc">tests/test_index.py:88</span></span></div>
      </div>
      <div class="dec">
        <div class="lab2" style="margin-top:0">What each choice does</div>
        <div class="k-choices" role="radiogroup" aria-label="Your decision on search-index"><div class="k-choice" role="radio" aria-checked="true" tabindex="0"><span class="k-choice-title">Approve</span><span class="k-keys"><span class="k-key">↵</span></span><span class="k-choice-then">Pushes kstrl/factory/search-index and merges its PR. search-index is done once the merge is confirmed.</span></div><div class="k-choice" role="radio" aria-checked="false" tabindex="-1"><span class="k-choice-title">Retry</span><span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">↵</span></span><span class="k-choice-then">The engineer runs it again, told that a person asked for changes. Your own words are not passed on. Uses 1 of 3 retries.</span></div><div class="k-choice k-choice-danger" role="radio" aria-checked="false" tabindex="-1"><span class="k-choice-title">Reject</span><span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">⌫</span></span><span class="k-choice-then">search-index fails and search-api is skipped. Nothing is pushed.</span></div><div class="k-choice" role="radio" aria-checked="false" tabindex="-1"><span class="k-choice-title">Later</span><span class="k-keys"><span class="k-key">esc</span></span><span class="k-choice-then">It keeps waiting here. Nothing times out.</span></div></div>
        <div class="lab2">One level down</div>
        <div class="sec2"><span>Open the diff</span><span class="v-measure">+412 −18 · 5 files</span></div>
        <div class="sec2"><span>Spend on this part</span><span class="v-measure">≥$4.34 · ≥301k tok</span></div>
      </div>
    </div>
    <div class="k-cmd-bar"><span class="k-cmd-bar-note"><span class="k-mk sm you"></span><span>L2 · every merge waits for your approval</span></span><button class="k-cmd-act k-cmd-act-primary">Approve and merge <span class="k-keys"><span class="k-key">↵</span></span></button></div>
  </div>"""
d = Path('../system/project/components/Map6Approve'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(page('5 · Approving: the claim beside what measured it', 'Frame 3: approving a merge', a_css, a_overlay))
print('ok')
