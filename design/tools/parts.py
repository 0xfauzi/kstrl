"""The search spec at 21:40 (the approval parked at 21:28 has waited 12m; the queue last synced at 21:39), the moment every frame shows: one set of facts per part, so the Spec level, the Part card, the
command window and the title rows cannot disagree. Spend is what kstrl has counted (component_usage, when a phase ends),
so a part whose first phase is still running shows no spend. Parts plus the architect add up to the spec's total:
2.18 + 4.34 + 9.20 + 3.22 = 18.94, plus the architect's 2.90 = 21.84."""
from __future__ import annotations
from comp import chip

SPEC_TOTAL = '≥$21.84'
ARCHITECT = '≥$2.90'
# name: (mark, who, line, sub, meta, tries, state class)
PARTS: dict[str, tuple[str, str, list[str], str, str, list[str], str]] = {
    'search-schema':    ('landed', 'merged #48', ['done', 'done', 'done', 'done'], 'merged at 20:12, CI passing', '1 try · ≥$2.18', [], ''),
    'search-index':     ('you', 'your approval', ['done', 'done', 'you', ''], 'every check agreed · 12m', '1 try · ≥$4.34', [], ' k-card-ask'),
    'search-query':     ('work', chip('reviewer'), ['done', 'work', '', ''], 'in review for 1m 12s', 'try 3 of 4 · ≥$9.20', ['fail', 'fail', 'now', ''], ' k-card-work'),
    'search-rank':      ('work', chip('engineer'), ['work', '', '', ''], '<span class="k-fresh" data-state="still">iteration 9</span> of up to 10', 'try 1 of 4', ['now', '', '', ''], ' k-card-work'),
    'search-highlight': ('fail', 'stopped', ['fail', '', '', ''], '3 iterations changed nothing', 'try 2 of 4 · ≥$3.22', ['fail', 'fail', '', ''], ' k-card-stop'),
    'search-api':       ('wait', 'waiting', ['', '', '', ''], 'needs 3 parts · 0 merged', 'not started', [], ' k-card-idle'),
    'search-cli':       ('skip', 'skipped', ['', '', '', ''], 'search-highlight stopped', 'reset if you retry it', [], ' k-card-idle k-card-skip'),
}


def args(name: str) -> tuple:
    """pcard's positional arguments for a part."""
    m, who, line, sub, meta, tries, cls = PARTS[name]
    return (m, name, who, line, sub, meta, tries, cls)
