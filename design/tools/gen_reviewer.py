"""Step level, one checker: search-query's reviewer on try 2 (21:15, 64s). Its verdict story by story, beside the engineer's claims."""
from __future__ import annotations
from pathlib import Path
from level_common import HINT, NEEDS as LC_NEEDS, page, top

stories = [
  ('US-1', 'Match words in a title or a body', True, [
     ('pass', 'A word returns every snippet with it in the title or the body', '', ''),
     ('pass', 'Matching ignores case and accents', '', '')]),
  ('US-2', 'Quoted phrases match exactly', True, [
     ('fail', 'A quoted phrase matches only those words, in that order',
      'The phrase is split into words at query.py:41, so “exact phrase” matches the words in any order.', 'Pass the phrase to FTS5 as one quoted string.')]),
  ('US-3', 'Answer in under 100 ms for 10,000 snippets', True, [
     ('fail', 'Results come back in under 100 ms for 10,000 snippets',
      'No test measures the bound. The only benchmark, in tests/test_query.py, builds 100 snippets.', 'Build 10,000 in a fixture and assert the time.'),
     ('pass', 'Each match comes back with its score', '', '')]),
]
def crit(v, c, why, sug):
    mark = 'pass' if v == 'pass' else 'fail'
    extra = f'<div class="cx"><p>{why}</p><p class="sg"><span>suggests</span>{sug}</p></div>' if why else ''
    return f'<div class="cr {"c-ok" if v == "pass" else "c-no"}"><span class="k-mk sm {mark}"></span><div><div class="ct">{c}</div>{extra}</div></div>'
blocks = []
for sid, title, claimed, crits in stories:
    fails = sum(1 for v, *_ in crits if v == 'fail')
    verdict = 'met' if not fails else f'{fails} of {len(crits)} not met'
    disagree = claimed and fails
    blocks.append(f'''<div class="k-well{' k-well-alert' if fails else ''} sy">
          <div class="sh"><span class="v-measure">{sid}</span><b>{title}</b><span class="grow"></span>
            <span class="claim">engineer: done</span><span class="vd {'bad' if fails else ''}">reviewer: {verdict}</span></div>
          {''.join(crit(*c) for c in crits)}
        </div>''')
body = '    ' + top('<span class="nm">search-query</span><span class="k-chip k-chip-checker">reviewer</span>', 'try 2 of 4 · review · 21:15, 64s · sent back', 3, ['Verdict', 'Log', 'Prompt'], 0, 'esc back') + f'''
    <div class="two">
      <div class="k-tile vt k-tile-hero">
        <div class="k-label">Its verdict, criterion by criterion</div>
        {''.join(blocks)}
        <div class="on"><div class="k-label">Its summary</div><p>US-1 is solid. US-2 and US-3 are marked done but not met; the missing benchmark is the larger gap.</p></div>
      </div>
      <div class="side">
        <div class="k-tile k-tile-ink hd2">
          <div class="k-label">Claim against measurement</div>
          <p class="say">The engineer marked all 3 stories done. The reviewer found 2 criteria not met.</p>
          <p class="sub">Both disagreements are filed as findings. The review failed, so search-query goes back for try 3 with them.</p>
        </div>
        <div class="k-tile df">
          <div class="k-label">Did it read the right change?</div>
          <div class="dr"><span>it saw</span><b>4 files · +212 −31</b></div>
          <div class="dr"><span>git says</span><b>4 files · +212 −31</b><span class="k-mk sm pass"></span></div>
          <p>kstrl checks the diffstat a reviewer reports against git. In hard mode a mismatch voids the review.</p>
        </div>
        <div class="k-tile cn">
          <div class="k-label">Also noted, not blocking</div>
          <div class="cc"><span class="v-measure">test_quality</span><p>The benchmark times building the index together with the query.</p></div>
          <p class="hint2">It says it searched exhaustively. That is its own claim; nothing checks it.</p>
        </div>
      </div>
    </div>'''
# Needs you is the app's, not the level's: this is the same moment as the Factory, Spec and Part frames (≥$31.10 today,
# search-index waiting 12m), so the band lists the same two needs. Only its hint is this level's.
REVIEW_HINT = 'Log is review.log: the git commands it ran, then its verdict as JSON.'
NEEDS = LC_NEEDS.replace(HINT, REVIEW_HINT)
assert REVIEW_HINT in NEEDS
css = """
  .ttl .k-chip { align-self:center; margin-left:-4px; }
  .two { position:absolute; left:32px; right:32px; top:70px; bottom:14px; display:grid; grid-template-columns:minmax(0,1fr) 400px; gap:14px; }
  .vt { gap:10px; }
  .sh { display:flex; align-items:baseline; gap:10px; margin-bottom:4px; }
  .sh .v-measure { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .sh b { font:var(--t-body); font-weight:600; }
  .claim { font:var(--t-label); font-weight:400; color:var(--text-3); }
  .vd { font:var(--t-label); font-weight:600; color:var(--text-2); }
  .vd.bad { color:var(--text); }
  .cr { display:grid; grid-template-columns:18px 1fr; column-gap:8px; padding:4px 0; font:var(--t-small); }
  .cr .k-mk { margin-top:2px; }
  .cr.c-ok .ct { color:var(--text-2); }
  .cr.c-no .ct { font-weight:600; color:var(--text); }
  .cx p { margin:3px 0 0; color:var(--text-2); font:var(--t-small); }
  .cx .sg span { font:var(--t-micro); font-weight:400; color:var(--text-3); margin-right:6px; }
  .on { margin-top:auto; padding-top:10px; border-top:1px solid var(--line); }
  .on p { margin:4px 0 0; font:var(--t-body); }
  .side { display:flex; flex-direction:column; gap:14px; min-height:0; }

  .hd2 .say { margin:8px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); }
  .hd2 .sub { margin:10px 0 0; font:var(--t-small); opacity:.75; }

  .dr { display:grid; grid-template-columns:62px 1fr 16px; align-items:baseline; margin-top:8px; font:var(--t-small); color:var(--text-3); } .dr > :is(.k-mk, .k-dot, .k-ring, .k-chip, .k-button, .k-keys) { align-self:center; }
  .dr b { font:var(--t-measure); font-weight:500; color:var(--text); }
  .df p, .cn p { margin:8px 0 0; font:var(--t-small); color:var(--text-2); }
  .cc { margin-top:8px; }
  .cc .v-measure { font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
  .cc p { margin:2px 0 0 !important; color:var(--text) !important; }
  .cn { flex:1; }
  .hint2 { margin-top:auto !important; color:var(--text-3) !important; padding-top:10px; }
"""
out = page('<!-- @dsCard group="Frames (proposal)" height=800 width=1280 page subtitle="3e · Step level, one checker: its verdict beside the engineer’s claims" -->',
           'Step level: a checker', ['search', 'search-query', 'review, try 2'], 3, body, css, NEEDS)
d = Path('../system/project/components/Map3Reviewer'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(out)
print('ok')
