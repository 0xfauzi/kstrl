"""Drive the prototype's keyboard and command flows in headless Chrome and report.

Builds a copy of prototype/index.html with selftest.js appended, loads it
in headless Chrome, and prints one PASS or FAIL line per check from the
page's own DOM. Run: python3 docs/design/web-ui/v2/selftest.py

The checks are the behaviours DESIGN.md promises: number keys open the
Needs you items, a choice key opens a confirmation that states the
consequence, Esc unwinds, the command field routes a recorded phrasing and
labels an unrecorded one, low confidence lists options, a decision with no
typed target opens the list, a made view floats and pins, a catalogue gap
becomes a component request.
"""

# ruff: noqa: E501
from __future__ import annotations

import html as html_mod
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTO = HERE / "prototype"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

TEST = (HERE / "selftest.js").read_text(encoding="utf-8")


def main() -> int:
    page = (PROTO / "index.html").read_text(encoding="utf-8")
    for name in ("app.css", "data.js", "replay.js", "app.js"):
        page = page.replace(f'"{name}"', f'"file://{PROTO / name}"')
    page = page.replace("</body>", TEST + "\n</body>")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "selftest.html"
        path.write_text(page, encoding="utf-8")
        dom = subprocess.run(
            [
                CHROME,
                "--headless=new",
                "--disable-gpu",
                "--virtual-time-budget=6000",
                "--dump-dom",
                f"file://{path}",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        ).stdout
    m = re.search(r'<pre id="results">(.*?)</pre>', dom, re.S)
    if not m:
        print("NO RESULTS: the page did not run the test script")
        return 2
    text = html_mod.unescape(m.group(1))
    print(text)
    return 0 if "FAIL" not in text and "DONE" in text else 1


if __name__ == "__main__":
    sys.exit(main())
