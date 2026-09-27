"""One scaffold for every component card, so they share a frame: canvas page, window tiles, 12px labels, the anatomy helper."""
from __future__ import annotations
from pathlib import Path

BASE_CSS = """
  html, body { margin:0; }
  body { background:var(--canvas); color:var(--text); font:var(--t-body); -webkit-font-smoothing:antialiased; font-variant-numeric:tabular-nums; }
  .c-wrap { padding:24px; display:grid; gap:14px; }
  .c-tile { background:var(--window); border-radius:var(--radius-xl); box-shadow:var(--shadow-card); padding:18px 20px; min-width:0; }
  .c-lab { font:var(--t-label); font-weight:400; color:var(--text-3); margin-bottom:12px; }
  .c-two { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:14px; }
  .c-three { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:14px; }
  .c-row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  .c-cap { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .c-note { font:var(--t-label); font-weight:400; color:var(--text-3); margin-top:12px; }
  .c-anat { position:relative; }
  .c-dl { stroke:var(--text-3); stroke-width:1; shape-rendering:crispEdges; }
  .c-dt { fill:var(--text-2); font:var(--t-measure-small); font-weight:400; }
  .c-grid { display:grid; align-items:center; row-gap:10px; }
  .c-gh { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .c-gr { font:var(--t-small); color:var(--text-2); }
  .c-ink { background:var(--text); color:var(--window); border-radius:var(--radius-lg); padding:14px 16px; }
  /* a patch of the app's ground inside a card, for a component shown where it lives (a band, a notice, a window) */
  .c-stage { background:var(--canvas); border-radius:var(--radius-lg); box-shadow:inset 0 0 0 1px var(--line); }
  /* a token or command named in a caption is one word: it never breaks at its hyphen */
  :is(.c-lab, .c-cap, .c-note) .v-measure { white-space:nowrap; }
"""
ANAT = Path('anat.js').read_text()

def card(name: str, group: str, height: int, subtitle: str, body: str, css: str = '', script: str = '', width: int = 1120) -> None:
    html = f'''<!-- @dsCard group="{group}" height={height} width={width} subtitle="{subtitle}" -->
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{name}</title><style>{BASE_CSS}{css}</style></head><body>
<div class="c-wrap">
{body}
</div>
<script>
{ANAT}
// Everything below measures rendered text (thumbs, bars, filmstrips, anatomy), so it runs once the web fonts have loaded:
// measured earlier, it would size to the fallback font (a thumb 4 to 6px too wide was the measured result).
(document.fonts ? document.fonts.ready : Promise.resolve()).then(function(){{
{script}
}});
</script>
</body></html>
'''
    d = Path('../system/project/components') / name
    d.mkdir(parents=True, exist_ok=True)
    (d / 'preview.html').write_text(html)
