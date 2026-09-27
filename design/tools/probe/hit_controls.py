"""Which element a pointer at the centre of each control actually hits, in the STATIC frames (out/static)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.argv = [sys.argv[0]]
from playwright.sync_api import sync_playwright

import render
from browsers import launch

OUT = Path('out/render/_px'); OUT.mkdir(parents=True, exist_ok=True)
JS = """()=>[...document.querySelectorAll('.k-crumb, .k-seg-item, .k-tab, .k-need, .k-ask, button, [role=radio], [role=tab], input')].map(e=>{
  const r=e.getBoundingClientRect(); if(!r.width) return null; const h=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
  return (h===e||e.contains(h))?null:{c:e.className, t:e.textContent.trim().slice(0,24), hit:h?(h.className||h.tagName)+'':'none', box:[r.left,r.top,r.width,r.height].map(Math.round)}}).filter(Boolean)"""
with sync_playwright() as p:
    br = launch(p)
    pg = br.new_page(viewport={'width': 1280, 'height': 800})
    for f in sorted(Path('../system/project/components').glob('*/preview.html')):
        first = f.read_text().split('\n', 1)[0]
        if 'Frames (proposal)' not in first: continue
        src = (Path('out/static') / f'{f.parent.name}.html')
        src = src.read_text() if src.exists() else f.read_text()
        h = OUT / f'hit-{f.parent.name}.html'; h.write_text(src.replace('<head>', '<head>' + render.style, 1))
        pg.goto(f'file://{h.resolve()}'); pg.evaluate('document.fonts.ready'); pg.wait_for_timeout(150)
        # a window open over the page covers it on purpose: only count controls outside [role=dialog] when none is open
        bad = pg.evaluate(JS)
        if pg.evaluate("!!document.querySelector('[role=dialog]')"): bad = [b for b in bad if 'k-scrim' not in b['hit'] and 'k-cmd' not in b['hit']]
        print(f'{f.parent.name:16}', 'clear' if not bad else bad)
    br.close()
