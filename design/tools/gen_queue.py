"""Queue: work waiting, running and finished, laid out by kstrl's own queue states, with adding a spec beside it. 21:40."""
from __future__ import annotations

from pathlib import Path

from comp import steps
from level_common import page, top


def qcard(name: str, quote: str, meta: str, pos: str, cls: str = '') -> str:
    return f'''<div class="k-card{' k-card-work' if cls == 'run' else ''} qc"><div class="qh"><span class="n">{name}</span><span class="pos">{pos}</span></div><div class="ex">“{quote}”</div><div class="mm">{meta}</div></div>'''
nxt = f'''      <div class="col">
        <div class="ch"><span>Next</span><b>2</b><span class="grow"></span><span class="t3">priority, then oldest</span></div>
        {qcard('tags', 'Snippets can carry tags, and search can filter by them.', 'priority 5 · local<br>stops at a PR · 3 attempts', '1st')}
        {qcard('import', 'Import snippets from a GitHub gist by its URL.', 'priority 0 · GitHub #31<br>stops at a PR · 3 attempts', '2nd')}
        <p class="note">Order is set when a spec is added. kstrl has no command that changes it afterwards.</p>
      </div>'''
run = '''      <div class="col">
        <div class="ch"><span>Running</span><b>1</b></div>
        <div class="k-card k-card-work qc"><div class="qh"><span class="n">search</span><span class="k-mk sm work"></span></div><div class="ex">“People can find a snippet by the words in its title or its body.”</div>
          <div class="pl">''' + steps(['done', 'you', 'work', 'work', 'fail', '', '']) + '''</div>
          <div class="mm">run 7c21d0 · since 19:40<br>attempt 1 of 3</div></div>
        <div class="ch" style="margin-top:14px"><span>Waiting for you</span><b>0</b></div>
        <p class="note">A queue item waits here when its run ends with a merge parked. search is still running, so it is not here yet.</p>
      </div>'''
fin = '''      <div class="col">
        <div class="ch"><span>Finished this week</span><b>4</b></div>
        <div class="fr"><span class="k-mk sm landed"></span><span class="n2">export</span><span class="v">2 PRs · Wed</span></div>
        <div class="fr"><span class="k-mk sm landed"></span><span class="n2">sharing</span><span class="v">3 PRs · Tue</span></div>
        <div class="fr"><span class="k-mk sm fail"></span><span class="n2">markdown-export</span><span class="v">poison · Tue</span></div>
        <div class="fx">Stopped at review in all 3 attempts. <span class="lnk">Retry, starting the attempts again</span></div>
        <div class="fr"><span class="k-mk sm landed"></span><span class="n2">snippets</span><span class="v">6 PRs · Mon</span></div>
      </div>'''
add = '''      <div class="k-tile addt">
        <div class="k-label">Add a spec</div>
        <div class="k-field k-field-measure qn"><div class="k-field-box"><span class="k-field-affix">specs/</span><input class="k-field-input" value="pinning.md" aria-label="Spec file" autocomplete="off"></div></div><div class="k-field k-field-area qa"><div class="k-field-box" data-state="focus"><textarea class="k-field-input" rows="4" aria-label="The spec, in your words">People can pin a snippet to the top of their list.
Pinned snippets keep their order.</textarea></div></div>
        <div class="opt"><span class="ol">Priority</span><div class="k-field k-field-measure num"><div class="k-field-box"><input class="k-field-input" value="0" inputmode="numeric" aria-label="Priority" autocomplete="off"></div></div><span class="oh">higher runs first; it cannot be changed later</span></div>
        <div class="opt"><span class="ol">When it is built</span><div class="k-seg k-seg-sm" role="radiogroup" aria-label="When it is built"><span class="k-seg-thumb" aria-hidden="true"></span><button class="k-seg-item" role="radio" aria-checked="true" tabindex="0">Stop at a PR</button><button class="k-seg-item" role="radio" aria-checked="false" tabindex="-1">Merge when green</button></div></div>
        <div class="oh2">Merge when green is a request: at L2 every merge still waits for you.</div>
        <div class="opt"><span class="ol">Attempts</span><div class="k-field k-field-measure num"><div class="k-field-box"><input class="k-field-input" value="3" inputmode="numeric" aria-label="Attempts" autocomplete="off"></div></div><span class="oh">waits 60 s before a retry, doubling to at most 30 min</span></div>
        <div class="ab"><button class="k-button k-button-primary">Add to the queue <span class="k-keys"><span class="k-key"><span class="k-kg k-kg-cmd" role="img" aria-label="Command"></span></span><span class="k-key">↵</span></span></button><span class="t3 ty-small">3rd, after import</span></div>
      </div>
      <div class="k-tile gh">
        <div class="gh1"><span class="k-label">From GitHub issues</span><span class="on2">on</span></div>
        <p>Label an issue <span class="v-measure">kstrl:queued</span>. ks serve takes up to 5 per sync, every 60 s, if the issue was not edited after it was labelled.</p>
        <div class="mm">last sync 21:39 · nothing new</div>
      </div>'''
body = '    ' + top('<span class="nm">Queue</span>', '2 next · 1 running · ks serve is running and checks every 60 s', None, None, 0,
                    '<button class="k-button">Pause the queue</button>') + f'''
    <div class="two">
      <div class="k-tile k-tile-flush board">
{nxt}
{run}
{fin}
      </div>
      <div class="side">
{add}
      </div>
    </div>'''
FOOT = '''  <div class="k-needs">
    <span class="k-needs-label">Needs you</span>
    <button class="k-need k-need-ask"><span class="k-mk you"></span><b>Approve search-index</b><span class="k-need-sub">every check agreed · 12m</span></button>
    <button class="k-need"><span class="k-mk fail"></span><b>search-highlight stopped</b><span class="k-need-sub">retry it, or add guidance first</span></button>

    <span class="k-needs-hint">Pausing stops new specs starting. search keeps running.</span>
  </div>'''
css = """

  .two { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:14px; }
  .board { flex-direction:row; gap:0; }
  .col { flex:1; min-width:0; padding:18px; display:flex; flex-direction:column; gap:8px; }
  .col + .col { border-left:1px solid var(--line); }
  .ch { display:flex; align-items:baseline; gap:6px; font:var(--t-label); font-weight:400; color:var(--text-3); margin-bottom:2px; }
  .ch b { font:var(--t-measure-inline); font-weight:600; color:var(--text-2); }
  .qh { display:flex; align-items:baseline; justify-content:space-between; gap:8px; }
  .qh .n { font:var(--t-intent); }
  .qh .k-mk { align-self:center; }
  .pos { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .ex { font:var(--t-intent-sm); color:var(--text-2); margin-top:2px; text-wrap:balance; }
  .mm { font:var(--t-measure-small); font-weight:400; color:var(--text-3); margin-top:6px; }
  .mm + .mm { margin-top:1px; }
  .pl { margin-top:10px; }
  .note { margin:4px 2px 0; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .fr { display:grid; grid-template-columns:18px 1fr auto; column-gap:6px; align-items:baseline; padding:8px 2px; border-bottom:1px solid var(--line); } .fr > .k-mk { align-self:center; }
  .fr .n2 { font:var(--t-intent-sm); }
  .fr .v { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .fx { font:var(--t-label); font-weight:400; color:var(--text-2); padding:6px 2px 8px 26px; border-bottom:1px solid var(--line); margin-top:-8px; }
  .lnk { color:var(--text); text-decoration:underline; text-decoration-color:var(--line-strong); text-underline-offset:3px; }
  .side { display:flex; flex-direction:column; gap:14px; min-height:0; }
  .addt { flex:1; }
  .qn { margin-top:10px; } .qa { margin-top:8px; }
  .opt { display:flex; align-items:baseline; gap:10px; margin-top:12px; font:var(--t-small); }
  .ol { width:104px; color:var(--text-2); flex:none; }
  .num { width:64px; flex:none; }
  .oh { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .oh2 { font:var(--t-label); font-weight:400; color:var(--text-3); margin:6px 0 0 114px; }
  .ab { display:flex; align-items:center; gap:12px; margin-top:auto; padding-top:12px; }

  .gh1 { display:flex; justify-content:space-between; align-items:center; }
  .on2 { font:var(--t-micro); color:var(--pass); }
  .gh p { margin:6px 0 0; font:var(--t-small); color:var(--text-2); }
  .gh .v-measure { font:var(--t-measure-small); font-weight:400; color:var(--text); }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="8 · Queue: what runs next, and adding a spec" -->',
           'Queue', ['Queue'], 0, body, css, FOOT)
d = Path('../system/project/components/Queue'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
