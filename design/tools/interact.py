"""Drive the control cards by keyboard and pointer in real Chromium and assert what a person would check. interact.py [night]"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from browsers import launch

THEME = 'night' if 'night' in sys.argv else 'day'
fails: list[str] = []
def check(name: str, ok: bool, detail: str = '') -> None:
    print(('  ok   ' if ok else '  FAIL ') + name + (f'  ({detail})' if detail else ''))
    if not ok: fails.append(name)

def page_for(p, browser, card: str):
    html = Path(f'out/render/{card}{"" if THEME == "day" else "-night"}.html').resolve()
    import re
    w = re.search(r'width=(\d+)', Path(f'../system/project/components/{card}/preview.html').read_text().splitlines()[0])
    pg = browser.new_page(viewport={'width': int(w.group(1)) if w else 1120, 'height': 900})
    pg.goto(f'file://{html}'); pg.wait_for_load_state('load'); pg.evaluate('document.fonts.ready'); pg.wait_for_timeout(150)
    return pg

def thumb_ok(pg, sel):
    return pg.evaluate("""s=>{const r=document.querySelector(s),t=r.querySelector('.k-seg-thumb'),i=[...r.querySelectorAll('.k-seg-item')].find(x=>x.getAttribute('aria-checked')==='true');
      const m=new DOMMatrix(getComputedStyle(t).transform);return {w:parseFloat(t.style.width),iw:i.offsetWidth,x:m.m41,ix:i.offsetLeft,label:i.textContent}}""", sel)

with sync_playwright() as p:
    br = launch(p)
    print(f'[{THEME}] Segmented')
    pg = page_for(p, br, 'Segmented')
    t = thumb_ok(pg, '#liveZ'); check('thumb matches checked option at load', abs(t['w'] - t['iw']) < 0.5 and abs(t['x'] - t['ix']) < 0.5, str(t))
    pg.focus('#liveZ .k-seg-item[aria-checked="true"]'); pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(400)
    t = thumb_ok(pg, '#liveZ'); check('ArrowRight selects the next option and the thumb follows', t['label'] == 'Part' and abs(t['x'] - t['ix']) < 0.5, str(t))
    check('focus moved with selection', pg.evaluate("document.activeElement.textContent") == 'Part')
    check('readout announces the choice', pg.inner_text('#readout') == 'Part · Stage', pg.inner_text('#readout'))
    pg.keyboard.press('End'); pg.wait_for_timeout(400); check('End jumps to the last option', thumb_ok(pg, '#liveZ')['label'] == 'Step')
    pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(400); check('ArrowRight wraps to the first', thumb_ok(pg, '#liveZ')['label'] == 'Factory')
    tabidx = pg.evaluate("[...document.querySelectorAll('#liveZ .k-seg-item')].map(i=>i.tabIndex).join(',')"); check('roving tabindex: only the checked option is in the tab order', tabidx == '0,-1,-1,-1', tabidx)
    pg.click('#liveV .k-seg-item:nth-of-type(2)'); pg.wait_for_timeout(400)
    t = thumb_ok(pg, '#liveV'); check('pointer selects Grid and the thumb follows', t['label'] == 'Grid' and abs(t['x'] - t['ix']) < 0.5, str(t))
    pg.close()

    print(f'[{THEME}] Tabs')
    pg = page_for(p, br, 'Tabs')
    live = pg.evaluate("(()=>{const r=[...document.querySelectorAll('.k-tabs-js')].find(x=>x.querySelector('[aria-controls]'));return r?r.id:null})()")
    check('a live tablist with panels exists', bool(live), str(live))
    if live:
        sel = f'#{live}'
        def bar():
            return pg.evaluate("""s=>{const r=document.querySelector(s),b=r.querySelector('.k-tabs-bar'),t=[...r.querySelectorAll('.k-tab')].find(x=>x.getAttribute('aria-selected')==='true');
              const m=new DOMMatrix(getComputedStyle(b).transform);return {w:parseFloat(b.style.width),tw:t.offsetWidth,x:m.m41,tx:t.offsetLeft,label:t.textContent,
              shown:[...document.querySelectorAll('[role=tabpanel]')].filter(p=>!p.hidden).map(p=>p.id).join(',')}}""", sel)
        b = bar(); check('bar matches the selected tab at load', abs(b['w'] - b['tw']) < 0.5 and abs(b['x'] - b['tx']) < 0.5, str(b))
        pg.focus(f'{sel} .k-tab[aria-selected="true"]'); pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(400)
        b = bar(); check('ArrowRight selects the next tab; bar and panel follow', abs(b['x'] - b['tx']) < 0.5 and b['label'].lower() in b['shown'].lower(), str(b))
        pg.keyboard.press('Home'); pg.wait_for_timeout(400); b = bar(); check('Home returns to the first tab', abs(b['x'] - b['tx']) < 0.5, str(b))
    def vbar():
        return pg.evaluate("""(()=>{const r=document.getElementById('liveV'),b=r.querySelector('.k-tabs-bar'),t=r.querySelector('[aria-selected=true]');
          const m=new DOMMatrix(getComputedStyle(b).transform);return {y:m.m42,ty:t.offsetTop+(t.offsetHeight-20)/2,h:b.getBoundingClientRect().height,label:t.firstChild.textContent,
          focus:document.activeElement===t,panel:document.getElementById('vgh').textContent,lb:document.getElementById('vgp').getAttribute('aria-labelledby'),id:t.id,
          tabs:[...r.querySelectorAll('.k-tab')].map(x=>x.tabIndex).join(',')}})()""")
    v = vbar(); check('vertical: the bar sits beside the selected tab at load, 20px tall', abs(v['y'] - v['ty']) < 0.5 and abs(v['h'] - 20) < 0.5, str(v))
    pg.focus('#liveV .k-tab[aria-selected="true"]'); pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(400)
    check('vertical: Right does nothing', vbar()['label'] == 'How work is checked')
    pg.keyboard.press('ArrowDown'); pg.wait_for_timeout(400); v = vbar()
    check('vertical: Down selects the next group; bar, focus and panel follow', v['label'] == 'How much runs at once' and abs(v['y'] - v['ty']) < 0.5 and v['focus'] and v['panel'] == v['label'] and v['lb'] == v['id'], str(v))
    pg.keyboard.press('End'); pg.wait_for_timeout(400); pg.keyboard.press('ArrowDown'); pg.wait_for_timeout(400); v = vbar()
    check('vertical: End then Down wraps to the first, one tab stop', v['label'] == 'How work is checked' and v['tabs'] == '0,-1,-1,-1,-1', str(v))
    pg.close()

    print(f'[{THEME}] Toggle')
    pg = page_for(p, br, 'Toggle')
    x0 = pg.evaluate("document.querySelector('#tg1 .k-toggle-track').getBoundingClientRect().left")
    pg.click('#tg1'); pg.wait_for_timeout(300)
    x1 = pg.evaluate("document.querySelector('#tg1 .k-toggle-track').getBoundingClientRect().left")
    check('pointer flips it on', pg.get_attribute('#tg1', 'aria-checked') == 'true')
    check('the track does not move when the word changes', abs(x0 - x1) < 0.01, f'{x0} -> {x1}')
    check('the visible word matches the state', pg.evaluate("[...document.querySelectorAll('#tg1 .k-toggle-word > span')].filter(s=>getComputedStyle(s).visibility==='visible').map(s=>s.textContent).join()") == 'on')
    pg.focus('#tg2'); pg.keyboard.press('Space'); pg.wait_for_timeout(50); check('Space flips it', pg.get_attribute('#tg2', 'aria-checked') == 'false')
    pg.keyboard.press('Enter'); pg.wait_for_timeout(50); check('Enter flips it', pg.get_attribute('#tg2', 'aria-checked') == 'true')
    pg.click('#tg3', force=True); pg.focus('#tg3'); pg.keyboard.press('Space'); check('the unavailable toggle ignores pointer and keys', pg.get_attribute('#tg3', 'aria-checked') == 'true')
    check('the unavailable toggle is focusable and names its reason', pg.evaluate("(()=>{const b=document.getElementById('tg3');b.focus();return document.activeElement===b && !!document.getElementById(b.getAttribute('aria-describedby'))})()"))
    pg.close()

    print(f'[{THEME}] Field')
    pg = page_for(p, br, 'Field')
    def st():
        return pg.evaluate("""(()=>{const i=document.getElementById('live'),w=i.closest('.k-field'),m=w.querySelector('.k-field-msg');
          return {inv:w.getAttribute('data-invalid'),aria:i.getAttribute('aria-invalid'),desc:i.getAttribute('aria-describedby'),msg:m?m.textContent:null}})()""")
    pg.fill('#live', '-5'); s = st(); check('a negative budget is refused with kstrl\'s rule', s['inv'] == 'true' and s['aria'] == 'true' and s['desc'] == 'live-msg' and s['msg'] == 'Must be 0 or more. 0 means no budget.', str(s))
    pg.fill('#live', 'abc'); s = st(); check('a non-number says what is accepted', s['msg'] == 'Must be a number, like 40 or 12.50.', str(s))
    pg.fill('#live', ''); s = st(); check('empty says 0 turns it off', s['msg'] == 'Needs a number. 0 means no budget.', str(s))
    pg.fill('#live', '12.50'); s = st(); check('a valid value clears every trace of the error', s['inv'] is None and s['aria'] is None and s['desc'] is None and s['msg'] is None, str(s))
    ro = pg.evaluate("(()=>{const i=[...document.querySelectorAll('.k-field[data-locked] input')][0];i.focus();const f=document.activeElement===i;const v=i.value;i.setSelectionRange(0,v.length);return {f, ro:i.readOnly, sel:i.selectionEnd-i.selectionStart, n:v.length}})()")
    check('the locked field takes focus and its value can be selected', ro['f'] and ro['ro'] and ro['sel'] == ro['n'], str(ro))
    pg.keyboard.type('9'); check('typing into the locked field changes nothing', pg.evaluate("document.querySelector('.k-field[data-locked] input').value") == '25.00')
    # the area grows a line at a time as you write, stops at the page's limit and then scrolls inside itself; at the card's
    # declared size the card never scrolls while it does
    import re as _re
    _h = int(_re.search(r'height=(\d+)', Path('../system/project/components/Field/preview.html').read_text().splitlines()[0]).group(1))
    pg.set_viewport_size({'width': 1120, 'height': _h})
    A = "(()=>{const t=document.querySelector('.k-field-area textarea'),d=document.documentElement;return {lh:parseFloat(getComputedStyle(t).lineHeight),h:t.getBoundingClientRect().height,inner:t.scrollHeight>t.clientHeight,over:d.scrollHeight-d.clientHeight,ring:getComputedStyle(t.closest('.k-field-box')).outlineWidth}})()"
    a0 = pg.evaluate(A); pg.click('.k-field-area textarea'); pg.keyboard.press('Control+End'); pg.keyboard.type(' Then check that search-highlight still passes its own tests.'); a1 = pg.evaluate(A)
    check('the area grows by one line of its type (intent) as the words wrap, with the focus ring', a1['h'] - a0['h'] == a0['lh'] and not a1['inner'] and a1['ring'] == '2px', f'{a0} -> {a1}')
    for _ in range(6): pg.keyboard.type(' Then check that search-highlight still passes its own tests.')
    a2 = pg.evaluate(A)
    check('past its limit the area scrolls inside itself and the card does not scroll', a2['inner'] and a2['over'] <= 0 and a2['h'] == 3 * a2['lh'], str(a2))
    pg.close()

    print(f'[{THEME}] Button')
    pg = page_for(p, br, 'Button')
    w0 = pg.evaluate("document.getElementById('go').getBoundingClientRect().width")
    pg.click('#go'); pg.wait_for_timeout(100)
    b = pg.evaluate("(()=>{const g=document.getElementById('go');return {busy:g.getAttribute('aria-busy'),w:g.getBoundingClientRect().width,spin:getComputedStyle(g.querySelector('.k-spin')).display}})()")
    check('click sets busy and the spinner shows', b['busy'] == 'true' and b['spin'] == 'block', str(b))
    check('busy keeps the width', abs(b['w'] - w0) < 0.01, f'{w0} -> {b["w"]}')
    pg.wait_for_timeout(1300); check('busy clears after 1.2 s', pg.get_attribute('#go', 'aria-busy') is None)
    dis = pg.evaluate("(()=>{const d=document.querySelector('.k-button[aria-disabled=\"true\"]');if(!d)return null;d.focus();return document.activeElement===d})()")
    check('an unavailable button is focusable', dis is True, str(dis))
    pg.close()

    print(f'[{THEME}] Ring')
    pg = page_for(p, br, 'Ring')
    def rs():
        return pg.evaluate("""(()=>{const R=document.getElementById('liveR'),d=R.querySelector('.k-ring-done'),n=R.querySelector('.k-ring-now');
          return {slot:+R.dataset.slot,done:parseFloat(getComputedStyle(d).strokeDasharray),off:parseFloat(getComputedStyle(n).strokeDashoffset),nowlen:parseFloat(getComputedStyle(n).strokeDasharray),
          label:R.getAttribute('aria-label'),count:document.getElementById('liveN').textContent,dis:document.getElementById('next').getAttribute('aria-disabled')}})()""")
    a = rs(); check('starts at iteration 8', abs(a['done'] - a['slot'] * 7) < 0.05 and a['label'] == 'Iteration 8 of up to 10' and a['count'] == '8/10', str(a))
    pg.click('#next'); pg.wait_for_timeout(750); a = rs()
    check('finishing one moves both arcs one slot', abs(a['done'] - a['slot'] * 8) < 0.05 and abs(a['off'] + a['slot'] * 8) < 0.05, str(a))
    check('the name and the count follow', a['label'] == 'Iteration 9 of up to 10' and a['count'] == '9/10', str(a))
    pg.click('#next'); pg.wait_for_timeout(50); pg.click('#next'); pg.wait_for_timeout(750); a = rs()
    check('after the tenth, no running arc and the button is unavailable', a['nowlen'] == 0 and a['dis'] == 'true' and a['label'] == 'All 10 iterations used', str(a))
    pg.click('#next', force=True); pg.wait_for_timeout(100); check('finishing past the limit does nothing', rs()['count'] == '10/10 used')
    pg.click('#reset'); pg.wait_for_timeout(750); a = rs(); check('start over returns to iteration 1', a['done'] == 0 and a['count'] == '1/10' and a['dis'] == 'false', str(a))
    pg.close()

    print(f'[{THEME}] Steps')
    pg = page_for(p, br, 'Steps')
    seen = [pg.inner_text('#ev')]
    for _ in range(5):
        pg.click('#adv'); pg.wait_for_timeout(50); seen.append(pg.inner_text('#ev'))
    check('next event walks the try through its checks and wraps', seen[0] == seen[5] and len(set(seen[:5])) == 5, str(seen))
    st = pg.evaluate("[...document.querySelectorAll('#liveS .k-step')].map(s=>s.dataset.step).join(',')")
    check('after one full cycle the line shows verify running again', st == 'now,,,', st)
    pg.close()

    print(f'[{THEME}] Tile')
    pg = page_for(p, br, 'Tile')
    ring_css = pg.evaluate("""(()=>{const t=document.querySelector('.k-tile-selected[tabindex]');t.focus();const cs=getComputedStyle(t);
      const probe=document.createElement('span');probe.style.color='var(--text)';document.body.appendChild(probe);
      return {w:cs.outlineWidth,c:cs.outlineColor,text:getComputedStyle(probe).color,off:cs.outlineOffset,focused:document.activeElement===t}})()""")
    check('a focused tile shows the 2px text ring, not rufous', ring_css['focused'] and ring_css['w'] == '2px' and ring_css['c'] == ring_css['text'] and ring_css['off'] == '2px', str(ring_css))
    pg.close()

    print(f'[{THEME}] Change')
    pg = page_for(p, br, 'Change')
    pg.click('#ev1'); pg.wait_for_timeout(100)
    a = pg.evaluate("(()=>{const s=document.getElementById('spend');return {t:s.textContent,fresh:s.classList.contains('k-fresh'),bg:getComputedStyle(s).backgroundColor}})()")
    check('an event changes the value and marks only it', a['t'] == '≥$33.20' and a['fresh'] and not pg.evaluate("document.getElementById('stage').classList.contains('k-fresh')"), str(a))
    pg.wait_for_timeout(2100)
    check('the mark is gone after dur-fresh', not pg.evaluate("document.getElementById('spend').classList.contains('k-fresh')"))
    pg.click('#ev3'); pg.wait_for_timeout(2200)
    b = pg.evaluate("(()=>({age:document.getElementById('age').textContent,typing:!document.getElementById('typ').hidden}))()")
    check('when output stops the dots go and the age ticks', (not b['typing']) and b['age'] in ('output 2s ago', 'output 3s ago'), str(b))
    pg.close()

    print(f'[{THEME}] NeedsYou')
    pg = page_for(p, br, 'NeedsYou')
    pg.click('#arr'); pg.wait_for_timeout(100)
    a = pg.evaluate("(()=>{const it=document.querySelectorAll('#items .k-need');return {n:it.length,arr:it[it.length-1].classList.contains('k-arrive'),tab:document.getElementById('tabt').textContent}})()")
    check('an ask arrives once, marked, and the tab counts it', a['n'] == 2 and a['arr'] and a['tab'] == '(2) kstrl', str(a))
    pg.click('#arr'); pg.wait_for_timeout(50)
    check('a repeat does not add a second item', pg.evaluate("document.querySelectorAll('#items .k-need').length") == 2)
    pg.wait_for_timeout(2100); check('the arrival mark ends', not pg.evaluate("document.querySelector('#items .k-need:last-child').classList.contains('k-arrive')"))
    pg.close()

    print(f'[{THEME}] Notice')
    pg = page_for(p, br, 'Notice')
    pg.click('#come'); pg.wait_for_timeout(300)
    check('the notice arrives without taking focus', pg.evaluate("!!document.getElementById('live-notice') && !document.getElementById('live-notice').contains(document.activeElement)"))
    check('it is announced', pg.inner_text('#note2').startswith('Merge approval'))
    pg.keyboard.press('F6'); check('F6 moves focus to Review', pg.evaluate("document.activeElement.classList.contains('nr')"))
    pg.keyboard.press('Escape'); pg.wait_for_timeout(300)
    check('esc inside it means Later, and it leaves', pg.evaluate("!document.getElementById('live-notice')") and pg.inner_text('#note2').startswith('Later'))
    pg.close()

    print(f'[{THEME}] Choice')
    pg = page_for(p, br, 'Choice')
    pg.focus('#chg .k-choice[aria-checked="true"]'); pg.keyboard.press('ArrowDown'); pg.keyboard.press('ArrowDown')
    a = pg.evaluate("(()=>{const c=[...document.querySelectorAll('#chg .k-choice')];const b=document.getElementById('doit');return {checked:c.findIndex(x=>x.getAttribute('aria-checked')==='true'),focus:c.indexOf(document.activeElement),label:b.querySelector('span').textContent,cls:b.className,keys:[...b.querySelectorAll('.k-key')].map(k=>k.textContent||k.querySelector('[aria-label]').getAttribute('aria-label')).join('+'),tabs:c.map(x=>x.tabIndex).join(',')}})()")
    check('arrows choose, and the button names the choice with its keys', a['checked'] == 2 and a['focus'] == 2 and a['label'] == 'Reject' and 'k-button-danger' in a['cls'] and a['keys'] == 'Command+⌫' and a['tabs'] == '-1,-1,0,-1', str(a))
    pg.close()

    print(f'[{THEME}] Row')
    pg = page_for(p, br, 'Row')
    pg.focus('#rl .k-row[aria-selected="true"]'); pg.keyboard.press('End')
    a = pg.evaluate("(()=>{const r=[...document.querySelectorAll('#rl .k-row')];return {sel:r.findIndex(x=>x.getAttribute('aria-selected')==='true'),focus:r.indexOf(document.activeElement),tabs:r.map(x=>x.tabIndex).join(',')}})()")
    n = pg.evaluate("document.querySelectorAll('#rl .k-row').length")
    check('End selects and focuses the last row, with one tab stop', a['sel'] == n - 1 and a['focus'] == n - 1 and a['tabs'] == ','.join(['-1'] * (n - 1) + ['0']), str(a))
    pg.keyboard.press('ArrowDown'); check('Down at the end stays', pg.evaluate("[...document.querySelectorAll('#rl .k-row')].indexOf(document.activeElement)") == n - 1)
    pg.close()
    import interact_chrome
    shots = 'out/interact'; Path(shots).mkdir(parents=True, exist_ok=True)
    print(f'[{THEME}] Chrome')
    pg = page_for(p, br, 'Chrome'); interact_chrome.chrome(pg, check, shots); pg.close()
    print(f'[{THEME}] CommandWindow')
    pg = page_for(p, br, 'CommandWindow'); interact_chrome.command_window(pg, check, shots); pg.close()
    import interact_frames
    print(f'[{THEME}] Frames')
    interact_frames.frames(p, br, page_for, check)
    br.close()
print(f'{len(fails)} failed' + (': ' + '; '.join(fails) if fails else ''))
sys.exit(1 if fails else 0)
