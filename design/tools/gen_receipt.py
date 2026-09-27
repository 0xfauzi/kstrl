"""Receipt: search after it finished. What you asked for, what was built, the one thing to know, how much it needed you, what it cost."""
from __future__ import annotations
from pathlib import Path
from level_common import page, top
from comp import steps
CI = {'p': ('pass', 'CI passed'), 'r': ('work', 'CI running')}

parts = [
  ('search-schema', 'Stores each snippet’s searchable text in its own table.', '#48', '1 try', 'p', ''),
  ('search-index', 'Keeps a full-text index of titles and bodies, updated on every save and delete.', '#49', '1 try', 'p', 'if'),
  ('search-query', 'Runs a search against the index and returns the matches with their scores.', '#50', '3 tries', 'p', 'if'),
  ('search-rank', 'Orders the matches by BM25, SQLite FTS5’s built-in ranking.', '#51', '1 try', 'p', ''),
  ('search-highlight', 'Marks where the words matched in each result.', '#52', '3 tries', 'p', ''),
  ('search-api', 'Answers GET /search?q= with the matches as JSON.', '#53', '1 try', 'p', ''),
  ('search-cli', 'Prints the ten best matches with their titles, the words highlighted.', '#54', '1 try', 'r', ''),
]
# the name and its facts (tries, PR, CI, a finding) are one line; the description runs under them
rows = ''.join(f'''<div class="pr"><span class="k-mk sm landed"></span><b class="pn">{n}</b><span class="tries">{t}</span><span class="prn">{pr}</span><span class="k-mk sm {CI[c][0]} ci" role="img" aria-label="{CI[c][1]}"></span>{'<span class="k-ref k-ref-ink flag">IF-1</span>' if f else '<span></span>'}<span class="pds">{d}</span></div>''' for n, d, pr, t, c, f in parts)
# the five factory runs, 19:40 (0) to 22:31 (171); four started by an approval
runs = [(0, 26, False), (30, 128, True), (132, 156, True), (158, 166, True), (167, 171, True)]
W = 404
def x(m: float) -> float: return m / 171 * W
strip = [f'<svg class="rs" viewBox="0 0 {W} 30" width="{W}" height="30" aria-hidden="true"><line x1="0" y1="21" x2="{W}" y2="21" class="base"/>']
for a, b, you in runs:
    strip.append(f'<rect x="{x(a):.1f}" y="15" width="{max(x(b)-x(a), 3):.1f}" height="12" rx="4" class="run"/>')
    if you:
        strip.append(f'<path d="M{x(a):.1f} 2 L{x(a)+5:.1f} 7 L{x(a):.1f} 12 L{x(a)-5:.1f} 7Z" class="ym"/>')
strip.append('</svg>')
body = '    ' + top('<span class="v-human">search</span>', 'finished 22:31 · 7 of 7 merged · ≥$33.20', 1, ['Graph', 'Text', 'Receipt'], 2) + f'''
    <div class="rc">
      <div class="k-tile got k-tile-hero">
        <div class="k-label">You asked for</div>
        <p class="asked">“People can find a snippet by the words in its title or its body.”</p>
        <div class="more">and 4 more sentences, with 3 decisions the architect made · <span class="lnk">Text view</span></div>
        <div class="k-label" style="margin-top:22px">What was built, in the order it merged</div>
        <div class="prs">{rows}</div>
      </div>
      <div class="side">
        <div class="k-tile k-tile-ink k-ink one k-tile-hero">
          <div class="k-label">One thing to know · integration review, 22:28</div>
          <p class="say">Accents are folded two ways. <span class="v-token">search-index</span> stores “Ærø” as one word and <span class="v-token">search-query</span> searches for another.</p>
          <div class="of"><span>Recorded only: blocking is off, so nothing was built from it.</span><button class="k-button k-button-inverse k-button-sm">Write a follow-up spec</button></div>
        </div>
        <div class="two2">
          <div class="k-tile tmt">
            <div class="k-label">It needed you 4 times</div>
            <div class="big2"><b>2h 51m</b><span>start to last merge</span></div>
            {''.join(strip)}
            <div class="rsl"><span>19:40</span><span>22:31</span></div>
            <p>8 answers in 4 visits: 7 approvals and 1 retry. Each visit started the next run. The longest wait: <span class="nw">search-highlight</span>, stopped for 47m.</p>
          </div>
        </div>
        <div class="two2">
          <div class="k-tile sp">
            <div class="k-label">Cost</div>
            <div class="big2"><b>≥$33.20</b></div>
            <div class="bars"><div><span>engineers</span><i class="k-bar" style="--k-bar:100%"></i><em>$24.10</em></div><div><span>checkers</span><i class="k-bar" style="--k-bar:29.9%"></i><em>$7.20</em></div><div><span>the rest</span><i class="k-bar" style="--k-bar:7.9%"></i><em>$1.90</em></div></div>
          </div>
          <div class="k-tile cit">
            <div class="k-label">CI on what merged</div>
            <div class="big2"><b>6<small>/7</small></b><span>passed</span></div>
            <div class="sq">{steps(['done'] * 6 + ['now'])}</div>
            <p>#54 still running</p>
          </div>
        </div>
      </div>
    </div>'''
FOOT = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <span class="k-needs-none">Nothing from search</span>
    
    <span class="k-needs-hint">The day finished over its budget, so ks serve paused the queue until midnight.</span>
  </div>'''
css = """
  .rc { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 452px; gap:14px; }

  .asked { margin:8px 0 0; font:var(--t-intent-xl); letter-spacing:var(--t-intent-xl-ls); max-width:30em; }
  .more { font:var(--t-small); color:var(--text-3); margin-top:6px; }
  .lnk { color:var(--text); text-decoration:underline; text-decoration-color:var(--line-strong); text-underline-offset:3px; }
  .prs { margin-top:6px; }
  .pr { display:grid; grid-template-columns:18px minmax(0,1fr) 54px 34px 12px 40px; column-gap:10px; align-items:baseline; padding:8px 0; border-bottom:1px solid var(--line); }
  .pr > .k-mk:first-child { grid-row:1 / 3; align-self:center; } .pr > .ci, .pr > .flag, .pr > span:empty { align-self:center; }
  .pr:last-child { border-bottom:0; }
  .pn { font:var(--t-body); font-weight:600; }
  .pds { grid-column:2 / -1; font:var(--t-small); color:var(--text-2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tries, .prn { font:var(--t-measure-small); font-weight:400; color:var(--text-3); text-align:right; }
  .prn { color:var(--text-2); }
  .flag { justify-self:end; }
  .side { display:flex; flex-direction:column; gap:14px; min-height:0; }

  .one .say { margin:10px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); }
  .of { display:flex; align-items:center; gap:12px; margin-top:14px; font:var(--t-small); opacity:.8; }
  .of span:first-child { flex:1; }
  .k-button-inverse { flex:none; }
  .two2 { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(0,1fr); gap:14px; }

  .big2 { display:flex; align-items:baseline; gap:8px; margin-top:6px; }
  .big2 b { font:var(--t-stat-sm); letter-spacing:var(--t-stat-sm-ls); }
  .big2 b small { font:var(--t-measure); font-weight:500; letter-spacing:var(--t-measure-ls); color:var(--text-3); margin-left:1px; }
  .big2 span { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .rs { display:block; margin-top:10px; }
  .nw { white-space:nowrap; }
  .rs .base { stroke:var(--line); stroke-width:1; }
  .rs .run { fill:var(--selected); stroke:var(--line-strong); stroke-width:1; }
  .rs .ym { fill:var(--you); }
  .rsl { display:flex; justify-content:space-between; font:var(--t-measure-small); font-weight:400; color:var(--text-3); margin-top:2px; }
  .tmt p, .cit p { margin:8px 0 0; font:var(--t-small); color:var(--text-2); }
  .bars { margin-top:10px; display:flex; flex-direction:column; gap:6px; }
  .bars div { display:grid; grid-template-columns:64px 1fr 48px; align-items:center; gap:8px; font:var(--t-label); font-weight:400; color:var(--text-2); }
  .bars em { font:var(--t-measure-small); font-weight:400; color:var(--text); text-align:right; }
  .sq { margin-top:12px; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="10 · Receipt: what a finished spec delivered, and what to know about it" -->',
           'Receipt: search', ['search'], 1, body, css, FOOT)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">not live · last event 22:31</span>').replace('≥$31.10 <small>of $40.00 today</small>', '≥$42.46 <small>of $40.00 today</small>')
d = Path('../system/project/components/Receipt'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
