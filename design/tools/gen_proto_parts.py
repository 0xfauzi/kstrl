"""The Part level of the six parts Map2Part does not draw, at the prototype's moment (search being built, 21:40).

Every fact here is one another screen at 21:40 already states, so they cannot disagree: states, tries and spend from
parts.py (the Spec level's cards); search-index's evidence from the approval (Map6Approve); search-highlight's stop, its
tries and its last words from the command window's answer (Map5Question); search-schema's merge from the Factory
(#48 at 20:12, CI passing on a41c9e2) and the Inbox (you approved it at 20:10); the decisions from the plan (Map4Plan).
What no screen states is marked where it is drawn: iteration counts and durations of the finished tries, and
search-rank's latest words, are this prototype's example values.

These are prototype frames, not design-system cards: they are written to out/static, where build_prototype.py reads
every frame, and audited from there (audit.py ProtoPart-<part>)."""
from __future__ import annotations

from pathlib import Path

from level_common import top
from part_level import part_page

OUT = Path('out/static')
CMD = '<span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">{}</span></span>'
ENTER = '<span class="k-keys"><span class="k-key">↵</span></span>'
TODO = ('todo',)


def mk(m: str) -> str:
    return f'<span class="k-mk sm {m}"></span>'


def head(title: str, meta: str, desc: str, ctx: list[str]) -> str:
    return ('    ' + top(title, meta, 2) + f'''
    <div class="pd"><span>{desc}</span>
      <span class="ctx">{"".join(f"<span>{c}</span>" for c in ctx)}</span></div>''')


def steps(f1: str, f2: str, f3: str) -> str:
    return f'''    <div class="steps">
{f1}
{f2}
{f3}
    </div>'''


def ink(sh: str, sev: str, say: str, src: str) -> str:
    return f'''      <div class="k-tile k-tile-ink f1 k-tile-hero">
        <div class="sh">{sh}</div>
        <div class="sev">{sev}</div>
        <p class="say">{say}</p>
        <div class="src">{src}</div>
      </div>'''


def button(label: str, keys: str = '', extra: str = '') -> str:
    return f'<button class="k-button k-button-block"{extra}>{label}{keys}</button>'


PARTS = {
  'search-schema': dict(
    title='<span class="nm">search-schema</span>', meta='merged #48 · 1 try · ≥$2.18',
    desc='Adds the full-text table that search reads, and its migration.',
    ctx=['needs nothing: the first part', f'{mk("work")}needed by 4 parts, started when it merged'],
    rows=[('try 1', [('done', '', '5 iterations', '18m'), ('ok', 'pass', '12 passed', '31s'), ('ok', 'pass', '0 blocking', '52s'),
                     ('ok', 'pass', '0 findings', '44s'), ('done', '', '1 fact', '29s'), ('ok', 'pass', 'approved', '20:10'),
                     ('ok sel', 'landed', '#48 merged', '20:12')])],
    returns=[],
    step=steps(
      ink('<span>merge · #48</span><span class="grow"></span><span>CI passing on a41c9e2</span>', 'merged at 20:12',
          'You approved it at 20:10 and it merged two minutes later. The four parts that needed it started at once.',
          '#48 · a41c9e2 · 20:12'),
      '''      <div class="k-tile f2">
        <div class="sev2">the distiller recorded <span>1 fact</span></div>
        <p>search_fts is an FTS5 table whose rowid is the snippet’s id.</p>
        <div class="tf">every part after it reads this</div>
      </div>''',
      f'''      <div class="k-tile f3">
        <div class="k-label">Its one try</div>
        <p class="cl">5 iterations, every check agreed the first time.</p>
        <div class="acts">
          {button('Read the review log', CMD.format('L'))}
          {button('Open #48')}
        </div>
        <div class="tf">Nothing here waits for you.</div>
      </div>''')),

  'search-index': dict(
    title='<span class="nm">search-index</span>', meta='your approval · waiting 12m · 1 try · ≥$4.34',
    desc='Keeps a full-text index of snippet titles and bodies, updated on every save and delete.',
    ctx=[f'{mk("landed")}needs search-schema, merged', f'{mk("wait")}needed by search-api'],
    rows=[('try 1', [('done', '', '10 iterations', '1h 12m'), ('ok', 'pass', '31 passed', '41s'), ('ok', 'pass', '0 blocking', '2 advisory · 80s'),
                     ('ok', 'pass', '0 findings', '52s'), ('done', '', '2 facts', '38s'), ('you sel', 'you', 'waiting', 'since 21:28'), TODO])],
    returns=[],
    step=steps(
      ink('<span>your approval · waiting 12m</span><span class="grow"></span><span>L2 · every merge waits for you</span>', 'every check agreed',
          '31 tests passed, and neither review nor security found anything that blocks.',
          '3 stories claimed done · +412 −18 · 5 files'),
      '''      <div class="k-tile f2">
        <div class="sev2">error_handling <span>search/index.py:52</span></div>
        <p>A failed reindex is logged and skipped, so the index can fall behind the table.</p>
        <div class="tf">advisory · 1 more finding · neither blocks</div>
      </div>''',
      f'''      <div class="k-tile f3">
        <div class="k-label">Your decision</div>
        <p class="cl">Approve, retry, reject, or leave it waiting. Each choice says what it does before you take it.</p>
        <div class="acts">
          {button('Open the approval', ENTER)}
        </div>
        <div class="tf">Opening it approves nothing.</div>
      </div>''')),

  'search-rank': dict(
    title='<span class="nm">search-rank</span><span class="k-chip">engineer</span>', meta='try 1 of 4 · iteration 9 of up to 10',
    desc='Orders the matches by relevance.',
    ctx=[f'{mk("landed")}needs search-schema, merged', f'{mk("wait")}needed by search-api', 'decision 2: BM25, SQLite FTS5’s built-in ranking'],
    rows=[('try 1', [('now sel', 'work', 'iteration 9', 'of up to 10 · 1h 28m'), TODO, TODO, TODO, TODO, TODO, TODO])],
    returns=[],
    step=steps(
      ink('<span>engineer · try 1 · iteration 9 · output 6s ago</span><span class="grow"></span><span>no spend counted until a phase ends</span>',
          'its latest words', 'Titles now count twice as much as bodies in bm25(). The ranking tests pass; running the whole suite.',
          'engineer.log · iteration 9'),
      '''      <div class="k-tile f2">
        <div class="sev2">fast checks after each iteration</div>
        <p>Tests failed after iterations 1 to 5 and passed from 6 on. Typecheck passed throughout.</p>
        <div class="tf">with fast_iteration_checks on: tests, typecheck</div>
      </div>''',
      f'''      <div class="k-tile f3">
        <div class="k-label">What it has claimed</div>
        <p class="cl">1 of 2 stories done: ranking by BM25.</p>
        <div class="acts">
          {button('Open its log', ENTER)}
          {button('Tell the engineers…', CMD.format('G'))}
        </div>
        <div class="tf">Guidance reaches its next try, not this one.</div>
      </div>''')),

  'search-highlight': dict(
    title='<span class="nm">search-highlight</span>', meta='stopped on try 2 of 4 · no progress · ≥$3.22',
    desc='Marks the matching words in each result’s title and body.',
    ctx=[f'{mk("landed")}needs search-schema, merged', f'{mk("skip")}needed by search-cli, now skipped'],
    rows=[('try 1', [('done', '', '6 iterations', '28m'), ('ok', 'pass', '24 passed', '33s'), ('bad', 'fail', '3 blocking', '58s'), None, None, None, None]),
          ('try 2', [('bad sel', 'fail', 'stopped', 'no progress in 4, 5, 6'), None, None, None, None, None, None]),
          ('try 3', 'not run · the breaker fails a part at once rather than spend its last two tries on the same prompt')],
    returns=[(3, 0, 'sent back with its 3 blocking findings')],
    step=steps(
      ink('<span>engineer · try 2 · stopped at 21:03</span><span class="grow"></span><span>the no-progress breaker</span>',
          'why it stopped', 'Iterations 4, 5 and 6 left the code and the test results unchanged.',
          'events.jsonl · manifest.json'),
      '''      <div class="k-tile f2">
        <div class="sev2">its last words <span>engineer.log</span></div>
        <p>Overlapping matches (“sea” inside “search”) still render as two spans.</p>
        <div class="tf">it did not find a fix inside this part’s scope</div>
      </div>''',
      f'''      <div class="k-tile f3">
        <div class="k-label">What you can do</div>
        <p class="cl">Retry it, with guidance first if the same prompt would stop the same way.</p>
        <div class="acts">
          {button('Retry search-highlight', '', ' aria-disabled="true"')}
          {button('Tell the engineers…', CMD.format('G'))}
        </div>
        <div class="tf">Retry opens when this run ends: it needs the factory lock the run holds.</div>
      </div>''')),

  'search-api': dict(
    title='<span class="nm">search-api</span>', meta='waiting · needs 3 parts · not started',
    desc='Answers GET /search?q= with the matches as JSON.',
    ctx=[f'{mk("wait")}needs search-index, search-query and search-rank', f'{mk("skip")}needed by search-cli, now skipped'],
    rows=[('try 1', 'not started · it starts once search-index, search-query and search-rank have merged')],
    returns=[],
    step=steps(
      ink('<span>waiting · not started</span><span class="grow"></span><span>3 parts to merge first</span>', 'what it waits for',
          'The nearest of the three is search-index, and it waits for you.', 'manifest.json · dependencies'),
      f'''      <div class="k-tile k-tile-ask f2">
        <div class="dh">{mk("you")}<div class="sev2">search-index <span>your approval · 12m</span></div></div>
        <p>Every check agreed. It waits for your decision.</p>
        <div class="acts">{button('Open search-index')}</div>
      </div>''',
      f'''      <div class="k-tile f3">
        <div class="k-label">Still being built</div>
        <p class="cl">search-query is in review on try 3; search-rank is on iteration 9.</p>
        <div class="acts">
          {button('Open search-query')}
          {button('Open search-rank')}
        </div>
      </div>''')),

  'search-cli': dict(
    title='<span class="nm">search-cli</span>', meta='skipped · search-highlight stopped',
    desc='Prints the best matches in the terminal, with their titles, the matching words highlighted.',
    ctx=[f'{mk("fail")}needs search-highlight, stopped', f'{mk("wait")}needs search-api, waiting', 'decision 3: the ten best'],
    rows=[('try 1', 'skipped · it needs search-highlight, which stopped at 21:03')],
    returns=[],
    step=steps(
      ink('<span>skipped</span><span class="grow"></span><span>nothing ran</span>', 'why',
          'search-highlight stopped, and search-cli needs it. Retrying search-highlight resets search-cli too.',
          'component_skipped · events.jsonl'),
      f'''      <div class="k-tile f2">
        <div class="sev2">from the plan <span>decision 3</span></div>
        <p>The ten best matches: ten fit on one terminal screen with their titles.</p>
        <div class="acts">{button('Open decision 3 in the plan')}</div>
      </div>''',
      f'''      <div class="k-tile f3">
        <div class="k-label">What it needs</div>
        <p class="cl">search-highlight to be retried and merge, and search-api to be built.</p>
        <div class="acts">
          {button('Open search-highlight')}
          {button('Open search-api')}
        </div>
      </div>''')),
}
EXTRA_CSS = """
  .f2 .acts { margin-top:auto; padding-top:10px; }
  .dh { display:flex; align-items:center; gap:6px; }
"""

if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    for part, p in PARTS.items():
        card = f'<!-- @dsCard group="Prototype" height=800 width=1280 page subtitle="Part level: {part}, at 21:40" -->'
        html = part_page(card, f'Part level: {part}', part, head(p['title'], p['meta'], p['desc'], p['ctx']),
                         p['rows'], p['returns'], p['step'], EXTRA_CSS)
        (OUT / f'ProtoPart-{part}.html').write_text(html)
    print(f'{len(PARTS)} part screens')
