"""Pixel identity of the wired frames: every state a wired frame can reach must be the static frame of that screen.

For each wired frame: its first state against its own static render, then every screen one control away (reached by a
real click), and each window opened from the Spec screen against the static frame that draws it. Focus and pointer are
parked first (a focused control draws a ring the static frame does not have). Both renders go through the same browser.
Usage: python3 probe/frame_pixels.py [night]"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.argv, THEME_ARGS = [sys.argv[0]], sys.argv[1:]
from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

import render  # builds the tokens + bundle style; renders nothing without arguments
from browsers import launch

THEME = 'night' if 'night' in THEME_ARGS else 'day'
OUT = Path('out/render/_px'); OUT.mkdir(parents=True, exist_ok=True)
def prep(src: str, name: str) -> Path:
    html = src.replace('<head>', '<head>' + render.style, 1).replace('<html', f'<html data-theme="{THEME}"', 1)
    f = OUT / f'{name}-{THEME}.html'; f.write_text(html); return f.resolve()

masks: dict[str, list] = {}
def diff(a: Path, b: Path) -> tuple[int, tuple | None, int]:
    x, y = Image.open(a).convert('RGB'), Image.open(b).convert('RGB')
    if x.size != y.size: return (-1, None, 0)
    d = ImageChops.difference(x, y).convert('L').point(lambda v: 255 if v > 8 else 0)
    inside = 0
    for box in masks.get(a.name, []) + masks.get(b.name, []):
        region = d.crop(tuple(box)); inside += sum(1 for v in region.get_flattened_data() if v); d.paste(0, tuple(box))
    return (sum(1 for v in d.get_flattened_data() if v), d.getbbox(), inside)

WIRED = ['Map0Factory', 'Map1Spec', 'Map4Plan', 'Map2Part', 'Map3Reviewer', 'Notify2Paths', 'Notify3Channels', 'Map5Question', 'Map6Approve',
         'Map3Step', 'Map3StepGrid', 'Map3Agent', 'Inbox', 'Learning', 'Queue', 'Settings', 'Trust']
fails = []
with sync_playwright() as p:
    br = launch(p)
    pg = br.new_page(viewport={'width': 1280, 'height': 800})
    def shot(url: Path, out: Path, act=None) -> None:
        pg.goto(f'file://{url}'); pg.wait_for_load_state('load'); pg.evaluate('document.fonts.ready'); pg.wait_for_timeout(200)
        if act: act(); pg.wait_for_timeout(700)
        pg.evaluate('document.activeElement && document.activeElement.blur()'); pg.mouse.move(2, 790); pg.wait_for_timeout(250)
        # looping animations (typing dots, a running ring) are pinned to their first frame in both renders: two captures
        # taken at different moments of a loop differ by its phase, not by anything the wiring did
        pg.evaluate("document.getAnimations().filter(a=>a instanceof CSSAnimation).forEach(a=>{a.pause(); a.currentTime=0;})")
        # an opacity animation runs on the compositor: wait two frames so the capture shows the pinned frame, not the last one
        pg.evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))"); pg.wait_for_timeout(100)
        pg.screenshot(path=str(out))
        # the zoom's thumb and the views' bar: the script places them at offsetLeft/offsetWidth, which round to whole
        # pixels, where the static frame paints the item's own box. Kept out of the identity check and reported apart.
        masks[out.name] = pg.evaluate("""[...document.querySelectorAll('.k-titletools .k-seg, .k-titletools .k-tabs')].map(e=>e.getBoundingClientRect())
          .filter(r=>r.width).map(r=>[Math.floor(r.left)-1, Math.floor(r.top)-1, Math.ceil(r.right)+1, Math.ceil(r.bottom)+1])""")
    ref = {}
    for n in WIRED:
        ref[n] = OUT / f'ref-{n}-{THEME}.png'
        shot(prep(Path(f'out/static/{n}.html').read_text(), f'ref-{n}'), ref[n])
    def compare(label: str, got: Path, want: str) -> None:
        n, box, inside = diff(ref[want], got)
        ok = n == 0
        print(('  ok   ' if ok else '  FAIL ') + f'{label:44} = static {want:16} {"" if ok else f"{n}px box {box}"}' + (f'  (zoom/views boxes: {inside}px)' if inside else ''))
        if not ok: fails.append(label)
    for f in WIRED:
        src = Path(f'../system/project/components/{f}/preview.html').read_text()
        w = json.loads(re.search(r'var W=(\{.*?\});\n', src).group(1))
        url = prep(src, f'wired-{f}')
        out = OUT / f'{f}-first-{THEME}.png'; shot(url, out)
        compare(f'{f}: first state', out, f)
        if w['open']: continue
        start, c = w['start'], w['screens'][w['start']]
        acts = []
        for i, t in enumerate(c['zoom'] or []):
            if i != c['level']: acts.append((f'zoom {["Factory", "Spec", "Part", "Step"][i]}', f'[data-screen="{start}"] .k-seg[aria-label="Zoom level"] .k-seg-item >> nth={i}', t))
        for i, t in enumerate(c['crumbs']):
            if t: acts.append((f'crumb {i}', f'[data-screen="{start}"] .k-header .k-crumb >> nth={i}', t))
        for i, t in enumerate(c['tabs'] or []):
            if t and t != start: acts.append((f'tab {i}', f'[data-screen="{start}"] .k-titletools .k-tab >> nth={i}', t))
        for i, t in enumerate(c['needs']):
            if t: acts.append((f'need {i}', f'[data-screen="{start}"] .k-needs .k-need >> nth={i}', {'approve': 'Map6Approve', 'question': 'Map5Question'}[t] if start == 'Map1Spec' else None))
        if c['ask']: acts.append(('ask field', f'[data-screen="{start}"] .k-ask', 'Map5Question' if start == 'Map1Spec' else None))
        for label, sel, t in acts:
            if t is None: continue
            t = w['memory'][t[1:]] if t.startswith('@') else t
            out = OUT / f'{f}-{label.replace(" ", "_")}-{THEME}.png'
            shot(url, out, lambda sel=sel: pg.click(sel))
            compare(f'{f}: {label}', out, t)
    br.close()
print(f'[{THEME}] {len(fails)} failed')
sys.exit(1 if fails else 0)
