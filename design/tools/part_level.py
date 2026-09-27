"""The Part level for any part: its tries as rows across the stations of the line, the paths of the tries that were
sent back, and the step that is open below. gen_part.py draws search-query with it (the Map2Part frame);
gen_proto_parts.py draws the other six parts at the same moment, for the prototype."""
from __future__ import annotations

from level_common import page

X0 = 32
COLS = [('', 64), ('Build', 244), ('Verify', 150), ('Review', 176), ('Security', 150), ('Distill', 150), ('Your approval', 150), ('Merge', 132)]
WHAT = ['', 'the engineer, every 2 s', 'tests, lint, scope', 'a reviewer agent', 'a security agent', 'records facts', 'you, at L2', 'PR and CI']
GT, HH, RH, GAP = 100, 40, 50, 14
xs = [X0]
for _, w in COLS: xs.append(xs[-1] + w)
def row_top(i: int) -> int: return GT + HH + i * (RH + GAP)


# a cell's state: its class, and ' sel' after it for the open step
STATE = {'done': '', 'ok': '', 'bad': ' k-tile-alert', 'now': ' k-tile-work', 'you': ' k-tile-ask'}


def grid(rows: list, returns: list[tuple[int, int, str]]) -> list[str]:
    """rows: (label, cells) per try, cells one per station (None: never reached; ('todo',): not yet; (state, mark, main,
    sub)), or (label, text) for a try that did not run. returns: (station column, try, note) for a try sent back from
    that station to the next try's Build."""
    html = ['<div class="grid">']
    for j, ((name, w), what) in enumerate(zip(COLS, WHAT)):
        if j == 0: continue
        html.append(f'<div class="ch" style="left:{xs[j]+3}px;top:{GT}px;width:{w-6}px"><b>{name}</b><span>{what}</span></div>')
    for i, (label, cells) in enumerate(rows):
        y = row_top(i)
        html.append(f'<div class="rl" style="left:{xs[0]}px;top:{y}px;height:{RH}px">{label}</div>')
        if isinstance(cells, str):
            html.append(f'<div class="k-tile k-tile-cell k-tile-idle cell left" style="left:{xs[1]+3}px;top:{y}px;width:{xs[-1]-xs[1]-6}px;height:{RH}px"><p class="lt">{cells}</p></div>')
            continue
        for j, c in enumerate(cells):
            x, w = xs[j + 1], COLS[j + 1][1]
            if c is None:
                # a station this try never reached: a hairline in the grid's own dash, not a cell
                html.append(f'<svg class="gnone" style="left:{x+3}px;top:{y}px" width="{w-6}" height="{RH}" aria-hidden="true"><line class="gl" x1="10" x2="{w-16}" y1="{RH // 2 + .5}" y2="{RH // 2 + .5}"/></svg>')
            elif c[0] == 'todo':
                html.append(f'<div class="k-tile k-tile-cell k-tile-idle cell" style="left:{x+3}px;top:{y}px;width:{w-6}px;height:{RH}px"></div>')
            else:
                cls, mk, main, sub = c
                m = f'<span class="k-mk sm {mk}"></span>' if mk else ''
                state = STATE[cls.removesuffix(' sel')] + (' k-tile-selected' if cls.endswith(' sel') else '')
                html.append(f'<div class="k-tile k-tile-cell{state} cell" style="left:{x+3}px;top:{y}px;width:{w-6}px;height:{RH}px"><div class="a">{m}{main}</div><div class="b">{sub}</div></div>')
    if returns:
        # return paths: from the station that sent a try back to the next try's Build
        svg = ['<svg class="ret" viewBox="0 0 1280 520" width="1280" height="520" aria-hidden="true"><defs><marker id="ra2" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0.5 7 4 0 7.5Z" class="rh"/></marker></defs>']
        for col, i, note in returns:
            rx = xs[col] + COLS[col][1] / 2
            yb = row_top(i) + RH
            gy = yb + GAP / 2
            ny = row_top(i + 1) + RH / 2
            svg.append(f'<path d="M{rx} {yb} V{gy} H{xs[1]-10} V{ny} H{xs[1]+1}" class="rp" marker-end="url(#ra2)"/>')
            svg.append(f'<text x="{rx+10}" y="{gy+4}" class="rt">{note}</text>')
        svg.append('</svg>')
        html.append(''.join(svg))
    html.append('</div>')
    return html


CSS = """
  .ttl .k-chip { align-self:center; margin-left:-4px; }
  .pd { position:absolute; left:32px; right:32px; top:60px; font:var(--t-body); color:var(--text-2); display:flex; align-items:baseline; gap:22px; white-space:nowrap; }
  .pd .ctx { display:flex; gap:16px; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .pd .ctx > span { display:inline-flex; gap:6px; align-items:baseline; } .pd .ctx > span > .k-mk { align-self:center; }
  .pd .ctx > span:not(:last-child) { color:var(--text-2); }
  .grid { position:absolute; left:0; top:0; width:1280px; height:520px; }
  /* the grid is a positioning layer over the whole map, title row included, and comes after it: without this the grid
     took every click meant for the zoom (measured: all four zoom items hit .grid) */
  .top { z-index:1; }
  .ch { position:absolute; height:40px; padding:0 10px; box-sizing:border-box; display:flex; flex-direction:column; justify-content:center; border-bottom:1px solid var(--line); }
  .ch b { font:var(--t-small); font-weight:600; } .ch span { font:var(--t-micro); font-weight:400; color:var(--text-3); }
  .rl { position:absolute; width:64px; display:flex; align-items:center; font:var(--t-measure-inline); color:var(--text-3); }
  .cell { position:absolute; }
  .cell .a { display:flex; align-items:center; gap:6px; font:var(--t-measure); color:var(--text); }
  .cell .b { font:var(--t-measure-small); font-weight:400; color:var(--text-3); margin-top:1px; white-space:nowrap; }
  .gnone { position:absolute; } .gl { stroke:var(--line-strong); stroke-width:1; stroke-dasharray:1 3; }
  .lt { margin:auto 0 auto 4px; font:var(--t-small); color:var(--text-3); }
  .ret { position:absolute; inset:0; pointer-events:none; }
  .rp { fill:none; stroke:var(--fail); stroke-width:1.5; opacity:.8; }
  .rh { fill:var(--fail); }
  .rt { fill:var(--text-3); font:var(--t-micro); font-weight:400; }
  .steps { position:absolute; left:32px; right:32px; top:402px; bottom:14px; display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:14px; }
  .f1 { grid-column:1 / span 2; }
  .f1 .sh { display:flex; align-items:center; gap:10px; font:var(--t-label); font-weight:400; opacity:.8; }
  .f1 .sev { margin-top:auto; font:var(--t-measure-small); font-weight:600; letter-spacing:var(--t-measure-small-ls); opacity:.7; }
  .f1 .say { margin:6px 0 0; font:var(--t-statement); letter-spacing:var(--t-statement-ls); max-width:34ch; }
  .f1 .src { margin-top:12px; font:var(--t-measure-inline); opacity:.65; }
  .sev2 { font:var(--t-measure-small); font-weight:600; color:var(--text-2); letter-spacing:var(--t-measure-small-ls); }
  .sev2 span { font-weight:400; color:var(--text-3); letter-spacing:0; margin-left:6px; }
  .f2 p { margin:10px 0 0; font:var(--t-input); letter-spacing:var(--t-input-ls); }
  .k-tile .tf { margin-top:auto; font:var(--t-label); font-weight:400; color:var(--text-3); }
  .f3 .cl { margin:6px 0 0; font:var(--t-body); }
  .acts { display:flex; flex-direction:column; gap:6px; margin-top:14px; }
  .f3 .tf { margin-top:auto; padding-top:8px; }
"""


def part_page(card: str, title: str, part: str, head: str, rows: list, returns: list[tuple[int, int, str]], step: str,
              extra_css: str = '', needs: str | None = None) -> str:
    body = head + '\n' + '\n'.join(grid(rows, returns)) + '\n' + step
    return page(card, title, ['search', part], 2, body, CSS + extra_css, needs)
