"""Spend: today against the daily budget, what each spec cost this week, the limits and what reaching each does, and how sure the numbers are. Wed 22:40."""
from __future__ import annotations
from pathlib import Path
from level_common import page, top

CAP, TODAY = 40.0, 42.46
W = 520
def px(v: float) -> float: return v / 48 * W      # axis to $48 so the overshoot shows
segs = [('export, its last run, 00:12', 9.26), ('search, 5 runs, 19:40 to 22:31', 33.20)]
x0 = 0.0; bar = []
for i, (lab, v) in enumerate(segs):
    w = px(v) - (2 if i < len(segs) - 1 else 0)
    bar.append(f'<rect x="{x0:.1f}" y="30" width="{w:.1f}" height="14" rx="4" class="{"s1" if i == 0 else "s2"}"/>')
    x0 += px(v)
cx = px(CAP)
svg = (f'<svg class="tb" viewBox="0 0 {W} 64" width="{W}" height="64" aria-hidden="true">'
       f'<rect x="0" y="30" width="{W}" height="14" rx="4" class="trk"/>' + ''.join(bar) +
       f'<line x1="{cx:.1f}" y1="20" x2="{cx:.1f}" y2="54" class="cap"/><text x="{cx:.1f}" y="14" text-anchor="middle" class="capl">$40.00 daily budget</text>'
       f'<text x="0" y="62" class="ax">$0</text><text x="{px(9.26)+4:.1f}" y="62" class="ax2">search started here, at $9.26</text></svg>')
week = [('snippets', 41.20, 'Mon'), ('search', 33.20, 'Wed'), ('sharing', 17.40, 'Tue'), ('export', 11.90, 'Tue to Wed'), ('markdown-export', 8.80, 'Tue, poisoned')]
wk = ''.join(f'<div class="wr"><span class="wn">{n}</span><span class="wb"><i class="k-bar" style="--k-bar:{v/41.2*100:.1f}%"></i></span><b>${v:.2f}</b><em>{d}</em></div>' for n, v, d in week)
limits = [
  ('Daily budget', '[serve] daily_budget_usd', '$40.00', 'Checked before each spec starts. At the cap the queue pauses until midnight.'),
  ('Per run', '[factory] max_cost_usd', 'not set', 'Checked between phases. At the cap the part fails and the rest are not started.'),
  ('Checker calls per run', 'max_adversarial_calls', 'not set', 'Review, security and distill. At the cap a hard review refuses and the part fails.'),
  ('Per part', '', 'none', 'kstrl has no per-part limit.'),
]
lm = ''.join(f'<div class="lr"><div><b>{a}</b><span class="k">{k}</span></div><span class="v {"off" if v in ("not set", "none") else ""}">{v}</span><p>{d}</p></div>' for a, k, v, d in limits)
body = '    ' + top('<span class="nm">Spend</span>', 'Wednesday · over the daily budget · the queue is paused until 00:00', None, ['Today', 'This week'], 0) + f'''
    <div class="sg">
      <div class="k-tile td k-tile-hero">
        <div class="k-label">Spent today</div>
        <div class="hero"><span class="bign2">≥$42.46</span><span class="of">of $40.00</span></div>
        {svg}
        <div class="lg2"><span><svg class="sw" width="12" height="8" aria-hidden="true"><rect width="12" height="8" rx="3" class="s1"/></svg>export, its last run, 00:12 · $9.26</span><span><svg class="sw" width="12" height="8" aria-hidden="true"><rect width="12" height="8" rx="3" class="s2"/></svg>search, 5 runs · $33.20</span></div>
        <p class="ex">The budget is checked before a spec starts, not while it runs. search started under it and finished $2.46 over, so ks serve paused the queue until midnight.</p>
        <p class="ex3">Resuming the queue by hand changes nothing: ks serve checks the budget again within 60 s. Raising daily_budget_usd does.</p>
      </div>
      <div class="k-tile lim">
        <div class="k-label">Limits, and what reaching each one does</div>
        {lm}
        <div class="lf"><button class="k-button">Open kstrl.toml</button><span class="t3 ty-small">No limit is on unless you set it.</span></div>
      </div>
      <div class="k-tile wk">
        <div class="k-label">What each spec cost this week</div>
        <div class="wrs">{wk}</div>
        <p class="ex2">markdown-export spent $8.80 over 3 attempts and delivered nothing.</p>
      </div>
      <div class="k-tile cov">
        <div class="k-label">How sure these numbers are</div>
        <div class="hero"><span class="bign2 sm">412<small>/418</small></span></div>
        <p>calls today reported what they cost. The other 6 did not, so every total is a lower bound, written with <span class="v-measure">≥</span>.</p>
        <p class="t3n">ks serve refuses to spend when no call has reported a cost, unless you allow it.</p>
      </div>
    </div>'''
FOOT = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <button class="k-need"><span class="k-mk fail"></span><b>Daily budget reached</b><span class="k-need-sub">the queue resumes at 00:00 · tags is next</span></button>
    
    <span class="k-needs-hint">Spend is counted when a phase ends, so a running part’s cost appears in steps.</span>
  </div>'''
css = """
  .sg { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); grid-template-rows:minmax(0,1fr) 250px; gap:14px; }
  .td { grid-column:1 / span 2; }
  .hero { display:flex; align-items:baseline; gap:10px; margin-top:8px; }
  .bign2 { font:var(--t-stat-lg); letter-spacing:var(--t-stat-lg-ls); }
  .bign2.sm { font:var(--t-stat); letter-spacing:var(--t-stat-ls); }
  .bign2 small { font:var(--t-measure-code); color:var(--text-3); margin-left:2px; letter-spacing:var(--t-measure-code-ls); }
  .of { font:var(--t-measure-code); color:var(--text-3); }
  .tb { display:block; margin-top:14px; overflow:visible; }
  .tb .trk { fill:var(--selected); }
  .tb .s1 { fill:var(--text-3); } .tb .s2 { fill:var(--text); }
  .tb .cap { stroke:var(--fail); stroke-width:2; }
  .tb .capl { fill:var(--text-2); font:var(--t-micro); font-weight:400; }
  .tb .ax, .tb .ax2 { fill:var(--text-3); font:var(--t-measure-small); font-weight:400; }
  .tb .ax2 { font:var(--t-micro); font-weight:400; }
  .lg2 { display:flex; gap:18px; margin-top:12px; font:var(--t-label); font-weight:400; color:var(--text-2); }
  .lg2 span { display:inline-flex; align-items:center; gap:6px; }
  /* the legend keys the chart above it, so its swatches are drawn as the chart is, from the same fills */
  .sw { flex:none; } .sw .s1 { fill:var(--text-3); } .sw .s2 { fill:var(--text); }
  .ex3 { margin:6px 0 0; font:var(--t-small); color:var(--text-3); max-width:60ch; }
  .ex { margin:auto 0 0; font:var(--t-body); color:var(--text-2); max-width:56ch; }
  .lim { grid-column:3 / span 2; }
  .lr { display:grid; grid-template-columns:1fr auto; column-gap:12px; padding:9px 0; border-bottom:1px solid var(--line); align-items:baseline; }
  .lr b { font:var(--t-body); font-weight:600; margin-right:8px; }
  .lr .k { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .lr .v { font:var(--t-measure); font-weight:600; }
  .lr .v.off { color:var(--text-3); font:var(--t-small); }
  .lr p { grid-column:1 / span 2; margin:3px 0 0; font:var(--t-small); color:var(--text-2); }
  .lf { display:flex; align-items:center; gap:12px; margin-top:auto; padding-top:10px; }

  .wk { grid-column:1 / span 3; }
  .wrs { margin-top:10px; display:flex; flex-direction:column; gap:9px; }
  .wr { display:grid; grid-template-columns:130px 1fr 64px 110px; align-items:center; column-gap:12px; font:var(--t-small); }
  .wn { font:var(--t-intent-sm); }
  .wb { height:8px; }
  .wr b { font:var(--t-measure); font-weight:600; text-align:right; }
  .wr em { font-style:normal; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .ex2 { margin:auto 0 0; font:var(--t-small); color:var(--text-2); }

  .cov p { margin:6px 0 0; font:var(--t-small); color:var(--text-2); }
  .t3n { color:var(--text-3) !important; margin-top:auto !important; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="12 · Spend: today against the budget, the limits, how sure the numbers are" -->',
           'Spend', ['Spend'], 0, body, css, FOOT)
out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">not live · last event 22:31</span>').replace('≥$31.10 <small>of $40.00 today</small>', '≥$42.46 <small>of $40.00 today</small>')
d = Path('../system/project/components/Spend'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
