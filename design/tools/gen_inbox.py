"""Inbox: every ask and notice kstrl has filed for this repo, at 21:40 in the search run."""
from __future__ import annotations
from pathlib import Path
from level_common import page, top

def row(mark: str, title: str, sub: str, cls: str = '', meta: str = '') -> str:
    m = f'<span class="k-mk {mark}"></span>' if mark else '<span class="k-dot k-dot-ring" aria-hidden="true"></span>'  # a notice waits quietly
    sel = 'sel' in cls
    k = ' k-row-ask' if (sel and mark == 'you') else ''
    k += ' k-row-closed' if 'done' in cls else ''
    return (f'<div class="k-row{k}" role="option" aria-selected="{"true" if sel else "false"}" tabindex="{0 if sel else -1}">{m}'
            f'<span><span class="k-row-title">{title}</span><span class="k-row-sub">{sub}</span></span><span class="k-row-meta">{meta}</span></div>')

lst = f'''      <div class="k-tile lst k-tile-dense">
        <div role="listbox" aria-label="Inbox" class="lbx">
        <div role="group" aria-labelledby="g1"><div class="sec" id="g1">Waiting on you <b>2</b></div>
        {row('you', 'search-index is ready to merge', 'merge approval', 'sel', '12m')}
        {row('fail', 'search-highlight stopped', 'no progress in 3 iterations · retry, or close', '', '37m')}</div>
        <div role="group" aria-labelledby="g2"><div class="sec" id="g2">For your information <b>1</b></div>
        {row('', 'retry_rate is above its control limit', 'health check, seen twice · closing it changes nothing', '', 'Tue')}</div>
        <div role="group" aria-labelledby="g3"><div class="sec" id="g3">Closed today <b>1</b></div>
        {row('landed', 'search-schema merged', 'you approved it', 'done', '20:10')}</div>
        </div>
        <div class="cap"><span class="k-meter capm" aria-hidden="true" style="--k-meter:6%"><i></i></span><span>3 open of 50. At 50, ks serve takes no new spec.</span></div>
      </div>'''
det = '''      <div class="k-tile det k-tile-hero">
        <div class="dh"><span class="k-mk sm you"></span><span class="kind">Merge approval</span><span class="grow"></span><span class="age">parked at 21:28 · 12m</span></div>
        <div class="dt">search-index is ready to merge</div>
        <div class="dd">Keeps a full-text index of snippet titles and bodies, updated on every save and delete.</div>
        <div class="why">
          <div><div class="k-label">Why it is here</div><p>At L2 every merge waits for you. ks serve ran this spec with nobody attached, so kstrl parked <span class="v-token">search-index</span> instead of asking. Nothing was pushed and no PR was opened.</p></div>
          <div><div class="k-label">What waits on it</div><p>search-api, which needs it. And the queue: ks serve starts no new spec while a merge is parked.</p></div>
        </div>
        <div class="k-well evw"><div class="ev">
          <div><span class="k">branch</span><span class="v">kstrl/factory/search-index</span></div>
          <div><span class="k">commit</span><span class="v">7e19b4c</span></div>
          <div><span class="k">verify</span><span class="v">31 passed</span></div>
          <div><span class="k">review</span><span class="v">0 blocking · 2 advisory</span></div>
          <div><span class="k">security</span><span class="v">0 findings</span></div>
          <div><span class="k">spend</span><span class="v">≥$4.34</span></div>
        </div></div>
        <div class="acts">
          <div class="k-choices k-choices-grid" role="radiogroup" aria-label="What to do with search-index"><div class="k-choice" role="radio" aria-checked="true" tabindex="0"><span class="k-choice-title">Approve and merge</span><span class="k-keys"><span class="k-key">↵</span></span><span class="k-choice-then">Recorded now. search is still running, so 7e19b4c merges when the next run starts, after this one ends. If the branch moves first, search-index fails.</span></div><div class="k-choice k-choice-danger" role="radio" aria-checked="false" tabindex="-1"><span class="k-choice-title">Reject</span><span class="k-keys"><span class="k-key">⌫</span></span><span class="k-choice-then">Asks for a reason. search-index fails and search-api is skipped. Nothing is pushed.</span></div><div class="k-choice" role="radio" aria-checked="false" tabindex="-1"><span class="k-choice-title">Snooze a day</span><span class="k-keys"><span class="k-key">S</span></span><span class="k-choice-then">Out of this list until 21:40 tomorrow. The merge stays parked.</span></div><div class="k-choice" role="radio" aria-checked="false" tabindex="-1"><span class="k-choice-title">Open the review</span><span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">↵</span></span><span class="k-choice-then">The engineer’s claims beside what measured them, and the diff.</span></div></div>
        </div>
      </div>'''
body = '    ' + top('<span class="nm">Inbox</span>', 'snippetvault · 2 waiting on you · 1 notice', None, ['Open', 'Snoozed', 'Closed'], 0, 'esc back to the map') + f'''
    <div class="two">
{lst}
{det}
    </div>'''
KEYS = '''  <div class="k-needs">
    <span class="k-needs-label">Keys</span>
    <span class="kh"><span class="k-keys"><span class="k-key">↑</span><span class="k-key">↓</span></span> move</span>
    <span class="kh"><span class="k-keys"><span class="k-key">↵</span></span> the first action</span>
    <span class="kh"><span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">K</span></span> every action and question</span>
    
    <span class="k-needs-hint">Oldest first within each group. A repeat of an open item adds to it rather than filing a new one.</span>
  </div>'''
css = """
  .kh { display:inline-flex; align-items:center; gap:4px; font:var(--t-small); color:var(--text-2); margin-right:10px; }
  .two { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:400px minmax(0,1fr); gap:14px; }

  .sec { display:flex; gap:6px; font:var(--t-label); font-weight:400; color:var(--text-3); padding:10px 10px 6px; }
  .sec b { font:var(--t-measure-inline); font-weight:600; color:var(--text-2); }
  .lbx > div:first-child .sec { padding-top:4px; }
  .lbx { display:grid; gap:2px; }
  .cap { margin-top:auto; padding:10px 10px 2px; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .capm { margin-bottom:8px; }

  .dh { display:flex; align-items:center; gap:8px; }
  .kind { font:var(--t-small); font-weight:600; color:var(--you); }
  .age { font:var(--t-measure-inline); color:var(--text-3); }
  .dt { font:var(--t-page-title); letter-spacing:var(--t-page-title-ls); margin-top:8px; }
  .dd { font:var(--t-body); color:var(--text-2); margin-top:4px; }
  .why { display:grid; grid-template-columns:1fr 1fr; gap:28px; margin-top:16px; }
  .why p { margin:6px 0 0; font:var(--t-body); }
  .evw { margin:16px 0 14px; }
  .ev { display:grid; grid-template-columns:repeat(3, 1fr); gap:10px 18px; }
  .ev .k { display:block; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .ev .v { font:var(--t-measure); }
  .acts { margin-top:auto; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="6 · Inbox: every ask and notice, with what each answer does" -->',
           'Inbox', ['Inbox'], 0, body, css, KEYS)
out = out.replace('<title>Inbox</title>', '<title>(2) kstrl</title>')
d = Path('../system/project/components/Inbox'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
