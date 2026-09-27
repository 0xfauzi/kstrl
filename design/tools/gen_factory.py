"""Level 0 (Factory): built, building, next. Each planned spec drawn as the shape of its plan."""
from __future__ import annotations

from pathlib import Path

from comp import steps
from level_common import page, top


def glyph(tiers: list[list[str]], edges: list[tuple[int, int, int, int]], w: int, h: int, r: float, big: bool = False) -> str:
    """tiers: per tier, node states. edges: (tier_a, i, tier_b, j)."""
    n = len(tiers)
    xs = [r + 2 + k * (w - 2 * r - 4) / max(n - 1, 1) for k in range(n)]
    def ys(k: int) -> list[float]:
        m = len(tiers[k]); step = min((h - 2 * r - 4) / max(m - 1, 1), r * 4.2)
        tot = step * (m - 1); top = h / 2 - tot / 2
        return [top + i * step for i in range(m)]
    Y = [ys(k) for k in range(n)]
    o = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="gl" aria-hidden="true">']
    for a, i, b, j in edges:
        x0, y0, x1, y1 = xs[a], Y[a][i], xs[b], Y[b][j]
        st = tiers[a][i]
        cls = 'ge ok' if st == 'm' else ('ge bad' if st == 'f' else 'ge')
        mx = (x0 + x1) / 2
        o.append(f'<path d="M{x0:.1f} {y0:.1f} C{mx:.1f} {y0:.1f}, {mx:.1f} {y1:.1f}, {x1:.1f} {y1:.1f}" class="{cls}"/>')
    for k, col in enumerate(tiers):
        for i, s in enumerate(col):
            x, y = xs[k], Y[k][i]
            if s == 'y':
                o.append(f'<path d="M{x:.1f} {y-r*1.25:.1f} L{x+r*1.25:.1f} {y:.1f} L{x:.1f} {y+r*1.25:.1f} L{x-r*1.25:.1f} {y:.1f}Z" class="gn y"/>')
            else:
                o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" class="gn {s}"/>')
    o.append('</svg>')
    return ''.join(o)

# states: m merged, y your approval, w working, f stopped, p waiting, s skipped
SEARCH = ([['m'], ['y', 'w', 'w', 'f'], ['p'], ['s']],
          [(0, 0, 1, 0), (0, 0, 1, 1), (0, 0, 1, 2), (0, 0, 1, 3), (1, 0, 2, 0), (1, 1, 2, 0), (1, 2, 2, 0), (2, 0, 3, 0), (1, 3, 3, 0)])
CORE = ([['m', 'm'], ['m', 'm'], ['m'], ['m']], [(0, 0, 1, 0), (0, 1, 1, 0), (0, 1, 1, 1), (1, 0, 2, 0), (1, 1, 2, 0), (2, 0, 3, 0)])
SHARING = ([['m'], ['m', 'm']], [(0, 0, 1, 0), (0, 0, 1, 1)])
EXPORT = ([['m'], ['m']], [(0, 0, 1, 0)])

built = [
  ('export', EXPORT, '2 of 2 merged', '$11.90 · Wednesday'),
  ('sharing', SHARING, '3 of 3 merged', '$17.40 · Tuesday'),
  ('snippets', CORE, '6 of 6 merged', '$41.20 · Monday'),
]
left = ''.join(f"""        <div class="bs"><div class="gw">{glyph(t, e, 84, 50, 4.5)}</div><div><div class="n">{name}</div><div class="r"><span class="k-mk sm landed"></span>{res}</div><div class="mm">{meta}</div></div></div>
""" for name, (t, e), res, meta in built)

# The spec being built, drawn as its plan at 21:40. Same drawing as the Step level's Grid hero; here with states instead of speakers.
dag = """<svg class="dag" viewBox="0 0 500 196" width="500" height="196" aria-hidden="true">
  <path d="M62 98 C112 98 112 26 162 26 M62 98 C112 98 112 74 162 74 M62 98 C112 98 112 122 162 122 M62 98 C112 98 112 170 162 170" class="e ok"/>
  <path d="M318 26 C366 26 366 98 402 98 M318 74 C366 74 366 98 402 98 M318 122 C366 122 366 98 402 98" class="e"/><path d="M416 98 H452" class="e"/>
  <path d="M318 170 C400 170 440 136 461 110" class="e bad"/>
  <circle cx="54" cy="98" r="8" class="n m"/>
  <rect x="162" y="13" width="156" height="26" rx="13" class="n y"/><path d="M180 20.5 L186.5 26 L180 31.5 L173.5 26Z" class="mk y"/><text x="193" y="30.5" class="nt y">search-index</text>
  <rect x="162" y="61" width="156" height="26" rx="13" class="n w"/><text x="176" y="78.5" class="nt">search-query</text>
  <rect x="162" y="109" width="156" height="26" rx="13" class="n w"/><text x="176" y="126.5" class="nt">search-rank</text>
  <rect x="162" y="157" width="156" height="26" rx="13" class="n f"/><path d="M175.5 165.5 L184.5 174.5 M184.5 165.5 L175.5 174.5" class="mk f"/><text x="193" y="174.5" class="nt f">search-highlight</text>
  <circle cx="409" cy="98" r="7" class="n p"/><circle cx="460" cy="102" r="7" class="n s"/><path d="M455.5 106.5 L464.5 97.5" class="n s"/>
  <text x="42" y="125" class="lt">schema</text><text x="398" y="125" class="lt">api</text><text x="452" y="129" class="lt">cli</text>
</svg>"""
hero = f"""      <div class="k-tile k-tile-selected hero k-tile-hero">
        <div class="hh"><span class="k-label">Building</span><span class="hint2"><span class="k-keys"><span class="k-key">↵</span></span> open</span></div>
        <div class="sn">search</div>
        <div class="sub">started by ks serve from the queue at 19:40 · run 7c21d0</div>
        <div class="dagw">{dag}</div>
        <div class="stats"><div><b>1<small>/7</small></b><span>parts merged</span></div><div><b>2</b><span>agents working</span></div><div><b>1</b><span>stopped</span></div><div><b>≥$21.84</b><span>counted</span></div></div>
      </div>
"""
nxt = """      <div class="k-tile k-tile-idle nx">
        <div class="k-label">Next in the queue</div>
        <div class="qi"><div class="qh"><span class="n">tags</span><span class="pr">1st · local</span></div><div class="ex">“Snippets can carry tags, and search can filter by them.”</div></div>
        <div class="qi"><div class="qh"><span class="n">import</span><span class="pr">2nd · GitHub #31</span></div><div class="ex">“Import snippets from a GitHub gist by its URL.”</div></div>
        <div class="gates">
          <div class="gh">Before tags starts, ks serve checks in order</div>
          <div class="g"><span class="k-mk sm pass"></span><span>queue not paused</span><span class="v"></span></div>
          <div class="g"><span class="k-mk sm pass"></span><span>no poisoned streak</span><span class="v">0 of 3</span></div>
          <div class="g"><span class="k-mk sm pass"></span><span>daily budget</span><span class="v">$8.90 left</span></div>
          <div class="g"><span class="k-mk sm pass"></span><span>inbox not full</span><span class="v">3 of 50</span></div>
          <div class="g"><span class="k-mk sm work"></span><span>search finishes</span><span class="v">1 spec at a time</span></div>
          <div class="g"><span class="k-mk sm you"></span><span>no merge parked</span><span class="v">search-index</span></div>
          <div class="g"><span class="k-mk sm pass"></span><span>open pull requests</span><span class="v">0 of 1</span></div>
        </div>
      </div>
"""
# nine of fifteen clean merges: a count toward a threshold, drawn as a step sequence (the words beside it say the numbers)
ticks = steps(['done'] * 9 + [''] * 6)
conds = f"""      <div class="k-tile c"><div class="k-label">Trust</div><div class="row"><span class="k-stat-value">L2</span><span class="w">every merge waits for you</span></div><div class="tk">{ticks}</div><div class="ts">9 of 15 clean merges toward L3</div><div class="tf">thresholds are placeholders · promote in a terminal</div></div>
      <div class="k-tile c"><div class="k-label">Spend today</div><div class="row"><span class="k-stat-value">≥$31.10<small>of $40.00</small></span></div><span class="k-meter bar" aria-hidden="true" style="--k-meter:78%"><i></i></span><div class="ts">At $40.00, ks serve pauses the queue until midnight.</div></div>
      <div class="k-tile c"><div class="k-label">Main</div><div class="row"><span class="k-mk pass"></span><span class="bw">CI passing</span></div><div class="mm">a41c9e2 · #48 search-schema · 20:12</div><div class="ts">Every check ran in the last finished run. Safe mode is off.</div></div>
      <div class="k-tile c"><div class="k-label">Learning</div><div class="row"><span class="k-stat-value">42<small>facts</small></span></div><div class="ts"><span class="v-measure">≥27</span> referenced in engineers’ work.</div><div class="ts" style="margin-top:4px">ks evolve: 1 failure recurs in 3 runs, missing benchmark tests.</div></div>
"""
body = '    ' + top('<span class="v-human">snippetvault</span>', '3 specs built this week · 1 building · 2 queued', 0) + f"""
    <div class="bento">
      <div class="k-tile built"><div class="k-label">Built this week</div>
{left}        <div class="tot"><b>11</b> parts merged · <b>$70.50</b></div>
      </div>
{hero}{nxt}{conds}    </div>"""
css = """
  .bento { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); grid-template-rows:1fr 1fr 164px; gap:14px; }
  .built { grid-row:1 / span 2; }
  .bs { display:grid; grid-template-columns:96px 1fr; column-gap:14px; align-items:center; padding:18px 0; border-bottom:1px solid var(--line); }
  .tot { margin-top:auto; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .tot b { font:var(--t-measure); font-weight:600; color:var(--text); }
  .bs .gw { display:flex; justify-content:center; }
  .n { font:var(--t-intent); }
  .r { display:flex; gap:6px; align-items:center; font:var(--t-small); color:var(--text-2); margin-top:2px; }
  .mm { font:var(--t-measure-small); font-weight:400; color:var(--text-3); margin-top:3px; }
  .hero { grid-column:2 / span 2; grid-row:1 / span 2; }
  .hh { display:flex; justify-content:space-between; align-items:center; }
  .hint2 { display:inline-flex; gap:6px; align-items:center; font:var(--t-label); font-weight:400; color:var(--text-2); }
  .sn { font:var(--t-intent-display); margin-top:6px; letter-spacing:var(--t-intent-display-ls); }
  .sub { font:var(--t-small); color:var(--text-3); margin-top:2px; }
  .dagw { flex:1; display:flex; align-items:center; justify-content:center; }
  .dag .e { fill:none; stroke:var(--text-3); stroke-width:1.5; stroke-dasharray:3 4; }
  .dag .e.ok { stroke:var(--line-strong); stroke-dasharray:none; } .dag .e.bad { stroke:var(--fail); }
  .dag .n.m { fill:var(--pass); } .dag .n.p { fill:var(--canvas); stroke:var(--text-3); stroke-width:1.5; stroke-dasharray:2 2; }
  .dag .n.s { fill:var(--window); stroke:var(--text-3); stroke-width:1.5; }
  .dag .n.w { fill:var(--work-tint); stroke:var(--work); stroke-width:1.5; }
  .dag .n.y { fill:var(--you-tint); stroke:var(--you); stroke-width:1.5; }
  .dag .n.f { fill:var(--window); stroke:var(--fail); stroke-width:1.5; }
  .dag .mk.y { fill:var(--you); } .dag .mk.f { stroke:var(--fail); stroke-width:1.8; stroke-linecap:round; fill:none; }
  .dag .nt { fill:var(--work); font:var(--t-small); font-weight:600; } .dag .nt.y { fill:var(--you); } .dag .nt.f { fill:var(--fail); }
  .dag .lt { fill:var(--text-3); font:var(--t-measure-small); font-weight:400; }
  .stats { display:grid; grid-template-columns:repeat(4, 1fr); border-top:1px solid var(--line); padding-top:14px; }
  .stats b { display:block; font:var(--t-stat-sm); letter-spacing:var(--t-stat-sm-ls); }
  .stats b small { font:var(--t-measure); font-weight:500; letter-spacing:var(--t-measure-ls); color:var(--text-3); margin-left:1px; }
  .stats span { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .nx { grid-column:4; grid-row:1 / span 2; }
  .qi { padding:10px 0; border-bottom:1px solid var(--line); }
  .qi .qh { display:flex; align-items:baseline; justify-content:space-between; }
  .qi .pr { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .qi .ex { font:var(--t-intent-sm); color:var(--text-2); margin-top:1px; text-wrap:balance; }
  .gates { margin-top:12px; }
  .gates .gh { font:var(--t-label); font-weight:400; color:var(--text-3); margin-bottom:5px; }
  .g { display:grid; grid-template-columns:18px 1fr auto; column-gap:6px; align-items:baseline; font:var(--t-small); } .g > .k-mk { align-self:center; }
  .g .v { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .c .row { display:flex; align-items:baseline; gap:10px; margin-top:8px; }
  .c .row .k-mk { align-self:center; width:18px; height:18px; }
  .c .w { font:var(--t-small); color:var(--text-2); }
  .c .bw { font:var(--t-title); letter-spacing:var(--t-title-ls); }
  .c .ts { font:var(--t-small); color:var(--text-2); margin-top:8px; }
  .c .tf { margin-top:auto; font:var(--t-micro); font-weight:400; color:var(--text-3); }
  .c .mm { margin-top:6px; }
  .tk { margin-top:10px; }
  .c .tk + .ts { margin-top:6px; }
  .gl .ge { fill:none; stroke:var(--line-strong); stroke-width:1.2; }
  .gl .gn.m { fill:var(--pass); }
  .bar { margin-top:10px; }
"""
html = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="0 · Factory level: built, building, next" -->',
            'Factory level', [], 0, body, css)
html = html.replace('Select a part and press ', 'Select a spec and press ')
d = Path('../system/project/components/Map0Factory'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(html)
print('ok')
