"""Spec level, as text: your spec on one sheet, the architect's decisions pinned to the words they resolved."""
from __future__ import annotations
from pathlib import Path
from level_common import HINT, page, top

def pin(n: int) -> str:
    return f'<span class="k-ref pinm">{n}</span>'
decisions = [
  (1, 'search-query', 'How fast is fast, and for how large a collection?', 'Under 100 ms for 10,000 snippets.',
   'The spec gives no number; 10,000 is the largest collection in the test fixtures.', 'no bound, measured only'),
  (2, 'search-rank', 'What does relevance mean?', 'BM25, SQLite FTS5’s built-in ranking.',
   'FTS5 is already a dependency, and BM25 needs no tuning.', 'newest first'),
  (3, 'search-cli', 'How many matches is “the best matches”?', 'The ten best.',
   'Ten fit on one terminal screen with their titles.', 'all matches, paged'),
]
dec = ''.join(f'''        <div class="k-tile d k-tile-dense"><div class="dh">{pin(n)}<span>applies to {part}</span><span class="grow"></span><span class="chg">change</span></div>
          <div class="q">{q}</div><div class="r">{r}</div><div class="why">{why}</div><div class="alt">Considered: {alt}.</div></div>
''' for n, part, q, r, why, alt in decisions)
parts = [('landed', 'search-schema'), ('you', 'search-index'), ('work', 'search-query'), ('work', 'search-rank'),
         ('fail', 'search-highlight'), ('wait', 'search-api'), ('skip', 'search-cli')]
chips = ''.join(f'<span class="pt"><span class="k-mk sm {m}"></span>{n}</span>' for m, n in parts)
body = '    ' + top('<span class="v-human">search</span>', '7 parts · 3 of 4 slots in use · ≥$21.84 since 19:40', 1, ['Graph', 'Text'], 1) + f'''
    <div class="two">
      <div class="k-tile doc k-tile-hero">
        <div class="file">specs/search.md · your words</div>
        <p>People can find a snippet by the words in its title or its body.</p>
        <p>Matching ignores case and accents, so “Café” finds “cafe”.</p>
        <p>Results come back <mark class="k-hl">fast, even for a large collection</mark>{pin(1)}, ranked <mark class="k-hl">by relevance</mark>{pin(2)}.</p>
        <p><span class="code">snip search &lt;words&gt;</span> prints <mark class="k-hl">the best matches</mark>{pin(3)} with their titles, with the matching words highlighted.</p>
        <p>The API answers <span class="code">GET /search?q=</span> with the matches as JSON.</p>
        <div class="made"><div class="k-label">The architect made 7 parts from it</div><div class="pts">{chips}</div></div>
      </div>
      <div class="side">
        <div class="k-label">The architect decided 3 things your spec left open</div>
{dec}        <div class="k-tile k-tile-idle nq k-tile-dense"><div class="k-label">Questions for you</div><p>None. When the architect cannot decide something, the run does not start and the question waits here.</p></div>
      </div>
    </div>'''
css = """
  .two { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 392px; gap:14px; }

  .doc .file { font:var(--t-measure-inline); color:var(--text-3); margin-bottom:16px; }
  .doc p { margin:0 0 14px; font:var(--t-intent); max-width:31em; }
  .doc .code { font:var(--t-measure-code); }
  .pinm { margin-left:4px; vertical-align:3px; }
  .made { margin-top:auto; border-top:1px solid var(--line); padding-top:14px; }
  .pts { display:grid; grid-template-columns:repeat(4, max-content); gap:8px 30px; margin-top:10px; font:var(--t-small); }
  .pt { display:inline-flex; align-items:center; gap:6px; }
  .side { display:flex; flex-direction:column; gap:10px; min-height:0; }
  .side > .k-label { margin:2px 0 0 4px; }
  .d .dh { display:flex; align-items:center; gap:6px; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .d .dh .k-ref { margin:0 2px 0 0; vertical-align:0; }
  .d .chg { color:var(--text-2); }
  .d .q { font:var(--t-small); color:var(--text-2); margin-top:6px; }
  .d .r { font:var(--t-body); font-weight:600; margin-top:2px; }
  .d .why { font:var(--t-small); color:var(--text-2); margin-top:3px; }
  .d .alt { font:var(--t-label); font-weight:400; color:var(--text-3); margin-top:5px; }
  .nq { flex:1; }
  .nq p { margin:6px 0 0; font:var(--t-small); color:var(--text-2); }
"""
NEEDS_HINT = 'To change a decision, change the sentence it came from. The next run plans the spec again.'
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="3 · Spec level, as text: your spec and what the architect decided" -->',
           'Spec level, as text', ['search'], 1, body, css)
d = Path('../system/project/components/Map4Plan'); d.mkdir(parents=True, exist_ok=True)
out = out.replace(HINT, NEEDS_HINT)
assert NEEDS_HINT in out
(d / 'preview.html').write_text(out)
print('ok')
