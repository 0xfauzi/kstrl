"""Leave every file build_all.sh writes as the repository's commit hooks would: no trailing whitespace on any line and
exactly one newline at the end (pre-commit's trailing-whitespace and end-of-file-fixer, .pre-commit-config.yaml).
Without this, a commit that touches a generated file has the hooks rewrite it, and the committed file then differs
from what the artifact serves. Both rules work on bytes as the hooks do: only ASCII whitespace is stripped, so a
no-break space is kept. Runs last in build_all.sh; whitespace between tags and after a comma renders the same, which
the pixel checks confirm (probe/frame_pixels.py, interact_prototype.py)."""
from __future__ import annotations

from pathlib import Path

ROOTS = [Path('../system'), Path('../prototype')]
TEXT = {'.html', '.css', '.js', '.json', '.md', '.txt', '.svg'}


def tidy(raw: bytes) -> bytes:
    if not raw:
        return raw
    lines = [line.rstrip() for line in raw.split(b'\n')]
    return b'\n'.join(lines).rstrip(b'\n') + b'\n'


if __name__ == '__main__':
    changed = 0
    for root in ROOTS:
        for p in sorted(root.rglob('*')):
            if p.is_file() and p.suffix in TEXT:
                raw = p.read_bytes()
                new = tidy(raw)
                if new != raw:
                    p.write_bytes(new)
                    changed += 1
    print(f'tidied {changed} files')
