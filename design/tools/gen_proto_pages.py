"""Spend and Health at the prototype's moment (search being built, Wednesday 21:40). The cards (gen_spend.py,
gen_health.py) draw both at 22:40, after search finished over the daily budget; at 21:40 it is still running.

Facts are the ones other screens at 21:40 state: ≥$31.10 spent today (the header, the Factory's Spend tile), which is
export's last run at 00:12 ($9.26, as the Spend card has it) and search so far (≥$21.84, the Spec level); $8.90 left
(the Queue's admission checks); safe mode off (the Factory's Main tile); retry_rate above its limit, seen twice since
Tuesday (the Inbox). The charts are the cards' own history without search's run, which has not finished: 18 runs, and
the rules computed on them by the cards' own chart(). The count of calls that reported a cost is this prototype's
example. Prototype frames, audited from out/static (audit.py ProtoSpend, ProtoHealth)."""
from __future__ import annotations

from pathlib import Path

from gen_health import chart_tiles, cost, infra, retry
from gen_health import css as HEALTH_CSS
from gen_spend import css as SPEND_CSS
from gen_spend import lm, spend_bar, week_rows
from level_common import HINT, NEEDS, page, top

OUT = Path('out/static')

# ------------------------------------------------------------------ Spend at 21:40
segs = [('export, its last run, 00:12', 9.26), ('search, running since 19:40', 21.84)]
week = [('snippets', 41.20, 'Mon'), ('search', 21.84, 'Wed, running'), ('sharing', 17.40, 'Tue'), ('export', 11.90, 'Tue to Wed'),
        ('markdown-export', 8.80, 'Tue, poisoned')]
spend_body = '    ' + top('<span class="nm">Spend</span>', 'Wednesday · under the daily budget · $8.90 left', None, ['Today', 'This week'], 0) + f'''
    <div class="sg">
      <div class="k-tile td k-tile-hero">
        <div class="k-label">Spent today</div>
        <div class="hero"><span class="bign2">≥$31.10</span><span class="of">of $40.00</span></div>
        {spend_bar(segs, 9.26)}
        <div class="lg2"><span><svg class="sw" width="12" height="8" aria-hidden="true"><rect width="12" height="8" rx="3" class="s1"/></svg>export, its last run, 00:12 · $9.26</span><span><svg class="sw" width="12" height="8" aria-hidden="true"><rect width="12" height="8" rx="3" class="s2"/></svg>search, running since 19:40 · <span class="v-measure">≥$21.84</span></span></div>
        <p class="ex">The budget is checked before a spec starts, not while it runs. search started at $9.26, under it, and runs to its end whatever it costs.</p>
        <p class="ex3">tags, next in the queue, starts only if today’s spend is under $40.00 when search ends.</p>
      </div>
      <div class="k-tile lim">
        <div class="k-label">Limits, and what reaching each one does</div>
        {lm}
        <div class="lf"><button class="k-button">Open kstrl.toml</button><span class="t3 ty-small">No limit is on unless you set it.</span></div>
      </div>
      <div class="k-tile wk">
        <div class="k-label">What each spec cost this week</div>
        <div class="wrs">{week_rows(week)}</div>
        <p class="ex2">markdown-export spent $8.80 over 3 attempts and delivered nothing.</p>
      </div>
      <div class="k-tile cov">
        <div class="k-label">How sure these numbers are</div>
        <div class="hero"><span class="bign2 sm">327<small>/331</small></span></div>
        <p>calls today reported what they cost. The other 4 did not, so every total is a lower bound, written with <span class="v-measure">≥</span>.</p>
        <p class="t3n">ks serve refuses to spend when no call has reported a cost, unless you allow it.</p>
      </div>
    </div>'''
SPEND_NEEDS = NEEDS.replace(HINT, 'Spend is counted when a phase ends, so a running part’s cost appears in steps.')

# ------------------------------------------------------------------ Health at 21:40: search's run is not a point yet
charts = [('Parts sent back', 'retry_rate', retry[:-1], lambda v: f'{v:.2f}'),
          ('Cost per merged part', 'cost_per_merged_component', cost[:-1], lambda v: f'${v:.2f}'),
          ('Infrastructure errors', 'infrastructure_error_rate', infra[:-1], lambda v: f'{v:.2f}')]
tiles, facts = chart_tiles(charts)
health_body = '    ' + top('<span class="nm">Health</span>', 'is the factory working as it usually does? · 18 runs of history', None, ['Health', 'Readiness'], 0) + f'''
    <div class="hg">
      {''.join(tiles)}
      <div class="k-tile k-tile-ink why k-tile-hero">
        <div class="k-label">What a breach does</div>
        <p class="say">It files a notice, and that is all.</p>
        <p class="sub">Trust drops on a breach only if <span class="v-measure">demote_on_health_breach</span> is on. It is off here, as it is by default.</p>
      </div>
      <div class="k-tile smt">
        <div class="k-label">Safe mode</div>
        <div class="sr"><span class="k-mk sm pass"></span><div><b>Off</b><p>None of its four reasons holds: the control directory is trusted, trust is not clamped, the queue is running, and review and security ran in the last finished run.</p></div></div>
        <p class="ft">A report, not a switch: it turns nothing off, and clears when its reason does.</p>
      </div>
      <div class="k-tile dr">
        <div class="k-label">Ready for kstrl · ks doctor</div>
        <div class="dh"><span class="k-stat-value">9<small>/10</small></span><span>checks pass</span></div>
        <div class="dw"><span class="k-mk sm absent"></span><div><b>git_clean</b><p>2 uncommitted files. Each part is built from the last commit, so none of this reaches an engineer. Commit or stash it.</p></div></div>
      </div>
    </div>'''
HEALTH_NEEDS = NEEDS.replace(HINT, 'Limits come from this project’s own runs: every run but the last 3, at least 8 of them.')

if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    card = '<!-- @dsCard group="Prototype" height=800 width=1280 page subtitle="{}" -->'
    (OUT / 'ProtoSpend.html').write_text(page(card.format('Spend, at 21:40'), 'Spend', ['Spend'], 0, spend_body, SPEND_CSS, SPEND_NEEDS))
    (OUT / 'ProtoHealth.html').write_text(page(card.format('Health, at 21:40'), 'Health', ['Health'], 0, health_body, HEALTH_CSS, HEALTH_NEEDS))
    r = facts['retry_rate']
    print('2 pages; retry_rate at 21:40: latest %.2f, limits %.3f and %.3f, breach %r' % (r['last'], r['u2'], r['u3'], r['breach']))
