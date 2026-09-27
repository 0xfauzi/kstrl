"""Step level, two modes of the same moment (20:46, four agents): Stage and Grid."""
from __future__ import annotations
from pathlib import Path
from level_common import page, ring, checks, top

def TOP(on: int, hint: str = '') -> str:
    return '    ' + top('<span class="v-human">search</span>', '4 agents working · 20:46', 3, ['Stage', 'Grid'], on, hint)
NEEDS = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <span class="k-needs-none">Nothing right now</span>
    
    <span class="k-needs-hint"><span class="k-keys k-keys-inline"><span class="k-key">↵</span></span> on an agent opens everything it has written · <span class="k-keys k-keys-inline"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">K</span></span> ask</span>
  </div>'''
BASE_CSS = """
"""

# ------------------------------------------------------------------ Stage
stage_body = TOP(0, 'follows whoever spoke last · <span class="k-keys"><span class="k-key">P</span></span> pins') + f'''
    <div class="k-tile k-tile-hero stage"><div class="stage-grid">
      <div class="spk">
        <div class="who"><span class="k-chip k-chip-checker">reviewer</span><span class="p">search-query</span><span class="t3">try 1 of 4 · checking</span><span class="now"><span class="k-typing" aria-hidden="true"><i></i><i></i><i></i></span>speaking</span></div>
        <div class="hist"><p>Verify passed: 28 tests.</p><p>Stories 1 and 2 look met. The tests cover title and body matches and quoted phrases.</p></div>
        <p class="say">Criterion 3 asks for 100 ms over 10,000 snippets. The only benchmark in the tests uses 100.</p>
      </div>
      <div class="ctx">
        <div class="lbl">Checking search-query</div>
        {checks(['done', 'now', '', ''])}
        <div class="lbl" style="margin-top:22px">Its stories</div>
        <div class="sl"><span>Match words in a title or a body</span><em>being checked</em></div>
        <div class="sl"><span>Quoted phrases match exactly</span><em>being checked</em></div>
        <div class="sl"><span>Answer in under 100 ms for 10,000 snippets</span><em>being checked</em></div>
        <div class="foot">built in 7 iterations over 34m · from the plan: decision 1</div>
      </div>
    </div></div>
    <div class="tiles">
      <div class="k-tile"><div class="th"><span class="k-chip">engineer</span><span class="p">search-index</span><span class="ag">6s</span></div>
        <div class="tb">{ring(7, 10)}<div class="num"><b>8</b><span>of 10 iterations</span></div></div>
        <p>Deletes now remove the index row in the same transaction.</p><div class="tf"><span class="k-typing" aria-hidden="true"><i></i><i></i><i></i></span></div></div>
      <div class="k-tile"><div class="th"><span class="k-chip">engineer</span><span class="p">search-rank</span><span class="ag">11s</span></div>
        <div class="tb">{ring(3, 10)}<div class="num"><b>4</b><span>of 10 iterations</span></div></div>
        <p>Body repeats still outrank titles. Trying a length-normalised score.</p></div>
      <div class="k-tile"><div class="th"><span class="k-chip">engineer</span><span class="p">search-highlight</span><span class="ag">38s</span></div>
        <div class="tb">{ring(0, 10)}<div class="num"><b>1</b><span>of 10 · try 2</span></div></div>
        <p>Working through review’s three findings from try 1.</p></div>
      <div class="k-tile k-tile-idle"><div class="th"><span class="p">Not being worked on</span></div>
        <div class="il"><span class="k-mk sm landed"></span>search-schema<em>merged</em></div>
        <div class="il"><span class="k-mk sm wait"></span>search-api<em>needs 3</em></div>
        <div class="il"><span class="k-mk sm wait"></span>search-cli<em>needs 2</em></div>
        <div class="sofar"><b>25</b> iterations · <b>1</b> sent back · <b>≥$9.60</b></div></div>
    </div>'''
stage_css = BASE_CSS + """
  .stage { position:absolute; left:32px; right:32px; top:70px; height:382px; }
  .stage-grid { flex:1; min-height:0; display:grid; grid-template-columns:minmax(0,1fr) 340px; column-gap:48px; }
  .spk { display:flex; flex-direction:column; min-width:0; }
  .who { display:flex; align-items:center; gap:10px; font:var(--t-small); }
  .who .p { font:var(--t-body); font-weight:600; }
  .who .now { margin-left:auto; display:inline-flex; align-items:center; gap:8px; font:var(--t-label); font-weight:600; color:var(--work); }
  .hist { margin-top:auto; }
  .hist p { margin:0 0 8px; font:var(--t-body); color:var(--text-3); max-width:44ch; }
  .hist p:first-child { font:var(--t-body); }
  .say { margin:8px 0 0; font:var(--t-statement-xl); letter-spacing:var(--t-statement-xl-ls); color:var(--text); max-width:24ch; }
  .ctx { border-left:1px solid var(--line); padding-left:32px; display:flex; flex-direction:column; }
  .lbl { font:var(--t-label); font-weight:400; color:var(--text-3); margin-bottom:10px; }
  .sl { display:flex; justify-content:space-between; gap:10px; font:var(--t-body); padding:7px 0; border-bottom:1px solid var(--line); align-items:baseline; }
  .sl em { font-style:normal; font:var(--t-label); font-weight:400; color:var(--text-2); white-space:nowrap; }
  .foot { margin-top:auto; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .tiles { position:absolute; left:32px; right:32px; top:466px; height:192px; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:14px; }
  .th { display:flex; align-items:center; gap:8px; }
  .th .p { font:var(--t-body); font-weight:600; }
  .th .ag { margin-left:auto; font:var(--t-measure-small); color:var(--text-3); }
  .tb { display:flex; align-items:center; gap:14px; margin-top:12px; }
  .num b { display:block; font:var(--t-stat); letter-spacing:var(--t-stat-ls); }
  .num span { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .tiles .k-tile p { margin:10px 0 0; font:var(--t-body); color:var(--text-2); }
  .tf { margin-top:auto; }
  .il { display:flex; align-items:center; gap:7px; font:var(--t-small); margin-top:9px; }
  .il em { font-style:normal; margin-left:auto; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .sofar { margin-top:auto; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .sofar b { font:var(--t-measure); font-weight:600; color:var(--text); }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="3a · Step level, Stage: the agent speaking takes the stage" -->',
           'Step level: Stage', ['search', 'being built'], 3, stage_body, stage_css, NEEDS)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">live · last event 2s ago</span>').replace('≥$31.10 <small>of $40.00 today</small>', '≥$19.40 <small>of $40.00 today</small>')
Path('../system/project/components/Map3Step/preview.html').write_text(out)

# ------------------------------------------------------------------ Grid
dag = '''<svg class="dag" viewBox="0 0 480 190" width="480" height="190" aria-hidden="true">
  <path d="M60 95 C110 95 110 26 160 26 M60 95 C110 95 110 72 160 72 M60 95 C110 95 110 118 160 118 M60 95 C110 95 110 164 160 164" class="e ok"/>
  <path d="M300 26 C350 26 350 95 392 95 M300 72 C350 72 350 95 392 95 M300 118 C350 118 350 95 392 95" class="e"/>
  <path d="M300 164 C380 164 420 130 450 104" class="e"/><path d="M404 95 H438" class="e"/>
  <circle cx="52" cy="95" r="8" class="n m"/>
  <rect x="160" y="14" width="140" height="24" rx="12" class="n w"/><text x="174" y="30" class="nt">search-index</text>
  <rect x="160" y="60" width="140" height="24" rx="12" class="n r"/><text x="174" y="76" class="nt r">search-query</text>
  <rect x="160" y="106" width="140" height="24" rx="12" class="n w"/><text x="174" y="122" class="nt">search-rank</text>
  <rect x="160" y="152" width="140" height="24" rx="12" class="n w"/><text x="174" y="168" class="nt">search-highlight</text>
  <circle cx="398" cy="95" r="7" class="n p"/><circle cx="446" cy="98" r="7" class="n p"/>
  <text x="40" y="122" class="lt">schema</text><text x="386" y="122" class="lt">api</text><text x="440" y="125" class="lt">cli</text>
</svg>'''
grid_body = TOP(1) + f'''
    <div class="bento">
      <div class="k-tile k-tile-hero b hero">
        <div class="hh"><div><div class="big"><b>4</b> agents working</div><div class="sub">on 4 of the 7 parts of search · 1 merged</div></div><div class="live2"><span class="k-typing" aria-hidden="true"><i></i><i></i><i></i></span>2 speaking</div></div>
        <div class="dagw">{dag}</div>
        <div class="stats"><div><b>25</b><span>iterations</span></div><div><b>4</b><span>checks</span></div><div><b>1</b><span>sent back</span></div><div><b>≥$9.60</b><span>counted</span></div></div>
      </div>
      <div class="k-tile b ag"><div class="th"><span class="k-chip">engineer</span><span class="ag2">6s</span></div><div class="p">search-index</div>
        <div class="tb">{ring(7, 10, 56, 6)}<div class="num"><b>8</b><span>/10</span></div></div><p>Deletes now remove the index row in the same transaction.</p></div>
      <div class="k-tile b ag"><div class="th"><span class="k-chip k-chip-checker">reviewer</span><span class="ag2">2s</span></div><div class="p">search-query</div>
        <div class="ckw">{checks(['done', 'now', '', ''])}</div><p>The only benchmark in the tests uses 100 snippets.</p></div>
      <div class="k-tile b ag"><div class="th"><span class="k-chip">engineer</span><span class="ag2">11s</span></div><div class="p">search-rank</div>
        <div class="tb">{ring(3, 10, 56, 6)}<div class="num"><b>4</b><span>/10</span></div></div><p>Trying a length-normalised body score.</p></div>
      <div class="k-tile b ag"><div class="th"><span class="k-chip">engineer</span><span class="ag2">38s</span></div><div class="p">search-highlight</div>
        <div class="tb">{ring(0, 10, 56, 6)}<div class="num"><b>1</b><span>/10 · try 2</span></div></div><p>Working through review’s three findings.</p></div>
      <div class="k-tile k-tile-ink b quote"><div class="qh"><span class="k-chip k-chip-checker">reviewer</span>search-query · just now</div><p>“Criterion 3 asks for 100 ms over 10,000 snippets. The only benchmark in the tests uses 100.”</p><div class="qf">From the plan, decision 1 for search-query: under 100 ms for 10,000 snippets.</div></div>
      <div class="k-tile k-tile-idle b wait"><div class="p">Not being worked on</div><div class="il"><span class="k-mk sm landed"></span>search-schema<em>merged</em></div><div class="il"><span class="k-mk sm wait"></span>search-api<em>needs 3</em></div><div class="il"><span class="k-mk sm wait"></span>search-cli<em>needs 2</em></div></div>
    </div>'''
grid_css = BASE_CSS + """
  .bento { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); grid-template-rows:1fr 1fr 170px; gap:14px; }
  .b { min-width:0; min-height:0; }
  .hero { grid-column:1 / span 2; grid-row:1 / span 2; }
  .hh { display:flex; justify-content:space-between; align-items:flex-start; }
  .big { font:var(--t-input); color:var(--text-2); }
  .big b { font:var(--t-stat-lg); letter-spacing:var(--t-stat-lg-ls); color:var(--text); margin-right:8px; }
  .sub { font:var(--t-small); color:var(--text-3); margin-top:6px; }
  .live2 { display:inline-flex; gap:8px; align-items:center; font:var(--t-label); font-weight:600; color:var(--work); }
  .dagw { flex:1; display:flex; align-items:center; justify-content:center; }
  .dag .e { fill:none; stroke:var(--line-strong); stroke-width:1.5; stroke-dasharray:3 4; }
  .dag .e.ok { stroke-dasharray:none; }
  .dag .n.m { fill:var(--pass); } .dag .n.p { fill:var(--window); stroke:var(--text-3); stroke-width:1.5; stroke-dasharray:2 2; }
  .dag .n.w { fill:var(--work-tint); stroke:var(--work); stroke-width:1.5; } .dag .n.r { fill:var(--text); }
  .dag .nt { fill:var(--work); font:var(--t-label); font-weight:600; } .dag .nt.r { fill:var(--window); }
  .dag .lt { fill:var(--text-3); font:var(--t-measure-small); font-weight:400; }
  .stats { display:grid; grid-template-columns:repeat(4, 1fr); border-top:1px solid var(--line); padding-top:14px; }
  .stats b { display:block; font:var(--t-stat-sm); letter-spacing:var(--t-stat-sm-ls); }
  .stats span { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .th { display:flex; align-items:center; justify-content:space-between; }
  .ag2 { font:var(--t-measure-small); color:var(--text-3); }
  .b .p { font:var(--t-body); font-weight:600; margin-top:10px; letter-spacing:var(--t-body-ls); }
  .tb { display:flex; align-items:center; gap:12px; margin-top:12px; }
  .num b { font:var(--t-stat); letter-spacing:var(--t-stat-ls); }
  .num span { font:var(--t-measure); font-weight:500; color:var(--text-3); margin-left:2px; }
  .ckw { margin-top:16px; }
  .b > p { margin:auto 0 0; font:var(--t-body); color:var(--text-2); }
  .quote { grid-column:1 / span 3; }
  .quote .qh { display:flex; align-items:center; gap:10px; font:var(--t-label); font-weight:400; color:var(--window); opacity:.8; }
  .quote p { margin:12px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); color:var(--window); max-width:46ch; }
  .quote .qf { margin-top:auto; font:var(--t-small); color:var(--window); opacity:.7; }
  .wait .p { margin-top:0; font:var(--t-body); font-weight:600; }
  .il { display:flex; align-items:center; gap:7px; font:var(--t-small); margin-top:10px; }
  .il em { font-style:normal; margin-left:auto; font:var(--t-label); font-weight:400; color:var(--text-3); }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="3b · Step level, Grid: the run at a glance, every agent a tile" -->',
           'Step level: Grid', ['search', 'being built'], 3, grid_body, grid_css, NEEDS)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">live · last event 2s ago</span>').replace('≥$31.10 <small>of $40.00 today</small>', '≥$19.40 <small>of $40.00 today</small>')
d = Path('../system/project/components/Map3StepGrid'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
