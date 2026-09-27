"""Write tokens.json's type groups from scale.py, so the type foundation, the bundle's --t-* variables and the audit read one scale."""
from __future__ import annotations
import json
from pathlib import Path
from scale import SCALE
GROUPS = [('Your voice', 'human'), ('Agent voice', 'agent'), ('Measurement', 'measure')]
p = Path('../system/project/tokens.json'); tok = json.loads(p.read_text())
groups = []
for title, voice in GROUPS:
    styles = []
    for n, v, s, lh, ws, ls, sample, usage in SCALE:
        if v != voice: continue
        st = {'name': n, 'fontSize': f'{s}px', 'lineHeight': f'{lh}px', 'fontWeight': ws[0]}
        if ls != '0': st['letterSpacing'] = ls.replace('-.', '-0.')
        also = [str(w) for w in ws[1:]]
        st['sample'] = sample
        st['usage'] = usage + (f' Also at {" and ".join(also)}.' if also else '')
        styles.append(st)
    groups.append({'name': title, 'family': voice, 'styles': styles})
tok['type']['groups'] = groups
p.write_text(json.dumps(tok, indent=2, ensure_ascii=False) + '\n')
print(sum(len(g['styles']) for g in groups), 'styles')
