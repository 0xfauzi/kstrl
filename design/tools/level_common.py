"""Shared chrome for the drill-down levels: header, sprite, zoom control, map CSS."""
from __future__ import annotations
import re
from comp import seg, tabs as comp_tabs, ring as comp_ring, steps as comp_steps

LOGO = '<svg class="k-logo" viewBox="0 0 64 64" aria-hidden="true"><g fill="var(--text)"><path d="M30 20 L6 4 L9 12 L18 18 L28 27 Z"/><path d="M34 20 L58 4 L55 12 L46 18 L36 27 Z"/><path d="M32 9 L35 15 L36 24 L32 46 L28 24 L29 15 Z"/><path d="M32 44 L24 58 L32 55 L40 58 Z"/></g></svg>'
SPRITE = '''<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="you" viewBox="0 0 16 16"><path d="M8 1.5 14.5 8 8 14.5 1.5 8Z"/></symbol>
<symbol id="work" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" fill="none" stroke-opacity=".35" stroke-width="2"/><path d="M8 2.5A5.5 5.5 0 0 1 13.5 8" fill="none" stroke-width="2" stroke-linecap="round"/></symbol>
<symbol id="wait" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" fill="none" stroke-width="1.5" stroke-dasharray="2.2 2.2"/></symbol>
<symbol id="fail" viewBox="0 0 16 16"><path d="M4 4 12 12M12 4 4 12" fill="none" stroke-width="2" stroke-linecap="round"/></symbol>
<symbol id="pass" viewBox="0 0 16 16"><path d="M3 8.5 6.5 12 13 4.5" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol>
<symbol id="skip" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" fill="none" stroke-width="1.5"/><path d="M4.2 11.8 11.8 4.2" stroke-width="1.5"/></symbol>
<symbol id="landed" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.5"/><path d="M4.8 8.3 7 10.5 11.3 5.8" fill="none" stroke="var(--window)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></symbol>
</defs></svg>'''
CSS = """
  html, body { margin:0; }
  .scr { width:1280px; height:800px; background:var(--canvas); color:var(--text); font:var(--t-body); display:grid; grid-template-columns:minmax(0,1fr); grid-template-rows:56px 1fr 72px; overflow:hidden; position:relative; -webkit-font-smoothing:antialiased; font-variant-numeric:tabular-nums; --sel:0 0 0 2px var(--canvas), 0 0 0 4px var(--text); }
  .map { position:relative; min-height:0; }
  .map > svg { display:block; font-family:var(--font-agent); }

  /* shared language (Stage/Grid): title row, zoom, tabs, chips, rings, checks, tiles */
  .top { position:absolute; left:32px; right:32px; top:16px; }
"""
def header(crumbs: list[str]) -> str:
    """The app header (Header component). Crumbs zoom back out; the last is where you are."""
    levels = ['snippetvault'] + crumbs
    items = []
    for i, name in enumerate(levels):
        cur = i == len(levels) - 1
        cls = 'k-crumb' + (' k-crumb-root' if i == 0 else '')
        cur_attr = ' aria-current="page"' if cur else ''
        items.append(f'<li><button class="{cls}"{cur_attr}>{name}</button></li>')
    conds = '' if len(crumbs) != 1 else '<span class="k-conds"><span>L2 · merges wait for you</span><span><span class="k-mk sm pass"></span>all checks ran</span><span><span class="k-mk sm pass"></span>CI passing</span></span>'
    return f'''  <header class="k-header">
    {LOGO}<nav aria-label="Where you are"><ol class="k-crumbs">{"".join(items)}</ol></nav>
    {conds}
    <span class="grow"></span>
    <span class="k-live">live · last event 3s ago</span>
    <span class="k-spend">≥$31.10 <small>of $40.00 today</small></span>
    <button class="k-ask" aria-label="Ask or do anything"><span>Ask or do anything</span><span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">K</span></span></button>
  </header>'''
def ring(done: int, total: int, size: int = 64, stroke: int = 6, running: bool = True) -> str:
    """Iterations as a ring (the Ring component)."""
    return comp_ring(done, total, size, stroke, running)

def checks(state: list[str]) -> str:
    """A try's four checks (the Steps component, labelled). Verify is the only check kstrl runs itself; any other check
    in progress has an agent working on it, so the frames' 'now' there is 'work'."""
    st = [('work' if (s == 'now' and i > 0) else s) for i, s in enumerate(state)]
    return comp_steps(st, ['verify', 'review', 'security', 'distill'])

def zoom(level: int) -> str:
    names = ['Factory', 'Spec', 'Part', 'Step']
    return seg(names, level, 'Zoom level')

def top(title_html: str, meta: str, level: int | None, tabs: list[str] | None = None, tab_on: int = 0, hint: str = '', tools: str = '') -> str:
    """The title row (Title row component): title and one line of facts, then hint, views and zoom."""
    # spend is a measurement: its lower-bound mark and amount are set in measure (Geist Mono draws the mark; Instrument Sans has none)
    meta = re.sub(r'≥\$[0-9][0-9.,]*', lambda m: f'<span class="v-measure">{m.group(0)}</span>', meta)
    title_html = title_html.replace('<span class="v-human">', '<span class="k-title-human">').replace('<span class="nm">', '<span class="k-title-name">')
    t = comp_tabs(tabs, tab_on, 'View') if tabs else ''
    h = f'<span class="k-hint">{hint}</span>' if hint else ''
    z = '' if level is None else zoom(level)
    return f'<div class="top k-titlerow"><h1 class="k-title">{title_html}<span class="k-title-meta">{meta}</span></h1><div class="k-titletools">{h}{tools}{t}{z}</div></div>'

HINT = 'Select a part and press <span class="k-keys k-keys-inline"><span class="k-key">↵</span></span> to zoom in · <span class="k-keys k-keys-inline"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">K</span></span> to ask why, what it cost, what runs next'
NEEDS = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <button class="k-need k-need-ask"><span class="k-mk you"></span><b>Approve search-index</b><span class="k-need-sub">every check agreed · 12m</span></button>
    <button class="k-need"><span class="k-mk fail"></span><b>search-highlight stopped</b><span class="k-need-sub">retry it, or add guidance first</span></button>
    
    <span class="k-needs-hint">Select a part and press <span class="k-keys k-keys-inline"><span class="k-key">↵</span></span> to zoom in · <span class="k-keys k-keys-inline"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">K</span></span> to ask why, what it cost, what runs next</span>
  </div>'''
def page(card: str, title: str, crumbs: list[str], level: int, body: str, extra_css: str = '', needs: str | None = None) -> str:
    return f'''{card}
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{CSS}{extra_css}</style>
</head>
<body>
{SPRITE}
<div class="scr">
{header(crumbs)}
  <div class="map">
{body}
  </div>
{NEEDS if needs is None else needs}
</div>
</body>
</html>
'''
