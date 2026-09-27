"""Write components/bundle.css: the command-window vocabulary shared by every frame."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from scale import css_classes, css_vars


def svg(body: str) -> str:
    s = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">{body}</svg>'
    return 'url("data:image/svg+xml,' + quote(s, safe=' =:/-.,') + '")'

W = 'fill="none" stroke="#000" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
marks = {
    'pass':   svg(f'<path d="M3 8.5 6.5 12 13 4.5" {W}/>'),
    'fail':   svg(f'<path d="M4 4 12 12M12 4 4 12" {W}/>'),
    'you':    svg('<path d="M8 1.5 14.5 8 8 14.5 1.5 8Z" fill="#000"/>'),
    'work':   svg('<circle cx="8" cy="8" r="5.5" fill="none" stroke="#000" stroke-opacity=".35" stroke-width="2"/><path d="M8 2.5A5.5 5.5 0 0 1 13.5 8" fill="none" stroke="#000" stroke-width="2" stroke-linecap="round"/>'),
    'wait':   svg('<circle cx="8" cy="8" r="5.5" fill="none" stroke="#000" stroke-width="1.5" stroke-dasharray="2.2 2.2"/>'),
    'skip':   svg('<circle cx="8" cy="8" r="5.5" fill="none" stroke="#000" stroke-width="1.5"/><path d="M4.2 11.8 11.8 4.2" stroke="#000" stroke-width="1.5"/>'),
    'absent': svg('<rect x="2.5" y="2.5" width="11" height="11" rx="2.5" fill="none" stroke="#000" stroke-width="1.5" stroke-dasharray="2.4 2"/>'),
    'landed': svg('<defs><mask id="c"><rect width="16" height="16" fill="#fff"/><path d="M4.8 8.3 7 10.5 11.3 5.8" fill="none" stroke="#000" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></mask></defs><circle cx="8" cy="8" r="6.5" fill="#000" mask="url(#c)"/>'),
}
mark_color = {'pass': 'var(--pass)', 'fail': 'var(--fail)', 'you': 'var(--you)', 'work': 'var(--work)',
              'wait': 'var(--text-3)', 'skip': 'var(--text-3)', 'absent': 'var(--text-3)', 'landed': 'var(--pass)'}
css = ["""/* kstrl command-window vocabulary. Every value is a token from tokens.json. */
/* The type scale (scale.py): --t-<step> is a step's font at its default weight, --t-<step>-ls its tracking,
   --t-<step>-size and -lh its size and line height alone, for a size variant that keeps its base rule's weight. */
""" + css_vars() + """
.v-human { font-family:var(--font-human); }
.v-agent { font-family:var(--font-agent); }
.v-measure { font-family:var(--font-measure); font-size:calc(1em - 1px); }
/* ^ a measurement sits one step below the text it is in (Geist Mono reads larger than Instrument Sans at the same size):
   11 in a 12px label, 12 in 13px small text, 13 in 14px body. Standing alone, a value is set with --t-measure. */
/* A token in prose (a part's name, a flag) is one word: it never breaks at its hyphen. */
.v-token { white-space:nowrap; }
.t2 { color:var(--text-2); } .t3 { color:var(--text-3); } .t-you { color:var(--you); } .t-pass { color:var(--pass); } .t-fail { color:var(--fail); } .t-work { color:var(--work); }
/* A step as a class, for a run no component covers: a hint beside a button is .ty-small. */
""" + css_classes() + """
.k-logo { width:18px; height:18px; flex:none; }
.grow { flex:1; }
.k-mk { display:inline-block; width:16px; height:16px; flex:none; vertical-align:-3px; -webkit-mask-size:16px 16px; mask-size:16px 16px; -webkit-mask-repeat:no-repeat; mask-repeat:no-repeat; }
.k-mk.sm { width:12px; height:12px; -webkit-mask-size:12px 12px; mask-size:12px 12px; vertical-align:-1px; }
.k-mk.lg { width:24px; height:24px; -webkit-mask-size:24px 24px; mask-size:24px 24px; vertical-align:-6px; }
"""]
for name, url in marks.items():
    css.append(f'.k-mk.{name} {{ background:{mark_color[name]}; -webkit-mask-image:{url}; mask-image:{url}; }}')
css.append('/* @components */')
css.append(Path('components.css').read_text())
Path('../system/project/components/bundle.css').write_text('\n'.join(css) + '\n')
print(len('\n'.join(css)))
