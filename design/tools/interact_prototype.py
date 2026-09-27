"""The prototype page, checked as a person would use it, in both of the viewer's themes. interact_prototype.py [night]

It wraps the page the way the artifact viewer does (a doctype, a head with charset and viewport, [hidden] hidden, no body
margin) and stamps the viewer's theme attribute: data-theme="light" for day, "dark" for night. Then:
  - every screen, reached from the page's screen index, is its static frame pixel for pixel (outside the zoom's and the
    views' boxes, where the script places the thumb and bar at whole pixels);
  - every link in the page's table resolves to one element, and following it lands where the table says;
  - a tile opened by the keyboard (Tab, Enter) comes back by esc with focus on the tile;
  - narrower than the screen, the page scales the screen down and never scrolls sideways."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.argv, ARGS = [sys.argv[0]], sys.argv[1:]
import render  # noqa: E402  the tokens and bundle style for the static references
from browsers import launch  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

THEME = 'night' if 'night' in ARGS else 'day'
VIEWER = {'day': 'light', 'night': 'dark'}[THEME]
OUT = Path('out/prototype'); OUT.mkdir(parents=True, exist_ok=True)
PAGE = Path('../prototype/kstrl-prototype.html').read_text()
SKELETON = ('<!doctype html><html lang="en" data-theme="{t}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<style>:root{{color-scheme:light}} body{{margin:0;font:14px system-ui}} img{{max-width:100%}} [hidden]{{display:none!important}}</style>'
            '</head><body>{p}</body></html>')
fails: list[str] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    print(('  ok   ' if ok else '  FAIL ') + name + (f'  ({detail})' if detail and not ok else ''))
    if not ok:
        fails.append(name)


def diff(a: Path, b: Path, masks: list) -> tuple[int, tuple | None]:
    x, y = Image.open(a).convert('RGB'), Image.open(b).convert('RGB')
    if x.size != y.size:
        return (-1, None)
    d = ImageChops.difference(x, y).convert('L').point(lambda v: 255 if v > 8 else 0)
    for box in masks:
        d.paste(0, tuple(box))
    return (sum(1 for v in d.get_flattened_data() if v), d.getbbox())


def settle(pg) -> None:
    """Park focus and the pointer, and pin looping animations to their first frame, as the static references are."""
    pg.evaluate('document.activeElement && document.activeElement.blur()'); pg.mouse.move(2, 2); pg.wait_for_timeout(250)
    pg.evaluate("document.getAnimations().filter(a=>a instanceof CSSAnimation).forEach(a=>{a.pause(); a.currentTime=0;})")
    # an opacity animation runs on the compositor: wait two frames so the capture shows the pinned frame, not the last one
    pg.evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))"); pg.wait_for_timeout(100)


with sync_playwright() as p:
    br = launch(p)
    # ---- the static references, through the design system's own pipeline
    ref = br.new_page(viewport={'width': 1280, 'height': 800})
    W = json.loads(re.search(r'var W=(\{.*?\});\n', PAGE).group(1))
    from build_prototype import SCREENS
    frames = {sid: frame for sid, (frame, _, _) in SCREENS.items()}
    refs = {}
    for sid, frame in frames.items():
        src = Path(f'out/static/{frame}.html').read_text().replace('<head>', '<head>' + render.style, 1).replace('<html', f'<html data-theme="{THEME}"', 1)
        f = OUT / f'ref-{frame}-{THEME}.html'; f.write_text(src)
        ref.goto(f'file://{f.resolve()}'); ref.evaluate('document.fonts.ready'); ref.wait_for_timeout(200); settle(ref)
        refs[sid] = OUT / f'ref-{frame}-{THEME}.png'; ref.screenshot(path=str(refs[sid]))
    ref.close()

    page_file = OUT / f'prototype-{THEME}.html'; page_file.write_text(SKELETON.format(t=VIEWER, p=PAGE))
    pg = br.new_page(viewport={'width': 1360, 'height': 1000})
    def load(hash_: str = '') -> None:
        pg.goto('about:blank')  # a new hash alone would not reload the page
        pg.goto(f'file://{page_file.resolve()}' + (f'#{hash_}' if hash_ else '')); pg.evaluate('document.fonts.ready'); pg.wait_for_timeout(300)
    def shown() -> str:
        return pg.evaluate("[...document.querySelectorAll('[data-screen]')].filter(s=>!s.hidden).map(s=>s.dataset.screen).join(',')")
    def stage_shot(name: str) -> tuple[Path, list]:
        settle(pg)
        box = pg.evaluate("(()=>{const r=document.querySelector('.proto-stage').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
        out = OUT / f'{name}-{THEME}.png'
        pg.screenshot(path=str(out), clip={'x': box[0], 'y': box[1], 'width': 1280, 'height': 800})
        masks = pg.evaluate("""(b=>[...document.querySelectorAll('[data-screen]:not([hidden]) .k-titletools .k-seg, [data-screen]:not([hidden]) .k-titletools .k-tabs')]
          .map(e=>e.getBoundingClientRect()).map(r=>[Math.floor(r.left-b[0])-1, Math.floor(r.top-b[1])-1, Math.ceil(r.right-b[0])+1, Math.ceil(r.bottom-b[1])+1]))""", box)
        return out, masks

    print(f'[{THEME}] prototype: {len(W["screens"])} screens')
    load()
    check('it opens on the Factory, nothing focused', shown() == 'factory' and pg.evaluate('document.activeElement===document.body'), shown())
    missing = []
    for sid, c in W['screens'].items():
        for kind, sel, n, target, label in c['links']:
            cnt = pg.evaluate("([s,sel])=>document.querySelector('[data-screen=\"'+s+'\"]').querySelectorAll(sel).length", [sid, sel])
            if cnt <= n:
                missing.append(f'{sid}: {sel} #{n}')
    check('every link resolves to an element in its screen', not missing, str(missing))
    # ---- every screen is its static frame. Compared with the screen at the page's origin, where the reference is: at
    # another offset Chromium rasterizes the half-pixel rounded corners of keycaps differently (measured: 10 to 26 pixels
    # a screen at (40, 56), none at (0, 0)), which says where the screen is, not what is in it
    pg.add_style_tag(content='.proto{padding:0!important;gap:0!important;justify-items:start!important} .proto-bar,.proto-note{display:none!important} .proto-stage{box-shadow:none!important}')
    at = pg.evaluate("(()=>{const r=document.querySelector('.proto-stage').getBoundingClientRect();return [r.left,r.top]})()")
    check('for the comparison the screen sits at the page origin, as the reference does', at == [0, 0], str(at))
    for sid in W['screens']:
        pg.evaluate("(sid)=>{const i=document.getElementById('proto-index'); i.value=sid; i.dispatchEvent(new Event('change'))}", sid); pg.wait_for_timeout(350)
        got, masks = stage_shot(f'screen-{sid}')
        n, box = diff(refs[sid], got, masks)
        check(f'{sid} is its static frame, pixel for pixel', n == 0 and shown() == sid, f'{n}px box {box}, shown {shown()}')
    # ---- every link lands where the table says
    for sid, c in W['screens'].items():
        for kind, sel, n, target, label in c['links']:
            load(sid)
            if shown() != sid:
                check(f'{sid} opens from its link', False, shown()); continue
            el = f'[data-screen="{sid}"] {sel} >> nth={n}'
            pg.click(el); pg.wait_for_timeout(450)
            want = W['groups'].get(target[1:], [None])[0] if target.startswith('@') else target
            if kind in ('need', 'ask', 'window'):
                got = pg.evaluate("[...document.querySelectorAll('[data-overlay]')].filter(o=>!o.hidden).map(o=>o.dataset.overlay).join(',')")
                ok = got == target and pg.evaluate("!!document.activeElement.closest('[role=dialog]')")
                check(f'{sid}: {kind} opens the {target} window over it, focus inside', ok and shown() == sid, got)
                pg.keyboard.press('Escape'); pg.wait_for_timeout(450)
                check(f'{sid}: esc closes it, focus back on what opened it', pg.evaluate("[...document.querySelectorAll('[data-overlay]')].every(o=>o.hidden)") and pg.evaluate(f"document.activeElement===document.querySelectorAll('[data-screen=\"{sid}\"] {sel}')[{n}]"))
            else:
                check(f'{sid}: {kind} {sel} #{n} lands on {want}', shown() == want, shown())
    # ---- the zoom follows the part you were on, and Step does nothing where no step is drawn
    def zoom(label: str) -> None:
        pg.click(f'[data-screen]:not([hidden]) .k-seg[aria-label="Zoom level"] .k-seg-item >> text={label}'); pg.wait_for_timeout(450)
    load('spec-graph'); zoom('Part')
    check('zoom Part from the Spec level opens search-query, the selected part', shown() == 'part-search-query', shown())
    load('part-search-rank'); pg.click('[data-screen="part-search-rank"] .k-header .k-crumb >> nth=1'); pg.wait_for_timeout(350)
    sel = pg.evaluate("[...document.querySelectorAll('[data-screen=spec-graph] .pc.k-card')].filter(k=>k.classList.contains('k-card-selected')).map(k=>k.querySelector('.k-card-name').textContent+(k.querySelector('.k-card-meta .k-keys')?' ↵':'')+' '+k.tabIndex)")
    check('back on the Spec level, the selection is on search-rank, with its ↵ and the one tab stop', sel == ['search-rank ↵ 0'], str(sel))
    zoom('Part'); check('zoom Part now opens search-rank', shown() == 'part-search-rank', shown())
    zoom('Step'); check('zoom Step on search-rank opens its engineer, its open step', shown() == 'agent-search-rank', shown())
    load('part-search-api'); zoom('Step'); z = pg.evaluate("document.querySelector('[data-screen]:not([hidden]) .k-seg-item[aria-checked=true]').textContent")
    check('zoom Step on search-api does nothing (nothing ran), and the zoom stays on Part', shown() == 'part-search-api' and z == 'Part', f'{shown()} {z}')
    load('part-search-query'); zoom('Step')
    check('zoom Step on search-query opens its open step, the review on try 2', shown() == 'step-query-review-2', shown())
    load('factory'); zoom('Step')
    check('zoom Step from the Factory opens the spec’s Step level, Stage', shown() == 'step-stage', shown())
    zoom('Step'); check('pressing the level you are on does nothing', shown() == 'step-stage', shown())
    pg.click('[data-screen="step-stage"] .k-titletools .k-tab >> nth=1'); pg.wait_for_timeout(400)
    pg.click('[data-screen="step-grid"] .bento .ag >> nth=0'); pg.wait_for_timeout(350)
    check('Grid: the search-rank tile opens its engineer', shown() == 'agent-search-rank', shown())
    zoom('Part'); check('zoom Part from search-rank’s engineer opens search-rank', shown() == 'part-search-rank', shown())
    load('agent-search-rank'); pg.click('[data-screen="agent-search-rank"] .k-header .k-crumb >> nth=2'); pg.wait_for_timeout(350)
    check('the crumb being built returns to the Step level, Stage when none was left', shown() == 'step-stage', shown())
    # ---- the screen menu, used as a person would
    load('factory')
    pg.select_option('#proto-index', 'settings'); pg.wait_for_timeout(350)
    check('choosing Settings in the screen menu shows Settings, focus on its current crumb',
          shown() == 'settings' and pg.evaluate("document.activeElement.getAttribute('aria-current')") == 'page', shown())
    # ---- the keyboard: a tile opened with Enter comes back by esc, focus on the tile
    load('factory')
    pg.focus('[data-screen="factory"] .k-tile.c >> nth=0'); pg.keyboard.press('Enter'); pg.wait_for_timeout(350)
    cur = pg.evaluate("document.activeElement.getAttribute('aria-current')")
    check('Enter on the Trust tile opens Trust, focus on its current crumb', shown() == 'trust' and cur == 'page', f'{shown()} {cur}')
    pg.keyboard.press('Escape'); pg.wait_for_timeout(350)
    back = pg.evaluate("document.activeElement.getAttribute('aria-label')")
    check('esc goes back to the Factory, focus on the Trust tile', shown() == 'factory' and back == 'Open Trust', f'{shown()} {back}')
    pg.click('#proto-back'); pg.wait_for_timeout(200)
    check('Back with nowhere to go stays', shown() == 'factory')
    # ---- narrow: scaled, never scrolled sideways
    for w in (1024, 390):
        pg.set_viewport_size({'width': w, 'height': 900}); pg.wait_for_timeout(300)
        m = pg.evaluate("(()=>{const s=document.querySelector('.proto-stage').getBoundingClientRect();return {sw:document.scrollingElement.scrollWidth, iw:innerWidth, right:s.right, w:s.width}})()")
        check(f'at {w}px the screen is scaled to fit and the page does not scroll sideways', m['sw'] <= m['iw'] and m['right'] <= w - 15.5 and m['w'] < 1280, str(m))
    br.close()
print(f'[{THEME}] {len(fails)} failed' + (': ' + '; '.join(fails) if fails else ''))
sys.exit(1 if fails else 0)
