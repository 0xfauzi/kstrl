"""Gzipped size of each client runtime, bundled with its dependencies.

esm.sh answers `?bundle` with a one-line stub that re-exports the real
bundle, so this follows that one hop and reports the gzipped size of the
bundle itself. Run: python3 client_size.py
"""

from __future__ import annotations

import gzip
import re
import urllib.request

CANDIDATES = {
    "htmx 2": "https://esm.sh/htmx.org@2?bundle",
    "preact 10 + hooks": "https://esm.sh/preact@10/hooks?bundle",
    "htm 3": "https://esm.sh/htm@3?bundle",
    "lit 3": "https://esm.sh/lit@3?bundle",
    "solid-js 1 + web": "https://esm.sh/solid-js@1/web?bundle",
    "react 19 + react-dom": "https://esm.sh/react-dom@19/client?bundle",
    "svelte 5 runtime": "https://esm.sh/svelte@5/internal/client?bundle",
    "alpine 3": "https://esm.sh/alpinejs@3?bundle",
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main() -> None:
    for name, url in CANDIDATES.items():
        stub = fetch(url).decode()
        paths = re.findall(r'from\s+"(/[^"]+)"', stub) + re.findall(r'import\s+"(/[^"]+)"', stub)
        total_raw = total_gz = 0
        for p in dict.fromkeys(paths):
            body = fetch("https://esm.sh" + p)
            total_raw += len(body)
            total_gz += len(gzip.compress(body, 9))
        print(f"{name:<22} {total_gz:>7} bytes gz  {total_raw:>8} raw  ({len(paths)} file(s): {', '.join(paths)})")


if __name__ == "__main__":
    main()
