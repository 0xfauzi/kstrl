"""Keycap legends the shipped fonts do not draw. 12-unit grid; box 0.72em, so a 1.6-unit stroke is 0.096em: Geist Mono's stem at weight 500."""
from __future__ import annotations
from urllib.parse import quote
W = 1.6
GLYPHS = {
    # four loops of r=1.65 on a 3.4-unit centre square, one closed stroke; outer edge inside the grid. Stroke 1.4, not 1.6:
    # at keycap size the small closed loops concentrate ink, so the measured-equal stem reads heavier than the letters beside it.
    'cmd': ('Command', '<path d="M4.3 4.3V2.65A1.65 1.65 0 1 0 2.65 4.3H9.35A1.65 1.65 0 1 0 7.7 2.65V9.35A1.65 1.65 0 1 0 9.35 7.7H2.65A1.65 1.65 0 1 0 4.3 9.35Z" fill="none" stroke="#000" stroke-width="1.4"/>'),
    'opt': ('Option', f'<path d="M0.8 1H4.4L7.6 11H11.2M7.3 1H11.2" fill="none" stroke="#000" stroke-width="{W}" stroke-linejoin="miter"/>'),
    'ctrl': ('Control', f'<path d="M1.8 6.6 6 1.45 10.2 6.6" fill="none" stroke="#000" stroke-width="{W}" stroke-linejoin="miter"/>'),
}
def uri(name: str) -> str:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">{GLYPHS[name][1]}</svg>'
    return 'url("data:image/svg+xml,' + quote(svg, safe=' /:=",.') .replace('"', '%22') + '")'
def css() -> str:
    out = []
    for n in GLYPHS:
        u = uri(n)
        out.append(f'.k-kg-{n} {{ -webkit-mask-image:{u}; mask-image:{u}; }}')
    return '\n'.join(out)
def key(ch: str, extra: str = '') -> str:
    names = {'⌘': 'cmd', '⌥': 'opt', '⌃': 'ctrl'}
    cls = 'k-key' + (' ' + extra if extra else '')
    if ch in names:
        n = names[ch]
        return f'<span class="{cls}"><span class="k-kg k-kg-{n}" role="img" aria-label="{GLYPHS[n][0]}"></span></span>'
    return f'<span class="{cls}">{ch}</span>'
if __name__ == '__main__':
    print(css())
