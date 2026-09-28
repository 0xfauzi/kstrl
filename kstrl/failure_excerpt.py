"""The retry detail for gate output that no registered parser recognised (#622).

A gate whose output none of ``kstrl.gateparse``'s parsers recognised used to
show the engineer the primary parser's raw tail: the last 5 lines for the test
gate, the last 3 for typecheck and lint. For ``cargo test`` those lines are
cargo's own chatter, and the panic location and the ``left:`` / ``right:``
values sit earlier in the output and were dropped. The same held for ``go
test`` with more than one package, for jest, and for clippy.

``failure_excerpt`` keeps the lines around every ``<path>:<line>`` the output
names inside the worktree instead. It is a fallback, never a parser: it runs
only when no registered parser recognised the output, it produces no
``ParsedFailure`` and no signature, and it leaves the row unmeasured (#227). A
worktree path next to a failure is not evidence that the tool ran and reported,
and a signature keyed on a line number would read every edit above the failing
line as a fix. The tail stays when the output names no location in the worktree.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from kstrl.parsers import strip_ansi

#: Lines kept above and below each location inside the worktree. Measured on
#: the captures in tests/tool_output, not assumed: clippy prints its code
#: frame and caret message up to 6 lines BELOW its `--> src/x.rs:9:5` line,
#: and cargo test its `left:` / `right:` values 3 below the panic location,
#: while jest prints the test name and the expected and received values 16
#: lines ABOVE its `at Object.toBe (src/x.test.js:4:27)` frame.
EXCERPT_LINES_BEFORE = 16
EXCERPT_LINES_AFTER = 6

#: The cap on what the excerpt may put in a retry prompt. A design bound, not a
#: measurement: the largest excerpt of the captures is 23 lines and 932
#: characters, and a registered parser's own detail is at most 92 lines
#: (``format_for_prompt``'s 10 failures at 9 lines each, plus the summary and
#: the "more errors" line), so the fallback never shows more than a parse would.
EXCERPT_MAX_LINES = 80
EXCERPT_MAX_CHARS = 8_000

# `<path>:<line>`, where the path ends in a file extension. The extension keeps
# timestamps (`20:44:28`), ports (`localhost:8080`) and IP addresses off it, and
# the lookbehind keeps a URL's host and node's `node:internal/...` frames off it.
_LOCATION_RE = re.compile(r"(?<![\w./:-])(?P<path>[\w./-]*[\w-]\.[A-Za-z]\w*):\d+")

# Any absolute path on a line, however it is written: after a space, a bracket
# or a quote, inside a `file:///` URL, or holding characters `_LOCATION_RE`
# does not take (`@` in Homebrew's `node@20`, a space in a home directory).
# Deliberately broader than `_LOCATION_RE`: a line this matches outside the
# worktree is DROPPED, so matching too much costs a line of noise, while
# matching too little shows a toolchain path the engineer cannot use.
_ABSOLUTE_RE = re.compile(r"(?<![\w.~-])/(?=[\w.@+~%-])[^\s\"'`()\[\]<>,;:]*")


def _inside(path: str, root: str) -> bool:
    """Is ``path``, as the tool printed it, inside the worktree ``root``?

    Lexical, never a check that the file exists: go prints a test file relative
    to its PACKAGE directory (`pricing_test.go:7` for `pricing/pricing_test.go`),
    so an existence check against the root drops every go failure outside the
    module root. What must be dropped is the toolchain's own sources, the
    `/rustc/...` and `~/.rustup/...` frames of a Rust backtrace, and those are
    absolute paths outside the root.
    """
    joined = os.path.normpath(os.path.join(root, path))
    return joined == root or joined.startswith(root + os.sep)


def _locations(line: str, root: str) -> list[bool]:
    """For each `<path>:<line>` on ``line``, whether it is inside ``root``."""
    return [_inside(m.group("path"), root) for m in _LOCATION_RE.finditer(line)]


def _shown(line: str, root: str) -> bool:
    """May ``line`` be shown: not blank, and every path on it inside ``root``."""
    located = _locations(line, root)
    absolute = [_inside(m.group(), root) for m in _ABSOLUTE_RE.finditer(line)]
    return bool(line.strip()) and all(located) and all(absolute)


def _bounded(chosen: list[str]) -> str:
    """``chosen`` as text, cut to EXCERPT_MAX_LINES and EXCERPT_MAX_CHARS."""
    text = "\n".join(chosen[:EXCERPT_MAX_LINES])
    if len(chosen) <= EXCERPT_MAX_LINES and len(text) <= EXCERPT_MAX_CHARS:
        return text
    return (
        f"{text[:EXCERPT_MAX_CHARS]}\n... excerpt cut at {EXCERPT_MAX_LINES} lines or "
        f"{EXCERPT_MAX_CHARS} characters; {len(chosen)} lines matched"
    )


def failure_excerpt(raw: str, worktree: Path) -> str:
    """The lines of ``raw`` around each location inside ``worktree``, or "".

    A line that names any location or absolute path OUTSIDE the worktree is
    never shown, even inside another location's window, and blank lines are
    dropped. "" when the output names no location inside the worktree, so the
    caller keeps the tail.
    """
    root = os.path.normpath(os.path.abspath(worktree))
    lines = strip_ansi(raw).splitlines()
    shown = [_shown(line, root) for line in lines]
    keep: set[int] = set()
    for index, line in enumerate(lines):
        if shown[index] and _LOCATION_RE.search(line):
            start = max(0, index - EXCERPT_LINES_BEFORE)
            keep.update(range(start, min(len(lines), index + EXCERPT_LINES_AFTER + 1)))
    chosen = [lines[i] for i in sorted(keep) if shown[i]]
    return _bounded(chosen) if chosen else ""
