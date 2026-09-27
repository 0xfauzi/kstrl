"""The wired frames, driven by keyboard and pointer and asserted as a person would check. Called from interact.py.

What a person checks after each move: which screen shows (one, and the right one), where focus is (on the control they
used, in the new screen, visibly when they used the keyboard), that the moved control's thumb or bar sits on its item,
what was announced, and that a window opens over the screen with the page under it out of reach and closes back to
where they were."""
from __future__ import annotations

STATE = """(()=>{const v=[...document.querySelectorAll('[data-screen]')].filter(s=>!s.hidden);const a=document.activeElement;
  const o=[...document.querySelectorAll('[data-overlay]')].filter(x=>!x.hidden);
  return {shown:v.map(s=>s.dataset.screen).join(','), focus:a===document.body?'':(a.textContent||a.value||'').trim(), fv:a!==document.body&&a.matches(':focus-visible'),
    inShown:v.length===1&&v[0].contains(a), said:document.querySelector('[data-said]').textContent,
    open:o.map(x=>x.dataset.overlay).join(','), inDialog:!!a.closest('[role=dialog]'),
    inert:v.length===1?[...v[0].children].filter(c=>!c.hasAttribute('data-overlay')).every(c=>c.inert):null}})()"""
# the thumb (zoom) or bar (views) of the visible screen against its checked item: within 1px (the script places it at
# offsetLeft and offsetWidth, which round; the static frame paints the item's own box)
ALIGN = """(sel=>{const s=[...document.querySelectorAll('[data-screen]')].find(x=>!x.hidden), r=s.querySelector(sel); if(!r) return null;
  const t=r.querySelector('.k-seg-thumb, .k-tabs-bar'), i=r.querySelector('[aria-checked=true], [aria-selected=true]');
  const tr=t.getBoundingClientRect(), ir=i.getBoundingClientRect();
  return {label:i.textContent.trim(), dx:Math.abs(tr.left-ir.left), dw:Math.abs(tr.width-ir.width), shown:getComputedStyle(t).display!=='none',
    tabs:[...r.querySelectorAll('[role=radio], [role=tab]')].map(x=>x.tabIndex).join(',')}})"""


def frames(p, br, page_for, check) -> None:
    def st(pg):
        return pg.evaluate(STATE)
    def aligned(pg, sel):
        a = pg.evaluate(ALIGN, sel)
        return a is not None and a['shown'] and a['dx'] <= 1 and a['dw'] <= 1, a
    def vis(sel: str) -> str:
        return f'[data-screen]:not([hidden]) {sel}'

    # ---------------------------------------------------------------- set A, from the Spec level
    pg = page_for(p, br, 'Map1Spec')
    s = st(pg)
    check('Map1Spec at load: its own screen only, nothing focused, no window', s['shown'] == 'Map1Spec' and s['focus'] == '' and s['open'] == '', str(s))
    ok, a = aligned(pg, '.k-seg[aria-label="Zoom level"]'); check('at load the zoom thumb sits on Spec', ok and a['label'] == 'Spec', str(a))
    pg.focus(vis('.k-seg-item[aria-checked="true"]')); pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(450); s = st(pg)
    check('zoom ArrowRight: the Part level shows, focus on Part in it, visibly', s['shown'] == 'Map2Part' and s['focus'] == 'Part' and s['inShown'] and s['fv'], str(s))
    ok, a = aligned(pg, '.k-seg[aria-label="Zoom level"]'); check('the thumb slid to Part; one tab stop in the zoom', ok and a['label'] == 'Part' and a['tabs'] == '-1,-1,0,-1', str(a))
    check('the move is announced', s['said'] == 'Part level: search-query', s['said'])
    pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(450); s = st(pg)
    check('zoom ArrowRight again: the Step level (search-query review, try 2)', s['shown'] == 'Map3Reviewer' and s['focus'] == 'Step' and s['fv'], str(s))
    pg.keyboard.press('Home'); pg.wait_for_timeout(450); s = st(pg)
    check('zoom Home: the Factory level, focus on Factory', s['shown'] == 'Map0Factory' and s['focus'] == 'Factory' and s['inShown'], str(s))
    ok, a = aligned(pg, '.k-seg[aria-label="Zoom level"]'); check('the Factory thumb sits on Factory', ok and a['label'] == 'Factory', str(a))
    pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(450); s = st(pg)
    check('zoom back in to Spec lands on the Graph view it was left in', s['shown'] == 'Map1Spec' and s['focus'] == 'Spec', str(s))
    # views
    pg.focus(vis('.k-titletools .k-tab[aria-selected="true"]')); pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(450); s = st(pg)
    check('views ArrowRight: the Text view shows, focus on Text, visibly', s['shown'] == 'Map4Plan' and s['focus'] == 'Text' and s['inShown'] and s['fv'], str(s))
    ok, a = aligned(pg, '.k-titletools .k-tabs'); check('the bar slid to Text; one tab stop in the views', ok and a['label'] == 'Text' and a['tabs'] == '-1,0', str(a))
    check('the view is announced', s['said'] == 'Spec level, as text: search', s['said'])
    pg.click(vis('.k-seg-item >> text=Part')); pg.wait_for_timeout(450)
    pg.click(vis('.k-seg-item >> text=Spec')); pg.wait_for_timeout(450); s = st(pg)
    check('pointer: zoom to Part and back to Spec returns to the Text view', s['shown'] == 'Map4Plan', str(s))
    pg.click(vis('.k-titletools .k-tab >> text=Graph')); pg.wait_for_timeout(450); s = st(pg)
    check('pointer: the Graph tab shows the graph', s['shown'] == 'Map1Spec' and s['focus'] == 'Graph', str(s))
    ok, a = aligned(pg, '.k-titletools .k-tabs'); check('its bar sits on Graph', ok and a['label'] == 'Graph', str(a))
    # crumbs
    pg.click(vis('.k-seg-item >> text=Step')); pg.wait_for_timeout(450)
    pg.click(vis('.k-header .k-crumb >> text=search-query')); pg.wait_for_timeout(300); s = st(pg)
    cur = pg.evaluate("document.activeElement.getAttribute('aria-current')")
    check('pointer: the crumb search-query zooms out to its Part level; the crumb keeps focus, now current', s['shown'] == 'Map2Part' and s['focus'] == 'search-query' and cur == 'page', f'{s} current={cur}')
    ok, a = aligned(pg, '.k-seg[aria-label="Zoom level"]'); check('the zoom there sits on Part', ok and a['label'] == 'Part', str(a))
    pg.focus(vis('.k-header .k-crumb >> nth=0')); pg.keyboard.press('Enter'); pg.wait_for_timeout(300); s = st(pg)
    check('keyboard: Enter on the root crumb zooms out to the Factory, focus on it, visibly', s['shown'] == 'Map0Factory' and s['focus'] == 'snippetvault' and s['fv'], str(s))
    pg.click(vis('.k-header .k-crumb >> nth=0')); pg.wait_for_timeout(200)
    check('the current crumb does nothing', st(pg)['shown'] == 'Map0Factory')
    # Needs you: the approval opens over the screen you are on
    pg.click(vis('.k-need >> text=Approve search-index')); pg.wait_for_timeout(450); s = st(pg)
    check('pointer: Approve search-index opens the approval over the Factory, the query focused', s['shown'] == 'Map0Factory' and s['open'] == 'approve' and s['inDialog'] and s['inert'], str(s))
    WHO = """(()=>{const a=document.activeElement; if(!a.closest('[role=dialog]')) return 'outside';
      return a.classList.contains('k-cmd-q') ? 'query' : a.getAttribute('role')==='radio' ? 'choice '+a.querySelector('.k-choice-title').textContent : 'action '+a.firstChild.textContent.trim()})()"""
    order = [pg.evaluate(WHO)]
    for _ in range(3):
        pg.keyboard.press('Tab'); order.append(pg.evaluate(WHO))
    check('Tab stays in the window: query, the chosen choice, the action, round to the query', order == ['query', 'choice Approve', 'action Approve and merge', 'query'], str(order))
    pg.keyboard.press('Shift+Tab'); check('Shift+Tab goes back round to the action', pg.evaluate(WHO) == 'action Approve and merge', pg.evaluate(WHO))
    pg.keyboard.press('Escape'); pg.wait_for_timeout(450); s = st(pg)
    check('esc closes it; focus back on the need that opened it; the page is live again', s['open'] == '' and 'Approve search-index' in s['focus'] and s['inShown'] and s['inert'] is False, str(s))
    pg.keyboard.press('Enter'); pg.wait_for_timeout(450); s = st(pg)
    check('keyboard: Enter on the need opens it again', s['open'] == 'approve' and s['inDialog'], str(s))
    pg.mouse.click(640, 780); pg.wait_for_timeout(450); s = st(pg)
    check('a click on the scrim (over the needs band) closes it, and reaches nothing under it', s['open'] == '' and s['shown'] == 'Map0Factory' and 'Approve search-index' in s['focus'], str(s))
    pg.click(vis('.k-need >> text=search-highlight stopped')); pg.wait_for_timeout(300); s = st(pg)
    check('search-highlight stopped is static (its Part level is not drawn at this moment)', s['shown'] == 'Map0Factory' and s['open'] == '', str(s))
    # ask field and ⌘K: the command window, on the Spec level whose questions it lists
    pg.click(vis('.k-seg-item >> text=Spec')); pg.wait_for_timeout(450)
    pg.focus(vis('.k-titletools .k-tab[aria-selected="true"]')); pg.keyboard.press('Control+k'); pg.wait_for_timeout(450); s = st(pg)
    check('⌘K on the Spec level opens the command window, query focused', s['open'] == 'question' and s['inDialog'] and pg.evaluate("document.activeElement.classList.contains('k-cmd-q')"), str(s))
    pg.keyboard.type('why'); pg.keyboard.press('Control+k')
    check('⌘K while open selects the query', pg.evaluate("(()=>{const q=document.activeElement;return q.selectionStart===0&&q.selectionEnd===q.value.length&&q.value==='why'})()"))
    pg.keyboard.press('Escape'); pg.wait_for_timeout(450); s = st(pg)
    check('esc: back on the tab you were on', s['open'] == '' and s['focus'] == 'Graph', str(s))
    pg.evaluate("document.querySelector('[data-overlay=question] .k-cmd-q').value=''")
    pg.click(vis('.k-seg-item >> text=Part')); pg.wait_for_timeout(450); pg.keyboard.press('Control+k'); pg.wait_for_timeout(300)
    check('⌘K on the Part level does nothing here (its window is not drawn)', st(pg)['open'] == '')
    pg.close()

    # ---------------------------------------------------------------- set A, the frames that open with a window showing
    for frame, name, first in (('Map6Approve', 'approve', 'Approve search-index?'), ('Map5Question', 'question', '')):
        pg = page_for(p, br, frame); s = st(pg)
        check(f'{frame} at load: the window open over the Spec level, the page under it inert, nothing focused',
              s['shown'] == 'Map1Spec' and s['open'] == name and s['inert'] and s['focus'] == '', str(s))
        pg.keyboard.press('Tab'); s = st(pg)
        check(f'{frame}: the first Tab lands in the window', s['inDialog'] and pg.evaluate("document.activeElement.classList.contains('k-cmd-q')"), str(s))
        pg.keyboard.press('Escape'); pg.wait_for_timeout(450); s = st(pg)
        check(f'{frame}: esc closes it, focus on the ask field (nothing opened it)', s['open'] == '' and 'Ask or do anything' in pg.evaluate("document.activeElement.getAttribute('aria-label')||''") and s['inert'] is False, str(s))
        if name == 'approve':
            pg.click(vis('.k-need >> text=Approve search-index')); pg.wait_for_timeout(450)
        else:
            pg.keyboard.press('Enter'); pg.wait_for_timeout(450)
        s = st(pg)
        check(f'{frame}: it opens again from {"the need" if name == "approve" else "the ask field"}', s['open'] == name and s['inDialog'], str(s))
        pg.close()

    # ---------------------------------------------------------------- set A, notifications
    pg = page_for(p, br, 'Notify2Paths')
    pg.focus(vis('.k-titletools .k-tab[aria-selected="true"]')); pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(450); s = st(pg)
    check('Notify2Paths: views ArrowRight shows Every event, focus on it', s['shown'] == 'Notify3Channels' and s['focus'] == 'Every event' and s['fv'], str(s))
    ok, a = aligned(pg, '.k-titletools .k-tabs'); check('its bar sits on Every event', ok and a['label'] == 'Every event', str(a))
    pg.click(vis('.k-header .k-crumb >> nth=0')); pg.wait_for_timeout(300); s = st(pg)
    check('the root crumb lands on the Factory at the same moment', s['shown'] == 'Map0Factory' and s['focus'] == 'snippetvault', str(s))
    pg.close()

    # ---------------------------------------------------------------- set A, pages beside the map at the same moment
    pg = page_for(p, br, 'Trust')
    pg.click(vis('.k-need >> text=Approve search-index')); pg.wait_for_timeout(450); s = st(pg)
    check('Trust: the need opens the approval over the Trust page', s['shown'] == 'Trust' and s['open'] == 'approve' and s['inDialog'] and s['inert'], str(s))
    pg.keyboard.press('Escape'); pg.wait_for_timeout(450); s = st(pg)
    check('esc: back on the need, on the Trust page', s['shown'] == 'Trust' and 'Approve search-index' in s['focus'] and s['open'] == '', str(s))
    tabs0 = pg.evaluate("document.querySelector('[data-screen]:not([hidden]) .k-titletools .k-tab[aria-selected=true]').textContent")
    pg.click(vis('.k-titletools .k-tab >> nth=1')); pg.wait_for_timeout(300); s = st(pg)
    tabs1 = pg.evaluate("document.querySelector('[data-screen]:not([hidden]) .k-titletools .k-tab[aria-selected=true]').textContent")
    check('its views are static (History and Replay are not drawn)', s['shown'] == 'Trust' and tabs0 == tabs1 == 'Ladder', f'{s} {tabs0} {tabs1}')
    pg.focus(vis('.k-header .k-crumb >> nth=0')); pg.keyboard.press('Enter'); pg.wait_for_timeout(300); s = st(pg)
    check('keyboard: the root crumb lands on the Factory at the same moment', s['shown'] == 'Map0Factory' and s['focus'] == 'snippetvault' and s['fv'], str(s))
    pg.close()
    for page in ('Inbox', 'Learning', 'Queue', 'Settings'):
        pg = page_for(p, br, page)
        pg.click(vis('.k-header .k-crumb >> nth=0')); pg.wait_for_timeout(300); s = st(pg)
        check(f'{page}: the root crumb lands on the Factory', s['shown'] == 'Map0Factory' and s['focus'] == 'snippetvault', str(s))
        pg.close()

    # ---------------------------------------------------------------- set B, the Step level at 20:46
    pg = page_for(p, br, 'Map3Step')
    pg.focus(vis('.k-titletools .k-tab[aria-selected="true"]')); pg.keyboard.press('ArrowRight'); pg.wait_for_timeout(450); s = st(pg)
    check('Map3Step: views ArrowRight shows the Grid, focus on Grid, visibly', s['shown'] == 'Map3StepGrid' and s['focus'] == 'Grid' and s['fv'], str(s))
    ok, a = aligned(pg, '.k-titletools .k-tabs'); check('its bar sits on Grid', ok and a['label'] == 'Grid', str(a))
    pg.keyboard.press('ArrowLeft'); pg.wait_for_timeout(450); s = st(pg)
    check('ArrowLeft: back to the Stage', s['shown'] == 'Map3Step' and s['focus'] == 'Stage', str(s))
    pg.focus(vis('.k-seg-item[aria-checked="true"]')); pg.keyboard.press('ArrowLeft'); pg.wait_for_timeout(300); s = st(pg)
    z = pg.evaluate("document.querySelector('[data-screen]:not([hidden]) .k-seg-item[aria-checked=true]').textContent")
    check('the zoom here is static (no other level is drawn at 20:46)', s['shown'] == 'Map3Step' and z == 'Step', f'{s} zoom={z}')
    pg.close()
    pg = page_for(p, br, 'Map3Agent')
    pg.click(vis('.k-header .k-crumb >> text=being built')); pg.wait_for_timeout(300); s = st(pg)
    check('Map3Agent: the crumb being built zooms out to the Stage, focus on it', s['shown'] == 'Map3Step' and s['focus'] == 'being built', str(s))
    pg.click(vis('.k-header .k-crumb >> text=search')); pg.wait_for_timeout(300)
    check('the crumb search is static (the Spec level at 20:46 is not drawn)', st(pg)['shown'] == 'Map3Step')
    pg.close()
