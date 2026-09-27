"""Health: is the factory working as it usually does? ks health's control charts, safe mode, and ks doctor. Wed 22:40."""
from __future__ import annotations
import statistics as st
from pathlib import Path
from level_common import page, top

def chart(vals: list[float], fmt, W: int = 356, H: int = 170) -> tuple[str, dict]:
    base = vals[:-3]                                   # ks health: baseline is every run but the last 3
    mean, sd = st.fmean(base), st.pstdev(base)   # health.py: fmean and pstdev of the baseline
    u2, u3 = mean + 2 * sd, mean + 3 * sd
    last3 = vals[-3:]
    two_of_three = sum(v > u2 for v in last3) >= 2
    one_point = any(v > u3 for v in last3)
    hi = max(max(vals), u3) * 1.12 or 1
    G = 58                                             # a right gutter: limit labels sit beside the plot, never over the latest points
    def X(i: int) -> float: return 6 + i * (W - G - 12) / (len(vals) - 1)
    def Y(v: float) -> float: return H - 16 - v / hi * (H - 26)
    o = [f'<svg class="cc" viewBox="0 0 {W} {H}" width="{W}" height="{H}" aria-hidden="true">']
    o.append(f'<line x1="0" y1="{H-16}" x2="{W}" y2="{H-16}" class="base"/>')
    ly = {'2 sigma': Y(u2), '3 sigma': Y(u3)}
    if ly['2 sigma'] - ly['3 sigma'] < 13:              # two labels closer than a line apart: spread them about their middle
        mid = (ly['2 sigma'] + ly['3 sigma']) / 2; ly['3 sigma'], ly['2 sigma'] = mid - 6.5, mid + 6.5
    for lv, lab in ((mean, 'mean'), (u2, '2 sigma'), (u3, '3 sigma')):
        o.append(f'<line x1="0" y1="{Y(lv):.1f}" x2="{W-G+2}" y2="{Y(lv):.1f}" class="{"lm" if lab == "mean" else "lim"}"/>')
        if lab != 'mean': o.append(f'<text x="{W-G+6}" y="{ly[lab]+3.5:.1f}" class="ll">{lab}</text>')
    o.append(f'<rect x="{X(len(vals)-3)-5:.1f}" y="4" width="{X(len(vals)-1)-X(len(vals)-3)+10:.1f}" height="{H-20}" rx="5" class="win"/>')
    pts = ' '.join(f'{X(i):.1f},{Y(v):.1f}' for i, v in enumerate(vals))
    o.append(f'<polyline points="{pts}" class="ln"/>')
    for i, v in enumerate(vals):
        cls = 'pt'
        if i >= len(vals) - 3 and v > u2: cls = 'pt br'
        o.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="{4 if "br" in cls else 2.5}" class="{cls}"/>')
    o.append(f'<text x="0" y="{H-4}" class="ax">{len(vals)} runs ago</text><text x="{X(len(vals)-1)+5:.1f}" y="{H-4}" text-anchor="end" class="ax">latest</text></svg>')
    return ''.join(o), dict(mean=mean, sd=sd, u2=u2, u3=u3, last=vals[-1], breach='1 point beyond 3 sigma' if one_point else ('2 of 3 beyond 2 sigma' if two_of_three else ''), fmt=fmt)

retry = [0.14, 0.20, 0.10, 0.25, 0.17, 0.12, 0.22, 0.18, 0.15, 0.20, 0.13, 0.19, 0.24, 0.16, 0.21, 0.11, 0.36, 0.29, 0.43]
cost = [3.90, 4.40, 3.10, 4.80, 4.20, 3.60, 4.10, 5.20, 3.80, 4.50, 3.70, 4.30, 4.90, 4.00, 3.50, 4.60, 4.20, 5.10, 4.74]
infra = [0, 0, 0.10, 0, 0, 0, 0, 0.05, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
charts = [('Parts sent back', 'retry_rate', retry, lambda v: f'{v:.2f}'), ('Cost per merged part', 'cost_per_merged_component', cost, lambda v: f'${v:.2f}'),
          ('Infrastructure errors', 'infrastructure_error_rate', infra, lambda v: f'{v:.2f}')]
def chart_tiles(charts: list) -> tuple[list[str], dict]:
    tiles = []
    facts = {}
    for title, key, vals, fmt in charts:
        svg, s = chart(vals, fmt)
        facts[key] = s
        br = s['breach']
        state = f'<span class="brk"><span class="k-mk sm fail"></span>{br}</span>' if br else '<span class="okk"><span class="k-mk sm pass"></span>within its limits</span>'
        tiles.append(f'''<div class="k-tile ch {'k-tile-alert' if br else ''}"><div class="chh"><span class="k-label">{title}</span><span class="key">{key}</span></div>
      <div class="chv"><span class="k-stat-value">{fmt(s['last'])}</span><span class="lim2">latest · limits {fmt(s['u2'])} and {fmt(s['u3'])}</span></div>{svg}<div class="chs">{state}</div></div>''')
    return tiles, facts
tiles, facts = chart_tiles(charts)
r = facts['retry_rate']
body = '    ' + top('<span class="nm">Health</span>', 'is the factory working as it usually does? · 19 runs of history', None, ['Health', 'Readiness'], 0) + f'''
    <div class="hg">
      {''.join(tiles)}
      <div class="k-tile k-tile-ink why k-tile-hero">
        <div class="k-label">What a breach does</div>
        <p class="say">It files a notice, and that is all.</p>
        <p class="sub">Trust drops on a breach only if <span class="v-measure">demote_on_health_breach</span> is on. It is off here, as it is by default.</p>
      </div>
      <div class="k-tile smt">
        <div class="k-label">Safe mode</div>
        <div class="sr"><span class="k-mk sm fail"></span><div><b>The queue is paused</b><p>Daily budget reached. It clears itself at 00:00.</p></div></div>
        <p class="ft">A report, not a switch: it turns nothing off, and clears when its reason does.</p>
      </div>
      <div class="k-tile dr">
        <div class="k-label">Ready for kstrl · ks doctor</div>
        <div class="dh"><span class="k-stat-value">9<small>/10</small></span><span>checks pass</span></div>
        <div class="dw"><span class="k-mk sm absent"></span><div><b>git_clean</b><p>2 uncommitted files. Each part is built from the last commit, so none of this reaches an engineer. Commit or stash it.</p></div></div>
      </div>
    </div>'''
FOOT = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <button class="k-need"><span class="k-mk fail"></span><b>Daily budget reached</b><span class="k-need-sub">the queue resumes at 00:00 · tags is next</span></button>
    
    <span class="k-needs-hint">Limits come from this project’s own runs: every run but the last 3, at least 8 of them.</span>
  </div>'''
css = """
  .hg { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); grid-template-rows:minmax(0,1fr) 220px; gap:14px; }

  
  .chh { display:flex; justify-content:space-between; align-items:baseline; }
  .key { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .chv { display:flex; align-items:baseline; gap:10px; margin-top:6px; }
  .lim2 { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .cc { display:block; margin-top:10px; }
  .cc .base { stroke:var(--line); }
  .cc .lim { stroke:var(--line-strong); stroke-width:1; }
  .cc .lm { stroke:var(--line); stroke-width:1; }
  .cc .ll { fill:var(--text-3); font:var(--t-measure-small); font-weight:400; }
  .cc .win { fill:var(--selected); opacity:.6; }
  .cc .ln { fill:none; stroke:var(--text-2); stroke-width:1.5; stroke-linejoin:round; }
  .cc .pt { fill:var(--text-2); stroke:var(--window); stroke-width:1.5; }
  .cc .pt.br { fill:var(--fail); }
  .cc .ax { fill:var(--text-3); font:var(--t-measure-small); font-weight:400; }
  .chs { margin-top:auto; font:var(--t-small); }
  .chs > span { display:inline-flex; align-items:center; gap:6px; }
  .brk { color:var(--text); font-weight:600; } .okk { color:var(--text-2); }

  .why .say { margin:10px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); }
  .why .sub { margin:10px 0 0; font:var(--t-small); opacity:.75; }
  .why .v-measure { font:var(--t-measure-small); font-weight:400; }

  .sr, .dw { display:grid; grid-template-columns:18px 1fr; column-gap:8px; margin-top:12px; }
  .sr b, .dw b { font:var(--t-body); font-weight:600; }
  .dw b { font:var(--t-measure); font-weight:600; }
  .sr p, .dw p { margin:2px 0 0; font:var(--t-small); color:var(--text-2); }
  .ft { margin:auto 0 0; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .dh { display:flex; align-items:baseline; gap:8px; margin-top:6px; }
  .dh span:last-child { font:var(--t-small); color:var(--text-3); }
"""
if __name__ == '__main__':
    out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="14 · Health: is the factory working as it usually does?" -->',
               'Health', ['Health'], 0, body, css, FOOT)
    out = out.replace('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">not live · last event 22:31</span>').replace('≥$31.10 <small>of $40.00 today</small>', '≥$42.46 <small>of $40.00 today</small>')
    d = Path('../system/project/components/Health'); d.mkdir(parents=True, exist_ok=True)
    (d / 'preview.html').write_text(out)
    print('retry_rate: mean %.3f sd %.3f u2 %.3f u3 %.3f last3 %s breach=%r' % (r['mean'], r['sd'], r['u2'], r['u3'], retry[-3:], r['breach']))
    for k, s in facts.items(): print(k, 'breach:', repr(s['breach']))
