"""Static markup for the components, as the frames use them (no script: roving tabindex already set, selection painted by CSS)."""
from __future__ import annotations

import re

from glyph_svg import key


def keys(*chs: str, sm: bool = False) -> str:
    return '<span class="k-keys">' + ''.join(key(c, 'k-key-sm' if sm else '') for c in chs) + '</span>'

def seg(opts: list[str], on: int, label: str, sm: bool = False) -> str:
    items = ''.join(f'<button class="k-seg-item" role="radio" aria-checked="{"true" if i == on else "false"}" tabindex="{0 if i == on else -1}">{o}</button>'
                    for i, o in enumerate(opts))
    return f'<div class="k-seg{" k-seg-sm" if sm else ""}" role="radiogroup" aria-label="{label}"><span class="k-seg-thumb" aria-hidden="true"></span>{items}</div>'

def tabs(opts: list[str], on: int, label: str) -> str:
    items = ''.join(f'<button class="k-tab" role="tab" aria-selected="{"true" if i == on else "false"}" tabindex="{0 if i == on else -1}">{o}</button>'
                    for i, o in enumerate(opts))
    return f'<div class="k-tabs" role="tablist" aria-label="{label}"><span class="k-tabs-bar" aria-hidden="true"></span>{items}</div>'

def vtabs(items: list[tuple[str, int]], on: int, label: str, unit: str, ids: str = '') -> str:
    """Vertical tabs: the sections of one long page, each with a count ("9" read as "9 settings")."""
    def one(i: int, name: str, n: int) -> str:
        sel = 'true' if i == on else 'false'
        idat = f' id="{ids}{i}" aria-controls="{ids}p"' if ids else ''
        return (f'<button class="k-tab" role="tab" aria-selected="{sel}" tabindex="{0 if i == on else -1}"{idat}>'
                f'<span>{name}</span><span class="k-tab-count">{n}<span class="k-sr"> {unit}</span></span></button>')
    return (f'<div class="k-tabs k-tabs-v" role="tablist" aria-orientation="vertical" aria-label="{label}">'
            f'<span class="k-tabs-bar" aria-hidden="true"></span>' + ''.join(one(i, n, c) for i, (n, c) in enumerate(items)) + '</div>')

def ikeys(*chs: str) -> str:
    """Keys inside a sentence: 18px, centred on the line's lowercase."""
    return '<span class="k-keys k-keys-inline">' + ''.join(key(c) for c in chs) + '</span>'

_RING_N = [0]
def ring(done: int, total: int, size: int = 56, stroke: int = 6, running: bool = True, label: str = '') -> str:
    """Iterations as a ring, one segment per iteration: done in text-3, the running one in work, the rest selected.
    Segments are cut by a mask (gap 2px) so the done and running arcs stay single arcs that animate cleanly; with
    less than 8px per iteration the gaps are dropped. Both arcs are always present so they can animate from zero; caps are
    butt, so a zero-length arc paints nothing (a round cap would paint a dot, the old ring's stray mark at 12 o'clock).
    `label` gives the ring role=img and a name; without it the ring is aria-hidden and the count beside it says the same."""
    _RING_N[0] += 1
    mid = f'kr{_RING_N[0]}'
    r = (size - stroke) / 2; c = 2 * 3.141592653589793 * r; slot = c / total
    gap = 2.0 if slot >= 8 else 0.0
    cx = size / 2
    a11y = f'role="img" aria-label="{label}"' if label else 'aria-hidden="true"'
    rot = f'transform="rotate(-90 {cx} {cx})"'
    off = gap / 2  # the first segment starts half a gap after 12 o'clock, so the gaps sit symmetrically
    mask = ''
    use_mask = ''
    if gap:
        mask = (f'<mask id="{mid}" maskUnits="userSpaceOnUse" x="0" y="0" width="{size}" height="{size}">'
                f'<circle cx="{cx}" cy="{cx}" r="{r:.3f}" fill="none" stroke="white" stroke-width="{stroke + 2}" '
                f'stroke-dasharray="{slot - gap:.3f} {gap:.3f}" stroke-dashoffset="{-off:.3f}" {rot}/></mask>')
        use_mask = f' mask="url(#{mid})"'
    parts = [f'<circle class="k-ring-track" cx="{cx}" cy="{cx}" r="{r:.3f}" stroke-width="{stroke}"/>']
    now = slot if running and done < total else 0.0
    parts.append(f'<circle class="k-ring-done" cx="{cx}" cy="{cx}" r="{r:.3f}" stroke-width="{stroke}" '
                 f'stroke-dasharray="{slot * done:.3f} {c:.3f}" stroke-dashoffset="0" {rot}/>')
    parts.append(f'<circle class="k-ring-now" cx="{cx}" cy="{cx}" r="{r:.3f}" stroke-width="{stroke}" '
                 f'stroke-dasharray="{now:.3f} {c:.3f}" stroke-dashoffset="{-slot * done:.3f}" {rot}/>')
    return (f'<svg class="k-ring" viewBox="0 0 {size} {size}" width="{size}" height="{size}" {a11y} data-total="{total}" data-done="{done}" data-slot="{slot:.3f}">'
            f'{"<defs>" + mask + "</defs>" if mask else ""}<g{use_mask}>{"".join(parts)}</g></svg>')

STEP_MARK = {'work': 'work', 'you': 'you', 'fail': 'fail'}
STEP_WORD = {'done': 'passed', 'now': 'running', 'work': 'an agent is working', 'you': 'waits for you', 'fail': 'failed', '': 'not started'}
def steps(states: list[str], labels: list[str] | None = None, legend: bool = False) -> str:
    """A line of stations or checks. Unlabelled, the line is aria-hidden and must sit beside a mark and a word that say
    the state (as a part card's first line does). Labelled, each step names itself, the three states that lightness
    cannot separate (work, you, fail) carry their mark, and every state is spoken; below 300px the labels leave the
    drawing and a status line names the step that matters now."""
    if labels is None:
        return '<div class="k-steps" aria-hidden="true">' + ''.join(f'<span class="k-step" data-step="{s}"><i class="k-step-bar"></i></span>' for s in states) + '</div>'
    if legend:
        # A key to the line, not a state: labelled bars hidden from screen readers (the words beside the legend say what it
        # is), no state words and no status line.
        li = ''.join(f'<li class="k-step" data-step="{s}"><i class="k-step-bar"></i><span class="k-step-label">{n}</span></li>' for s, n in zip(states, labels))
        return f'<div class="k-steps-wrap"><ol class="k-steps k-steps-labeled" aria-hidden="true">{li}</ol></div>'
    items = []
    for s, n in zip(states, labels):
        mk = f'<span class="k-mk sm {STEP_MARK[s]}" aria-hidden="true"></span>' if s in STEP_MARK else ''
        items.append(f'<li class="k-step" data-step="{s}"><i class="k-step-bar" aria-hidden="true"></i><span class="k-step-label">{mk}{n}'
                     f'<span class="k-sr">: {STEP_WORD.get(s, "not started")}</span></span></li>')
    cur = next(((s, n) for s, n in zip(states, labels) if s != 'done'), None)
    if cur is None:
        status = '<span class="k-mk sm pass"></span><span>every check passed</span>'
    elif cur[0] == '':
        status = f'{cur[1]}<span>is next</span>'
    else:
        mk = f'<span class="k-mk sm {STEP_MARK[cur[0]]}"></span>' if cur[0] in STEP_MARK else ''
        status = f'{mk}{cur[1]}<span>{STEP_WORD[cur[0]]}</span>'
    return (f'<div class="k-steps-wrap"><ol class="k-steps k-steps-labeled">{"".join(items)}</ol>'
            f'<p class="k-steps-status" aria-hidden="true">{status}</p></div>')

def pcard(m: str, n: str, who: str, line: list[str], sub: str, meta: str, tries: list[str], cls: str, sel: bool = False,
          tab: int | None = None, fresh_step: int | None = None, fresh_tries: bool = False, attrs: str = '') -> str:
    """A part card (the Part card component). ``who``, ``sub`` and ``meta`` are markup, so a changed value can carry its own
    ``k-fresh``; ``fresh_step`` and ``fresh_tries`` mark a step or the tries as just changed."""
    tr = ''.join(f'<i class="k-try{" k-try-" + t if t in ("now", "fail") else ""}"></i>' for t in tries)
    fr = ' k-fresh" data-state="still' if fresh_tries else ''
    trs = f'<span class="k-tries{fr}" aria-hidden="true">{tr}</span>' if tries else ''
    t = f' tabindex="{tab}"' if tab is not None else ''
    hint = keys('↵', sm=True) if sel else ''
    ln = steps(line)
    if fresh_step is not None:
        parts = ln.split('<span class="k-step"')
        parts[fresh_step + 1] = ' data-state="still"' + parts[fresh_step + 1]
        ln = parts[0] + ''.join(('<span class="k-step k-fresh"' if i == fresh_step else '<span class="k-step"') + p for i, p in enumerate(parts[1:]))
    label = re.sub(r'<[^>]+>', '', sub)
    return (f'<div class="k-card{cls}{" k-card-selected" if sel else ""}"{t}{attrs} aria-label="{n}, {label}"><div class="k-card-head"><span class="k-mk {m}"></span><span class="k-card-name">{n}</span>'
            f'<span class="k-card-who">{who}</span></div>{ln}<div class="k-card-sub">{sub}</div><div class="k-card-meta">{trs}<span>{meta}</span>{hint}</div></div>')

def chip(role: str) -> str:
    kind = {'reviewer': ' k-chip-checker', 'security': ' k-chip-checker', 'you': ' k-chip-you'}.get(role, '')
    return f'<span class="k-chip{kind}">{role}</span>'

def stat(label: str, value: str, small: str = '', sm: bool = False) -> str:
    s = f'<small>{small}</small>' if small else ''
    return f'<div class="k-stat{" k-stat-sm" if sm else ""}"><span class="k-label">{label}</span><span class="k-stat-value">{value}{s}</span></div>'

def choices(items: list[tuple[str, tuple[str, ...], str, bool]], chosen: int, label: str, ids: str = '', state: dict[int, str] | None = None, grid: bool = False) -> str:
    """A decision list: (title, keys, consequence, destructive) per decision. Roving tabindex on the chosen one."""
    out = []
    for i, (t, k, then, danger) in enumerate(items):
        st = f' data-state="{state[i]}"' if state and i in state else ''
        out.append(f'<div class="k-choice{" k-choice-danger" if danger else ""}" role="radio" aria-checked="{"true" if i == chosen else "false"}" tabindex="{0 if i == chosen else -1}"{st}>'
                   f'<span class="k-choice-title">{t}</span>{keys(*k) if k else "<span></span>"}<span class="k-choice-then">{then}</span></div>')
    return f'<div class="k-choices{" k-choices-grid" if grid else ""}" role="radiogroup" aria-label="{label}"{ids}>{"".join(out)}</div>'

def notice_compact(body: str = 'Every check agreed. search-api waits on it.') -> str:
    """The Notice, compact: the merge approval for search-index as the Notifications page shows it (no actions)."""
    return ('<div class="k-notice k-notice-ask k-notice-compact">'
            '<div class="k-notice-head"><span class="k-mk sm you"></span><span class="k-notice-kind">Merge approval</span><span class="k-notice-age">just now</span></div>'
            f'<p class="k-notice-title">search-index is ready to merge</p><p class="k-notice-body">{body}</p></div>')


def notice(extra: str = '', motion: str = '') -> str:
    """The Notice component: the merge approval for search-index, as it arrives."""
    mo = f' data-motion="{motion}"' if motion else ''
    return (f'<div class="k-notice k-notice-ask"{mo}{extra} role="region" aria-label="Merge approval">'
            '<div class="k-notice-head"><span class="k-mk sm you"></span><span class="k-notice-kind">Merge approval</span><span class="k-notice-age">just now</span></div>'
            '<p class="k-notice-title">search-index is ready to merge</p>'
            '<p class="k-notice-body">Every check agreed. search-api waits on it, and ks serve starts no new spec until you answer.</p>'
            f'<div class="k-notice-actions"><button class="k-button k-button-primary nr"><span>Review</span>{keys("↵")}<span class="k-spin" aria-hidden="true"></span></button>'
            f'<button class="k-button nl"><span>Later</span>{keys("esc")}<span class="k-spin" aria-hidden="true"></span></button><span class="k-notice-foot">stays in Needs you</span></div></div>')


# Illustrations: drawings of things that are not kstrl (a browser's tab, an operating system's banner). They carry
# data-illustration, which the audit lists instead of failing, and a drawing that recurs is defined here once.
ILL_CSS = """
  .ill-tab { display:inline-flex; align-self:flex-start; padding:4px 10px; border-radius:8px 8px 0 0; background:var(--raised); box-shadow:inset 0 0 0 1px var(--line); font:12px/16px var(--font-agent); color:var(--text-2); }
"""
def ill_tab(text: str, ident: str = '', hidden: bool = False) -> str:
    """The browser tab's title, drawn: where the count of what needs you shows while kstrl sits in another tab."""
    i = f' id="{ident}"' if ident else ''
    h = ' aria-hidden="true"' if hidden else ''
    return f'<span class="ill-tab"{i}{h} data-illustration="the browser tab\'s title, drawn">{text}</span>'


def cmdline(text: str) -> str:
    """A command as one k-well-line whose words are v-tokens: it wraps at its spaces only, never inside a flag (--|to), and an
    option stays with its value (--actor <you>) so a line never ends on an option waiting for what it sets."""
    from html import escape
    words, units, i = text.split(' '), [], 0
    while i < len(words):
        if words[i].startswith('-') and i + 1 < len(words) and not words[i + 1].startswith('-'):
            units.append(words[i] + ' ' + words[i + 1]); i += 2
        else:
            units.append(words[i]); i += 1
    return '<span class="k-well-line">' + ' '.join(f'<span class="v-token">{escape(u)}</span>' for u in units) + '</span>'


def meter(share: float, cls: str = '') -> str:
    """A meter, hidden from screen readers: the words beside it always say the numbers (3 open of 50)."""
    c = f' {cls}' if cls else ''
    return f'<span class="k-meter{c}" aria-hidden="true" style="--k-meter:{share:.0f}%"><i></i></span>'
