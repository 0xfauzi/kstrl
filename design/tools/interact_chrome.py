"""Chrome and CommandWindow: driven by keyboard and pointer, asserted as a person would check. Called from interact.py."""
from __future__ import annotations

STATE = """(()=>{const h=document.getElementById('hd');const cr=[...h.querySelectorAll('.k-crumb')];
  return {crumbs:cr.map(b=>b.textContent), cur:cr.findIndex(b=>b.getAttribute('aria-current')==='page'),
    focus:document.activeElement===document.body?'':document.activeElement.textContent, conds:!h.querySelector('.k-conds').hidden,
    title:document.getElementById('ttl').textContent, views:!document.getElementById('views').hidden,
    zoom:[...document.querySelectorAll('#zoom .k-seg-item')].findIndex(x=>x.getAttribute('aria-checked')==='true'),
    where:document.getElementById('where').textContent}})()"""
# Every band fits: the header and Needs you do not overflow, and the title never runs into its tools.
FITS = """(()=>{const h=document.getElementById('hd'), n=document.getElementById('needs'), t=document.querySelector('#shell .k-title'), tools=document.querySelector('#shell .k-titletools');
  const tr=t.getBoundingClientRect(), to=tools.getBoundingClientRect(); const runs=[...t.children].map(c=>c.getBoundingClientRect().right);
  const sh=document.getElementById('shell').getBoundingClientRect(), bands=[h, n, document.querySelector('#shell .tr')].map(b=>Math.abs(b.getBoundingClientRect().width-sh.width));
  return {hdr:h.scrollWidth-h.clientWidth, needs:n.scrollWidth-n.clientWidth, title:Math.max(...runs)-to.left, width:sh.width, band:Math.max(...bands), tools:to.right-(sh.right-32),
    dropped:[...document.querySelectorAll('#shell [data-fit-hidden]')].map(e=>e.className||e.tagName).join(',')}})()"""


def chrome(pg, check, shots):
    s = pg.evaluate(STATE)
    check('at load: three crumbs, the last current, the zoom on Part, no conditions', s['focus'] == '' and s['crumbs'] == ['snippetvault', 'search', 'search-query'] and s['cur'] == 2 and s['zoom'] == 2 and not s['conds'], str(s))
    pg.click('#hd .k-crumb >> text=search'); pg.wait_for_timeout(350); s = pg.evaluate(STATE)
    check('a crumb zooms out: it becomes current and keeps focus', s['crumbs'] == ['snippetvault', 'search'] and s['cur'] == 1 and s['focus'] == 'search', str(s))
    check('one level in: the conditions show, the title is the spec in your words, its views appear', s['conds'] and s['title'].startswith('search7 parts') and s['views'] and s['zoom'] == 1, str(s))
    b = pg.evaluate("(()=>{const r=document.getElementById('views'),b=r.querySelector('.k-tabs-bar'),t=r.querySelector('[aria-selected=true]');const m=new DOMMatrix(getComputedStyle(b).transform);return {w:parseFloat(b.style.width),tw:t.offsetWidth,x:m.m41,tx:t.offsetLeft}})()")
    check('views first shown: its bar sits under the selected tab', abs(b['w'] - b['tw']) < 0.5 and abs(b['x'] - b['tx']) < 0.5, str(b))
    check('the zoom thumb followed the crumb', pg.evaluate("(()=>{const r=document.getElementById('zoom'),t=r.querySelector('.k-seg-thumb'),i=r.querySelector('[aria-checked=true]');return Math.abs(new DOMMatrix(getComputedStyle(t).transform).m41-i.offsetLeft)<0.5})()"))
    check('it is announced', s['where'] == 'Zoomed out to Spec: search.', s['where'])
    pg.focus('#zoom .k-seg-item[aria-checked="true"]'); pg.keyboard.press('ArrowLeft'); pg.wait_for_timeout(350); s = pg.evaluate(STATE)
    check('zoom to Factory: one crumb, no conditions (the level shows them as tiles), the factory title', s['crumbs'] == ['snippetvault'] and not s['conds'] and s['title'].startswith('snippetvault3 specs') and not s['views'], str(s))
    pg.keyboard.press('End'); pg.wait_for_timeout(350); s = pg.evaluate(STATE)
    check('zoom to Step: four crumbs ending in the step, the part title with its step', s['crumbs'] == ['snippetvault', 'search', 'search-query', 'review, try 3'] and s['cur'] == 3 and 'review' in s['title'], str(s))
    check('focus stayed on the zoom', s['focus'] == 'Step', s['focus'])
    pg.click('#needs .k-need[data-need="stopped"]'); pg.wait_for_timeout(350); s = pg.evaluate(STATE)
    check('a stopped part in Needs you lands on its Part level', s['crumbs'] == ['snippetvault', 'search', 'search-highlight'] and s['zoom'] == 2 and s['title'].startswith('search-highlightstopped'), str(s))
    pg.click('#needs .k-need[data-need="approve"]'); s2 = pg.evaluate(STATE)
    check('an approval does not navigate or approve from the strip', s2['crumbs'] == s['crumbs'] and 'Nothing is approved from the strip' in s2['where'], s2['where'])
    pg.focus('#hd .k-crumb >> text=snippetvault'); pg.keyboard.press('Enter'); pg.wait_for_timeout(350); s = pg.evaluate(STATE)
    check('Enter on a crumb zooms out and keeps focus', s['crumbs'] == ['snippetvault'] and s['focus'] == 'snippetvault', str(s))
    pg.evaluate("document.activeElement.blur()"); pg.keyboard.press('Control+k')
    check('⌘K focuses the ask field and says where the command window is', pg.evaluate("document.activeElement.id") == 'ask0' and 'command window' in pg.inner_text('#where'))
    # every level at every width the card offers
    # at the card's declared size the card never scrolls, in any state (a 1px overflow showed a scrollbar and narrowed the layout)
    import re as _re
    from pathlib import Path as _P
    _first = _P('../system/project/components/Chrome/preview.html').read_text().splitlines()[0]
    _vw = pg.viewport_size; pg.set_viewport_size({'width': int(_re.search(r'width=(\d+)', _first).group(1)), 'height': int(_re.search(r'height=(\d+)', _first).group(1))})
    SCROLL = "(()=>{const d=document.documentElement;return Math.max(d.scrollHeight-d.clientHeight, d.scrollWidth-d.clientWidth)})()"
    for wi, wname in enumerate(['1280', '1024', '768']):
        pg.click(f'#width .k-seg-item:nth-of-type({wi + 1})'); pg.wait_for_timeout(250)
        for lvl in range(4):
            pg.click(f'#zoom .k-seg-item:nth-of-type({lvl + 1})'); pg.wait_for_timeout(300)
            f = pg.evaluate(FITS)
            check(f'{wname}, level {lvl}: every band is the screen’s width; header and Needs you fit; the title clears its tools, which stay inside', f['band'] < 0.5 and f['tools'] <= 0.5 and f['hdr'] <= 0.5 and f['needs'] <= 0.5 and f['title'] <= 0, str(f))
            check(f'{wname}, level {lvl}: the card does not scroll at its declared size', pg.evaluate(SCROLL) <= 0, str(pg.evaluate(SCROLL)))
            if wname == '768' and lvl in (1, 3):
                pg.locator('#shell').screenshot(path=f'{shots}/chrome-{wname}-l{lvl}.png')
    f = pg.evaluate(FITS)
    pg.set_viewport_size(_vw)
    check('at 768 the header set aside its conditions-cap-live-words in order, never a crumb', 'k-crumb' not in f['dropped'], f['dropped'])
    pg.evaluate("document.getElementById('shell').style.width='440px'"); pg.wait_for_timeout(250)
    m = pg.evaluate("(()=>{const n=document.getElementById('needs'),m=n.querySelector('[data-fit-more]');return {band:n.getBoundingClientRect().width, over:n.scrollWidth-n.clientWidth, more:!m.hidden, label:m.textContent, set:[...n.querySelectorAll('.k-need[data-fit-hidden]')].map(x=>x.dataset.need)}})()")
    check('narrower still: needs from the right go into a counted more item, and the strip fits', m['more'] and abs(m['band'] - 440) < 0.5 and m['over'] <= 0.5 and m['label'] == f"{len(m['set'])} more" + 'in the inbox' and m['set'][0] == 'stopped', str(m))
    pg.locator('#needs').screenshot(path=f'{shots}/chrome-needs-440.png')
    pg.evaluate("document.getElementById('shell').style.width=''"); pg.wait_for_timeout(250)
    m = pg.evaluate("(()=>{const n=document.getElementById('needs');return {more:!n.querySelector('[data-fit-more]').hidden, set:n.querySelectorAll('[data-fit-hidden]').length}})()")
    check('wide again: everything comes back and the more item goes', not m['more'] and m['set'] == 0, str(m))


CW = """(()=>{const q=document.getElementById('cq'), w=document.getElementById('win'), go=document.getElementById('cgo');
  const rows=[...document.querySelectorAll('#cl .k-row')].filter(r=>!r.hidden);
  return {open:!w.hidden, ad:q.getAttribute('aria-activedescendant'), sel:rows.filter(r=>r.getAttribute('aria-selected')==='true').map(r=>r.id).join(','),
    rows:rows.map(r=>r.id).join(','), secs:[...document.querySelectorAll('#cl .k-cmd-sec')].filter(s=>!s.hidden).map(s=>s.textContent).join('|'),
    head:(document.querySelector('#cd .k-cmd-head')||{}).textContent, note:document.getElementById('cn').textContent, go:document.getElementById('cgol').textContent,
    goOff:go.getAttribute('aria-disabled')==='true', goHidden:go.hidden, focus:document.activeElement.id, inert:document.getElementById('under').inert,
    said:document.getElementById('said').textContent, motion:w.dataset.motion||''}})()"""


def command_window(pg, check, shots):
    s = pg.evaluate(CW)
    check('at load: open, the first row active, its answer and its action', s['open'] and s['ad'] == 'cr0' and s['sel'] == 'cr0' and s['head'] == 'search-highlight stopped at 21:03' and s['go'] == 'Go to search-highlight' and s['inert'], str(s))
    l = pg.evaluate("(()=>{const l=document.getElementById('cl');return l.scrollHeight-l.clientHeight})()")
    check('every row fits without scrolling at this size', l <= 0, str(l))
    pg.click('#cq'); pg.keyboard.type('retry'); s = pg.evaluate(CW)
    check('typing narrows the list and its sections; the match becomes active', s['rows'] == 'cr1' and s['secs'] == 'Do something about it' and s['ad'] == 'cr1', str(s))
    check('an unavailable action says when, and its button is unavailable, not hidden', s['goOff'] and not s['goHidden'] and s['note'] == 'Available when this run ends', str(s))
    pg.keyboard.press('Enter'); s = pg.evaluate(CW)
    check('↵ on it does nothing: the window stays', s['open'] and s['focus'] == 'cq', str(s))
    pg.fill('#cq', 'zzz'); s = pg.evaluate(CW)
    check('nothing matches: no active row, no action, and it says so', s['rows'] == '' and s['ad'] is None and s['goHidden'] and s['head'] == 'Nothing here matches' and s['note'] == 'Nothing to run', str(s))
    pg.keyboard.press('Enter'); check('↵ with nothing to run keeps it open', pg.evaluate(CW)['open'])
    pg.fill('#cq', 'ADD GUIDANCE'); s = pg.evaluate(CW)
    check('matching ignores case', s['rows'] == 'cr2', str(s))
    pg.fill('#cq', ''); s = pg.evaluate(CW)
    check('clearing the query brings every row back, the first active', s['rows'] == 'cr0,cr1,cr2,cr3,cr4,cr5' and s['ad'] == 'cr0', str(s))
    pg.keyboard.press('ArrowUp'); check('Up at the top stays', pg.evaluate(CW)['ad'] == 'cr0')
    pg.keyboard.press('ArrowDown'); pg.keyboard.press('ArrowDown'); s = pg.evaluate(CW)
    check('Down moves the active row through an unavailable one; focus never leaves the query', s['ad'] == 'cr2' and s['focus'] == 'cq' and not s['goOff'], str(s))
    for _ in range(6): pg.keyboard.press('ArrowDown')
    check('Down at the end stays', pg.evaluate(CW)['ad'] == 'cr5')
    pg.keyboard.press('Tab'); check('Tab goes to the action', pg.evaluate("document.activeElement.id") == 'cgo')
    pg.keyboard.press('Tab'); check('Tab wraps back to the query: focus stays in the window', pg.evaluate("document.activeElement.id") == 'cq')
    pg.keyboard.press('Shift+Tab'); check('Shift+Tab wraps to the action', pg.evaluate("document.activeElement.id") == 'cgo')
    pg.focus('#cq'); pg.keyboard.press('Escape'); pg.wait_for_timeout(450); s = pg.evaluate(CW)
    check('esc closes it; the page under it is live again; focus goes to the ask field', not s['open'] and not s['inert'] and s['focus'] == 'openask' and s['said'].startswith('Closed'), str(s))
    pg.click('#openask'); s = pg.evaluate(CW)
    check('the ask field opens it, empty, focused, the first row active, entering', s['open'] and s['focus'] == 'cq' and s['ad'] == 'cr0' and s['motion'] == 'enter', str(s))
    op = pg.evaluate("getComputedStyle(document.getElementById('win')).animationName")
    check('it enters on k-cmd-in', op == 'k-cmd-in', op)
    pg.wait_for_timeout(400)
    pg.keyboard.press('Control+g'); pg.wait_for_timeout(450); s = pg.evaluate(CW)
    check('a row’s own keys run it: ⌘G opens the guidance note and closes the window, focus back on the ask field', not s['open'] and s['said'].startswith('Opened the guidance note') and s['focus'] == 'openask', str(s))
    pg.evaluate("document.activeElement.blur()"); pg.keyboard.press('Control+k'); s = pg.evaluate(CW)
    check('⌘K from anywhere opens it', s['open'] and s['focus'] == 'cq', str(s))
    pg.keyboard.type('what'); pg.keyboard.press('Control+k')
    check('⌘K while open selects the query', pg.evaluate("(()=>{const q=document.getElementById('cq');return q.selectionStart===0&&q.selectionEnd===q.value.length&&q.value==='what'})()"))
    pg.fill('#cq', ''); pg.wait_for_timeout(400)
    box = pg.locator('#cr4').bounding_box(); pg.mouse.move(box['x'] + 30, box['y'] + 10); pg.mouse.move(box['x'] + 40, box['y'] + 12); s = pg.evaluate(CW)
    check('moving the pointer over a row makes it the active row; the query keeps focus', s['ad'] == 'cr4' and s['focus'] == 'cq' and s['head'] == 'One merge waits on you', str(s))
    pg.wait_for_timeout(300)
    bg = pg.evaluate("[...document.querySelectorAll('#cl .k-row')].map(r=>getComputedStyle(r).backgroundColor)")
    check('one highlight: only the active row is filled', len([b for b in bg if b not in ('rgba(0, 0, 0, 0)', 'transparent')]) == 1, str(bg))
    pg.locator('#stage').screenshot(path=f'{shots}/cmd-hover.png')
    pg.click('#cr5'); pg.wait_for_timeout(450); s = pg.evaluate(CW)
    check('a click runs the row', not s['open'] and s['said'] == 'Went to spend.', str(s))
    pg.click('#openask'); pg.wait_for_timeout(400); pg.mouse.click(30, 200); pg.wait_for_timeout(450)
    check('a click on the scrim closes it', not pg.evaluate(CW)['open'])
    pg.click('#openask'); pg.wait_for_timeout(400); pg.keyboard.press('Enter'); pg.wait_for_timeout(450); s = pg.evaluate(CW)
    check('↵ on a question does its action (go there), never something consequential', not s['open'] and s['said'].startswith('Went to search-highlight'), str(s))
    pg.emulate_media(reduced_motion='reduce'); pg.click('#openask')
    an = pg.evaluate("getComputedStyle(document.getElementById('win')).animationName")
    check('reduced motion: it fades in without scaling', an == 'k-cmd-in-still', an)
    pg.wait_for_timeout(400); pg.keyboard.press('Escape'); pg.wait_for_timeout(450)
    check('reduced motion: it still closes', not pg.evaluate(CW)['open'])
