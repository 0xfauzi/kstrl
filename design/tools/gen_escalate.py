"""Planning stopped: the architect planned tags at 00:01, after the daily budget reset, and refused one question. Spec level, as text."""
from __future__ import annotations
from pathlib import Path
from level_common import page, top

def pin(n: str, cls: str = '') -> str:
    return f'<span class="k-ref{" k-ref-ask" if cls == "ask" else ""} pinm">{n}</span>'
doc = f'''      <div class="k-tile doc k-tile-hero">
        <div class="file">specs/tags.md · your words · the queued copy</div>
        <p>Snippets can carry tags, and search can filter by them.</p>
        <p>A tag is <mark class="k-hl">a short word</mark>{pin('2')} such as <span class="code">python</span> or <span class="code">sql</span>.</p>
        <p><mark class="k-hl k-hl-ask">Anyone who can see a snippet can see its tags.</mark>{pin('?', 'ask')}</p>
        <p><span class="code">snip search tags:python</span> finds the snippets tagged python, <mark class="k-hl">using the same index as words</mark>{pin('3')}.</p>
        <p><mark class="k-hl">Tags can be renamed.</mark>{pin('1')}</p>
        <div class="made"><div class="k-label">No plan yet</div><p>The architect stopped before writing one, so there are no parts and nothing started. The Graph view fills in once a plan is written.</p></div>
      </div>'''
q = '''      <div class="k-tile k-tile-ask askt">
        <div class="kh"><span class="k-mk sm you"></span><span class="kind">The architect will not decide this</span><span class="grow"></span><span class="age">00:04</span></div>
        <div class="qq">Are tags shared by everyone who can see a snippet, or does each person keep their own?</div>
        <p class="why">Your sentence says who can see tags, not who owns them. Shared tags live on the snippet; personal tags need a table keyed by person. Moving from one to the other once code exists means migrating every tag.</p>
        <div class="howh">Answer it in your spec, with a sentence</div>
        <p class="how">You edit specs/tags.md. kstrl ran a copy of it, so answering removes the stopped queue item and adds your edited spec again, at priority 5.</p>
        <div class="ab"><button class="k-button k-button-primary">Edit the sentence <span class="k-keys"><span class="k-key">↵</span></span></button><span class="t3 ty-small">ks serve counted this as poisoned: 1&nbsp;of&nbsp;3 before it pauses the queue</span></div>
      </div>'''
dec = '''      <div class="k-tile dl">
        <div class="k-label">What it closed itself, 3</div>
        <div class="d"><span class="k-ref">1</span><div><b>decided</b> Renaming a tag renames it on every snippet. <span class="t3">Considered: keeping the old name as an alias.</span></div></div>
        <div class="d"><span class="k-ref">2</span><div><b>assumed</b> Lowercase, 1 to 32 characters, pinned as an acceptance criterion so a test checks it.</div></div>
        <div class="d"><span class="k-ref">3</span><div><b>spiked</b> Ran <span class="v-measure">sqlite3 :memory: 'pragma compile_options'</span>; it listed <span class="v-measure">ENABLE_FTS5</span>, so tags go in the same index.</div></div>
      </div>'''
body = '    ' + top('<span class="v-human">tags</span>', 'planned 00:01 to 00:04 · stopped on 1 question · nothing started', 1, ['Graph', 'Text'], 1) + f'''
    <div class="two">
{doc}
      <div class="side">
{q}
{dec}
      </div>
    </div>'''
NEEDS = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <button class="k-need k-need-ask"><span class="k-mk you"></span><b>Answer the architect on tags</b><span class="k-need-sub">1 question · 2m</span></button>
    
    <span class="k-needs-hint">One ask, two records: the question, and ks serve’s note that the item stopped. They are shown as one.</span>
  </div>'''
css = """
  .two { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 440px; gap:14px; }

  .doc .file { font:var(--t-measure-inline); color:var(--text-3); margin-bottom:14px; }
  .doc p { margin:0 0 12px; font:var(--t-intent); max-width:31em; }
  .doc .code { font:var(--t-measure-code); }
  .pinm { margin-left:4px; vertical-align:3px; }
  .made { margin-top:auto; border-top:1px solid var(--line); padding-top:14px; }
  .made p { margin:6px 0 0; font:var(--t-small); color:var(--text-2); max-width:none; }
  .side { display:flex; flex-direction:column; gap:14px; min-height:0; }

  .kh { display:flex; align-items:center; gap:7px; }
  .kind { font:var(--t-small); font-weight:600; color:var(--you); }
  .age { font:var(--t-measure-inline); color:var(--text-3); }
  .qq { font:var(--t-title); letter-spacing:var(--t-title-ls); margin-top:10px; }
  .why { margin:8px 0 0; font:var(--t-body); color:var(--text-2); }
  .howh { font:var(--t-label); font-weight:400; color:var(--text-3); margin-top:14px; }
  .how { margin:4px 0 0; font:var(--t-small); }
  .ab { display:flex; align-items:center; gap:12px; margin-top:14px; }
.k-button-primary { flex:none; }

  .dl { flex:1; }
  .d { display:grid; grid-template-columns:22px 1fr; gap:8px; font:var(--t-small); padding:8px 0; border-bottom:1px solid var(--line); }
  .d:last-child { border-bottom:0; }
  .d .k-ref { margin:1px 0 0; vertical-align:0; align-self:start; justify-self:start; }
  .d b { font:var(--t-measure-small); font-weight:600; color:var(--text-2); margin-right:4px; }
  .d .v-measure { }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="9 · Planning stopped: the architect asks, you answer in the spec" -->',
           'Planning stopped', ['tags'], 1, body, css, NEEDS)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">not live · last event 00:04</span>').replace('≥$31.10 <small>of $40.00 today</small>', '≥$0.86 <small>of $40.00 today</small>')
d = Path('../system/project/components/Escalation'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
