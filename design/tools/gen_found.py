"""Foundation cards on the card scaffold: Marks (one drawn shape per meaning) and Voices (who wrote a piece of text).
They were hand-written full-bleed panels at 960; the scaffold gives them the canvas, the tile and the width every other card has."""
from __future__ import annotations
import re
from pathlib import Path
from card import card
from scale import SCALE, FAMILY

def body_of(name: str) -> str:
    # the content rows, kept verbatim from the hand-written card (sources/<name>.html)
    src = Path(f'sources/{name}.html').read_text()
    inner = re.search(r'<body>\s*<div class="[gv]">(.*)</div>\s*</body>', src, re.S).group(1)
    return inner.strip()

# 'a measurement passed' measured 128px in a 129px column; 'a check passed' is kstrl's own word for it ("every check agreed")
marks = f'<div class="c-tile"><div class="mk">{body_of("Marks").replace("a measurement passed", "a check passed")}</div></div>'
marks_css = """
  .mk { display:grid; grid-template-columns:repeat(8, minmax(0,1fr)); font:var(--t-small); align-items:start; }
  .mk .c { padding:6px 8px; text-align:center; }
  .mk .n { margin-top:10px; font-weight:600; }
  .mk .d { color:var(--text-2); font:var(--t-label); font-weight:400; margin-top:2px; }
"""
card('Marks', 'Foundations', 166, 'Status marks: drawn shapes, one per meaning', marks, marks_css)

def face(voice: str) -> str:
    # the voice's steps, read from the scale so the line cannot drift from it
    sizes = [s for n, v, s, *_ in SCALE if v == voice]
    return f'{FAMILY[voice]} · {len(sizes)} steps, {min(sizes)} to {max(sizes)} px'
SAMPLE = {n: smp for n, v, s, lh, ws, ls, smp, use in SCALE}
cols = [('You', 'h', f'“{SAMPLE["intent-xl"]}”', 'Your intent, your answers, your guidance. Anything a person wrote, quoted exactly.', 'human'),
        ('An agent', 'a', 'All 4 stories are done.', 'What the engineer, architect or reviewer said. A claim until something else measures it.', 'agent'),
        ('Measured', 'm', '48 passed · 0 failed · 41s<br>+312 −40 · 4 files<br>≥$6.12', 'What a tool counted: tests, git, CI, time, spend. The only voice that can confirm a claim.', 'measure')]
voices = ('<div class="c-tile vt"><div class="vs">' + ''.join(
    f'<div class="c"><div class="who">{w}</div><div class="{k}">{s}</div><div class="rule">{r}</div><div class="face">{face(v)}</div></div>'
    for w, k, s, r, v in cols) + '</div></div>')
voices_css = """
  .vt { padding-top:0; padding-bottom:0; }
  .vs { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); }
  .vs .c { padding:22px 24px; border-left:1px solid var(--line); }
  .vs .c:first-child { border-left:0; padding-left:4px; } .vs .c:last-child { padding-right:4px; }
  .who { font:var(--t-label); color:var(--text-3); margin-bottom:14px; }
  .h { font:var(--t-intent-xl); letter-spacing:var(--t-intent-xl-ls); }
  .a { font:var(--t-statement); letter-spacing:var(--t-statement-ls); }
  .m { font:var(--t-measure-code); }
  .rule { margin-top:16px; font:var(--t-small); color:var(--text-2); }
  .face { margin-top:10px; font:var(--t-measure-small); font-weight:400; color:var(--text-3); }
"""
card('Voices', 'Foundations', 280, 'Three voices: who wrote this text', voices, voices_css)
