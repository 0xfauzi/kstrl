"""Render the design system's previews locally: a tokens.css replica (both themes, fonts) plus bundle.css."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path('../system/project').resolve()
tok = json.loads((root / 'tokens.json').read_text())
themes = [t['id'] for t in tok['color']['themes']]
def per_theme(fam: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {t: [] for t in themes}
    for t in tok.get(fam, {}).get('tokens', []):
        v = t['value']
        for th in themes:
            val = v if isinstance(v, str) else v.get(th, v[themes[0]])
            if isinstance(val, str) and val.startswith('{') and val.endswith('}'): val = f'var(--{val[1:-1]})'
            out[th].append(f"--{t['name']}: {val};")
    return out
col, sh = per_theme('color'), per_theme('shadow')
css = [f':root, [data-theme="{themes[0]}"] {{ ' + ' '.join(col[themes[0]] + sh[themes[0]]) + ' }']
for th in themes[1:]:
    css.append(f'[data-theme="{th}"] {{ ' + ' '.join(col[th] + sh[th]) + ' }')
flat = []
for fam, body in tok.items():
    if isinstance(body, dict) and 'tokens' in body and fam not in ('color', 'shadow'):
        flat += [f"--{t['name']}: {t['value']};" for t in body['tokens']]
flat += [f'--font-{k}: {v};' for k, v in tok['type']['families'].items()]
css.append(':root { ' + ' '.join(flat) + ' }')
for f in tok['type']['fonts']:
    css.append(f"@font-face {{ font-family:'{f['family']}'; src:url('file://{root}/{f['file']}') format('woff2'); font-weight:{f['weight']}; font-style:{f.get('style','normal')}; }}")
css.append((root / 'components/bundle.css').read_text())
style = '<style>' + '\n'.join(css) + '</style>'
out = Path('out/render'); out.mkdir(parents=True, exist_ok=True)
from browsers import HEADLESS as B

assert B, 'no Chromium found: set KSTRL_DESIGN_HEADLESS (see browsers.py)'
for comp in sys.argv[1:]:
    src = (root / 'components' / comp / 'preview.html').read_text()
    first = src.splitlines()[0]
    h = int(re.search(r'height=(\d+)', first).group(1)); w = re.search(r'width=(\d+)', first); w = int(w.group(1)) if w else 960
    th = os.environ.get('THEME', themes[0])
    html = src.replace('<head>', '<head>' + style, 1).replace('<html', f'<html data-theme="{th}"', 1)
    suffix = '' if th == themes[0] else '-' + th
    f = out / f'{comp}{suffix}.html'; f.write_text(html)
    subprocess.run([B, '--headless', '--no-sandbox', '--disable-gpu', '--hide-scrollbars', '--allow-file-access-from-files',
                    '--virtual-time-budget=3000', f'--screenshot={out.resolve()}/{comp}{suffix}.png', f'--window-size={w},{h}', f'file://{f.resolve()}'],
                   stderr=subprocess.DEVNULL, check=True)
    print(comp, w, h)
