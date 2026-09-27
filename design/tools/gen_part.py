"""Level 2 (Part): search-query's tries as rows across the stations of the line, with the step panel."""
from __future__ import annotations
from pathlib import Path
from level_common import top
from part_level import part_page

rows = [
  # (label, cells) cell = (cls, mark, main, sub)
  ('try 1', [('done', '', '7 iterations', '34m'), ('ok', 'pass', '28 passed', '39s'), ('bad', 'fail', '5 blocking', '71s'), None, None, None, None]),
  ('try 2', [('done', '', '5 iterations', '26m'), ('ok', 'pass', '30 passed', '40s'), ('bad sel', 'fail', '2 blocking', '64s'), None, None, None, None]),
  ('try 3', [('done', '', '4 iterations', '24m'), ('ok', 'pass', '31 passed', '41s'), ('now', 'work', 'reviewing', 'started 1m ago'), ('todo',), ('todo',), ('todo',), ('todo',)]),
  ('try 4', 'one try left · if review still fails, search-query stops and waits for you'),
]
returns = [(3, 0, 'sent back with its 5 blocking findings'), (3, 1, 'sent back with the 2 still failing')]

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
out = part_page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="2 · Part level: every try across the line, and one step open" -->',
                'Part level: search-query', 'search-query', head, rows, returns, step)
d = Path('../system/project/components/Map2Part'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
