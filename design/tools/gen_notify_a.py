"""Notifications 7a: an ask arrives in the app. The Factory level at 21:28:04, the moment search-index parks."""
from __future__ import annotations

from pathlib import Path

from comp import notice

src = Path('../system/project/components/Map0Factory/preview.html').read_text()
assert 'data-wired-frames' not in src, 'Map0Factory is already wired: run build_all.sh, which draws it before wiring it'
rep = [
  ('subtitle="0 · Factory level: built, building, next"', 'subtitle="7a · Notifications: an ask arrives, once, where you already are"'),
  ('<title>Factory level</title>', '<title>(2) kstrl</title>'),
  ('<span class="k-live">live · last event 3s ago</span>', '<span class="k-live">live · last event 0s ago</span>'),
  ('≥$31.10 <small>of $40.00 today</small>', '≥$29.36 <small>of $40.00 today</small>'),
  ('<b>≥$21.84</b><span>counted</span>', '<b>≥$20.10</b><span>counted</span>'),
  ('<span class="k-stat-value">≥$31.10<small>of $40.00</small></span>', '<span class="k-stat-value">≥$29.36<small>of $40.00</small></span>'),
  ('<span class="k-meter bar" aria-hidden="true" style="--k-meter:78%"><i></i></span>', '<span class="k-meter bar" aria-hidden="true" style="--k-meter:73%"><i></i></span>'),
  ('<span class="v">$8.90 left</span>', '<span class="v">$10.64 left</span>'),
  ('<span>no merge parked</span><span class="v">search-index</span>', '<span>no merge parked</span><span class="v"><span class="k-fresh" data-state="still">search-index</span></span>'),
  ('<button class="k-need k-need-ask"><span class="k-mk you"></span><b>Approve search-index</b><span class="k-need-sub">every check agreed · 12m</span></button>',
   '<button class="k-need k-need-ask k-arrive" data-state="still"><span class="k-mk you"></span><b>Approve search-index</b><span class="k-need-sub">every check agreed · just now</span></button>'),
]
for a, b in rep:
    assert a in src, a[:60]
    src = src.replace(a, b)
card = '\n    <div class="note">' + notice() + '</div>\n  </div>\n'
anchor = '\n  </div>\n  <div class="k-needs">'
assert anchor in src
src = src.replace(anchor, card.rstrip('\n').replace('\n  </div>\n', '\n', 0) + '\n  <div class="k-needs">', 1)
css = '''
  .note { position:absolute; right:32px; top:112px; z-index:5; }
'''
src = src.replace('</style>', css + '</style>', 1)
d = Path('../system/project/components/Notify1Arrive'); d.mkdir(parents=True, exist_ok=True)
(d / 'preview.html').write_text(src)
print('ok')
