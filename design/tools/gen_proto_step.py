"""The Step level at the prototype's moment (search being built, 21:40): Stage and Grid, and one agent, search-rank's
engineer. Map3Step, Map3StepGrid and Map3Agent draw the same level at 20:46, when four agents were working; at 21:40
two are (search-query's reviewer on try 3, search-rank's engineer on iteration 9), search-index waits for you and
search-highlight has stopped.

The layout and CSS are the cards' own (gen_stage.py, agent_level.py); only what is on the screen differs. Facts are
the ones other screens at 21:40 state (parts.py, the Part screens): totals are their sums (52 iterations: 5 + 10 +
16 + 9 + 12; 3 tries sent back; ≥$21.84 counted). What no other screen states is this prototype's example: the
reviewer's words on try 3, search-rank's log and its iteration durations.

Prototype frames, not cards: written to out/static and audited from there (audit.py ProtoStage, ProtoGrid,
ProtoAgent-search-rank)."""
from __future__ import annotations

from pathlib import Path

from agent_level import CSS as AGENT_CSS, agent_body
from gen_stage import grid_css, stage_css
from level_common import HINT, NEEDS, checks, page, ring, top

OUT = Path('out/static')
CRUMBS = ['search', 'being built']


def TOP(on: int, hint: str = '') -> str:
    return '    ' + top('<span class="v-human">search</span>', '2 agents working · 21:40', 3, ['Stage', 'Grid'], on, hint)


def mk(m: str) -> str:
    return f'<span class="k-mk sm {m}"></span>'


STAGE_NEEDS = NEEDS.replace(HINT, '<span class="k-keys k-keys-inline"><span class="k-key">↵</span></span> on an agent opens everything it has written · <span class="k-keys k-keys-inline"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">K</span></span> to ask why, what it cost, what runs next')

# ------------------------------------------------------------------ Stage: the reviewer spoke last
stage_body = TOP(0, 'follows whoever spoke last · <span class="k-keys"><span class="k-key">P</span></span> pins') + f'''
    <div class="k-tile k-tile-hero stage"><div class="stage-grid">
      <div class="spk">
        <div class="who"><span class="k-chip k-chip-checker">reviewer</span><span class="p">search-query</span><span class="t3">try 3 of 4 · checking</span><span class="now"><span class="k-typing" aria-hidden="true"><i></i><i></i><i></i></span>speaking</span></div>
        <div class="hist"><p>Verify passed: 31 tests.</p><p>Stories 1 and 2 look met, as they did on try 2.</p></div>
        <p class="say">The benchmark now runs over 10,000 snippets: 41 ms, under the 100 ms criterion 3 asks for.</p>
      </div>
      <div class="ctx">
        <div class="lbl">Checking search-query</div>
        {checks(['done', 'now', '', ''])}
        <div class="lbl" style="margin-top:22px">Its stories</div>
        <div class="sl"><span>Match words in a title or a body</span><em>being checked</em></div>
        <div class="sl"><span>Quoted phrases match exactly</span><em>being checked</em></div>
        <div class="sl"><span>Answer in under 100 ms for 10,000 snippets</span><em>being checked</em></div>
        <div class="foot">try 3 built in 4 iterations over 24m · from the plan: decision 1</div>
      </div>
    </div></div>
    <div class="tiles">
      <div class="k-tile"><div class="th"><span class="k-chip">engineer</span><span class="p">search-rank</span><span class="ag">6s</span></div>
        <div class="tb">{ring(8, 10)}<div class="num"><b>9</b><span>of 10 iterations</span></div></div>
        <p>Titles now count twice as much as bodies in bm25().</p><div class="tf"><span class="k-typing" aria-hidden="true"><i></i><i></i><i></i></span></div></div>
      <div class="k-tile k-tile-idle rest"><div class="th"><span class="p">Not being worked on</span></div>
        <div class="ilg">
          <div class="il">{mk("landed")}search-schema<em>merged</em></div>
          <div class="il">{mk("you")}search-index<em>waits for you</em></div>
          <div class="il">{mk("fail")}search-highlight<em>stopped</em></div>
          <div class="il">{mk("wait")}search-api<em>needs 3</em></div>
          <div class="il">{mk("skip")}search-cli<em>skipped</em></div>
        </div>
        <div class="sofar"><b>52</b> iterations · <b>3</b> sent back · <b>≥$21.84</b></div></div>
    </div>'''
STAGE_CSS = stage_css + """
  .rest { grid-column:2 / span 3; }
  .ilg { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); column-gap:28px; }
"""

# ------------------------------------------------------------------ Grid: the run at a glance
dag = '''<svg class="dag" viewBox="0 0 480 190" width="480" height="190" aria-hidden="true">
  <path d="M60 95 C110 95 110 26 160 26 M60 95 C110 95 110 72 160 72 M60 95 C110 95 110 118 160 118 M60 95 C110 95 110 164 160 164" class="e ok"/>
  <path d="M300 26 C350 26 350 95 392 95 M300 72 C350 72 350 95 392 95 M300 118 C350 118 350 95 392 95" class="e"/>
  <path d="M300 164 C380 164 420 130 450 104" class="e bad"/><path d="M404 95 H438" class="e"/>
  <circle cx="52" cy="95" r="8" class="n m"/>
  <rect x="160" y="14" width="140" height="24" rx="12" class="n y"/><text x="174" y="30" class="nt y">search-index</text>
  <rect x="160" y="60" width="140" height="24" rx="12" class="n r"/><text x="174" y="76" class="nt r">search-query</text>
  <rect x="160" y="106" width="140" height="24" rx="12" class="n w"/><text x="174" y="122" class="nt">search-rank</text>
  <rect x="160" y="152" width="140" height="24" rx="12" class="n f"/><text x="174" y="168" class="nt f">search-highlight</text>
  <circle cx="398" cy="95" r="7" class="n p"/><circle cx="446" cy="98" r="7" class="n p"/>
  <text x="40" y="122" class="lt">schema</text><text x="386" y="122" class="lt">api</text><text x="440" y="125" class="lt">cli</text>
</svg>'''
grid_body = TOP(1) + f'''
    <div class="bento">
      <div class="k-tile k-tile-hero b hero">
        <div class="hh"><div><div class="big"><b>2</b> agents working</div><div class="sub">on 2 of the 7 parts of search · 1 merged</div></div><div class="live2"><span class="k-typing" aria-hidden="true"><i></i><i></i><i></i></span>1 speaking</div></div>
        <div class="dagw">{dag}</div>
        <div class="stats"><div><b>52</b><span>iterations</span></div><div><b>3</b><span>sent back</span></div><div><b>1</b><span>stopped</span></div><div><b>≥$21.84</b><span>counted</span></div></div>
      </div>
      <div class="k-tile b ag"><div class="th"><span class="k-chip">engineer</span><span class="ag2">6s</span></div><div class="p">search-rank</div>
        <div class="tb">{ring(8, 10, 56, 6)}<div class="num"><b>9</b><span>/10</span></div></div><p>Titles now count twice as much as bodies in bm25().</p></div>
      <div class="k-tile b ag"><div class="th"><span class="k-chip k-chip-checker">reviewer</span><span class="ag2">2s</span></div><div class="p">search-query</div>
        <div class="ckw">{checks(['done', 'now', '', ''])}</div><p>The benchmark now runs over 10,000 snippets.</p></div>
      <div class="k-tile k-tile-ask b ag"><div class="th"><span class="sw">{mk("you")}your approval</span><span class="ag2">12m</span></div><div class="p">search-index</div>
        <p>Every check agreed. It waits for you.</p></div>
      <div class="k-tile k-tile-alert b ag"><div class="th"><span class="sw">{mk("fail")}stopped</span><span class="ag2">21:03</span></div><div class="p">search-highlight</div>
        <p>Iterations 4, 5 and 6 changed nothing.</p></div>
      <div class="k-tile k-tile-ink b quote"><div class="qh"><span class="k-chip k-chip-checker">reviewer</span>search-query · just now</div><p>“The benchmark now runs over 10,000 snippets: 41 ms, under the 100 ms criterion 3 asks for.”</p><div class="qf">try 3 · review · started 1m ago</div></div>
      <div class="k-tile k-tile-idle b wait"><div class="p">Not being worked on</div><div class="il">{mk("landed")}search-schema<em>merged</em></div><div class="il">{mk("wait")}search-api<em>needs 3</em></div><div class="il">{mk("skip")}search-cli<em>skipped</em></div></div>
    </div>'''
GRID_CSS = grid_css + """
  .dag .n.y { fill:var(--you-tint); stroke:var(--you); stroke-width:1.5; } .dag .nt.y { fill:var(--you); }
  .dag .n.f { fill:var(--window); stroke:var(--fail); stroke-width:1.5; } .dag .nt.f { fill:var(--fail); }
  .dag .e.bad { stroke:var(--fail); }
  .th .sw { display:inline-flex; align-items:center; gap:6px; font:var(--t-label); font-weight:600; }
"""

# ------------------------------------------------------------------ one agent: search-rank's engineer
LOG = [
  ('t', 'US-1 is done: matches come back ordered by bm25(), best first.'),
  ('k', 'Read', 'rank.py'), ('k', 'Read', 'test_rank.py'), ('k', 'Grep', 'bm25('),
  ('t', 'A snippet that repeats a word in its body still outranks one with the word in its title. US-2 asks for the title match first.'),
  ('k', 'Edit', 'rank.py'), ('k', 'Bash', 'uv run pytest tests/test_rank.py -q'),
  ('t', 'bm25() takes a weight per column. A title weight of 2 against the body’s 1 puts the title match first in all four cases.'),
  ('k', 'Edit', 'rank.py'), ('k', 'Edit', 'test_rank.py'), ('k', 'Bash', 'uv run pytest -q'),
]
# 8 finished iterations, 81m 33s from 20:12; tests failed after 1 to 5 and passed from 6 on, as the Part screen says
ITS = [(1, '11m 02s', ['f', 'p']), (2, '9m 40s', ['f', 'p']), (3, '10m 15s', ['f', 'p']), (4, '8m 51s', ['f', 'p']),
       (5, '12m 30s', ['f', 'p']), (6, '9m 05s', ['p', 'p']), (7, '10m 48s', ['p', 'p']), (8, '9m 22s', ['p', 'p'])]
agent_body_html = agent_body('<span class="nm">search-rank</span><span class="k-chip">engineer</span>',
                             'try 1 of 4 · iteration 9 of up to 10 · output 6s ago', 'try 1, from 20:12', LOG,
                             'Titles now count twice as much as bodies in bm25(). The ranking tests pass; running the whole suite.',
                             ITS, 8, 9, '9 running · 6m 27s',
                             [('US-1', 'Rank the matches by BM25, best first', 'done'), ('US-2', 'A title match ranks above a body match', 'not yet')],
                             'Read when the next try starts. Try 1 will not see it.')
AGENT_NEEDS = NEEDS.replace(HINT, 'Notes is its progress.txt · Prompt is exactly what it was sent, per call')

if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    card = '<!-- @dsCard group="Prototype" height=800 width=1280 page subtitle="{}" -->'
    (OUT / 'ProtoStage.html').write_text(page(card.format('Step level, Stage, at 21:40'), 'Step level: Stage', CRUMBS, 3, stage_body, STAGE_CSS, STAGE_NEEDS))
    (OUT / 'ProtoGrid.html').write_text(page(card.format('Step level, Grid, at 21:40'), 'Step level: Grid', CRUMBS, 3, grid_body, GRID_CSS, STAGE_NEEDS))
    (OUT / 'ProtoAgent-search-rank.html').write_text(page(card.format('Step level, one agent: search-rank, at 21:40'), 'Step level: one agent',
                                                          CRUMBS + ['search-rank'], 3, agent_body_html, AGENT_CSS, AGENT_NEEDS))
    print('3 step screens')
