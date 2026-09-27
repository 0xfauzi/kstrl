"""Set each failing card's declared height to what the audit measured (the larger of height_for_equal and height_for_no_scroll),
in the generator's card() call or the hand-written card's first line. Reads audit_all.txt; prints each change."""
import re
from pathlib import Path

need: dict[str, int] = {}
cur = []
for line in open('audit_all.txt'):
    m = re.match(r'(PASS|FAIL) (\S+)', line)
    if m:
        if m.group(1) == 'FAIL':
            hs = [int(x) for l in cur for x in re.findall(r"'height_for_(?:equal|no_scroll)': (\d+)", l)]
            if hs: need[m.group(2)] = max(hs)
        cur = []
    else:
        cur.append(line)
for card, h in need.items():
    done = False
    for g in sorted(Path('.').glob('gen_*.py')):
        t = g.read_text(); rx = re.compile(r"(card\('" + re.escape(card) + r"', '[^']*', )(\d+)(,)")
        if rx.search(t):
            old = rx.search(t).group(2); g.write_text(rx.sub(lambda m, h=h: m.group(1) + str(h) + m.group(3), t, count=1)); print(f'{card}: {old} -> {h} ({g})'); done = True; break
    if not done:
        p = Path(f'../system/project/components/{card}/preview.html'); lines = p.read_text().split('\n')
        old = re.search(r'height=(\d+)', lines[0]).group(1); lines[0] = re.sub(r'height=\d+', f'height={h}', lines[0], count=1)
        p.write_text('\n'.join(lines)); print(f'{card}: {old} -> {h} (hand-written)')
