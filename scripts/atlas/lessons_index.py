"""Render the lessons index, docs/lessons/index.html, from the register.

A lesson is a change taught as a page the reader can operate, kept as it was
on the day it landed. The register, docs/lessons/register.md, is the record
of every lesson and is the only thing this reads, so the index cannot name a
lesson the register does not, or describe one differently.

Each card carries the entry's title, its file, its git range, and the first
sentence of its prose. The register's prose opens with a short label
sentence ("What the lesson taught."), so when the first sentence is under
eight words the card shows it together with the sentence that follows.

The page is the atlas's identity, token for token from theme.py, and is
self-contained: no fonts, scripts or images are fetched.

Usage:  uv run python scripts/atlas/lessons_index.py [--out docs/lessons/index.html]
        --check   exit 1 when the committed page differs from what would render
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import theme as theme_mod

LABEL_WORDS = 8
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


@dataclass(frozen=True)
class Entry:
    title: str
    file: str
    range: str
    lead: str


def entries(register: Path) -> list[Entry]:
    """The log's `### ` entries: fields directly under the heading, then prose."""
    out: list[Entry] = []
    title = ""
    fields: dict[str, str] = {}
    prose: list[str] = []

    def flush() -> None:
        if title:
            out.append(
                Entry(
                    title=title,
                    file=fields.get("file", ""),
                    range=fields.get("range", ""),
                    lead=lead(" ".join(prose)),
                )
            )

    for line in register.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^###\s+(.*\S)\s*$", line)
        if heading:
            flush()
            title, fields, prose = heading.group(1), {}, []
            continue
        if not title:
            continue
        field = re.match(r"^-\s+([a-z-]+):\s*(.*)$", line.strip())
        if field and not prose:
            fields[field.group(1)] = field.group(2).strip()
        elif line.strip():
            prose.append(line.strip())
    flush()
    return out


def lead(prose: str) -> str:
    """The first sentence; joined with the next when the first is only a label."""
    sentences = [s for s in SENTENCE_END.split(prose.strip()) if s]
    if not sentences:
        return ""
    if len(sentences[0].split()) < LABEL_WORDS and len(sentences) > 1:
        return f"{sentences[0]} {sentences[1]}"
    return sentences[0]


def inline(text: str) -> str:
    """Escape, then let the register's backtick spans stand as code."""
    escaped = html.escape(text, quote=False)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)


CSS = """
{ROOT}
*{{box-sizing:border-box}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
html{{background:var(--bg)}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:var(--fs);font-size:15px;
line-height:1.5;-webkit-font-smoothing:antialiased}}
a{{color:var(--accent);text-decoration:none}}
a:hover{{text-decoration:underline}}
code{{font-family:var(--fm);font-size:.92em;color:var(--ink-2)}}
.bar{{padding:.9rem 1.4rem .7rem;border-bottom:1px solid var(--line);background:var(--surface);
display:flex;flex-wrap:wrap;align-items:baseline;gap:.3rem 1.4rem}}
.brand{{margin:0;font-family:var(--fm);font-size:17px;font-weight:600;letter-spacing:-.01em}}
.brand span{{color:var(--accent);font-weight:400}}
.meta{{margin:0;font-size:12.5px;color:var(--ink-3);max-width:60rem}}
.nav{{margin-left:auto;display:flex;gap:.9rem;font-size:12.5px}}
.nav a{{color:var(--ink-2)}}
.nav a:hover{{color:var(--accent);text-decoration:none}}
main{{max-width:46rem;margin:0 auto;padding:2.6rem 1.4rem 5rem}}
h1{{font-size:1.7rem;line-height:1.2;margin:0 0 .4rem;font-weight:600;letter-spacing:-.01em}}
.lede{{margin:0 0 2rem;color:var(--ink-3);font-size:.95rem;max-width:40rem}}
.card{{display:block;background:var(--surface);border:1px solid var(--raised);border-radius:8px;
padding:1rem 1.1rem 1.05rem;margin:0 0 .8rem;color:inherit}}
.card:hover{{border-color:var(--accent);text-decoration:none}}
.card__t{{display:block;font-size:1.05rem;font-weight:600;color:var(--ink);margin:0 0 .3rem;
letter-spacing:-.01em}}
.card__m{{display:flex;flex-wrap:wrap;gap:.3rem .9rem;margin:0 0 .5rem;font-family:var(--fm);
font-size:11px;letter-spacing:.05em;color:var(--ink-3)}}
.card__m code{{color:var(--ink-2);font-size:inherit}}
.card__s{{display:block;font-size:14px;color:var(--ink-2);margin:0}}
.card__s code{{background:var(--raised);padding:.05em .3em;border-radius:3px}}
.empty{{color:var(--ink-3);font-size:14px}}
.foot{{margin-top:2.4rem;padding-top:1rem;border-top:1px solid var(--line);font-size:12px;
color:var(--ink-3)}}
.foot a{{color:var(--ink-2)}}
.foot a:hover{{color:var(--accent)}}
"""


def build(items: list[Entry]) -> str:
    t = theme_mod.get()
    n = len(items)
    count = "one lesson" if n == 1 else f"{n} lessons"
    o: list[str] = []
    o.append("<!doctype html>")
    o.append('<html lang="en"><head><meta charset="utf-8">')
    o.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    o.append("<title>kstrl lessons</title>")
    o.append(f"<style>{CSS.format(ROOT=':root{' + theme_mod.css_vars(t) + '}')}</style>")
    o.append("</head><body>")
    o.append('<header class="bar">')
    o.append('<p class="brand">kstrl <span>lessons</span></p>')
    o.append(
        f'<p class="meta">What each merged change taught, as pages you can operate: {count}. '
        "Generated from <code>docs/lessons/register.md</code>.</p>"
    )
    o.append(
        '<nav class="nav"><a href="../atlas/index.html">Atlas</a>'
        '<a href="register.md">Register</a></nav>'
    )
    o.append("</header>")
    o.append("<main>")
    o.append("<h1>Lessons</h1>")
    o.append(
        '<p class="lede">A lesson is a change taught as a page you can operate, kept as it was '
        "on the day it landed.</p>"
    )
    if not items:
        o.append('<p class="empty">The register lists no lessons yet.</p>')
    for e in items:
        href = html.escape(e.file, quote=True)
        o.append(f'<a class="card" href="{href}">')
        o.append(f'<span class="card__t">{inline(e.title)}</span>')
        meta = f"<span><code>{html.escape(e.file)}</code></span>"
        if e.range:
            meta += f"<span>range <code>{html.escape(e.range)}</code></span>"
        o.append(f'<span class="card__m">{meta}</span>')
        if e.lead:
            o.append(f'<span class="card__s">{inline(e.lead)}</span>')
        o.append("</a>")
    o.append(
        '<p class="foot">Every lesson embeds the <a href="../atlas/index.html">system atlas</a> '
        "and marks where its change landed. The record of what each one taught, and the "
        'vocabulary they share, is <a href="register.md">register.md</a>.</p>'
    )
    o.append("</main></body></html>")
    return "\n".join(o) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", default="docs/lessons/register.md")
    parser.add_argument("--out", default="docs/lessons/index.html")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the committed page differs from what would be rendered",
    )
    args = parser.parse_args()

    register = Path(args.register)
    if not register.is_file():
        print(f"register missing: {register}", file=sys.stderr)
        return 1
    items = entries(register)
    for e in items:
        if not e.file or not (register.parent / e.file).is_file():
            print(f"{e.title}: file {e.file!r} does not exist", file=sys.stderr)
            return 1
    out = Path(args.out)
    page = build(items)

    if args.check:
        current = out.read_text(encoding="utf-8") if out.is_file() else ""
        if current != page:
            print(
                f"{out} is stale: it differs from what lessons_index.py would write.\n"
                "run: scripts/atlas/refresh.sh, then commit docs/lessons/index.html",
                file=sys.stderr,
            )
            return 1
        print(f"{out} is current")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.1f} KB, {len(items)} lessons)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
