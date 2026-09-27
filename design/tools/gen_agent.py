"""Step level, one agent: everything search-index's engineer has written in try 1, at 20:46 (the moment of Stage and Grid)."""
from __future__ import annotations

from pathlib import Path

from agent_level import CSS as css
from agent_level import agent_body
from level_common import page

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
# iterations, from engineer.jsonl: iteration_completed {duration_seconds, fast_checks_failed}
its = [(1, '5m 40s', ['f', 'p']), (2, '4m 12s', ['p', 'p']), (3, '6m 03s', ['p', 'f']), (4, '3m 20s', ['p', 'p']),
       (5, '4m 48s', ['p', 'p']), (6, '3m 55s', ['f', 'p']), (7, '3m 31s', ['p', 'p'])]
body = agent_body('<span class="nm">search-index</span><span class="k-chip">engineer</span>', 'try 1 of 4 · iteration 8 of up to 10 · output 6s ago',
                  'try 1, from 20:13', log,
                  'Deletes now remove the index row in the same transaction. Both test files pass; next is US-3, folding case and accents before indexing.',
                  its, 7, 8, '8 running · 47s',
                  [('US-1', 'Index a snippet’s title and body when it is saved', 'done'), ('US-2', 'Remove a snippet from the index when it is deleted', 'not yet'),
                   ('US-3', 'Fold case and accents before indexing', 'not yet')],
                  'Read when the next try starts. Try 1 will not see it.')
NEEDS = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <span class="k-needs-none">Nothing right now</span>

    <span class="k-needs-hint">Notes is its progress.txt · Prompt is exactly what it was sent, per call</span>
  </div>'''
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="3c · Step level, one agent: everything it has written" -->',
           'Step level: one agent', ['search', 'being built', 'search-index'], 3, body, css, NEEDS)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">live · last event 2s ago</span>').replace('≥$31.10 <small>of $40.00 today</small>', '≥$19.40 <small>of $40.00 today</small>')
d = Path('../system/project/components/Map3Agent'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
