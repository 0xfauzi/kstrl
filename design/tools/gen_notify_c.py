"""Notifications 7c: every event kstrl records against every channel that can carry it, with the gaps marked."""
from __future__ import annotations
from pathlib import Path
from level_common import page, top

I, Q, N, G, D = 'int', 'quiet', 'no', 'gap', 'gh'
def cell(kind: str, text: str) -> str:
    mark = {'int': '<i class="k-dot k-dot-ink dot"></i>', 'quiet': '<i class="k-dot k-dot-ring dot"></i>', 'no': '<i class="k-dot k-dot-none dot"></i>',
            'gap': '<span class="k-mk sm absent"></span>', 'hook': '<i class="k-dot dot"></i>', 'gh': '<i class="k-dot dot"></i>'}[kind]
    return f'<div class="c {kind}">{mark}<span>{text}</span></div>'
rows = [
  ('A merge waits for you', 'merge_gate', [cell(I, 'interrupts'), cell(I, 'banner'), cell('hook', '<code>on_inbox_item</code>, first of the run'), cell(D, 'label and comment, at run end')]),
  ('A part stopped', 'halted_run', [cell(I, 'interrupts'), cell(I, 'banner'), cell('hook', '<code>on_first_failure</code> and <code>on_inbox_item</code>, first of each'), cell(D, 'the run’s outcome, at run end')]),
  ('A run finished', 'run end', [cell(Q, 'a quiet line'), cell(N, 'no'), cell('hook', '<code>on_complete</code>'), cell(D, 'label and comment')]),
  ('A run reached its budget', 'budget_overrun', [cell(I, 'interrupts'), cell(I, 'banner'), cell('hook', '<code>on_inbox_item</code>, first of the run'), cell(N, 'no')]),
  ('The architect has a question', 'spec_escalation', [cell(I, 'interrupts'), cell(I, 'banner'), cell(G, 'not sent'), cell(D, 'the run’s outcome, at run end')]),
  ('Trust was lowered', 'demotion_notice', [cell(I, 'interrupts'), cell(I, 'banner'), cell(G, 'not sent'), cell(N, 'no')]),
  ('ks serve paused the queue', 'daily budget, poison streak', [cell(I, 'interrupts'), cell(I, 'banner'), cell(G, 'not sent'), cell(N, 'no')]),
  ('A health or calibration notice', 'health_breach, calibration_drift', [cell(Q, 'in the inbox'), cell(N, 'no'), cell(N, 'no, by design'), cell(N, 'no')]),
]
head = '<div class="mh"><span>When</span><span>In the app</span><span>On your desktop</span><span>Your hook</span><span>The GitHub issue</span></div>'
body_rows = ''.join(f'<div class="mr"><div class="e"><b>{t}</b><span>{k}</span></div>{"".join(cs)}</div>' for t, k, cs in rows)
body = '    ' + top('<span class="nm">Notifications</span>', 'what reaches you, where, and what does not', None, ['One event', 'Every event'], 1) + f'''
    <div class="two">
      <div class="k-tile mx">
        {head}
        {body_rows}
        <div class="lg"><span><i class="k-dot k-dot-ink dot"></i>interrupts you</span><span><i class="k-dot k-dot-ring dot"></i>waits quietly</span><span><i class="k-dot dot"></i>sent by kstrl</span><span><span class="k-mk sm absent"></span>kstrl files it, and no hook fires</span></div>
      </div>
      <div class="side">
        <div class="k-tile hk">
          <div class="k-label">Your hooks, in kstrl.toml</div>
          <div class="kv"><span>on_complete</span><em>not set</em></div>
          <div class="kv"><span>on_first_failure</span><b>~/bin/notify-phone</b></div>
          <div class="kv"><span>on_inbox_item</span><b>~/bin/notify-phone</b></div>
          <div class="kv"><span>hook_timeout</span><b>30s</b> </div>
          <p>Each runs as a shell command on the machine running kstrl, at most once per event per run.</p>
          <button class="k-button">Open kstrl.toml</button>
        </div>
        <div class="k-tile k-tile-idle gp">
          <div class="k-label">Gaps in kstrl today</div>
          <p>Three kinds of ask are filed without calling the hook: the architect’s question, a lowered trust level, and anything ks serve files. kstrl marks a lowered trust level as worth interrupting for; the path that files it skips the hook.</p>
          <p>With kstrl closed, these reach you nowhere.</p>
          <p class="gp2">kstrl sends no digest. In the app the inbox is the digest: banners and the tab count come from the app reading it.</p>
        </div>
      </div>
    </div>'''

css = """
  .two { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 330px; gap:14px; }

  .mh, .mr { display:grid; grid-template-columns:210px repeat(4, minmax(0,1fr)); column-gap:16px; }
  .mh { font:var(--t-label); font-weight:400; color:var(--text-3); padding:8px 0 10px; border-bottom:1px solid var(--line); }
  .mr { padding:9px 0; border-bottom:1px solid var(--line); align-items:baseline; }
  .e b { display:block; font:var(--t-body); font-weight:600; }
  .e span { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .c { display:flex; gap:7px; align-items:baseline; font:var(--t-small); color:var(--text-2); }
  .c code { font:var(--t-measure-small); font-weight:400; color:var(--text); }
  .c .k-mk { flex:none; align-self:center; }
  .c.gap > span:not(.k-mk) { color:var(--text); font-weight:600; }
  .c.no { color:var(--text-3); }
  .dot { margin-top:-1px; }
  .lg { margin-top:auto; display:flex; gap:22px; font:var(--t-label); font-weight:400; color:var(--text-3); padding-top:10px; }
  .lg > span { display:inline-flex; align-items:center; gap:6px; }
  .side { display:flex; flex-direction:column; gap:14px; min-height:0; }

  .hk .k-label { margin-bottom:6px; }
  .kv { display:flex; justify-content:space-between; gap:10px; padding:6px 0; border-bottom:1px solid var(--line); font:var(--t-measure-inline); }
  .kv span { color:var(--text-3); } .kv b { font-weight:500; } .kv em { font-style:normal; color:var(--text-3); font:var(--t-label); font-weight:400; }
  .hk p { margin:10px 0 12px; font:var(--t-small); color:var(--text-2); }
.hk .k-button { align-self:flex-start; }
  .gp { flex:1; }
  .gp p { margin:8px 0 0; font:var(--t-small); color:var(--text-2); }
  .gp p + p { color:var(--text); font-weight:600; } .gp p.gp2 { color:var(--text-2); font-weight:400; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="7c · Notifications: every event against every channel, gaps marked" -->',
           'Notifications: every event', ['Notifications'], 0, body, css)
d = Path('../system/project/components/Notify3Channels'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
